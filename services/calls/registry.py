"""In-memory call registry for `GET /v1/calls` and `GET /v1/calls/{id}` (backlog 5.2
partial; PRD FR-A3; architecture §2.5 "Timelines").

The registry is a consumer of published FR-A4 envelopes (`services/events`), the same
stream the API receives from the scorer, so it does not depend on scorer internals.
`apply(envelope)` updates one call; it never raises for an unexpected event, it counts
it, because the registry is a read model and must not stop the event consumer.

Rules (FR-A3 / architecture §2.5, with the choices made here marked):
- Timeline: each `score.updated` adds a point. Points within `full_window_s` (30 min)
  of the call's newest point stay at full resolution; older points are merged into
  fixed buckets of `bucket_s` seconds per direction (choice: 10 s, mean and maximum of
  the non-null scores, the most severe state, degraded if any point was). Buckets are
  aligned to multiples of `bucket_s` from the epoch. "Last 30 minutes" is measured
  from the call's own newest point, not the wall clock (choice), so an ended call keeps
  its final 30 minutes at full resolution.
- Memory bounds (counts, not bytes): at most `max_points` full-resolution points per
  call (oldest are bucketed early if exceeded), `max_buckets` buckets per call (oldest
  dropped and counted in `dropped_buckets`; full history is PostgreSQL's job, backlog
  5.1), `max_alerts` alerts per call (further ones counted in `alerts_dropped`),
  `max_active` active calls (further new calls refused and counted) and `max_ended`
  ended calls (oldest forgotten first). Direction labels over 16 characters are not
  stored.
- Idle calls (choice): an active call with no event for `idle_timeout_s` (300 s) of
  event time, measured against the newest envelope seen by the registry, is shown
  with `stale: true` but stays active. Only when the active limit is reached is the
  longest-idle call ended with `end_reason='idle_expired'` to admit a new call, so a
  lost `call.ended` cannot hold a slot forever. If an expired call later receives an
  event it is resumed (counted in `revived`).
- Calls are created by `call.started`, or implicitly by the first event of an unknown
  call (`started_seen=False`), so a restarted API still shows calls already in progress.
- Per call, an envelope whose `seq` is not greater than the last applied one is a
  duplicate or stale and is ignored (counted). Events after `call.ended` are ignored
  (counted). A new hub restarts `seq` at 1 (see `services/events`); a call that spans
  a hub restart therefore ignores events until its `seq` passes the old value: known
  limitation until the FR-I11 restart marker exists.
- Alerts: `alert.raised` opens an alert; `alert.cleared` clears it; an alert still open
  at `call.ended` becomes `ended_open` with no clear (architecture §2.4). A clear for an
  unknown alert is recorded with `raised_ts=None` (choice) and counted.
- `system.health` events are not about one call's timeline and are ignored here.
"""
from collections import OrderedDict, deque
import math
import threading

from services.events.contract import CALL_STATES, Envelope

FULL_WINDOW_S = 30 * 60
DEFAULT_BUCKET_S = 10.0
DEFAULT_MAX_POINTS = 2 * FULL_WINDOW_S  # 2 points/s headroom over the 30 min window
DEFAULT_MAX_BUCKETS = 2880  # 8 h of 10 s buckets per direction-mixed call
DEFAULT_MAX_ACTIVE = 64
DEFAULT_MAX_ENDED = 128
DEFAULT_MAX_ALERTS = 256
DEFAULT_IDLE_TIMEOUT_S = 300.0
IDLE_END_REASON = 'idle_expired'  # registry-inferred; not an FR-A4 call.ended reason
MAX_DIRECTION_CHARS = 16
MAX_ALERT_ID_CHARS = 128
TREND_POINTS = 20
SEVERITY = {'monitoring': 0, 'no_speech': 0, 'unmonitored': 1, 'suspicious': 2, 'alert': 3}
COUNTERS = ('applied', 'duplicate_or_stale', 'after_end', 'refused_active_limit',
            'implicit_start', 'unknown_alert_cleared', 'ignored_system_health', 'forgotten_ended',
            'lost_events', 'apply_errors', 'idle_expired', 'revived')


def _positive(name, value, kind=(int, float)):
    if isinstance(value, bool) or not isinstance(value, kind) or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be a positive finite number')


def _direction(value):
    """Directions are short labels (`caller`, `agent`); anything else is not stored."""
    return value if isinstance(value, str) and 0 < len(value) <= MAX_DIRECTION_CHARS else None


