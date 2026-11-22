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
subscriptions are refused. `seq` restarts at 1 with a new hub, which has a new random
`epoch` (17 Nov). A relay feeding the hub from Redis reports events lost before they arrived with
`report_upstream_loss`; subscribers read it with `drain_batch` (16 Nov).
"""
from collections import deque
import dataclasses
import re
import secrets
import threading
import time
from typing import NamedTuple

from .contract import _finite_real, make_envelope

DEFAULT_BUFFER = 256
MAX_BUFFER = 4096
DEFAULT_MAX_SUBSCRIBERS = 64


class HubFull(RuntimeError):
    """The subscriber limit is reached."""


class EventHubError(RuntimeError):
    """The hub itself failed (e.g. its clock); the event was not delivered."""


class Drained(NamedTuple):
    """One `drain_batch` result. `dropped`: envelopes this subscription's own buffer
    discarded. `upstream_missing`: events a relay knows were lost before they reached
    this hub (counted from the source sequence). `upstream_interruptions`: losses of
    unknown size (the source connection was lost, or the source restarted)."""
    envelopes: list
    dropped: int
    upstream_missing: int
    upstream_interruptions: int


class Subscription:
    def __init__(self, hub, call_id, maxlen):
        self._hub, self.call_id = hub, call_id
        self._buffer = deque(maxlen=maxlen)
        self.dropped_total = 0
        self._dropped_unreported = 0
        self._upstream_missing = 0
        self._upstream_interruptions = 0
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
            return self._take(limit)

    def drain_batch(self, limit=None):
        """Like `drain`, and also takes the upstream loss reported by a relay since the
        previous `drain_batch` (`EventHub.report_upstream_loss`), all read together.
        Loss is reported at the start of the batch in which it became known: some
        envelopes in that batch may have been published before the loss. Any nonzero
        count means state should be re-read over REST. `drain` leaves upstream loss
        for `drain_batch`."""
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError('limit must be a positive integer or None')
        with self._hub._lock:
            envelopes, dropped = self._take(limit)
            missing, self._upstream_missing = self._upstream_missing, 0
            interruptions, self._upstream_interruptions = self._upstream_interruptions, 0
            return Drained(envelopes, dropped, missing, interruptions)

    def _take(self, limit):  # called with the hub lock held
        count = len(self._buffer) if limit is None else min(limit, len(self._buffer))
        dropped, self._dropped_unreported = self._dropped_unreported, 0
        return [self._buffer.popleft() for _ in range(count)], dropped

    @property
    def hub_epoch(self):
        """The epoch of the hub whose `seq` numbers this subscription's envelopes."""
        return self._hub.epoch

    def buffered(self):
        """Envelopes waiting in this subscription now (read under the hub lock)."""
        with self._hub._lock:
            return len(self._buffer)

    def close(self):
        self._hub._remove(self)


EPOCH_PATTERN = re.compile(r'[0-9a-f]{32}')  # use with fullmatch (review L3: `$` accepts a trailing newline)


class EventHub:
    def __init__(self, *, max_subscribers=DEFAULT_MAX_SUBSCRIBERS, clock=time.time, epoch=None):
        """`epoch` names this hub's `seq` numbering (32 lowercase hex characters, random
        by default, so every new hub, and so every process restart, gets a new one). The
        Redis forwarder sends it with each event so a relay tells a source restart from
        a gap exactly (17 Nov). Pass one only in tests: it must be unique per hub, as a
        reused epoch makes a restarted hub's events look out of order to a relay."""
        if type(max_subscribers) is not int or max_subscribers < 1:
            raise ValueError('max_subscribers must be a positive integer')
        if epoch is not None and (not isinstance(epoch, str) or not EPOCH_PATTERN.fullmatch(epoch)):
            raise ValueError('epoch must be 32 lowercase hexadecimal characters')
        self.epoch = epoch or secrets.token_hex(16)
        self._lock = threading.Lock()
        self._subscribers = []
        self._max = max_subscribers
        self._clock = clock
        self._seq = 0
        self.upstream_missing_total = 0
        self.upstream_interruptions_total = 0

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

    def report_upstream_loss(self, *, missing=0, interruptions=0):
        """A relay feeding this hub lost events before they arrived: `missing` counted
        ones, `interruptions` losses of unknown size. Every current subscriber is told
        (the lost events' call IDs are unknown) through `drain_batch`. Uses no `seq`."""
        for name, value in (('missing', missing), ('interruptions', interruptions)):
            if type(value) is not int or value < 0:
                raise ValueError(f'{name} must be a non-negative integer')
        if not missing and not interruptions:
            raise ValueError('report at least one missing event or interruption')
        with self._lock:
            self.upstream_missing_total += missing
            self.upstream_interruptions_total += interruptions
            for subscription in self._subscribers:
                subscription._upstream_missing += missing
                subscription._upstream_interruptions += interruptions

    def publish(self, event_type, call_id, data, *, ts=None):
        """Validate, then deliver; returns the envelope. Raises only EventContractError
        (invalid event) or EventHubError (the hub's clock failed); in both cases nothing
        is delivered and no sequence number is used. Callers on the scoring path should
        catch both: publishing is advisory and must not stop scoring. Validation and JSON
        encoding run outside the hub lock. `ts` (seconds since the epoch) keeps an
        event's original time when it is relayed from another process (Redis relay);
        the hub still assigns its own `seq`. Without it the hub's clock is used."""
        draft = make_envelope(event_type, call_id, data, seq=0, ts=0 if ts is None else ts)
        with self._lock:
            if ts is None:
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
