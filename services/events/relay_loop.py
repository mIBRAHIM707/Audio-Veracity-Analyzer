"""The API process's Redis relay thread (backlog 5.2; PRD FR-A4; architecture §2.5
"WebSocket /v1/events fans out Redis pub/sub events").

`RelayLoop(relay)` runs `RedisRelay.pump(timeout=wait_s)` on one owned daemon thread,
never the API event loop:

- while subscribed the pump itself waits up to `wait_s` for a message, so events reach
  the local hub with at most a short idle pause after an empty turn;
- while Redis is unreachable it waits between attempts with doubling backoff
  (`backoff_s` first, at most `max_backoff_s`), reset after a successful subscription;
  a stop request wakes every wait;
- a fault raised by `pump()` is counted (`faults`, `last_fault` = the type name only)
  and the loop goes on (`RedisRelay.pump` normally never raises);
- `stop(join_timeout_s)` (default wait + 2 x client timeout + 1 s = 5.5 s) ends the loop and then closes the relay's subscription from the
  same side the thread used it, only once the thread has ended. A thread stuck in a
  client call is reported `stuck` and left behind (daemon), so it can never keep the
  process alive; the relay is then not closed under it.

The relay reports its loss (sequence gaps, source restarts, outages) to the hub, so
WebSocket clients get `stream.gap`; this loop adds nothing to that.

`open_relay_client(url)` builds the redis-py client the relay needs: bounded socket and
connect timeouts, TCP keepalive, a pub/sub health check and no client-side retries.
"""
import threading

DEFAULT_WAIT_S = 0.5
DEFAULT_BACKOFF_S = 0.5
DEFAULT_MAX_BACKOFF_S = 10.0
IDLE_S = 0.02  # after an empty turn, so a pump that returns at once cannot spin
CLIENT_TIMEOUT_S = 2.0  # above DEFAULT_WAIT_S: a pub/sub read must not time out while waiting
HEALTH_CHECK_S = 10


def _seconds(name, value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low < value <= high:
        raise ValueError(f'{name} must be a number above {low:g} and at most {high:g}')
    return float(value)


class RelayLoop:
    def __init__(self, relay, *, wait_s=DEFAULT_WAIT_S, backoff_s=DEFAULT_BACKOFF_S,
                 max_backoff_s=DEFAULT_MAX_BACKOFF_S):
        for attribute in ('pump', 'close'):
            if not callable(getattr(relay, attribute, None)):
                raise ValueError(f'relay must have a callable {attribute}()')
        self.relay = relay
        self.wait_s = _seconds('wait_s', wait_s, 0, 5)
        self.backoff_s = _seconds('backoff_s', backoff_s, 0, 60)
        self.max_backoff_s = _seconds('max_backoff_s', max_backoff_s, 0, 300)
        if self.max_backoff_s < self.backoff_s:
            raise ValueError('max_backoff_s must be at least backoff_s')
        self._stop = threading.Event()
        self._thread = None
        self.stats = {'turns': 0, 'relayed': 0, 'faults': 0, 'last_fault': None, 'backoff_waits': 0,
                      'stuck': False, 'closed': False}

    @property
    def alive(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self._thread is not None:
            raise RuntimeError('relay loop already started (one run per object)')
        self._thread = threading.Thread(target=self._run, name='ava-redis-relay', daemon=True)
        self._thread.start()

    def _run(self):
        backoff = self.backoff_s
        while not self._stop.is_set():
            self.stats['turns'] += 1
            try:
                moved = self.relay.pump(timeout=self.wait_s)
            except Exception as error:  # advisory: count and carry on
                self.stats['faults'] += 1
                self.stats['last_fault'] = type(error).__name__
                moved = 0
            moved = moved if type(moved) is int and moved > 0 else 0
            self.stats['relayed'] += moved
            if getattr(self.relay, 'subscribed', True):
                backoff = self.backoff_s
                if not moved:
                    self._stop.wait(IDLE_S)
            else:  # Redis unreachable or the subscription failed: wait, then retry
                self.stats['backoff_waits'] += 1
                self._stop.wait(backoff)
                backoff = min(backoff * 2, self.max_backoff_s)

    def stop(self, join_timeout_s=None):
        """End the loop; returns True when the thread has ended (and the relay is
        closed). Waits at most `join_timeout_s`; the default covers one pump wait plus a
        reconnect attempt (connect and subscribe, each up to the client timeout) and 1 s."""
        if join_timeout_s is None:
            timeout = self.wait_s + 2 * CLIENT_TIMEOUT_S + 1.0
        elif isinstance(join_timeout_s, bool) or not isinstance(join_timeout_s, (int, float)) \
                or not 0 <= join_timeout_s <= 300:
            raise ValueError('join_timeout_s must be from 0 to 300 seconds')
        else:
            timeout = float(join_timeout_s)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                self.stats['stuck'] = True
                return False
        if not self.stats['closed']:
            self.relay.close()
            self.stats['closed'] = True
        return True

    def snapshot(self):
        return {'alive': self.alive, 'subscribed': bool(getattr(self.relay, 'subscribed', False)), **self.stats}


def open_relay_client(url, *, timeout_s=CLIENT_TIMEOUT_S):
    """redis-py client for `RedisRelay(lambda: client, hub)`. Raises ImportError without
    `requirements-redis.txt`, ValueError for a bad URL. The caller owns and closes it."""
    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    return redis.Redis.from_url(url, socket_timeout=timeout_s, socket_connect_timeout=timeout_s,
                                socket_keepalive=True, health_check_interval=HEALTH_CHECK_S,
                                retry=Retry(NoBackoff(), 0))
