"""Login decisions for the API (backlog 7.3; PRD FR-U7; architecture §2.5).

`Authenticator` joins the password hasher and the account store and adds what neither
should own:

- equal work for unknown and known usernames (the dummy hash is verified when a name
  is unknown, malformed or disabled), and one answer for every refusal (`invalid`), so
  a caller cannot tell which names exist;
- a bound on concurrent Argon2 work (each verify takes 64 MiB): at most
  `max_hashing` at once, others are refused `busy` at once rather than queued;
- a failure limit per username: after `max_failures` failed passwords within
  `window_s` the name is refused `locked` for `lockout_s` without verifying anything.
  Every well-formed name is counted, known or not, so a lockout does not reveal which
  names exist. This is in memory (per API process; a restart forgets it) and lets anyone
  lock a name out for `lockout_s`: accepted for an on-premises lab, recorded in the
  README. The table is bounded (`max_tracked` names, oldest forgotten first, so a flood
  of names can shorten a lockout).
- hash upgrades: a correct password whose stored hash has older parameters is
  re-hashed and stored.

Login attempts are audited: success in the same transaction as its session
(`store.open_session`), refusals as `auth.login refused` with the reason and, for an
existing account only, the name tried (an unknown "name" may be a mistyped password). Not audited, only counted: attempts on a name
already locked (the lock was audited when it began) and `busy`/`unavailable` answers. If the audit row cannot be written the login is
not granted (`unavailable`). Pure apart from the injected store, hasher and clock.
"""
from collections import OrderedDict
from dataclasses import dataclass
import threading
import time

from services.auth.store import AuthStoreError, valid_username

MAX_FAILURES, WINDOW_S, LOCKOUT_S = 5, 15 * 60, 15 * 60
MAX_HASHING, MAX_TRACKED = 2, 10_000
LOGIN_OUTCOMES = ('ok', 'invalid', 'locked', 'busy', 'unavailable')


@dataclass(frozen=True)
class LoginResult:
    outcome: str  # one of LOGIN_OUTCOMES
    token: str | None = None
    principal: object = None


class Authenticator:
    def __init__(self, store, passwords, *, clock=time.monotonic, max_failures=MAX_FAILURES, window_s=WINDOW_S,
                 lockout_s=LOCKOUT_S, max_hashing=MAX_HASHING, max_tracked=MAX_TRACKED):
        self.store, self.passwords, self._clock = store, passwords, clock
        self.max_failures, self.window_s, self.lockout_s = max_failures, window_s, lockout_s
        self.max_tracked = max_tracked
        self._hashing = threading.BoundedSemaphore(max_hashing)
        self._lock = threading.Lock()
        self._failures = OrderedDict()  # username -> (failure times, locked until)
        self.counts = {outcome: 0 for outcome in LOGIN_OUTCOMES}

    def _count(self, outcome):
        with self._lock:
            self.counts[outcome] += 1

    def _locked(self, username, now):
        with self._lock:
            entry = self._failures.get(username)
            return entry is not None and entry[1] > now

    def _fail(self, username, now):
        """Record a failed password; returns True if this failure locks the name."""
        with self._lock:
            times, until = self._failures.pop(username, ([], 0.0))
            times = [t for t in times if t > now - self.window_s] + [now]
            locked = len(times) >= self.max_failures
            if locked:
                times, until = [], now + self.lockout_s
            self._failures[username] = (times, until)
            while len(self._failures) > self.max_tracked:
                self._failures.popitem(last=False)
            return locked

    def _succeed(self, username):
        with self._lock:
            self._failures.pop(username, None)

    def _refuse(self, username, reason, outcome='invalid', known=False):
        """Audit a refusal. The name tried is recorded only for an existing account
        (review L1): an unknown "name" may be a password typed into the wrong field."""
        detail = {'reason': reason}
        if known and valid_username(username):
            detail['username'] = username
        try:
            self.store.audit('anonymous', 'auth.login', 'refused', detail)
        except AuthStoreError:
            pass  # the refusal stands whether or not it could be recorded
        self._count(outcome)
        return LoginResult(outcome)

    def login(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str):
            return self._refuse(None, 'malformed')
        now = self._clock()
        if valid_username(username) and self._locked(username, now):
            # Counted, not audited: the lock itself was audited (`locked_now`), and a
            # flood of attempts on a locked name must not become a flood of writes.
            self._count('locked')
            return LoginResult('locked')
        if not self._hashing.acquire(blocking=False):
            self._count('busy')
            return LoginResult('busy')
        try:
            try:
                user = self.store.find_user(username)
            except AuthStoreError:
                self._count('unavailable')
                return LoginResult('unavailable')
            stored = user.password_hash if user is not None else self.passwords.dummy_hash
            correct = self.passwords.verify(stored, password)
            if user is None or user.disabled or not correct:
                # Every well-formed name counts, known or not: a lockout must not reveal
                # which names exist.
                known = user is not None
                if valid_username(username) and self._fail(username, self._clock()):
                    return self._refuse(username, 'locked_now', 'locked', known=known)
                reason = 'unknown_user' if user is None else 'disabled' if user.disabled else 'wrong_password'
                return self._refuse(username, reason, known=known)
            if self.passwords.needs_rehash(user.password_hash):
                try:
                    new_hash = self.passwords.hash(password)
                    if self.store.rehash(user.id, user.password_hash, new_hash):
                        user = type(user)(user.id, user.username, new_hash, user.role, user.disabled)
                    else:
                        # Another login upgraded it first (review L2): its hash is of this
                        # same password, so take the current record and check it again.
                        current = self.store.find_user(username)
                        if current is not None and self.passwords.verify(current.password_hash, password):
                            user = current
                except AuthStoreError:
                    pass  # the old hash still verifies; upgrade on a later login
        finally:
            self._hashing.release()
        try:
            opened = self.store.open_session(user)
        except AuthStoreError:
            self._count('unavailable')
            return LoginResult('unavailable')
        if opened is None:  # disabled or password changed between the read and now
            return self._refuse(username, 'changed', known=True)
        self._succeed(username)
        self._count('ok')
        token, principal = opened
        return LoginResult('ok', token, principal)
