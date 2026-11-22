"""Structured JSON operations log of the live service (backlog 7.2; PRD FR-O6
"Structured JSON logs"; §6.3 data minimisation).

`EventLog(hub, service, stream).pump()` runs on its own `PumpThread` (never the scoring
thread) and writes one JSON object per line:

- From its own hub subscription, lifecycle events only, each with an explicit field
  allowlist: `call.started` (call_id), `call.ended` (call_id, reason, final_state),
  `alert.raised` (call_id, alert_id, direction), `alert.cleared` (call_id, alert_id),
  `system.health` (call_id, reason, media_state). `score.updated` is never logged (it is
  per window; `/metrics` counts it). Caller and dialled numbers, scores, audio and any
  other data field are never written.
- From the service's counters, read as copies each turn: ARI connects and drops, new
  loop/hook/shutdown errors, the detector entering and leaving a failure run
  (`detector_failing` at `FAILING_AFTER` batches in a row, `detector_recovered` at 0),
  and new tap, sink and pipeline faults, as deltas. A `tap_failure` line also carries
  the latest failure's `last_stage` (rtp_session, bridge, snoop, snoop_join,
  external_media, media_join) and `last_reason` (`http_<status>`, `too_large`,
  `not_json`, or an error class name such as `TimeoutError` or `KeyError`)
  from the tap (22 Nov, backlog 11.1): without them a failed tap could not be traced.
- `log_dropped`: hub lines lost to the rate limit (`max_lines_per_s`, a token bucket
  with a one-second burst) or to hub overflow, reported once the budget allows. The
  service lines above come first and are never rate-limited; counters are compared at
  most once per second (`COUNTER_INTERVAL_S`), so they add at most one merged line per
  counter plus one transition per second, and a burst of call events cannot cost them.
  Within a turn, lines are not in `ts` order (service lines first): order by `ts`.

Each line: `ts` (UTC ISO 8601, ms; the event's own time for hub lines, the write time
for service lines), `level` (`info`/`warning`/`error`), `event`, then the allowed
fields, ASCII-escaped JSON. A failing stream is counted (`write_failures`), never
raised; a pump never raises (faults are the PumpThread's). The counter baseline is taken
when the log is built (before the service runs), so the first ARI connect is logged
and earlier history is not. Transitions shorter than one pump interval (0.2 s) can be
missed.
"""
from datetime import datetime, timezone
import json
import re
import time

from services.events.fanout import MAX_BUFFER
from services.live.health import FAILING_AFTER  # one rule for /readyz and the log (review L3)

DEFAULT_MAX_LINES_PER_S = 50
COUNTER_INTERVAL_S = 1.0  # service counters are compared at most this often (merged deltas)
HUB_FIELDS = {'call.started': (), 'call.ended': ('reason', 'final_state'), 'alert.raised': ('alert_id', 'direction'),
              'alert.cleared': ('alert_id',), 'system.health': ('reason', 'media_state')}
HUB_LEVELS = {'alert.raised': 'warning', 'system.health': 'warning'}
# (source, counter, event, level): a positive change is logged as `count` (the delta).
DELTAS = (('service', 'connects', 'ari_connected', 'info'),
          ('service', 'disconnects', 'ari_disconnected', 'warning'),
          ('service', 'loop_errors', 'loop_error', 'error'),
          ('service', 'hook_errors', 'hook_error', 'error'),
          ('service', 'shutdown_errors', 'shutdown_error', 'error'),
          ('tap', 'tap_failures', 'tap_failure', 'error'),
          ('tap', 'bad_events', 'ari_bad_event', 'warning'),
          ('tap', 'sink_errors', 'sink_error', 'error'),
          ('tap', 'refused_capacity', 'call_refused_capacity', 'warning'),
          ('sink', 'pump_errors', 'scoring_pump_error', 'error'),
          ('sink', 'errors', 'scoring_sink_error', 'error'),
          ('sink', 'recorder_errors', 'retention_error', 'error'),
          ('pipeline', 'result_errors', 'scoring_result_error', 'error'),
          ('pipeline', 'unmonitored_entered', 'direction_unmonitored', 'warning'))
_SAFE_TEXT = 128  # longest string value written (IDs, reasons, states)
_TOKEN = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,47}')  # tap failure stage/reason tokens


