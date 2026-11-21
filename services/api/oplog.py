"""Structured JSON operations log of the analysis API (backlog 7.2; PRD FR-O6
"Structured JSON logs"; §6.3 data minimisation). The live service has its own
(`services/live/eventlog.py`); both write the same line shape.

`ApiLog(stream)` takes records from any thread (`emit`, never blocking: the event loop,
the job worker and the registry pump all call it) into a bounded queue; one writer
thread (`start`/`stop`) writes them, so a slow or blocked stderr never stalls a request.
Events and their only fields:

| event | level | fields | rate-limited |
|---|---|---|---|
| `request` | info / warning (4xx, 503) / error (other 5xx) | `route` (fixed label, IDs replaced), `method`, `status`, `duration_ms` | yes |
| `job_queued`, `job_done` | info | `job_id` | no |
| `job_failed` | warning | `job_id`, `status` | no |
| `detector_ready` | info | `model_id`, `warmup_seconds` | no |
| `relay_gap`, `relay_interruption`, `relay_source_restart`, `relay_connection_error` | warning | `count` (delta) | no |
| `log_dropped` | warning | `count` | no |

Never written: query strings, file names, upload bytes, verdicts, scores, call or caller
data, URLs or credentials. `request` lines skip the probe routes (`/healthz`, `/readyz`,
`/metrics`), which a container health check calls every few seconds. Rate-limited lines
go through a token bucket (`max_lines_per_s`, one-second burst); a full queue
(`max_queued`) drops any line, and so does an `emit` after `stop`. Drops are counted
and reported as one `log_dropped` line at most every 0.5 s and at stop, after the
lines it follows. Lines from one thread are in `ts` order; `ts` is read before the
queue lock, so lines from different threads may be out of order by a few microseconds. Each line: `ts` (UTC ISO 8601, ms, the
time it was emitted), `level`, `event`, then the fields; ASCII-escaped JSON. A failing
stream is counted (`write_failures`), never raised. `job_id` is a random UUID: anyone
who can read this log and reach the API can fetch that job while it is held (the API
has no authentication; loopback by default).
"""
from collections import deque
from datetime import datetime, timezone
import json
import threading
import time

DEFAULT_MAX_LINES_PER_S = 50
DEFAULT_MAX_QUEUED = 1000
WRITER_INTERVAL_S = 0.5  # longest wait of the writer; drop reports come at most this often
PROBE_ROUTES = frozenset({'/healthz', '/readyz', '/metrics'})
# RedisRelay.stats key -> event (a positive change is logged as `count`).
RELAY_DELTAS = (('gaps', 'relay_gap'), ('interruptions', 'relay_interruption'),
                ('restarts', 'relay_source_restart'), ('connection_errors', 'relay_connection_error'))
_SAFE_TEXT = 128


def _iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _count(value):
    return type(value) is int and value >= 0