class _Call:
    def __init__(self, call_id, ts, started_seen):
        self.call_id, self.started_ts, self.started_seen = call_id, ts, started_seen
        self.ended_ts = self.final_state = self.end_reason = self.speech_seconds = None
        self.state = 'monitoring'
        self.last_seq = 0
        self.newest_ts = self.last_event_ts = -math.inf
        self.last_score = None
        self.degraded = False
        self.points = deque()  # full-resolution, in arrival (seq) order
        self.buckets = OrderedDict()  # (direction, bucket_start) -> bucket dict
        self.dropped_buckets = 0
        self.alerts = OrderedDict()
        self.alerts_dropped = 0

    def summary(self, stale=False):
        n = len(self.points)
        recent = [self.points[i]['score'] for i in range(max(0, n - TREND_POINTS), n)]
        open_alerts = sum(a['state'] == 'open' for a in self.alerts.values())
        return {'call_id': self.call_id, 'status': 'active' if self.ended_ts is None else 'ended',
                'state': self.state, 'started_ts': self.started_ts, 'started_seen': self.started_seen,
                'ended_ts': self.ended_ts, 'final_state': self.final_state, 'end_reason': self.end_reason,
                'last_score': self.last_score, 'degraded': self.degraded, 'score_trend': recent,
                'alerts_total': len(self.alerts), 'alerts_open': open_alerts,
                'alerts_dropped': self.alerts_dropped, 'stale': stale}

    def detail(self, stale=False):
        out = self.summary(stale)
        out['speech_seconds'] = self.speech_seconds
        out['alerts'] = [dict(a) for a in self.alerts.values()]
        out['timeline'] = {'full_resolution': [dict(p) for p in self.points],
                           'downsampled': sorted((dict(b) for b in self.buckets.values()),
                                                 key=lambda b: (b['start_ts'], str(b['direction']))),
                           'dropped_buckets': self.dropped_buckets}
        return out


