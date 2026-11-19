"""Read-only call history from PostgreSQL (backlog 5.1/5.2; PRD FR-A3 "list active and
historical calls with state, score trend and alerts ... with full history in
PostgreSQL"; architecture §2.5, §2.7).

The in-memory registry (`services/calls/registry.py`) keeps 30 minutes of a call at
full resolution and forgets ended calls after 128 newer ones or a restart. This module
reads what `PostgresEventStore` wrote (`calls`, `window_scores`, `alerts`), so the API
can answer for any stored call:

- `PostgresCallHistory.call(call_id)` -> every stored session of that call ID (newest
  first; a restarted or killed scorer leaves one row per session, see
  `services/store/README.md`), each with its alerts and its per-window scores in time
  order, or None when nothing is stored.
- `PostgresCallHistory.recent(status, limit, before)` -> session summaries, newest
  first, with a keyset cursor (`next_before`) for the next page.

Read only: every query runs in a `READ ONLY, REPEATABLE READ` transaction (one
consistent snapshot per answer, and no write is possible through this object whatever
the role). Never returned: `start_data`/`end_data` (they hold whatever the scorer put
in `call.started`/`call.ended`; caller numbers or SIP headers would need a privacy
decision) and nothing about audio (the store has none). A non-finite number (not
expected: the writer refuses them, but some columns would accept one) is returned as
None so the answer stays valid JSON.

Bounds (review M1/M2, 19 Nov): at most `max_sessions` sessions per call (newest kept),
`max_alerts` alerts per session, and `max_windows` window rows per answer across all
its sessions (newest session first; within a session the oldest rows; totals and
`*_truncated` flags say what was left out). One connection serves one read at a time:
a read waits at most `wait_s` for it (then `busy`), and a read that passes
`deadline_s` stops before its next statement (then `timeout`); each statement also has
`statement_timeout_ms` (`SET LOCAL`), so one answer takes at most about
`deadline_s + statement_timeout_ms`, plus the connect time when it must reconnect (the
factory's `connect_timeout`, per host in the URL).

The schema must have migration 0002 (`end_inferred`); this module never migrates (the
live process does, and the API may run with a role that cannot). Failures raise
`HistoryError` with `reason`: `unavailable` (no connection, or a lost one: retry
later), `busy` (another read holds the connection), `timeout` (a statement or the read
ran too long), `schema` (tables or columns missing: the database is not migrated) or
`error` (anything else). Messages carry the driver's error class name only. The
connection is reopened after a failure; `connect` is injected and must return a
psycopg connection with no transaction open.
"""
import math
import threading
import time

REQUIRED_MIGRATION = 2  # 0002_inferred_end.sql adds end_inferred and start_event_at
# 10,800 window rows = 6 h of one direction, or 3 h of both, at a 2 s hop.
MAX_SESSIONS, MAX_ALERTS, MAX_WINDOWS = 16, 256, 10_800
DEFAULT_LIMIT, MAX_LIMIT = 50, 200
WAIT_S, DEADLINE_S, STATEMENT_TIMEOUT_MS = 2.0, 15.0, 10_000
CLOSE_WAIT_S = 2.0
MAX_CALL_ID = 128  # as the calls.call_id CHECK
STATUSES = ('all', 'active', 'ended')
REASONS = ('unavailable', 'busy', 'timeout', 'schema', 'error')
TRANSIENT = ('OperationalError', 'InterfaceError')
SCHEMA_ERRORS = ('UndefinedTable', 'UndefinedColumn', 'InvalidSchemaName')
TIMEOUT_ERRORS = ('QueryCanceled', 'LockNotAvailable')  # statement_timeout, lock_timeout

SESSION_COLUMNS = ('id, call_id, started_at, implicit_start, state, driving_direction, quality, degraded, '
                   'effective_stride_s, last_event_at, ended_at, final_state, end_reason, end_inferred, '
                   'speech_seconds, verdict')
ALERT_COLUMNS = ('alert_id, direction, status, raised_at, raised_window, raised_ema, closed_at, cleared_window, '
                 'cleared_ema')
WINDOW_COLUMNS = ('direction, window_idx, scored_at, seq, scored, score, ema, provisional, direction_state, '
                  'call_state, quality, degraded, effective_stride_s, concealed_fraction')


class HistoryError(RuntimeError):
    """The history could not be read. `reason` is one of `REASONS`."""

    def __init__(self, message, *, reason):
        super().__init__(message)
        self.reason = reason


def _classify(error):
    names = {cls.__name__ for cls in type(error).__mro__}
    if names & set(SCHEMA_ERRORS):
        return 'schema'
    if names & set(TIMEOUT_ERRORS):  # checked before TRANSIENT: these are OperationalErrors too
        return 'timeout'
    if isinstance(error, OSError) or names & set(TRANSIENT):
        return 'unavailable'
    return 'error'


def _ts(value):
    return None if value is None else value.timestamp()


def _num(value):
    return None if isinstance(value, float) and not math.isfinite(value) else value


