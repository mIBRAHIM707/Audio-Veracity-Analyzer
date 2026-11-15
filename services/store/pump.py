"""Hub -> PostgreSQL pump (backlog 5.1/5.2 wiring; PRD FR-I11 "scores and state are
flushed to PostgreSQL every hop", FR-A3 history; architecture §2.4, §2.7).

`StorePump(subscription, store).pump()` drains an unfiltered `EventHub` subscription in
order and records each envelope with `PostgresEventStore.record`:

- stored / duplicate / after_end / not_stored / unknown_alert: done (counted by outcome);
- `StoreError` with `retryable=True` (database down, connection lost): the envelope and
  every later one wait, in order, and the pump stops for this turn; the next pump
  retries from the oldest. Order is kept because the store's after-end and replay rules
  depend on it. The pump stores what waits BEFORE taking more from the hub and takes at
  most the free backlog room (`max_backlog`), so it never drops anything itself; during
  a long outage new events stay in the hub subscription's buffer, which drops its
  oldest when full (any type) and is counted as `hub_dropped`. A lost `call.ended`,
  `call.started` or alert event leaves wrong database state (an open call, an implicit
  start, an unknown alert), not just fewer rows: subscribe with a large buffer
  (`fanout.MAX_BUFFER`) and pump often;
- `StoreError` with `retryable=False` (an envelope the database will never accept):
  counted and skipped, so one bad envelope cannot block the rest;
- `ValueError` from a misconfigured connection factory: counted as `store_misconfigured`
  and the envelope kept (it is not the envelope's fault);
- any other exception: counted as `faults`; the same head envelope is retried at most
  MAX_HEAD_FAULTS times, then skipped (`skipped_faulty`) so a deterministic fault cannot
  stall storage. `consecutive_failures` > 0 is the stall signal for health monitoring.

The store's `connect()` should set a connect timeout: the pump is only as non-blocking
as the database client.

Advisory: the pump never raises and never blocks the scorer; it runs on its own thread
(single owner; the store connection is not shared).
"""
from collections import deque

from .events_pg import OUTCOMES, StoreError

DEFAULT_BACKLOG = 10_000
MAX_HEAD_FAULTS = 5


class StorePump:
    def __init__(self, subscription, store, *, max_backlog=DEFAULT_BACKLOG, batch=512):
        if getattr(subscription, 'call_id', None) is not None:
            raise ValueError('pump an unfiltered subscription: every call must be stored')
        if type(max_backlog) is not int or max_backlog < 1 or type(batch) is not int or batch < 1:
            raise ValueError('max_backlog and batch must be positive integers')
        self._subscription, self._store = subscription, store
        self._backlog = deque()
        self.max_backlog, self.batch = max_backlog, batch
        self.consecutive_failures = 0  # health signal: > 0 means storage is stalled
        self._head_faults = 0
        self.counts = {**{outcome: 0 for outcome in OUTCOMES}, 'refused': 0, 'retry_waits': 0, 'faults': 0,
                       'skipped_faulty': 0, 'hub_dropped': 0, 'store_misconfigured': 0}

    @property
    def backlog(self):
        return len(self._backlog)

    def pump(self):
        """Store what is waiting first, then take from the hub only what fits in the
        backlog (the hub's buffer holds the rest) and store again. Returns the number of
        envelopes completed (stored, duplicate, after_end, ..., or refused) this turn."""
        done = self._flush()
        if self._backlog and self.consecutive_failures:
            return done  # the database is unavailable: leave new events in the hub buffer
        room = min(self.batch, self.max_backlog - len(self._backlog))
        if room > 0:
            envelopes, dropped = self._subscription.drain(room)
            self.counts['hub_dropped'] += dropped
            self._backlog.extend(envelopes)
        return done + self._flush()

    def _flush(self):
        done = 0
        while self._backlog:
            envelope = self._backlog[0]
            try:
                outcome = self._store.record(envelope)
            except StoreError as error:
                if error.retryable:
                    self.counts['retry_waits'] += 1
                    self.consecutive_failures += 1
                    break  # keep order: retry this one first next time
                self.counts['refused'] += 1
            except ValueError:
                self.counts['store_misconfigured'] += 1
                self.consecutive_failures += 1
                break  # not this envelope's fault: wait for a fixed connection factory
            except Exception:  # unexpected: never raise; a head that keeps failing is skipped
                self.counts['faults'] += 1
                self.consecutive_failures += 1
                self._head_faults += 1
                if self._head_faults < MAX_HEAD_FAULTS:
                    break
                self.counts['skipped_faulty'] += 1
            else:
                self.counts[outcome] = self.counts.get(outcome, 0) + 1
            self._backlog.popleft()
            self._head_faults = 0
            self.consecutive_failures = 0
            done += 1
        return done
