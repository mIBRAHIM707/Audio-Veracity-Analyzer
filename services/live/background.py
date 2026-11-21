"""Storage and fan-out pumps on their own threads (backlog 5.1/5.2/8.1 wiring; PRD
FR-I11 "scores and state are flushed to PostgreSQL every hop", FR-A4; architecture
§2.4: the scorer publishes to Redis and PostgreSQL; storage is advisory and must never
hold up scoring).

`PumpThread(name, pump, pending=)` calls `pump()` (a `StorePump.pump` or
`RedisForwarder.pump` on its own unfiltered hub subscription) in a loop on one owned
daemon thread:

- after a turn that moved something it pumps again at once (catch-up), otherwise it
  waits `interval_s`; a stop request wakes the wait;
- an exception from `pump()` is counted (`faults`, `last_fault` = the type name only)
  and the loop goes on: the pumps are advisory and normally never raise;
- `request_stop(drain_timeout_s)` asks the loop to finish: it keeps pumping until a
  turn moves nothing (so the final `call.ended` events published while the service
  stopped are handled) or the drain deadline passes. `drained` is then True only when
  `pending()` (if given: events still waiting, e.g. a store backlog during a database
  outage) is 0; without `pending` it means "the last turn moved nothing".
  `join(timeout)` waits for the thread; `stop()` does both. A pump stuck in a client
  call is reported `stuck: true` and left behind as a daemon thread, so it can never
  keep the process alive or delay the scorer. Request every pump's stop before joining
  any, so several pumps drain in parallel within one deadline.

The scoring thread never calls into a pump: it only publishes to the hub, whose
subscription buffers are bounded (`fanout.MAX_BUFFER`); past that the oldest events are
dropped and counted by the pump as `hub_dropped`.
"""
import threading
import time

DEFAULT_INTERVAL_S = 0.2
DEFAULT_DRAIN_S = 5.0
MAX_DRAIN_S = 300.0
JOIN_GRACE_S = 1.0
MAX_CATCH_UP_TURNS = 1000  # back-to-back turns before a wait, so a busy pump still yields


def check_drain(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= MAX_DRAIN_S:
        raise ValueError(f'drain timeout must be from 0 to {MAX_DRAIN_S:g} seconds')
    return float(value)


class PumpThread:
    def __init__(self, name, pump, *, pending=None, interval_s=DEFAULT_INTERVAL_S, clock=time.monotonic):
        if not isinstance(name, str) or not name:
            raise ValueError('name must be a nonempty string')
        if not callable(pump) or (pending is not None and not callable(pending)):
            raise ValueError('pump (and pending, if given) must be callable')
        if isinstance(interval_s, bool) or not isinstance(interval_s, (int, float)) or not 0 < interval_s <= 60:
            raise ValueError('interval_s must be a number above 0 and at most 60')
        self.name, self._pump, self._pending = name, pump, pending
        self.interval_s, self._clock = float(interval_s), clock
        self._stop = threading.Event()
        self._drain_deadline = None
        self._thread = None
        self.stats = {'turns': 0, 'moved': 0, 'faults': 0, 'last_fault': None, 'drain_turns': 0,
                      'drained': None, 'left_pending': None, 'stuck': False}

    @property
    def alive(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self._thread is not None:
            raise RuntimeError(f'{self.name} pump thread already started (one run per object)')
        self._thread = threading.Thread(target=self._run, name=f'ava-pump-{self.name}', daemon=True)
        self._thread.start()

    def _turn(self):
        self.stats['turns'] += 1
        try:
            moved = self._pump()
        except Exception as error:  # advisory: count and carry on
            self.stats['faults'] += 1
            self.stats['last_fault'] = type(error).__name__
            return 0
        moved = moved if type(moved) is int and moved > 0 else 0
        self.stats['moved'] += moved
        return moved

    def _left(self):
        if self._pending is None:
            return None
        try:
            left = self._pending()
        except Exception as error:
            self.stats['last_fault'] = type(error).__name__
            return None
        return left if type(left) is int and left >= 0 else None

    def _run(self):
        while not self._stop.is_set():
            turns = 0
            while self._turn() and turns < MAX_CATCH_UP_TURNS and not self._stop.is_set():
                turns += 1
            self._stop.wait(self.interval_s)
        # Final drain: what was published up to the stop (e.g. monitor_lost ends).
        quiet = False
        while self._clock() < self._drain_deadline:
            self.stats['drain_turns'] += 1
            if not self._turn():
                quiet = True
                break
        left = self._left()
        self.stats['left_pending'] = left
        # A quiet turn is not proof: a store in an outage moves nothing with a backlog.
        self.stats['drained'] = quiet and (left == 0 if self._pending is not None else True)

    def request_stop(self, drain_timeout_s=DEFAULT_DRAIN_S):
        """Ask the loop to drain for at most `drain_timeout_s` and end; does not wait."""
        drain = check_drain(drain_timeout_s)
        wanted = self._clock() + drain
        # A later, shorter request (a forced stop) cuts the drain; never lengthens it.
        self._drain_deadline = wanted if self._drain_deadline is None else min(self._drain_deadline, wanted)
        self._stop.set()

    def join(self, timeout):
        """Wait up to `timeout` seconds; returns True when the thread has ended."""
        if self._thread is None:
            return True
        self._thread.join(max(0.0, timeout))
        self.stats['stuck'] = self._thread.is_alive()
        return not self.stats['stuck']

    def stop(self, drain_timeout_s=DEFAULT_DRAIN_S, join_grace_s=JOIN_GRACE_S):
        """Stop after a bounded final drain; returns True when the thread has ended."""
        drain = check_drain(drain_timeout_s)
        if self._thread is None:
            return True
        self.request_stop(drain)
        return self.join(drain + join_grace_s)

    def snapshot(self):
        return {'name': self.name, 'alive': self.alive, **self.stats}
