"""Capacity refusal and live-first micro-batch scheduling (backlog 8.1 partial; PRD
FR-I9; architecture §2.2 capacity refusal, §2.3 micro-batching and priority, §7.3
`capacity.max_calls`, `capacity.batch_share`, `inference.max_batch_live/file`).

Pure logic with an explicit clock; no model, threads or I/O.

`CallGate` admits at most `max_calls` concurrent calls. Above that a new call is not
tapped and the caller gets a `system.health` payload with reason `capacity`; the call
itself continues (FR-I9). Thread-safe.

`MicroBatcher` collects windows for up to `collect_s` (100 ms) and hands out batches:
live windows first, at most `max_live` (4) per batch; batch windows at most
`max_file` (8) per batch. While any live call is active, batch windows are limited by
a token bucket that earns `batch_share x capacity_windows_per_s` windows per second
(25 % of capacity in time terms), holding at most `max_file` tokens and starting empty
when live calls become active; with no live call, batch is unlimited. Every window is
the same 4 s length, so a window count measures inference work; `capacity_windows_per_s`
must come from measurement (backlog 8.QA) at the file batch size used while live calls
are active (`max_file_while_live`, default 1), because per-window cost depends on batch
size. Interpretation choices: the bucket model, its cap (`max_file_while_live`), its
empty start and the small file batches while live. Capacity held back for live latency
may sit idle; a running file batch is not preempted (8.QA measures that delay). Config
mapping: `collect_s` is architecture §7.3 `batch_wait_ms` / 1000; `max_live`/`max_file`
are `max_batch_live`/`max_batch_file`; `capacity_windows_per_s`, the pending limits and
`max_file_while_live` have no §7.3 key yet (proposed). One owner (the inference loop);
not thread-safe.
"""
from collections import deque
import math
import numbers
import threading

DEFAULT_MAX_CALLS = 8
DEFAULT_BATCH_SHARE = 0.25
COLLECT_S = 0.100
MAX_BATCH_LIVE = 4
MAX_BATCH_FILE = 8


def _positive_int(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f'{name} must be a positive integer')
    return value


def _time(value, name='now'):
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ValueError(f'{name} must be a finite number of seconds')
    try:
        if not math.isfinite(value):
            raise ValueError(f'{name} must be a finite number of seconds')
    except OverflowError:
        raise ValueError(f'{name} must be a finite number of seconds') from None
    return float(value)


class LiveBacklogFull(BufferError):
    """Too many live windows pending: degrade the call's stride (FR-I10)."""


class FileBacklogFull(BufferError):
    """Too many file windows pending: hold the file producer."""


class CallGate:
    def __init__(self, max_calls=DEFAULT_MAX_CALLS):
        self.max_calls = _positive_int(max_calls, 'max_calls')
        self._lock = threading.Lock()
        self._active = set()
        self.refused = 0

    def admit(self, call_id):
        """Returns (True, None) when admitted (or already admitted), otherwise
        (False, system.health data). Never raises for capacity: the call continues.
        Call once per call at its start (architecture §2.2 StasisStart): calling again for
        a refused call counts another refusal and may admit it mid-call if a slot freed."""
        if not isinstance(call_id, str) or not call_id:
            raise ValueError('call_id must be a nonempty string')
        with self._lock:
            if call_id in self._active:
                return True, None
            if len(self._active) >= self.max_calls:
                self.refused += 1
                return False, {'reason': 'capacity', 'call_id': call_id,
                               'active_calls': len(self._active), 'max_calls': self.max_calls}
            self._active.add(call_id)
            return True, None

    def release(self, call_id):
        """Idempotent; returns whether the call was active."""
        with self._lock:
            if call_id in self._active:
                self._active.remove(call_id)
                return True
            return False

    @property
    def active(self):
        with self._lock:
            return len(self._active)