def valid_call_id(call_id):
    """A call ID the store could hold: 1-128 characters, no NUL (PostgreSQL text refuses it)."""
    return isinstance(call_id, str) and 0 < len(call_id) <= MAX_CALL_ID and '\x00' not in call_id


def _session(row):
    (pk, call_id, started_at, implicit, state, driving, quality, degraded, stride, last_event_at, ended_at,
     final_state, end_reason, end_inferred, speech_seconds, verdict) = row
    return {'session': pk, 'call_id': call_id, 'status': 'active' if ended_at is None else 'ended',
            'started_ts': _ts(started_at), 'started_seen': not implicit, 'state': state,
            'driving_direction': driving, 'quality': quality, 'degraded': degraded,
            'effective_stride_s': _num(stride), 'last_event_ts': _ts(last_event_at), 'ended_ts': _ts(ended_at),
            'final_state': final_state, 'end_reason': end_reason, 'end_inferred': end_inferred,
            'speech_seconds': _num(speech_seconds), 'verdict': verdict}


def _alert(row):
    alert_id, direction, status, raised_at, raised_window, raised_ema, closed_at, cleared_window, cleared_ema = row
    return {'alert_id': alert_id, 'direction': direction, 'state': status, 'raised_ts': _ts(raised_at),
            'raised_window': raised_window, 'raised_ema': _num(raised_ema), 'closed_ts': _ts(closed_at),
            'cleared_window': cleared_window, 'cleared_ema': _num(cleared_ema)}


def _window(row):
    (direction, window, scored_at, seq, scored, score, ema, provisional, direction_state, call_state, quality,
     degraded, stride, concealed) = row
    return {'direction': direction, 'window': window, 'ts': _ts(scored_at), 'seq': seq, 'scored': scored,
            'score': _num(score), 'ema': _num(ema), 'provisional': provisional, 'direction_state': direction_state,
            'state': call_state, 'quality': quality, 'degraded': degraded, 'effective_stride_s': _num(stride),
            'concealed_fraction': _num(concealed)}


def _bounded(name, value, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f'{name} must be an integer from {low} to {high}')
    return value


def _seconds(name, value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f'{name} must be from {low} to {high} seconds')
    return float(value)


class _Deadline:
    """The connection as one read sees it: refuses to start a statement after the deadline."""

    def __init__(self, connection, deadline, clock):
        self._connection, self._deadline, self._clock = connection, deadline, clock

    def execute(self, *args):
        if self._clock() > self._deadline:
            raise HistoryError('call history read ran past its deadline', reason='timeout')
        return self._connection.execute(*args)


