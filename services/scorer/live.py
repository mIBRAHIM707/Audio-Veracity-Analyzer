"""Live call table: admission, per-call scoring and publishing, cleanup and late-result
discard (backlog 5.2/5.3 partial; PRD FR-I4, FR-I9, FR-I11, FR-A4; architecture §2.2
capacity refusal, §2.4 durability and cleanup).

`LiveCalls` ties the existing pieces together for the live path:
- `start(call_id)` asks the `CallGate`; a refused call is not tapped and a
  `system.health` event with reason `capacity` is published (FR-I9; the call itself
  continues). An admitted call gets an `OrderedCallScorer` built from the config
  snapshot current at its start (FR-I4 call-scoped reload) and a
  `CallEventPublisher`; `call.started` is published.
- `result(...)` applies one inference result under the call's own lock and publishes
  its events in the same critical section, so a call's events reach the hub in scorer
  order even with several inference workers.
- `end(call_id, reason, speech_seconds=...)` removes the call from the table first,
  so a result racing with the end is discarded (FR-I11 "late results for an unknown
  call ID are discarded"), then ends the scorer, publishes `call.ended` and releases
  the gate slot. `is_active` lets the inference loop skip queued windows of an ended
  call (cancellation at dequeue; FR-I11 "drains or cancels in-flight windows").
- `end_all('monitor_lost', ...)` ends every open call, as when the tap receiver
  restarts (architecture §2.4).

Not done here (needs backlog 5.1/6): persisting state to Redis/PostgreSQL every hop and
resuming after a scorer restart (FR-I11). Publishing never raises (see `publish.py`);
scorer input errors (`ValueError`) are raised to the caller, whose loop must contain
them so a call is never affected.
"""
from collections import namedtuple
import threading

from .ordered import OrderedCallScorer
from .publish import CallEventPublisher
from .session import END_REASONS, SessionClosed

# applied: the scorer took the result; reason: None, 'duplicate', 'stale', 'unknown_call'
# or 'call_ended'; envelopes: what was published for it.
ResultOutcome = namedtuple('ResultOutcome', 'applied reason envelopes')


class _Entry:
    def __init__(self, scorer, publisher):
        self.scorer, self.publisher = scorer, publisher
        self.lock = threading.Lock()


