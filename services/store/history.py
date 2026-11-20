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
- `PostgresCallHistory.alerts(state, since, until, min_confidence, limit, cursor)` ->
  PRD FR-A6 alert search (19 Nov, attempt 5): stored alerts of every session, newest
  raised first, filtered by state, raised-time range and minimum confidence, with an
  opaque keyset cursor (`next_cursor`). "Confidence" here is `peak_ema`, the highest
  uncalibrated detector EMA the alert reached (see `PEAK_EMA`), not a probability.

Read only: every query runs in a `READ ONLY, REPEATABLE READ` transaction (one
consistent snapshot per answer, and no write is possible through this object whatever
the role). Never returned: `start_data`/`end_data` (they hold whatever the scorer put
in `call.started`/`call.ended`; caller numbers or SIP headers would need a privacy
decision), except `start_data`'s `codec` (FR-U5, 20 Nov) as `codec`, checked against a
format-name pattern (None otherwise), and nothing about audio (the store has none). A non-finite number (not
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
import base64
import binascii
from datetime import datetime, timedelta, timezone
import math
import re
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

_SESSION_NAMES = ('id, call_id, started_at, implicit_start, state, driving_direction, quality, degraded, '
                  'effective_stride_s, last_event_at, ended_at, final_state, end_reason, end_inferred, '
                  'speech_seconds, verdict')
# A JSON string only (`->>` would turn a stored true or 5 into text).
_CODEC_COLUMN = "CASE WHEN jsonb_typeof({p}start_data->'codec') = 'string' THEN {p}start_data->>'codec' END"
SESSION_COLUMNS = f"{_SESSION_NAMES}, {_CODEC_COLUMN.format(p='')}"
SESSION_WIDTH = 17
# FR-U5 "codec detected" (20 Nov): the one `start_data` field served, and only when it is
# an Asterisk format name as the tap records it (services/tap/tap_app.py CODEC_PATTERN);
# anything else (absent, null, not a name) is served as None ("not detected").
_CODEC = re.compile(r'[a-z][a-z0-9_]{0,23}(\|[a-z][a-z0-9_]{0,23}){0,3}')
ALERT_COLUMNS = ('alert_id, direction, status, raised_at, raised_window, raised_ema, closed_at, cleared_window, '
                 'cleared_ema')
# FR-A6 alert search (19 Nov, attempt 5).
ALERT_STATES = ('all', 'open', 'cleared', 'ended_open')
DEFAULT_ALERT_LIMIT, MAX_ALERT_LIMIT = 100, 10_000
MAX_TS = 32_503_680_000.0  # 3000-01-01 UTC: far beyond any stored row, inside datetime's range
MAX_CURSOR = 1_024
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_CURSOR = re.compile(r'(-?[0-9]{1,18}):(.+)', re.DOTALL)
_BASE64URL = re.compile(r'[A-Za-z0-9_-]+')
# FR-A6 "confidence" (review M, 19 Nov): the highest EMA the alert reached, i.e. the
# larger of its EMA at the raise (only if it is a number from 0 to 1: that column has no
# range check) and the highest EMA of the scored windows of its direction (any direction
# if it has none) from the raise to its close, or to the session's last event while it
# is open. The EMA at the raise alone is at or above the alert threshold, so it barely
# separates alerts. Uncalibrated, not a probability; NULL if there is neither. While the
# alert is open its peak can still rise (provisional). Computed per returned row.
PEAK_EMA = ('GREATEST(CASE WHEN a.raised_ema BETWEEN 0 AND 1 THEN a.raised_ema END, '
            '(SELECT max(w.ema) FROM window_scores w WHERE w.call_pk = a.call_pk AND w.scored '
            'AND (a.direction IS NULL OR w.direction = a.direction) AND w.scored_at >= a.raised_at '
            'AND w.scored_at <= COALESCE(a.closed_at, c.last_event_at)))')
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
     final_state, end_reason, end_inferred, speech_seconds, verdict, codec) = row
    return {'session': pk, 'call_id': call_id, 'status': 'active' if ended_at is None else 'ended',
            'started_ts': _ts(started_at), 'started_seen': not implicit, 'state': state,
            'driving_direction': driving, 'quality': quality, 'degraded': degraded,
            'effective_stride_s': _num(stride), 'last_event_ts': _ts(last_event_at), 'ended_ts': _ts(ended_at),
            'final_state': final_state, 'end_reason': end_reason, 'end_inferred': end_inferred,
            'speech_seconds': _num(speech_seconds), 'verdict': verdict,
            'codec': codec if isinstance(codec, str) and _CODEC.fullmatch(codec) else None}


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


def encode_cursor(raised_at, alert_id):
    """An opaque, URL- and header-safe position after which the next alert page starts:
    the exact raised time (integer microseconds, as PostgreSQL stores it) and the alert ID."""
    micros = (raised_at - EPOCH) // timedelta(microseconds=1)
    return base64.urlsafe_b64encode(f'{micros}:{alert_id}'.encode()).decode('ascii').rstrip('=')


