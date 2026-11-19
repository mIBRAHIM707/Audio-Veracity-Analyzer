"""PostgreSQL accounts, login sessions and audit log (backlog 7.3; PRD FR-U7, §6.3;
architecture §2.5, §2.7 `users`, `audit_log`). Tables from migration 0003.

`PostgresAuthStore` does no password work (see `passwords.py` and `service.py`): it
stores and reads hashes, sessions and audit rows. Every method is one transaction on
one connection, held by one call at a time: a call waits at most `wait_s` for it
(then `busy`), and each statement has `statement_timeout_ms`. Session times use the
database clock (`now()`), so every API process agrees on expiry.

Sessions: the token is 32 random bytes (`secrets.token_urlsafe`), returned once to be
set as a cookie; the table keeps its SHA-256. A session is valid while it is not
revoked, before `expires_at` (absolute lifetime), within `idle_s` of its last use, and
its user is not disabled. `last_seen_at` is written at most every `TOUCH_S`, so
reading pages does not write on every request (the idle limit is then exact to
`TOUCH_S`). Every account change (disable, enable, password, role) revokes all the
user's open sessions in the same transaction. Sessions that ended over a day ago are
deleted in bounded batches at each login.

Audit rows are written in the same transaction as the change they record (a login and
its session, a password change and the revocation), so an audited action that is not
recorded does not happen. Failures raise `AuthStoreError` with `reason` as in
`services/store/history.py` (`unavailable`, `busy`, `timeout`, `schema`, `error`) or
`conflict` (the username exists) / `not_found` (no such user). Messages carry the
driver's error class name only. The schema is never migrated here.
"""
from dataclasses import dataclass
import hashlib
import json
import re
import secrets
import threading

from services.store.history import _classify

REQUIRED_MIGRATION = 3
ROLES = ('analyst', 'admin')
OUTCOMES = ('ok', 'refused', 'failed')
USERNAME = re.compile(r'[a-z][a-z0-9._-]{0,63}')
ACTOR = re.compile(r'anonymous|system:[a-z][a-z0-9_-]{0,31}|[a-z][a-z0-9._-]{0,63}')
ACTION = re.compile(r'[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){0,2}')
TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')  # secrets.token_urlsafe(32)
MAX_DETAIL_BYTES, DETAIL_MARGIN = 4096, 128
PURGE_AFTER_S, PURGE_BATCH = 86400, 100  # sessions past their absolute end by a day (review L4)
WAIT_S, STATEMENT_TIMEOUT_MS, CLOSE_WAIT_S = 2.0, 5_000, 2.0
IDLE_S, ABSOLUTE_S, TOUCH_S = 30 * 60, 12 * 3600, 60
REASONS = ('unavailable', 'busy', 'timeout', 'schema', 'error', 'conflict', 'not_found')


class AuthStoreError(RuntimeError):
    def __init__(self, message, *, reason):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class UserRecord:
    id: int
    username: str
    password_hash: str
    role: str
    disabled: bool


@dataclass(frozen=True)
class Principal:
    """The user a valid session belongs to. `expires_ts`: the session's absolute end."""
    user_id: int
    username: str
    role: str
    expires_ts: float


RESERVED_NAMES = frozenset({'anonymous', 'system'})  # audit actors, never accounts (review L3)


def valid_username(name):
    return isinstance(name, str) and USERNAME.fullmatch(name) is not None and name not in RESERVED_NAMES


