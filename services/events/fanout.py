"""Bounded, non-blocking in-process event fan-out (backlog 5.1 partial; PRD FR-A4,
FR-I10 "rather than queue unboundedly"; architecture §2.5 optional call-ID filter).

`EventHub.publish` validates an FR-A4 envelope, gives it the next sequence number and
copies it to every matching subscriber's bounded buffer. It never blocks and never
waits for a consumer: a full buffer drops its oldest envelope and counts it, so a slow
dashboard client cannot slow scoring (AVA is advisory and must not hold up a call).
`drain` reports how many envelopes were dropped since the last drain; a subscriber
that lost envelopes should re-read state over REST. `seq` is hub-wide, so a
call-filtered subscriber normally sees gaps; order by `seq`, not `ts` (wall clock).
`system.health` without a call ID reaches every subscriber; a call-scoped one reaches
only that call's and unfiltered subscribers. The subscriber count is bounded; extra
subscriptions are refused. `seq` restarts at 1 with a new hub (no restart marker
yet). Redis pub/sub and the WebSocket endpoint are backlog 5.2.
"""
from collections import deque
import dataclasses
import threading
import time

from .contract import _finite_real, make_envelope

DEFAULT_BUFFER = 256
MAX_BUFFER = 4096
DEFAULT_MAX_SUBSCRIBERS = 64


class HubFull(RuntimeError):
    """The subscriber limit is reached."""


class EventHubError(RuntimeError):
    """The hub itself failed (e.g. its clock); the event was not delivered."""


class Subscription:
    def __init__(self, hub, call_id, maxlen):
        self._hub, self.call_id = hub, call_id
        self._buffer = deque(maxlen=maxlen)
        self.dropped_total = 0
        self._dropped_unreported = 0
        self.closed = False

    def _offer(self, envelope):  # called with the hub lock held
        if len(self._buffer) == self._buffer.maxlen:
            self.dropped_total += 1  # deque(maxlen) discards the oldest on append
            self._dropped_unreported += 1
        self._buffer.append(envelope)

    def drain(self, limit=None):
        """Take up to `limit` envelopes (all if None), oldest first, and the number of
        envelopes dropped since the previous drain, read together under the lock.
        Never blocks. A nonzero drop count means state should be re-read over REST;
        gaps in `seq` alone are normal for a call-filtered subscriber."""
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError('limit must be a positive integer or None')
        with self._hub._lock:
            count = len(self._buffer) if limit is None else min(limit, len(self._buffer))
            dropped, self._dropped_unreported = self._dropped_unreported, 0
            return [self._buffer.popleft() for _ in range(count)], dropped

    def close(self):
        self._hub._remove(self)


class EventHub:
    def __init__(self, *, max_subscribers=DEFAULT_MAX_SUBSCRIBERS, clock=time.time):
        if type(max_subscribers) is not int or max_subscribers < 1:
            raise ValueError('max_subscribers must be a positive integer')
        self._lock = threading.Lock()
        self._subscribers = []
        self._max = max_subscribers
        self._clock = clock
        self._seq = 0

    def subscribe(self, *, call_id=None, buffer=DEFAULT_BUFFER):
        """New subscription; `call_id` filters to one call (plus system.health)."""
        if call_id is not None and (not isinstance(call_id, str) or not call_id):
            raise ValueError('call_id filter must be None or a nonempty string')
        if type(buffer) is not int or not 1 <= buffer <= MAX_BUFFER:
            raise ValueError(f'buffer must be an integer from 1 to {MAX_BUFFER}')
        with self._lock:
            if len(self._subscribers) >= self._max:
                raise HubFull(f'subscriber limit {self._max} reached')
            subscription = Subscription(self, call_id, buffer)
            self._subscribers.append(subscription)
            return subscription

    def _remove(self, subscription):
        with self._lock:
            if not subscription.closed:
                subscription.closed = True
                self._subscribers.remove(subscription)
                subscription._buffer.clear()

    @property
    def subscriber_count(self):
        with self._lock:
            return len(self._subscribers)

    def publish(self, event_type, call_id, data):
        """Validate, then deliver; returns the envelope. Raises only EventContractError
        (invalid event) or EventHubError (the hub's clock failed); in both cases nothing
        is delivered and no sequence number is used. Callers on the scoring path should
        catch both: publishing is advisory and must not stop scoring. Validation and JSON
        encoding run outside the hub lock."""
        draft = make_envelope(event_type, call_id, data, seq=0, ts=0)
        with self._lock:
            try:
                ts = self._clock()
            except Exception as error:  # any clock fault is the hub's, not the caller's
                raise EventHubError(f'event clock failed: {type(error).__name__}') from None
            if not _finite_real(ts) or ts < 0:
                raise EventHubError('event clock returned an unusable timestamp')
            envelope = dataclasses.replace(draft, seq=self._seq + 1, ts=float(ts))
            self._seq += 1
            for subscription in self._subscribers:
                if subscription.call_id is None or envelope.call_id in (None, subscription.call_id):
                    subscription._offer(envelope)
            return envelope