class PostgresCallHistory:
    def __init__(self, connect, *, max_sessions=MAX_SESSIONS, max_alerts=MAX_ALERTS, max_windows=MAX_WINDOWS,
                 wait_s=WAIT_S, deadline_s=DEADLINE_S, statement_timeout_ms=STATEMENT_TIMEOUT_MS,
                 clock=time.monotonic):
        self._connect, self._connection = connect, None
        self._schema_checked = False
        self._closing = False
        self._lock = threading.Lock()
        self.max_sessions = _bounded('max_sessions', max_sessions, 1, 1_000)
        self.max_alerts = _bounded('max_alerts', max_alerts, 1, 100_000)
        self.max_windows = _bounded('max_windows', max_windows, 1, 1_000_000)
        self.wait_s = _seconds('wait_s', wait_s, 0, 60)
        self.deadline_s = _seconds('deadline_s', deadline_s, 0.001, 600)
        self.statement_timeout_ms = _bounded('statement_timeout_ms', statement_timeout_ms, 1, 600_000)
        self._clock = clock
        self.failures = 0

    def close(self):
        """Close the connection. A read in progress keeps it until it finishes (waited for
        at most `CLOSE_WAIT_S`), then closes it itself."""
        self._closing = True
        if self._lock.acquire(timeout=CLOSE_WAIT_S):
            try:
                self._drop()
            finally:
                self._lock.release()

    def _drop(self):
        connection, self._connection = self._connection, None
        self._schema_checked = False
        if connection is not None:
            try:
                connection.close()
            except Exception:  # closing a broken connection must not mask the original error
                pass

    # -- public reads ----------------------------------------------------------------

    def call(self, call_id):
        """Every stored session of `call_id`, newest first, or None if none is stored."""
        if not valid_call_id(call_id):
            return None
        return self._read(lambda db: self._call(db, call_id))

    def recent(self, status='all', limit=DEFAULT_LIMIT, before=None):
        """Session summaries newest first (by storage order). `before` is the `session`
        of the last summary already seen (the previous page's `next_before`)."""
        if status not in STATUSES:
            raise ValueError(f'status must be one of {STATUSES}')
        _bounded('limit', limit, 1, MAX_LIMIT)
        if before is not None:
            _bounded('before', before, 1, 2 ** 63 - 1)
        return self._read(lambda db: self._recent(db, status, limit, before))

    # -- one read-only snapshot per answer -------------------------------------------

    def _read(self, query):
        if not self._lock.acquire(timeout=self.wait_s):
            raise HistoryError('another call history read holds the connection', reason='busy')
        try:
            return self._read_locked(query)
        finally:
            if self._closing:
                self._drop()
            self._lock.release()

    def _read_locked(self, query):
        try:
            if self._closing:
                raise HistoryError('call history is closed', reason='unavailable')
            deadline = self._clock() + self.deadline_s
            if self._connection is None or self._connection.closed:
                self._connection = self._connect()
                self._schema_checked = False
            if getattr(self._connection.info.transaction_status, 'name', None) != 'IDLE':
                raise HistoryError('connect() returned a connection with a transaction open (or broken)',
                                   reason='error')
            with self._connection.transaction():
                # First statement of the transaction: one snapshot, and no writes possible.
                self._connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                self._connection.execute(f'SET LOCAL statement_timeout = {int(self.statement_timeout_ms)}')
                db = _Deadline(self._connection, deadline, self._clock)
                if not self._schema_checked:
                    self._check_schema(db)
                result = query(db)
            self._schema_checked = True
            return result
        except HistoryError:
            self.failures += 1
            self._drop()
            raise
        except Exception as error:  # driver errors, lost connections, a broken factory
            self.failures += 1
            self._drop()
            raise HistoryError(f'call history read failed: {type(error).__name__}',
                               reason=_classify(error)) from error

    @staticmethod
    def _check_schema(db):
        row = db.execute('SELECT max(version) FROM schema_migrations').fetchone()
        if row is None or row[0] is None or row[0] < REQUIRED_MIGRATION:
            raise HistoryError(f'the database needs migration {REQUIRED_MIGRATION:04d} or later', reason='schema')

    def _call(self, db, call_id):
        total = db.execute('SELECT count(*) FROM calls WHERE call_id = %s', (call_id,)).fetchone()[0]
        if not total:
            return None
        rows = db.execute(f'SELECT {SESSION_COLUMNS} FROM calls WHERE call_id = %s ORDER BY id DESC LIMIT %s',
                          (call_id, self.max_sessions)).fetchall()
        sessions, budget = [], self.max_windows  # the window budget is shared by the whole answer
        for row in rows:
            session = _session(row)
            pk = session['session']
            alerts_total = db.execute('SELECT count(*) FROM alerts WHERE call_pk = %s', (pk,)).fetchone()[0]
            alerts = db.execute(f'SELECT {ALERT_COLUMNS} FROM alerts WHERE call_pk = %s '
                                'ORDER BY raised_at, alert_id LIMIT %s', (pk, self.max_alerts)).fetchall()
            windows_total = db.execute('SELECT count(*) FROM window_scores WHERE call_pk = %s', (pk,)).fetchone()[0]
            windows = []
            if budget > 0 and windows_total:
                windows = db.execute(f'SELECT {WINDOW_COLUMNS} FROM window_scores WHERE call_pk = %s '
                                     'ORDER BY scored_at, direction, window_idx LIMIT %s', (pk, budget)).fetchall()
                budget -= len(windows)
            session.update(alerts=[_alert(a) for a in alerts], alerts_total=alerts_total,
                           alerts_truncated=alerts_total > len(alerts), windows=[_window(w) for w in windows],
                           windows_total=windows_total, windows_truncated=windows_total > len(windows))
            sessions.append(session)
        return {'call_id': call_id, 'sessions': sessions, 'sessions_total': total,
                'sessions_truncated': total > len(sessions)}

    @staticmethod
    def _recent(db, status, limit, before):
        where, params = [], []
        if status == 'active':
            where.append('c.ended_at IS NULL')
        elif status == 'ended':
            where.append('c.ended_at IS NOT NULL')
        if before is not None:
            where.append('c.id < %s')
            params.append(before)
        clause = f'WHERE {" AND ".join(where)} ' if where else ''
        columns = ', '.join(f'c.{name.strip()}' for name in SESSION_COLUMNS.split(','))
        # One row more than asked tells whether another page exists. The counts are
        # computed per page (not stored): their cost on long calls is not measured yet.
        rows = db.execute(
            f'SELECT {columns}, '
            '(SELECT count(*) FROM alerts a WHERE a.call_pk = c.id), '
            "(SELECT count(*) FROM alerts a WHERE a.call_pk = c.id AND a.status = 'open'), "
            '(SELECT count(*) FROM window_scores w WHERE w.call_pk = c.id), '
            '(SELECT count(*) FROM window_scores w WHERE w.call_pk = c.id AND w.scored), '
            '(SELECT max(w.score) FROM window_scores w WHERE w.call_pk = c.id) '
            f'FROM calls c {clause}ORDER BY c.id DESC LIMIT %s', (*params, limit + 1)).fetchall()
        summaries = []
        for row in rows[:limit]:
            summary = _session(row[:16])
            alerts_total, alerts_open, windows_total, windows_scored, score_max = row[16:]
            summary.update(alerts_total=alerts_total, alerts_open=alerts_open, windows_total=windows_total,
                           windows_scored=windows_scored, score_max=_num(score_max))
            summaries.append(summary)
        more = len(rows) > limit
        return {'calls': summaries, 'next_before': summaries[-1]['session'] if more else None}