def _safe(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if value == value and value not in (float('inf'), float('-inf')) else None
    if isinstance(value, str):
        return value[:_SAFE_TEXT]
    return None


class ApiLog:
    def __init__(self, stream, *, max_lines_per_s=DEFAULT_MAX_LINES_PER_S, max_queued=DEFAULT_MAX_QUEUED,
                 wall=time.time, clock=time.monotonic):
        if type(max_lines_per_s) is not int or not 1 <= max_lines_per_s <= 10_000:
            raise ValueError('max_lines_per_s must be an integer from 1 to 10000')
        if type(max_queued) is not int or not 1 <= max_queued <= 100_000:
            raise ValueError('max_queued must be an integer from 1 to 100000')
        self.stream, self.max_lines_per_s, self.max_queued = stream, max_lines_per_s, max_queued
        self._wall, self._clock = wall, clock
        self._lock = threading.Condition()
        self._drain = threading.Lock()  # one drainer at a time (review L4): line order and counts
        self._queue = deque()
        self._tokens, self._refilled = float(max_lines_per_s), None
        self._dropped = 0  # not yet reported in a log_dropped line
        self._reported_at = None  # clock of the last log_dropped line
        self._ready_logged = False
        self._relay_seen = None
        self._thread, self._stopping = None, False
        self.counts = {'lines': 0, 'dropped': 0, 'write_failures': 0}

    # -- producers (any thread) ----------------------------------------------------------
    def emit(self, event, level='info', *, limited=False, **fields):
        """Queue one line; never blocks, never raises for a full queue or rate limit."""
        record = {'ts': self._wall(), 'level': level, 'event': event,
                  **{name: _safe(value) for name, value in fields.items()}}
        with self._lock:
            if self._stopping:  # review L2: after stop nothing is written; counted, not lost silently
                self._drop()
                return False
            if limited and not self._take_token():
                self._drop()
                return False
            if len(self._queue) >= self.max_queued:
                self._drop()
                return False
            self._queue.append(record)
            self._lock.notify()
            return True

    def request(self, route, method, status, seconds):
        if route in PROBE_ROUTES:
            return False
        # Review L5: 503 is the API's deliberate load shedding or "no live source": warning.
        level = 'error' if status >= 500 and status != 503 else 'warning' if status >= 400 else 'info'
        return self.emit('request', level, limited=True, route=route, method=method, status=status,
                         duration_ms=round(seconds * 1000, 1) if seconds is not None else None)

    def detector_ready(self, model_id, warmup_seconds):
        """Logged once per process (the first successful warm-up)."""
        with self._lock:
            if self._ready_logged:
                return False
        written = self.emit('detector_ready', model_id=model_id, warmup_seconds=warmup_seconds)
        if written:  # review L7: a line lost to a full queue is tried again at the next warm-up
            with self._lock:
                if self._ready_logged:  # another thread logged it meanwhile: keep one
                    return False
                self._ready_logged = True
        return written

    def relay_stats(self, stats):
        """Log positive changes of the relay's loss counters since the previous call (the
        first call only sets the baseline). The relay's counters only grow, so the
        baseline keeps the highest value seen per counter: two callers (the registry pump
        and `/metrics`) reading the stats in one order and storing them in the other never
        log the same loss twice (re-review 18 Nov, low 1)."""
        current = {key: stats.get(key, 0) for key, _ in RELAY_DELTAS}
        deltas = []
        with self._lock:
            previous = self._relay_seen
            if previous is None:
                self._relay_seen = {key: value if _count(value) else 0 for key, value in current.items()}
                return 0
            for key, event in RELAY_DELTAS:
                now, before = current[key], previous[key]
                if _count(now) and now > before:
                    deltas.append((event, now - before))
                    previous[key] = now
        logged = 0
        for event, count in deltas:
            logged += self.emit(event, 'warning', count=count)
        return logged

    def _take_token(self):
        now = self._clock()
        if self._refilled is not None:
            self._tokens = min(float(self.max_lines_per_s), self._tokens + (now - self._refilled) * self.max_lines_per_s)
        self._refilled = now
        if self._tokens < 1:
            return False
        self._tokens -= 1
        return True

    def _drop(self):
        self._dropped += 1
        self.counts['dropped'] += 1

    # -- writer ----------------------------------------------------------------------------
    def write_pending(self):
        """Write every queued line (the writer thread's turn; also callable directly; one
        drainer at a time). Returns the number written. Dropped lines are reported as one
        `log_dropped` line after the queued ones (so lines stay in `ts` order), at most
        once per WRITER_INTERVAL_S (review M1): a flood adds at most two lines a second to
        the rate limit, and the report is never starved by it. At stop it is always
        written."""
        with self._drain:
            with self._lock:
                lines = list(self._queue)
                self._queue.clear()
                now = self._clock()
                due = self._reported_at is None or now - self._reported_at >= WRITER_INTERVAL_S
                if self._dropped and (self._stopping or due):
                    lines.append({'ts': self._wall(), 'level': 'warning', 'event': 'log_dropped',
                                  'count': self._dropped})
                    self._dropped, self._reported_at = 0, now
            written = failures = 0
            for record in lines:
                line = {**record, 'ts': _iso(record['ts'])}
                try:
                    self.stream.write(json.dumps(line, sort_keys=False, ensure_ascii=True) + '\n')
                    self.stream.flush()
                except Exception:  # a closed or broken stream: counted, never raised
                    failures += 1
                    continue
                written += 1
            with self._lock:
                self.counts['lines'] += written
                self.counts['write_failures'] += failures
            return written

    def start(self):
        if self._thread is not None:
            raise RuntimeError('the API log writer is already running')
        self._thread = threading.Thread(target=self._run, name='ava-api-log', daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            with self._lock:
                # Woken by queued lines or stop only (review M1): drops alone wait for the
                # timed pass, so they merge into one report.
                if not self._queue and not self._stopping:
                    self._lock.wait(WRITER_INTERVAL_S)
                stopping = self._stopping
            self.write_pending()
            if stopping:
                return

    def stop(self, timeout=2.0):
        """Write what is queued (including a final drop report) and end the writer; later
        `emit`s are counted as dropped. True when the writer ended in time; False (stderr
        blocked) means a line may still be written after the caller's own next line."""
        with self._lock:
            self._stopping = True
            self._lock.notify()
        if self._thread is None:
            self.write_pending()
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def report(self):
        with self._lock:
            return dict(self.counts)