class MicroBatcher:
    def __init__(self, *, capacity_windows_per_s, collect_s=COLLECT_S, max_live=MAX_BATCH_LIVE,
                 max_file=MAX_BATCH_FILE, batch_share=DEFAULT_BATCH_SHARE, max_pending_live=256,
                 max_pending_file=1024, max_file_while_live=1):
        self.capacity = _time(capacity_windows_per_s, 'capacity_windows_per_s')
        if self.capacity <= 0:
            raise ValueError('capacity_windows_per_s must be > 0 (measured, backlog 8.QA)')
        self.collect_s = _time(collect_s, 'collect_s')
        if self.collect_s < 0:
            raise ValueError('collect_s must be >= 0')
        self.max_live = _positive_int(max_live, 'max_live')
        self.max_file = _positive_int(max_file, 'max_file')
        self.batch_share = _time(batch_share, 'batch_share')
        if not 0 <= self.batch_share <= 1:
            raise ValueError('batch_share must be from 0 to 1')
        self.max_pending_live = _positive_int(max_pending_live, 'max_pending_live')
        self.max_pending_file = _positive_int(max_pending_file, 'max_pending_file')
        self.max_file_while_live = _positive_int(max_file_while_live, 'max_file_while_live')
        self._live, self._file = deque(), deque()
        self._live_active = False
        self._tokens, self._refilled_at, self._last_now = 0.0, None, None

    @property
    def live_calls_active(self):
        """Read-only; change it with set_live_calls_active (wired from CallGate.active)."""
        return self._live_active

    def _check_clock(self, now):
        now = _time(now)
        if self._last_now is not None and now < self._last_now:
            raise ValueError('now went backwards; use a monotonic clock')
        return now

    def set_live_calls_active(self, active, *, now):
        """Tell the batcher whether any live call is active (from CallGate.active)."""
        if not isinstance(active, bool):
            raise ValueError('active must be a boolean')
        now = self._check_clock(now)
        self._last_now = now
        if active and not self._live_active:
            self._tokens, self._refilled_at = 0.0, now  # bucket starts empty
        self._live_active = active

    def submit(self, item, *, kind, now):
        """Queue a window ('live' or 'file') at time `now`. ValueError for bad input.
        A full live queue raises LiveBacklogFull (the scorer degrades the stride,
        FR-I10); a full file queue raises FileBacklogFull (hold the file producer; never
        a reason to degrade live). Each kind has its own limit, so file work can never
        crowd out live windows. Nothing is queued or recorded on error."""
        if kind not in ('live', 'file'):
            raise ValueError("kind must be 'live' or 'file'")
        now = self._check_clock(now)
        if kind == 'live' and len(self._live) >= self.max_pending_live:
            raise LiveBacklogFull(f'{self.max_pending_live} live windows already pending')
        if kind == 'file' and len(self._file) >= self.max_pending_file:
            raise FileBacklogFull(f'{self.max_pending_file} file windows already pending')
        self._last_now = now
        (self._live if kind == 'live' else self._file).append((now, item))

    def pending(self):
        return {'live': len(self._live), 'file': len(self._file)}

    def _waited(self, queue, now):
        """Oldest window has waited the collection interval. The 1e-9 s tolerance absorbs
        rounding such as 5.1 - 5.0 < 0.1 for clock values well below 1e7 s."""
        return now - queue[0][0] >= self.collect_s - 1e-9

    def next_batch(self, now):
        """Return ('live'|'file', [items]) when a batch is due at `now`, else None.
        Live is due when `max_live` windows wait or the oldest has waited `collect_s`;
        file likewise with `max_file`, only when no live window waits. While live calls
        are active a file batch holds at most `max_file_while_live` windows (default 1,
        so a live window arriving just after waits for one window, not eight) and only
        as many as whole tokens allow."""
        now = self._check_clock(now)
        self._last_now = now
        if self._live_active:
            self._tokens = min(float(self.max_file_while_live), self._tokens
                               + (now - self._refilled_at) * self.batch_share * self.capacity)
            self._refilled_at = now
        if self._live and (len(self._live) >= self.max_live or self._waited(self._live, now)):
            return 'live', [self._live.popleft()[1] for _ in range(min(self.max_live, len(self._live)))]
        if self._live or not self._file:
            return None
        if len(self._file) < self.max_file and not self._waited(self._file, now):
            return None
        count = min(self.max_file, len(self._file))
        if self._live_active:
            count = min(count, self.max_file_while_live, int(self._tokens + 1e-9))
            if count == 0:
                return None
            self._tokens = max(0.0, self._tokens - count)
        return 'file', [self._file.popleft()[1] for _ in range(count)]