class CallRegistry:
    def __init__(self, *, full_window_s=FULL_WINDOW_S, bucket_s=DEFAULT_BUCKET_S,
                 max_points=DEFAULT_MAX_POINTS, max_buckets=DEFAULT_MAX_BUCKETS,
                 max_active=DEFAULT_MAX_ACTIVE, max_ended=DEFAULT_MAX_ENDED,
                 max_alerts=DEFAULT_MAX_ALERTS, idle_timeout_s=DEFAULT_IDLE_TIMEOUT_S):
        _positive('full_window_s', full_window_s)
        _positive('bucket_s', bucket_s)
        _positive('idle_timeout_s', idle_timeout_s)
        for name, value in (('max_points', max_points), ('max_buckets', max_buckets),
                            ('max_active', max_active), ('max_ended', max_ended),
                            ('max_alerts', max_alerts)):
            _positive(name, value, int)
        self.full_window_s, self.bucket_s = float(full_window_s), float(bucket_s)
        self.max_points, self.max_buckets = max_points, max_buckets
        self.max_active, self.max_ended = max_active, max_ended
        self.max_alerts, self.idle_timeout_s = max_alerts, float(idle_timeout_s)
        self._latest_ts = -math.inf
        self._lock = threading.Lock()
        self._active = OrderedDict()
        self._ended = OrderedDict()
        self._counters = dict.fromkeys(COUNTERS, 0)

    # --- event intake -------------------------------------------------------------------
    def apply(self, envelope):
        """Apply one published envelope. Returns True if it changed the registry."""
        if not isinstance(envelope, Envelope):
            raise TypeError('apply needs a services.events Envelope')
        with self._lock:
            try:
                return self._apply(envelope)
            except Exception:  # a read model must not stop its consumer; counted instead
                self._counters['apply_errors'] += 1
                return False

    def _apply(self, envelope):
        self._latest_ts = max(self._latest_ts, envelope.ts)
        if envelope.type == 'system.health':
            self._counters['ignored_system_health'] += 1
            return False
        call = self._call_for(envelope)
        if call is None:
            return False
        if envelope.seq <= call.last_seq:
            self._counters['duplicate_or_stale'] += 1
            return False
        call.last_seq = envelope.seq
        call.last_event_ts = max(call.last_event_ts, envelope.ts)
        getattr(self, '_on_' + envelope.type.replace('.', '_'))(call, envelope, envelope.data)
        self._counters['applied'] += 1
        return True

    def _idle(self, call):
        return call.last_event_ts < self._latest_ts - self.idle_timeout_s

    def _free_slot(self):
        """True if a new call may become active. When the limit is reached, the call idle
        longest (no event for `idle_timeout_s` of event time) is ended as `idle_expired`
        to make room: a lost `call.ended` must not hold a slot forever. Without that
        pressure an idle call stays active and is only flagged `stale`."""
        if len(self._active) < self.max_active:
            return True
        idle = [c for c in self._active.values() if self._idle(c)]
        if not idle:
            self._counters['refused_active_limit'] += 1
            return False
        call = min(idle, key=lambda c: c.last_event_ts)
        self._end(call, call.last_event_ts, call.state, IDLE_END_REASON, None)
        self._counters['idle_expired'] += 1
        return True

    def _call_for(self, envelope):
        call_id = envelope.call_id
        if call_id in self._ended:
            ended = self._ended[call_id]
            if ended.end_reason != IDLE_END_REASON:
                self._counters['after_end'] += 1
                return None
            if len(self._active) >= self.max_active and not any(map(self._idle, self._active.values())):
                self._counters['refused_active_limit'] += 1  # no room: it stays where it was
                return None
            del self._ended[call_id]  # out first, so making room cannot forget this call
            self._free_slot()  # succeeds: there is a free or an idle slot
            # it was only quiet: resume it
            ended.ended_ts = ended.final_state = ended.end_reason = None
            for alert in ended.alerts.values():
                if alert['state'] == 'ended_open':
                    alert['state'] = 'open'
            self._active[call_id] = ended
            self._counters['revived'] += 1
            return ended
        call = self._active.get(call_id)
        if call is not None:
            return call
        if not self._free_slot():
            return None
        started = envelope.type == 'call.started'
        if not started:
            self._counters['implicit_start'] += 1
        call = _Call(call_id, envelope.ts, started)
        self._active[call_id] = call
        return call

    def _on_call_started(self, call, envelope, data):
        if not call.started_seen:  # a late start after implicit creation keeps the earlier ts
            call.started_seen = True
            call.started_ts = min(call.started_ts, envelope.ts)

    def _on_score_updated(self, call, envelope, data):
        score = data['score']
        call.state, call.degraded = data['state'], data['degraded']
        if score is not None:
            call.last_score = float(score)
        point = {'ts': envelope.ts, 'seq': envelope.seq, 'score': None if score is None else float(score),
                 'state': data['state'], 'degraded': data['degraded'],
                 'effective_stride_s': float(data['effective_stride_s']),
                 'direction': _direction(data.get('direction'))}
        self._add_point(call, point)

    def _alert_slot(self, call, alert_id):
        """True if the alert may be recorded; beyond the per-call cap it is counted only."""
        if alert_id in call.alerts:
            return True
        if not isinstance(alert_id, str) or len(alert_id) > MAX_ALERT_ID_CHARS \
                or len(call.alerts) >= self.max_alerts:
            call.alerts_dropped += 1
            return False
        return True

    def _on_alert_raised(self, call, envelope, data):
        alert_id = data['alert_id']
        if alert_id not in call.alerts and self._alert_slot(call, alert_id):
            call.alerts[alert_id] = {'alert_id': alert_id, 'state': 'open', 'raised_ts': envelope.ts,
                                     'cleared_ts': None, 'direction': _direction(data.get('direction'))}
        call.state = 'alert'

    def _on_alert_cleared(self, call, envelope, data):
        alert_id = data['alert_id']
        alert = call.alerts.get(alert_id)
        if alert is None:
            self._counters['unknown_alert_cleared'] += 1
            if not self._alert_slot(call, alert_id):
                return
            alert = call.alerts[alert_id] = {'alert_id': alert_id, 'raised_ts': None,
                                             'direction': _direction(data.get('direction'))}
        alert.update(state='cleared', cleared_ts=envelope.ts)

    def _on_call_ended(self, call, envelope, data):
        self._end(call, envelope.ts, data['final_state'], data['reason'], float(data['speech_seconds']))

    def _end(self, call, ts, final_state, reason, speech_seconds):
        call.ended_ts, call.final_state = ts, final_state
        call.end_reason, call.speech_seconds = reason, speech_seconds
        call.state = final_state
        for alert in call.alerts.values():
            if alert['state'] == 'open':
                alert['state'] = 'ended_open'
        del self._active[call.call_id]
        self._ended[call.call_id] = call
        while len(self._ended) > self.max_ended:
            self._ended.popitem(last=False)
            self._counters['forgotten_ended'] += 1

    def _add_point(self, call, point):
        """Incremental: points arrive in seq order and almost always in time order, so only
        the front of the deque is examined. A point already older than the horizon (the
        hub clock stepped back) is bucketed at once; one that becomes older later while
        behind newer-stamped points waits until those leave the front (bounded by
        `max_points`)."""
        call.newest_ts = max(call.newest_ts, point['ts'])
        horizon = call.newest_ts - self.full_window_s
        if point['ts'] < horizon:
            self._add_to_bucket(call, point)
        else:
            call.points.append(point)
        while call.points and (call.points[0]['ts'] < horizon or len(call.points) > self.max_points):
            self._add_to_bucket(call, call.points.popleft())
        while len(call.buckets) > self.max_buckets:
            oldest = min(call.buckets, key=lambda k: (call.buckets[k]['start_ts'], str(k[0])))
            del call.buckets[oldest]
            call.dropped_buckets += 1

    def _add_to_bucket(self, call, point):
        start = math.floor(point['ts'] / self.bucket_s) * self.bucket_s
        key = (point['direction'], start)
        bucket = call.buckets.get(key)
        if bucket is None:
            bucket = call.buckets[key] = {'direction': point['direction'], 'start_ts': start,
                                          'end_ts': start + self.bucket_s, 'points': 0,
                                          'scored_points': 0, 'score_mean': None, 'score_max': None,
                                          'state': point['state'], 'degraded': False}
        bucket['points'] += 1
        bucket['degraded'] = bucket['degraded'] or point['degraded']
        if SEVERITY.get(point['state'], 0) > SEVERITY.get(bucket['state'], 0):
            bucket['state'] = point['state']
        score = point['score']
        if score is not None:
            n = bucket['scored_points']
            bucket['score_mean'] = score if n == 0 else (bucket['score_mean'] * n + score) / (n + 1)
            bucket['score_max'] = score if n == 0 else max(bucket['score_max'], score)
            bucket['scored_points'] = n + 1

    # --- queries ------------------------------------------------------------------------
    def list_calls(self, status='all'):
        """Summaries, active calls first (oldest first), then ended (most recent first)."""
        if status not in ('all', 'active', 'ended'):
            raise ValueError("status must be 'all', 'active' or 'ended'")
        with self._lock:
            out = []
            if status in ('all', 'active'):
                out += [c.summary(self._idle(c)) for c in self._active.values()]
            if status in ('all', 'ended'):
                out += [c.summary() for c in reversed(self._ended.values())]
            return out

    def get_call(self, call_id):
        """Detail of one call; `stale` marks an active call with no event for
        `idle_timeout_s` of event time (it may have lost its `call.ended`)."""
        with self._lock:
            if call_id in self._active:
                call = self._active[call_id]
                return call.detail(self._idle(call))
            call = self._ended.get(call_id)
            return None if call is None else call.detail()

    def stats(self):
        with self._lock:
            return {'active': len(self._active), 'ended': len(self._ended), **self._counters}

    def note_lost(self, count):
        """Record envelopes the feed lost before they reached the registry."""
        with self._lock:
            self._counters['lost_events'] += count


class RegistryFeed:
    """Feeds a registry from an in-process `EventHub` subscription. `pump()` drains what
    is buffered (never blocks) and applies it; envelopes the subscription dropped are
    counted in the registry's `lost_events` (the registry may then be out of date for
    some calls until PostgreSQL recovery exists, backlog 5.1/5.3)."""

    def __init__(self, registry, hub, *, buffer=4096):
        self.registry = registry
        self._subscription = hub.subscribe(buffer=buffer)
        self._lock = threading.Lock()  # concurrent pumps must not apply out of seq order

    def pump(self):
        with self._lock:
            envelopes, dropped = self._subscription.drain()
            if dropped:
                self.registry.note_lost(dropped)
            for envelope in envelopes:
                self.registry.apply(envelope)
            return len(envelopes)

    def close(self):
        self._subscription.close()


if set(SEVERITY) != set(CALL_STATES):  # keep the severity order in step with the contract
    raise RuntimeError('registry SEVERITY does not cover services.events CALL_STATES')