def token_digest(token):
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def check_audit(actor, action, outcome, detail):
    """Validate an audit row before any database work; returns the detail as JSON text."""
    if not isinstance(actor, str) or not ACTOR.fullmatch(actor):
        raise ValueError('audit actor is not valid')
    if not isinstance(action, str) or not ACTION.fullmatch(action):
        raise ValueError('audit action is not valid')
    if outcome not in OUTCOMES:
        raise ValueError(f'audit outcome must be one of {OUTCOMES}')
    if not isinstance(detail, dict):
        raise ValueError('audit detail must be an object')
    text = json.dumps(detail, allow_nan=False, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    # The table's CHECK measures PostgreSQL's own text form (", " and ": " separators):
    # measured here the same way, with a margin for number formatting (review nit).
    spaced = json.dumps(detail, ensure_ascii=False)
    if len(spaced.encode('utf-8')) > MAX_DETAIL_BYTES - DETAIL_MARGIN or '\\u0000' in text:
        raise ValueError(f'audit detail must be at most {MAX_DETAIL_BYTES - DETAIL_MARGIN} bytes of JSON without NUL')
    return text


def _interval(name, value, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f'{name} must be an integer from {low} to {high}')
    return value


class PostgresAuthStore:
    def __init__(self, connect, *, idle_s=IDLE_S, absolute_s=ABSOLUTE_S, wait_s=WAIT_S,
                 statement_timeout_ms=STATEMENT_TIMEOUT_MS):
        self._connect, self._connection = connect, None
        self._lock = threading.Lock()
        self._closing = False
        self._schema_checked = False
        self.absolute_s = _interval('absolute_s', absolute_s, 60, 7 * 86400)
        self.idle_s = _interval('idle_s', idle_s, 60, self.absolute_s)
        if isinstance(wait_s, bool) or not isinstance(wait_s, (int, float)) or not 0 <= wait_s <= 60:
            raise ValueError('wait_s must be from 0 to 60 seconds')
        self.wait_s = float(wait_s)
        self.statement_timeout_ms = _interval('statement_timeout_ms', statement_timeout_ms, 1, 600_000)
        self.failures = 0
        self.purge_failures = 0

    # -- connection handling (as services/store/history.py, but read-write) -----------

    def close(self):
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
            except Exception:
                pass

    def _run(self, work):
        if not self._lock.acquire(timeout=self.wait_s):
            raise AuthStoreError('another account operation holds the connection', reason='busy')
        try:
            if self._closing:
                raise AuthStoreError('account store is closed', reason='unavailable')
            if self._connection is None or self._connection.closed:
                self._connection = self._connect()
                self._schema_checked = False
            if getattr(self._connection.info.transaction_status, 'name', None) != 'IDLE':
                raise AuthStoreError('connect() returned a connection with a transaction open (or broken)',
                                     reason='error')
            with self._connection.transaction():
                db = self._connection
                db.execute(f'SET LOCAL statement_timeout = {int(self.statement_timeout_ms)}')
                if not self._schema_checked:
                    row = db.execute('SELECT max(version) FROM schema_migrations').fetchone()
                    if row is None or row[0] is None or row[0] < REQUIRED_MIGRATION:
                        raise AuthStoreError(f'the database needs migration {REQUIRED_MIGRATION:04d} or later',
                                             reason='schema')
                result = work(db)
            self._schema_checked = True
            return result
        except AuthStoreError as error:
            if error.reason not in ('conflict', 'not_found'):
                self.failures += 1
                self._drop()
            raise
        except Exception as error:
            self.failures += 1
            self._drop()
            reason = 'conflict' if type(error).__name__ == 'UniqueViolation' else _classify(error)
            raise AuthStoreError(f'account store operation failed: {type(error).__name__}', reason=reason) \
                from error
        finally:
            if self._closing:
                self._drop()
            self._lock.release()

    @staticmethod
    def _audit(db, actor, action, outcome, detail):
        db.execute('INSERT INTO audit_log (actor, action, outcome, detail) VALUES (%s, %s, %s, %s::jsonb)',
                   (actor, action, outcome, check_audit(actor, action, outcome, detail)))

    # -- accounts ---------------------------------------------------------------------

    def find_user(self, username):
        """The account for a login attempt, or None (also for a name that is not valid)."""
        if not valid_username(username):
            return None
        row = self._run(lambda db: db.execute(
            'SELECT id, username, password_hash, role, disabled FROM users WHERE username = %s',
            (username,)).fetchone())
        return None if row is None else UserRecord(*row)

    def add_user(self, username, password_hash, role, *, actor):
        if not valid_username(username):
            raise ValueError('username must be 1-64 characters: a-z first, then a-z, 0-9, ".", "_" or "-"')
        if role not in ROLES:
            raise ValueError(f'role must be one of {ROLES}')
        check_audit(actor, 'user.add', 'ok', {})

        def work(db):
            exists = db.execute('SELECT 1 FROM users WHERE username = %s', (username,)).fetchone()
            if exists:
                raise AuthStoreError('a user with this name exists', reason='conflict')
            user_id = db.execute('INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s) '
                                 'RETURNING id', (username, password_hash, role)).fetchone()[0]
            self._audit(db, actor, 'user.add', 'ok', {'username': username, 'role': role})
            return user_id
        return self._run(work)

    def set_password(self, username, password_hash, *, actor):
        """Replace the hash and revoke every session of the user. Returns sessions revoked."""
        return self._change(username, actor, 'user.password',
                            'UPDATE users SET password_hash = %s, password_changed_at = now() WHERE id = %s',
                            (password_hash,), {})

    def set_disabled(self, username, disabled, *, actor):
        """Disable (revoking every session) or enable a user. Returns sessions revoked."""
        return self._change(username, actor, 'user.disable' if disabled else 'user.enable',
                            'UPDATE users SET disabled = %s WHERE id = %s', (bool(disabled),), {})

    def set_role(self, username, role, *, actor):
        if role not in ROLES:
            raise ValueError(f'role must be one of {ROLES}')
        return self._change(username, actor, 'user.role', 'UPDATE users SET role = %s WHERE id = %s', (role,),
                            {'role': role})

    def _change(self, username, actor, action, sql, values, detail):
        if not valid_username(username):
            raise AuthStoreError('no such user', reason='not_found')
        check_audit(actor, action, 'ok', {'username': username, **detail})

        def work(db):
            row = db.execute('SELECT id FROM users WHERE username = %s FOR UPDATE', (username,)).fetchone()
            if row is None:
                raise AuthStoreError('no such user', reason='not_found')
            db.execute(sql, (*values, row[0]))
            revoked = db.execute('UPDATE auth_sessions SET revoked_at = now() WHERE user_id = %s '
                                 'AND revoked_at IS NULL AND expires_at > now()', (row[0],)).rowcount
            self._audit(db, actor, action, 'ok', {'username': username, **detail, 'sessions_revoked': revoked})
            return revoked
        return self._run(work)

    def rehash(self, user_id, old_hash, new_hash):
        """Store an upgraded hash only if the old one is still current (no audit row:
        same password, new parameters). Returns True if stored."""
        return self._run(lambda db: db.execute(
            'UPDATE users SET password_hash = %s WHERE id = %s AND password_hash = %s',
            (new_hash, user_id, old_hash)).rowcount == 1)

    def list_users(self):
        rows = self._run(lambda db: db.execute(
            'SELECT u.username, u.role, u.disabled, extract(epoch FROM u.created_at), '
            'extract(epoch FROM u.password_changed_at), (SELECT count(*) FROM auth_sessions s WHERE s.user_id = u.id '
            'AND s.revoked_at IS NULL AND s.expires_at > now() AND s.last_seen_at > now() - make_interval(secs => %s)) '
            'FROM users u ORDER BY u.username', (self.idle_s,)).fetchall())
        return [{'username': name, 'role': role, 'disabled': disabled, 'created_ts': float(created),
                 'password_changed_ts': float(changed), 'active_sessions': sessions}
                for name, role, disabled, created, changed, sessions in rows]

    # -- sessions ---------------------------------------------------------------------

    def open_session(self, user, *, detail=None):
        """Create a session for `user` (a `UserRecord` just verified) and audit the login,
        in one transaction. Returns (token, Principal). Refused (None) if the user was
        disabled or changed its password since it was read."""
        token = secrets.token_urlsafe(32)
        digest = token_digest(token)
        detail = dict(detail or {})
        check_audit(user.username, 'auth.login', 'ok', detail)

        def work(db):
            row = db.execute('SELECT 1 FROM users WHERE id = %s AND NOT disabled AND password_hash = %s FOR SHARE',
                             (user.id, user.password_hash)).fetchone()
            if row is None:
                return None
            expires = db.execute(
                'INSERT INTO auth_sessions (token_sha256, user_id, created_at, last_seen_at, expires_at) '
                'VALUES (%s, %s, now(), now(), now() + make_interval(secs => %s)) '
                'RETURNING extract(epoch FROM expires_at)', (digest, user.id, self.absolute_s)).fetchone()[0]
            self._audit(db, user.username, 'auth.login', 'ok', detail)
            # Each login removes a bounded batch of sessions that ended over a day ago, so
            # the table does not grow without bound (revoked ones keep their row until then).
            # In a savepoint (re-review M): a purge that fails, e.g. a role without DELETE
            # or a lock timeout, is skipped and never refuses the login.
            try:
                with db.transaction():
                    db.execute('DELETE FROM auth_sessions WHERE ctid IN (SELECT ctid FROM auth_sessions '
                               'WHERE expires_at < now() - make_interval(secs => %s) LIMIT %s)',
                               (PURGE_AFTER_S, PURGE_BATCH))
            except Exception as error:
                if _classify(error) == 'unavailable':  # the connection itself is gone: not skippable
                    raise
                self.purge_failures += 1
            return token, Principal(user.id, user.username, user.role, float(expires))
        return self._run(work)

    def resolve(self, token):
        """The Principal of a valid session, or None (unknown, revoked, expired, idle,
        disabled user, or not a token at all; nothing is read for a malformed token)."""
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return None
        digest = token_digest(token)

        def work(db):
            row = db.execute(
                'SELECT u.id, u.username, u.role, extract(epoch FROM s.expires_at), '
                's.last_seen_at < now() - make_interval(secs => %s) '
                'FROM auth_sessions s JOIN users u ON u.id = s.user_id '
                'WHERE s.token_sha256 = %s AND s.revoked_at IS NULL AND s.expires_at > now() '
                'AND s.last_seen_at > now() - make_interval(secs => %s) AND NOT u.disabled',
                (TOUCH_S, digest, self.idle_s)).fetchone()
            if row is None:
                return None
            user_id, username, role, expires, stale = row
            if stale:
                db.execute('UPDATE auth_sessions SET last_seen_at = now() WHERE token_sha256 = %s', (digest,))
            return Principal(user_id, username, role, float(expires))
        return self._run(work)

    def close_session(self, token, principal):
        """Revoke the session (logout) and audit it in the same transaction. Returns True
        if it was open; closing a session that is already closed changes and records nothing."""
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return False
        digest = token_digest(token)

        def work(db):
            closed = db.execute('UPDATE auth_sessions SET revoked_at = now() WHERE token_sha256 = %s '
                                'AND user_id = %s AND revoked_at IS NULL', (digest, principal.user_id)).rowcount
            if closed == 1:
                self._audit(db, principal.username, 'auth.logout', 'ok', {})
            return closed == 1
        return self._run(work)

    # -- audit ------------------------------------------------------------------------

    def audit(self, actor, action, outcome='ok', detail=None):
        """Append one audit row in its own transaction."""
        detail = {} if detail is None else detail
        check_audit(actor, action, outcome, detail)
        self._run(lambda db: self._audit(db, actor, action, outcome, detail))

    def recent_audit(self, limit=100):
        _interval('limit', limit, 1, 1_000)
        rows = self._run(lambda db: db.execute(
            'SELECT id, extract(epoch FROM at), actor, action, outcome, detail FROM audit_log '
            'ORDER BY at DESC, id DESC LIMIT %s', (limit,)).fetchall())
        return [{'id': i, 'ts': float(ts), 'actor': actor, 'action': action, 'outcome': outcome, 'detail': detail}
                for i, ts, actor, action, outcome, detail in rows]