class LiveCalls:
    def __init__(self, config_store, hub, gate, *, new_alert_id=None):
        self._configs, self._hub, self._gate = config_store, hub, gate
        self._new_alert_id = new_alert_id
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._calls = {}
        self.counters = {'started': 0, 'refused': 0, 'ended': 0, 'discarded_unknown_call': 0,
                         'discarded_after_end': 0, 'health_publish_failures': 0,
                         'speech_seconds_missing': 0, 'publish_failures': 0, 'end_all_failures': 0}

    def _count(self, name):
        with self._lock:
            self.counters[name] += 1

    def start(self, call_id, **started_data):
        """Admit and start scoring a call. Returns True if the call is (already) tapped.
        Starts are serialized (they are rare), so a failing start cannot release a slot
        another start of the same call is using."""
        with self._start_lock:
            with self._lock:
                if call_id in self._calls:
                    return True
            admitted, health = self._gate.admit(call_id)
            if not admitted:
                self._count('refused')
                try:
                    self._hub.publish('system.health', call_id, health)
                except Exception:  # advisory: the refusal itself must not fail
                    self._count('health_publish_failures')
                return False
            try:
                scorer = OrderedCallScorer.create(self._configs.for_new_call(), call_id=call_id,
                                                  new_alert_id=self._new_alert_id)
                entry = _Entry(scorer, CallEventPublisher(self._hub, call_id))
            except Exception:
                self._gate.release(call_id)  # do not leak the slot
                raise
            # Hold the call's lock from before it is visible until call.started is
            # published, so no result or end for it can reach the hub first.
            with entry.lock:
                with self._lock:
                    self._calls[call_id] = entry
                    self.counters['started'] += 1
                entry.publisher.started(**started_data)
            return True

    def is_active(self, call_id):
        with self._lock:
            return call_id in self._calls

    def token(self, call_id):
        """Opaque identity of the active call with this ID (None if not active). A call
        that ends and starts again under the same ID gets a different token, so callers
        holding per-call state can tell the two apart."""
        with self._lock:
            return self._calls.get(call_id)

    def scored_directions(self, call_id):
        """Directions `end` needs speech seconds for, or None for an unknown call."""
        entry = self._entry(call_id)
        if entry is None:
            return None
        with entry.lock:
            return entry.scorer.scored_directions()

    def human_agent_answered(self, call_id):
        entry = self._entry(call_id)
        if entry is None:
            return False
        with entry.lock:
            try:
                entry.scorer.human_agent_answered()
            except SessionClosed:
                return False
        return True

    def _entry(self, call_id):
        with self._lock:
            return self._calls.get(call_id)

    def result(self, call_id, direction, window_idx, p_synthetic, *, concealed_fraction=0.0,
               degraded=False, effective_stride_s):
        """Apply and publish one inference result. Returns a ResultOutcome."""
        entry = self._entry(call_id)
        if entry is None:
            self._count('discarded_unknown_call')
            return ResultOutcome(False, 'unknown_call', [])
        with entry.lock:
            try:
                outcome = entry.scorer.add_result(direction, window_idx, p_synthetic,
                                                  concealed_fraction=concealed_fraction)
            except SessionClosed:  # ended while this result waited for the lock
                self._count('discarded_after_end')
                return ResultOutcome(False, 'call_ended', [])
            envelopes = entry.publisher.window(outcome['events'], degraded=degraded,
                                               effective_stride_s=effective_stride_s)
        return ResultOutcome(outcome['applied'], outcome['reason'], envelopes)

    def status(self, call_id, state, *, degraded, effective_stride_s, reason=None):
        """Publish a state without a scored window (`no_speech`, `unmonitored`)."""
        entry = self._entry(call_id)
        if entry is None:
            return []
        with entry.lock:
            return entry.publisher.status(state, degraded=degraded, effective_stride_s=effective_stride_s,
                                          reason=reason)

    def end(self, call_id, reason, *, speech_seconds):
        """End one call; returns the published `call.ended` envelopes ([] if unknown or
        if publishing failed). Invalid `reason`/`speech_seconds` raise ValueError and
        leave the call running."""
        entry = self._entry(call_id)
        if entry is None:
            return []
        with entry.lock:
            entry.scorer.validate_end(reason, speech_seconds=speech_seconds)
            return self._finish(call_id, entry, reason, speech_seconds)[1]

    def _finish(self, call_id, entry, reason, speech_seconds, **extra):
        """With `entry.lock` held and the end validated: remove, end, publish, release.
        Returns (removed, envelopes); the slot is released even if ending fails."""
        with self._lock:
            if self._calls.get(call_id) is not entry:
                return False, []  # a concurrent end won
            del self._calls[call_id]
            self.counters['ended'] += 1
        try:
            events = entry.scorer.end(reason, speech_seconds=speech_seconds)
            return True, entry.publisher.ended(events, **extra)
        finally:
            self._gate.release(call_id)
            failures = sum(entry.publisher.failures.values())
            if failures:
                with self._lock:
                    self.counters['publish_failures'] += failures

    def end_all(self, reason, *, speech_seconds_for):
        """End every open call (e.g. `monitor_lost`, architecture §2.4: every open
        session emits `call.ended`). `speech_seconds_for(call_id, directions)` supplies
        per-direction speech seconds; if it fails or gives invalid values, the call
        still ends, with zero speech seconds and `speech_seconds_known: false`
        (counted in `speech_seconds_missing`). Returns the number of calls ended."""
        if reason not in END_REASONS:
            raise ValueError(f'reason must be one of {END_REASONS}')
        with self._lock:
            entries = list(self._calls.items())
        ended = 0
        for call_id, entry in entries:
            try:
                with entry.lock:  # directions read and end in one critical section
                    directions = entry.scorer.scored_directions()
                    extra = {}
                    try:
                        speech = speech_seconds_for(call_id, directions)
                        entry.scorer.validate_end(reason, speech_seconds=speech)
                    except SessionClosed:
                        continue
                    except Exception:
                        speech, extra = dict.fromkeys(directions, 0.0), {'speech_seconds_known': False}
                        self._count('speech_seconds_missing')
                    removed, _ = self._finish(call_id, entry, reason, speech, **extra)
                    ended += removed
            except Exception:  # one bad call must not keep the others open
                self._count('end_all_failures')
        return ended

    def snapshot(self):
        with self._lock:
            entries = dict(self._calls)
            counters = dict(self.counters)
        calls = {}
        for call_id, entry in entries.items():
            with entry.lock:
                calls[call_id] = entry.scorer.snapshot()
                calls[call_id]['publish_failures'] = dict(entry.publisher.failures)
        return {'calls': calls, 'counters': counters, 'gate_active': self._gate.active}