def _iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _safe(value):
    """Strings (cut to 128 characters), finite numbers, booleans and None only."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if value == value and value not in (float('inf'), float('-inf')) else None
    if isinstance(value, str):
        return value[:_SAFE_TEXT]
    return None


class EventLog:
    def __init__(self, hub, service, stream, *, max_lines_per_s=DEFAULT_MAX_LINES_PER_S, wall=time.time,
                 clock=time.monotonic, config_watcher=None):
        """`config_watcher` (optional, FR-A9): a `config_reload.ConfigFileWatcher` whose
        reload outcomes are written as service lines."""
        if type(max_lines_per_s) is not int or not 1 <= max_lines_per_s <= 10_000:
            raise ValueError('max_lines_per_s must be an integer from 1 to 10000')
        self._config = config_watcher
        self._subscription = hub.subscribe(buffer=MAX_BUFFER)
        self.service, self.stream = service, stream
        self.max_lines_per_s, self._wall, self._clock = max_lines_per_s, wall, clock
        self._tokens, self._refilled = float(max_lines_per_s), None
        self._failing = False
        self._dropped = 0  # lines not written yet (rate limit, hub overflow)
        self._counters_at = None
        self.counts = {'lines': 0, 'dropped': 0, 'hub_dropped': 0, 'write_failures': 0, 'read_failures': 0}
        # Review L2: the baseline is taken here, on the thread that builds the service,
        # before `run` starts, so the first ARI connect is always logged.
        self._seen = self._counters()

    # -- one turn ------------------------------------------------------------------------
    def pump(self):
        """Write what happened since the last turn; returns the number of lines written."""
        # Review M1: service lines first and outside the rate limit (at most one per
        # DELTAS entry plus one transition per turn), so a burst of call events can never
        # cost the ARI drop or the detector failure.
        # Re-review N1: counters are compared at most once per second, so a counter rising
        # on every turn gives one merged line per second, never one per increment.
        now = self._clock()
        lines = []
        if self._counters_at is None or now - self._counters_at >= COUNTER_INTERVAL_S:
            self._counters_at = now
            lines = [dict(line, priority=True) for line in self._counter_lines()]
        lines += [dict(line, priority=True) for line in self._config_lines()]
        envelopes, overflow = self._subscription.drain()
        self._dropped += overflow
        self.counts['dropped'] += overflow
        self.counts['hub_dropped'] += overflow
        for envelope in envelopes:
            fields = HUB_FIELDS.get(envelope.type)
            if fields is None:
                continue
            data = envelope.data
            line = {'event': envelope.type.replace('.', '_'), 'level': HUB_LEVELS.get(envelope.type, 'info'),
                    'at': envelope.ts, 'call_id': _safe(envelope.call_id)}  # review L4: the event's time
            line.update({name: _safe(data.get(name)) for name in fields if name in data})
            lines.append(line)
        return self._write(lines)

    def _counters(self):
        try:
            return {'service': dict(self.service.counts), 'tap': dict(self.service.tap.stats),
                    'sink': dict(self.service.sink.counts), 'pipeline': dict(self.service.pipeline.counts),
                    'failure_run': getattr(self.service, 'detector_failure_run', 0)}
        except Exception:  # a service missing a part: nothing to compare this turn
            self.counts['read_failures'] += 1
            return None

    def _last_tap_failure(self):
        """`last_stage`/`last_reason` of the tap's latest set-up failure at write time
        (TapApp `last_failure`; with a merged count above 1, earlier failures may differ,
        and a failure after the counters were read may show here and be counted on the
        next line). Only short identifier-like tokens are written, anything else becomes
        None; that is a format check, the privacy rests on the tap only setting fixed
        tokens."""
        failure = getattr(self.service.tap, 'last_failure', None)
        if not isinstance(failure, dict):
            return {}
        out = {}
        for key in ('stage', 'reason'):
            value = failure.get(key)
            out[f'last_{key}'] = value if isinstance(value, str) and _TOKEN.fullmatch(value) else None
        return out

    def _config_lines(self):
        """Reload outcomes (at most one per distinct file state, bounded by the watcher):
        values pass through `_safe`, problem lists through `_safe` per item."""
        if self._config is None:
            return []
        try:
            records = self._config.take_records()
        except Exception:
            self.counts['read_failures'] += 1
            return []
        lines = []
        for record in records:
            line = {'event': _safe(record.get('event')), 'level': record.get('level', 'info')}
            for key, value in record.items():
                if key in ('event', 'level'):
                    continue
                line[key] = [_safe(item) for item in value[:8]] if isinstance(value, list) else _safe(value)
            lines.append(line)
        return lines

    def _counter_lines(self):
        current = self._counters()
        if current is None:
            return []
        previous, self._seen = self._seen, current
        lines = []
        if previous is not None:
            for source, key, event, level in DELTAS:
                now, before = current[source].get(key, 0), previous[source].get(key, 0)
                if isinstance(now, int) and isinstance(before, int) and now > before:
                    line = {'event': event, 'level': level, 'count': now - before}
                    if event == 'tap_failure':  # 22 Nov: the latest failure's step and reason
                        line.update(self._last_tap_failure())
                    lines.append(line)
        run = current['failure_run']
        if isinstance(run, int):
            if not self._failing and run >= FAILING_AFTER:
                self._failing = True
                lines.append({'event': 'detector_failing', 'level': 'error', 'failed_batches_in_a_row': run})
            elif self._failing and run == 0:
                self._failing = False
                lines.append({'event': 'detector_recovered', 'level': 'info'})
        return lines

    def _write(self, lines):
        now = self._clock()
        if self._refilled is not None:
            self._tokens = min(float(self.max_lines_per_s), self._tokens + (now - self._refilled) * self.max_lines_per_s)
        self._refilled = now
        if self._dropped and self._tokens >= 1:
            lines = [{'event': 'log_dropped', 'level': 'warning', 'count': self._dropped}] + lines
            self._dropped = 0
        written = 0
        now = self._wall()
        for line in lines:
            priority = line.pop('priority', False)
            if self._tokens < 1 and not priority and line['event'] != 'log_dropped':
                self._dropped += 1
                self.counts['dropped'] += 1
                continue
            self._tokens = max(0.0, self._tokens - 1)
            at = line.pop('at', None)
            stamp = _iso(at if isinstance(at, (int, float)) and at == at and abs(at) < 1e11 else now)
            record = {'ts': stamp, 'level': line.pop('level'), 'event': line.pop('event'), **line}
            try:
                # Review L1: ASCII-escaped, like the start/stop records, so every line stays
                # valid JSON on any console encoding and carries no raw control characters.
                self.stream.write(json.dumps(record, sort_keys=False, ensure_ascii=True) + '\n')
                self.stream.flush()
            except Exception:  # a closed or broken stream: counted, never raised
                self.counts['write_failures'] += 1
                continue
            written += 1
            self.counts['lines'] += 1
        return written

    def pending(self):
        return self._subscription.buffered()

    def report(self):
        return {'counts': dict(self.counts)}

    def close(self):
        self._subscription.close()