def decode_cursor(cursor):
    """(raised_at, alert_id) from `encode_cursor`; ValueError for anything else."""
    if not isinstance(cursor, str) or not 0 < len(cursor) <= MAX_CURSOR or not _BASE64URL.fullmatch(cursor):
        raise ValueError('cursor is not valid')
    try:
        text = base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)).decode()
        micros, alert_id = _CURSOR.fullmatch(text).groups()
        raised_at = EPOCH + timedelta(microseconds=int(micros))
    except (binascii.Error, UnicodeDecodeError, AttributeError, OverflowError, ValueError):
        raise ValueError('cursor is not valid') from None
    if not valid_call_id(alert_id):  # alert IDs follow the same 1-128 characters rule
        raise ValueError('cursor is not valid')
    return raised_at, alert_id


def _epoch(name, value):
    """Unix seconds -> aware UTC datetime for a query parameter."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not 0 <= value <= MAX_TS:
        raise ValueError(f'{name} must be Unix seconds from 0 to {MAX_TS:.0f}')
    return EPOCH + timedelta(microseconds=round(value * 1_000_000))


def check_alert_filters(state='all', since=None, until=None, min_confidence=None, cursor=None):
    """Validate FR-A6 alert filters without reading: (since, until, cursor position) as
    query parameters, or ValueError with a message safe to show a client."""
    if state not in ALERT_STATES:
        raise ValueError(f'state must be one of {", ".join(ALERT_STATES)}')
    start, end = _epoch('since', since), _epoch('until', until)
    if start is not None and end is not None and start >= end:
        raise ValueError('since must be earlier than until')
    if min_confidence is not None and (isinstance(min_confidence, bool)
                                       or not isinstance(min_confidence, (int, float))
                                       or not 0 <= min_confidence <= 1):  # NaN fails both comparisons
        raise ValueError('min_confidence must be from 0 to 1')
    return start, end, None if cursor is None else decode_cursor(cursor)


def _alert_row(row):
    *alert, call_id, call_pk, peak_ema = row
    return {**_alert(alert), 'peak_ema': _num(peak_ema), 'call_id': call_id, 'session': call_pk}


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

    def alerts(self, state='all', since=None, until=None, min_confidence=None, limit=DEFAULT_ALERT_LIMIT,
               cursor=None):
        """FR-A6 alert search: stored alerts newest first by raised time (ties by alert ID),
        each with its call ID and session. Filters: `state` (`ALERT_STATES`), raised time
        `since <= t < until` (Unix seconds), `min_confidence` (0-1, compared with
        `peak_ema`, see `PEAK_EMA`; an alert with no EMA at all never matches it).
        `cursor` is the previous page's `next_cursor`; the same filters must be passed."""
        start, end, after = check_alert_filters(state, since, until, min_confidence, cursor)
        _bounded('limit', limit, 1, MAX_ALERT_LIMIT)
        return self._read(lambda db: self._alerts(db, state, start, end, min_confidence, limit, after))

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
        columns = ', '.join(f'c.{name.strip()}' for name in _SESSION_NAMES.split(','))
        columns += ', ' + _CODEC_COLUMN.format(p='c.')
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
            summary = _session(row[:SESSION_WIDTH])
            alerts_total, alerts_open, windows_total, windows_scored, score_max = row[SESSION_WIDTH:]
            summary.update(alerts_total=alerts_total, alerts_open=alerts_open, windows_total=windows_total,
                           windows_scored=windows_scored, score_max=_num(score_max))
            summaries.append(summary)
        more = len(rows) > limit
        return {'calls': summaries, 'next_before': summaries[-1]['session'] if more else None}

    @staticmethod
    def _alerts(db, state, start, end, min_confidence, limit, after):
        where, params = [], []
        if state != 'all':
            where.append('a.status = %s')
            params.append(state)
        if start is not None:
            where.append('a.raised_at >= %s')
            params.append(start)
        if end is not None:
            where.append('a.raised_at < %s')
            params.append(end)
        if after is not None:
            where.append('(a.raised_at, a.alert_id) < (%s, %s)')
            params.extend(after)
        clause = f'WHERE {" AND ".join(where)} ' if where else ''
        columns = ', '.join(f'a.{name.strip()}' for name in ALERT_COLUMNS.split(','))
        query = (f'SELECT {columns}, c.call_id, a.call_pk, {PEAK_EMA} AS peak_ema '
                 f'FROM alerts a JOIN calls c ON c.id = a.call_pk {clause}')
        if min_confidence is not None:  # NULL never matches; the peak is computed only for candidates
            query = f'SELECT * FROM ({query}) s WHERE s.peak_ema >= %s '
            params.append(float(min_confidence))
            order = 'ORDER BY s.raised_at DESC, s.alert_id DESC'
        else:
            order = 'ORDER BY a.raised_at DESC, a.alert_id DESC'
        rows = db.execute(f'{query}{order} LIMIT %s', (*params, limit + 1)).fetchall()
        page = rows[:limit]
        more = len(rows) > limit
        # The cursor is taken from the stored datetime (exact), not the float served.
        return {'alerts': [_alert_row(row) for row in page],
                'next_cursor': encode_cursor(page[-1][3], page[-1][0]) if more else None}
