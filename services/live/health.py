"""Health, readiness and metrics of the live service process (backlog 7.2/8.1; PRD FR-A8
`GET /healthz`, `GET /readyz`, `GET /metrics` in Prometheus format; FR-O6 queue depth,
active calls, dropped windows, inference latency; architecture §2.3).

`HealthServer(service, pumps=..., host=, port=)` serves three read-only GET routes from
a standard-library HTTP server on its own daemon threads (no web framework, no
runtime download, nothing written):

- `/healthz` 200 while the process serves HTTP (liveness).
- `/readyz` 200 only when (1) the ARI event stream is connected (`service.ready`),
  (2) the scoring loop published its pipeline snapshot within `stale_after_s` (10 s by
  default: one loop turn can hold a detector batch of up to ~2 s and an ARI request of
  up to 5 s; a hung detector or loop shows here) and (3) the detector has not failed
  `FAILING_AFTER` batches in a row with no success since (`service.detector_failure_run`;
  a detector failing fast keeps the loop turning). Otherwise 503 with the first failing
  reason: `ari_disconnected`, `loop_not_started`, `loop_stalled`, `detector_failing`.
  FR-A8 says `/readyz` "performs a warm-up inference": here the warm-up runs once in the
  start-up gate (`load_detector`, which refuses to start without it) and readiness then
  follows recent detector success, rather than running inference per request on the
  HTTP thread, which any client could trigger and which would compete with scoring
  (a proposed FR-A8 wording is in services/live/README.md). A failure run clears only
  when a batch succeeds, which needs a scored call: after a brief detector fault the
  process can stay unready while idle (a conservative choice; a spec question). Storage being down does not make
  the service unready (advisory storage); it shows in the store metrics.
- `/metrics` Prometheus text 0.0.4: the shared live-pipeline families
  (`services/scorer/metrics_text.py`, the same as the analysis API's), readiness, ARI
  connects/disconnects, loop/hook/shutdown errors, the detector failure run, active
  calls and capacity, tap and sink counters, and per-pump health (alive, stuck, faults,
  store outcomes and waits, backlog, consecutive failures, forwarded/failed/unheard
  publishes, hub drops). No call IDs, caller numbers or audio, ever.

Reads are of counters owned by other threads (copied, never locked across the loop):
a value can be one turn old. A bad value drops that family, never the endpoint.
Connections are bounded (`MAX_CONNECTIONS`, extra ones closed at once) and a client
that sends nothing is dropped after `CLIENT_TIMEOUT_S`. The port is bound exclusively
(no `SO_REUSEADDR` on Windows, where it would let a second process share the port).
Bind to 127.0.0.1 unless a scraper elsewhere needs it; there is no authentication.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import socket
import threading

from services.scorer.metrics_text import histogram_lines, number, pipeline_lines, snapshot_valid_lines
from services.tap.jitter import HOLD_BUCKETS_MS, HOLD_KEYS
from services.tap.scoring_sink import TAP_STAGES

DEFAULT_STALE_AFTER_S = 10.0
FAILING_AFTER = 3  # detector batches failed in a row before /readyz says detector_failing
MAX_CONNECTIONS = 8
CLIENT_TIMEOUT_S = 5.0
_LABEL_VALUE = re.compile(r'^[A-Za-z0-9_.:-]{1,64}$')
SERVICE_COUNTERS = (('ava_live_ari_connects_total', 'connects', 'ARI event stream connections made.'),
                    ('ava_live_ari_disconnects_total', 'disconnects', 'ARI event stream drops (not stops).'),
                    ('ava_live_loop_errors_total', 'loop_errors', 'Unexpected faults in the service loop.'),
                    ('ava_live_hook_errors_total', 'hook_errors', 'Faults in loop tick hooks.'),
                    ('ava_live_shutdown_errors_total', 'shutdown_errors', 'Faults while shutting down.'))
TAP_COUNTERS = ('events', 'bad_events', 'tapped', 'tap_failures', 'refused_capacity', 'ended', 'sink_errors',
                'cleanup_errors', 'swept')
SINK_COUNTERS = ('started', 'refused', 'audio_chunks', 'audio_unfed', 'ended', 'ended_elsewhere', 'end_retries',
                 'ends_forced', 'end_cancelled_tail', 'health_published', 'health_failures', 'pump_errors',
                 'errors', 'recorder_errors', 'timer_failures')
# services/tap/rtp_receiver.py RtpSession.stats and services/tap/jitter.py JitterBuffer.stats
# (prefixed `jitter_`), summed over ended calls; a key never seen is exported as 0.
RECEIVER_COUNTERS = ('packets', 'malformed', 'other_payload', 'other_source', 'gaps', 'missing', 'late_or_duplicate',
                     'over_limit', 'ssrc_changes', 'other_ptime')
JITTER_COUNTERS = ('received', 'played', 'concealed_frames', 'repeated_frames', 'silence_frames', 'late', 'duplicate',
                   'reordered', 'resyncs', 'talkspurts', 'restarts', 'drift_adjustments', 'dropped_pending',
                   'dropped_handover', 'dropped_pending_received')
# The jitter buffer's hold-time stats, exported as `ava_live_jitter_hold_seconds` (18 Nov).
HOLD_STATS = (*HOLD_KEYS, 'hold_frames', 'hold_us_sum', 'hold_unmeasured')
STORE_OUTCOMES = ('stored', 'duplicate', 'after_end', 'not_stored', 'unknown_alert', 'refused', 'skipped_faulty')
STORE_WAITS = ('retry_waits', 'store_misconfigured', 'faults')


def _family(kind, name, text, samples):
    """HELP/TYPE plus `(labels, value)` samples; values validated (finite, >= 0) and
    label values restricted to a safe identifier set (review L3)."""
    lines = [f'# HELP {name} {text}', f'# TYPE {name} {kind}']
    for labels, value in samples:
        for _, label_value in labels:
            if not _LABEL_VALUE.match(str(label_value)):
                raise ValueError('unsafe metric label value')
        label = '' if not labels else '{' + ','.join(f'{k}="{v}"' for k, v in labels) + '}'
        lines.append(f'{name}{label} {number(value)!r}' if isinstance(value, float) else
                     f'{name}{label} {number(value)}')
    return lines


class _Server(ThreadingHTTPServer):
    # Windows SO_REUSEADDR lets a second socket bind a port already in use (review M2).
    allow_reuse_address = os.name != 'nt'
    daemon_threads = True

    def server_bind(self):
        if os.name == 'nt' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def process_request(self, request, client_address):
        # Bounded concurrency (review M3): refuse by closing when every slot is taken.
        if not self.slots.acquire(blocking=False):
            self.owner.count('refused_connections')
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:  # e.g. no thread could start: give the slot back, drop the client
            self.slots.release()
            self.shutdown_request(request)
            self.owner.count('refused_connections')

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class HealthServer:
    def __init__(self, service, *, pumps=(), host='127.0.0.1', port=0, stale_after_s=DEFAULT_STALE_AFTER_S,
                 max_connections=MAX_CONNECTIONS, client_timeout_s=CLIENT_TIMEOUT_S):
        if isinstance(stale_after_s, bool) or not isinstance(stale_after_s, (int, float)) \
                or not 0 < stale_after_s <= 300:
            raise ValueError('stale_after_s must be above 0 and at most 300 seconds')
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError('port must be an integer from 0 to 65535')
        if type(max_connections) is not int or not 1 <= max_connections <= 64:
            raise ValueError('max_connections must be an integer from 1 to 64')
        if isinstance(client_timeout_s, bool) or not isinstance(client_timeout_s, (int, float)) \
                or not 0 < client_timeout_s <= 60:
            raise ValueError('client_timeout_s must be above 0 and at most 60 seconds')
        self.service, self.pumps, self.stale_after_s = service, tuple(pumps), float(stale_after_s)
        self._lock = threading.Lock()
        self.counts = {'requests': 0, 'render_errors': 0, 'refused_connections': 0}
        handler = type('Handler', (_Handler,), {'owner': self, 'timeout': float(client_timeout_s)})
        self._server = _Server((host, port), handler)  # binds now: a bad or busy address fails at start-up
        self._server.owner, self._server.slots = self, threading.BoundedSemaphore(max_connections)
        self._thread = None

    def count(self, name):
        with self._lock:  # HTTP threads run concurrently (review L5)
            self.counts[name] += 1

    @property
    def address(self):
        host, port = self._server.server_address[:2]
        return host, port

    def start(self):
        if self._thread is not None:
            raise RuntimeError('health server already started')
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={'poll_interval': 0.2},
                                        name='ava-health', daemon=True)
        self._thread.start()

    def stop(self, timeout=2.0):
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout)
        self._server.server_close()

    close = stop  # so start-up refusals can close it like the other opened resources

    # -- views (also used directly by tests) -------------------------------------------

    def readiness(self):
        """(ready, reason, details) per the module docstring."""
        snapshot, age = self.service.pipeline_stats.read()
        failing = getattr(self.service, 'detector_failure_run', 0)
        details = {'ari_connected': self.service.ready.is_set(),
                   'loop_snapshot_age_s': None if age is None else round(age, 3),
                   'stale_after_s': self.stale_after_s, 'detector_failure_run': failing}
        if not details['ari_connected']:
            return False, 'ari_disconnected', details
        if snapshot is None:
            return False, 'loop_not_started', details
        if age > self.stale_after_s:
            return False, 'loop_stalled', details
        if failing >= FAILING_AFTER:
            return False, 'detector_failing', details
        return True, None, details

    def metrics(self):
        ready, _, _ = self.readiness()
        lines = _family('gauge', 'ava_live_ready', '1 when /readyz would answer 200.', [((), int(ready))])
        sections = (self._service_lines, self._call_lines, self._tap_lines, self._tap_stage_lines,
                    self._receiver_lines, self._jitter_hold_lines, self._pipeline_lines, self._pump_lines)
        for section in sections:
            try:
                lines += section()
            except Exception:  # one bad family must not break /metrics
                self.count('render_errors')
        with self._lock:
            counts = dict(self.counts)
        lines += (_family('counter', 'ava_live_metrics_render_errors_total',
                          'Metric families dropped because a value was invalid.', [((), counts['render_errors'])])
                  + _family('counter', 'ava_live_health_requests_total', 'Requests served by the health endpoint.',
                            [((), counts['requests'])])
                  + _family('counter', 'ava_live_health_refused_connections_total',
                            'Health connections closed because every slot was busy.',
                            [((), counts['refused_connections'])]))
        return '\n'.join(lines) + '\n'

    def _service_lines(self):
        counts = dict(self.service.counts)
        lines = []
        for name, key, text in SERVICE_COUNTERS:
            lines += _family('counter', name, text, [((), counts[key])])
        return lines + _family('gauge', 'ava_live_detector_failure_run',
                               'Detector batches failed in a row since the last success.',
                               [((), getattr(self.service, 'detector_failure_run', 0))])

    def _call_lines(self):
        gate = self.service.gate
        return (_family('gauge', 'ava_live_calls_active', 'Calls admitted for scoring now (FR-O6 active calls).',
                        [((), gate.active)])
                + _family('gauge', 'ava_live_calls_capacity', 'Maximum calls scored at once (max_calls).',
                          [((), gate.max_calls)])
                + _family('counter', 'ava_live_calls_refused_capacity_total',
                          'Calls not scored because capacity was full (the calls themselves go on).',
                          [((), gate.refused)]))

    def _tap_lines(self):
        tap, sink = dict(self.service.tap.stats), dict(self.service.sink.counts)
        return (_family('counter', 'ava_live_tap_events_total', 'Tap event counters by kind.',
                        [((('kind', key),), tap[key]) for key in TAP_COUNTERS])
                + _family('counter', 'ava_live_sink_events_total', 'Tap-to-scoring sink counters by kind.',
                          [((('kind', key),), sink[key]) for key in SINK_COUNTERS]))

    def _tap_stage_lines(self):
        """Its own section (review L3): a malformed histogram drops only these families."""
        stage_times = getattr(self.service.sink, 'stage_times', None)
        if stage_times is None:
            return []
        snapshot = stage_times.snapshot()
        audio = snapshot['audio_seconds']
        if set(audio) != set(TAP_STAGES):
            raise ValueError('tap stage audio seconds are malformed')
        return (histogram_lines('ava_live_tap_stage_seconds',
                                'Tap-to-scoring processing time per delivered chunk (one or more 20 ms frames) by '
                                'stage (FR-O6): resample (8 to 16 kHz, flags, PCM16), speech_feed (VAD, windowing, '
                                'offer). Divide by ava_live_tap_audio_seconds_total for the cost per audio second.',
                                'stage', TAP_STAGES, snapshot)
                + _family('counter', 'ava_live_tap_audio_seconds_total',
                          'Seconds of tapped 8 kHz audio each tap stage processed (for the cost per audio second).',
                          [((('stage', stage),), float(audio[stage])) for stage in TAP_STAGES]))

    def _receiver_lines(self):
        tap = self.service.tap
        # Ended calls plus open calls as last read (<= 1 s old, 18 Nov); replaced, never mutated.
        totals = dict(getattr(tap, 'receiver_live', None) or {})
        degraded = getattr(tap, 'calls_degraded', 0)
        return (_family('counter', 'ava_live_rtp_receiver_total',
                        'RTP receiver counts of ended calls plus calls in progress (read at most 1 s ago): '
                        'packets, malformed, gaps, ...; kinds have different units, do not sum across kinds.',
                        [((('kind', key),), totals.get(key, 0)) for key in RECEIVER_COUNTERS])
                + _family('counter', 'ava_live_jitter_buffer_total',
                          'Jitter-buffer counts of ended calls plus calls in progress (frames, packets and events; '
                          'do not sum across kinds); dropped_handover = concealment skipped by the hand-over '
                          'budget (FR-I2); dropped_pending_received = the received frames among dropped_pending.',
                          [((('kind', key),), totals.get(f'jitter_{key}', 0)) for key in JITTER_COUNTERS])
                + _family('gauge', 'ava_live_rtp_calls_degraded',
                          'Tapped calls now in a sustained-loss episode (over 5 % of the last 10 s of frames '
                          'concealed, FR-I2 media_quality).', [((), degraded)]))

    def _jitter_hold_lines(self):
        """`ava_live_jitter_hold_seconds`, the first hop of the architecture §3 latency
        budget (RTP arrival -> frame available, 60 ms), from the same ended-plus-open
        totals; its own section, so inconsistent counts drop only these families."""
        totals = dict(getattr(self.service.tap, 'receiver_live', None) or {})
        cumulative = [number(totals.get(f'jitter_{key}', 0)) for key in HOLD_KEYS]
        count = number(totals.get('jitter_hold_frames', 0))
        total_us = number(totals.get('jitter_hold_us_sum', 0))
        if cumulative != sorted(cumulative) or cumulative[-1] > count or (count == 0 and total_us != 0):
            raise ValueError('jitter hold buckets are malformed')
        name = 'ava_live_jitter_hold_seconds'
        lines = [f'# HELP {name} Time each received 20 ms frame spent from the loop reading it off the RTP socket to '
                 'the loop handing it to scoring: the 60 ms buffer delay plus loop-turn quantisation; socket wait '
                 'before a read is not included (FR-O6). Ended calls plus calls in progress (read at most 1 s ago).',
                 f'# TYPE {name} histogram']
        lines += [f'{name}_bucket{{le="{ms / 1000!r}"}} {n}' for ms, n in zip(HOLD_BUCKETS_MS, cumulative)]
        lines += [f'{name}_bucket{{le="+Inf"}} {count}', f'{name}_sum {total_us / 1e6!r}', f'{name}_count {count}']
        return lines + _family('counter', 'ava_live_jitter_hold_unmeasured_total',
                               'Received frames handed out without a usable hold time (no clock, it went back, or the '
                               'hold is too large to represent).',
                               [((), totals.get('jitter_hold_unmeasured', 0))])

    def _pipeline_lines(self):
        snapshot, age = self.service.pipeline_stats.read()
        lines = []
        if snapshot is not None:
            try:
                lines = pipeline_lines(snapshot, age)
            except Exception:  # malformed snapshot: say so in the validity gauge
                self.count('render_errors')
        return lines + snapshot_valid_lines(lines)

    def _pump_lines(self):
        if not self.pumps:
            return []
        alive, stuck, faults, moved = [], [], [], []
        stored, waits, backlog, failures, hub_dropped, superseded = [], [], [], [], [], []
        forwarded, publish_failures, no_receivers = [], [], []
        snippets, snippets_queued = [], []
        for pump in self.pumps:
            thread = pump['thread']
            label = (('pump', thread.name),)
            snap = thread.snapshot()
            alive.append((label, int(snap['alive'])))
            stuck.append((label, int(bool(snap['stuck']))))
            faults.append((label, snap['faults']))
            moved.append((label, snap['moved']))
            report = pump['report']()
            if 'snippets' in report:  # FR-O5 snippet recorder (services/live/retention.py)
                snippets += [((('outcome', key),), value) for key, value in sorted(report['snippets'].items())]
                snippets_queued.append(((), report['queued']))
                continue
            counts = report['counts']
            hub_dropped.append((label, counts.get('hub_dropped', 0)))
            if 'backlog' in report:
                stored += [((('outcome', key),), counts.get(key, 0)) for key in STORE_OUTCOMES]
                waits += [((('kind', key),), counts.get(key, 0)) for key in STORE_WAITS]
                backlog.append(((), report['backlog']))
                failures.append(((), report['consecutive_failures']))
                if 'superseded' in report:
                    superseded.append(((), report['superseded']))
            if 'forwarded' in counts:
                forwarded.append(((), counts['forwarded']))
                publish_failures.append(((), counts['publish_failures']))
                no_receivers.append(((), counts.get('no_receivers', 0)))
        lines = (_family('gauge', 'ava_live_pump_alive', '1 while the pump thread runs.', alive)
                 + _family('gauge', 'ava_live_pump_stuck', '1 when the pump did not end at stop.', stuck)
                 + _family('counter', 'ava_live_pump_faults_total', 'Unexpected pump exceptions.', faults)
                 + _family('counter', 'ava_live_pump_moved_total', 'Envelopes handled by the pump.', moved)
                 + _family('counter', 'ava_live_pump_hub_dropped_total',
                           'Events the hub dropped before the pump took them (buffer full).', hub_dropped))
        if backlog:
            lines += (_family('counter', 'ava_live_store_events_total', 'Events by final store outcome.', stored)
                      + _family('counter', 'ava_live_store_waits_total',
                                'Store attempts that left the event waiting, by cause.', waits)
                      + _family('gauge', 'ava_live_store_backlog', 'Events waiting to be stored.', backlog)
                      + _family('gauge', 'ava_live_store_consecutive_failures',
                                'Store attempts failed in a row (> 0: storage is stalled).', failures))
        if superseded:
            lines += _family('counter', 'ava_live_store_superseded_total',
                             'Stored starts that ended an open session by inference (an earlier scorer stopped '
                             'abruptly; also counted as stored; source of truth: calls.end_inferred).', superseded)
        if snippets:
            lines += (_family('counter', 'ava_live_snippets_total',
                              'Alert snippet recorder counts (FR-O5): captured, written, write_failed, no_audio, '
                              'queue_full, cleanup runs/deleted, ...; kinds differ, do not sum.', snippets)
                      + _family('gauge', 'ava_live_snippets_queued', 'Snippets waiting to be written.',
                                snippets_queued))
        if forwarded:
            lines += (_family('counter', 'ava_live_redis_forwarded_total', 'Events published on Redis.', forwarded)
                      + _family('counter', 'ava_live_redis_publish_failures_total',
                                'Events lost because Redis publishing failed.', publish_failures)
                      + _family('counter', 'ava_live_redis_no_receivers_total',
                                'Events published while nobody was subscribed.', no_receivers))
        return lines


class _Handler(BaseHTTPRequestHandler):
    owner = None  # set per server
    server_version = 'ava-live'
    sys_version = ''

    def log_message(self, format, *args):  # no request logging to stderr (the JSON records stay clean)
        pass

    def _send(self, status, body, content_type):
        payload = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802 (http.server naming)
        owner = self.owner
        owner.count('requests')
        path = self.path.split('?', 1)[0]
        try:
            if path == '/healthz':
                self._send(200, json.dumps({'status': 'alive'}), 'application/json')
            elif path == '/readyz':
                ready, reason, details = owner.readiness()
                body = {'ready': ready, **({'reason': reason} if reason else {}), **details}
                self._send(200 if ready else 503, json.dumps(body), 'application/json')
            elif path == '/metrics':
                self._send(200, owner.metrics(), 'text/plain; version=0.0.4; charset=utf-8')
            else:
                self._send(404, json.dumps({'error': 'not found'}), 'application/json')
        except Exception:  # a broken read never kills the server thread
            owner.count('render_errors')
            self._send(500, json.dumps({'error': 'internal'}), 'application/json')

    def _not_allowed(self):
        self.send_response(405)
        self.send_header('Allow', 'GET')
        self.send_header('Content-Length', '0')
        self.end_headers()

    # Every other method is 405 with `Allow: GET` (review L4), HEAD and OPTIONS included.
    do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _not_allowed  # noqa: N815
