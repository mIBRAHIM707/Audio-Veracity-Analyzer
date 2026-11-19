"""Password rules and Argon2id hashing for local accounts (backlog 7.3; PRD FR-U7;
architecture §2.5 "Argon2", §6 access control).

Hashing uses argon2-cffi (`requirements-auth.txt`) with the parameters pinned here: the
RFC 9106 "second recommended option" (Argon2id, t=3, m=64 MiB, p=4, 16-byte salt,
32-byte tag), argon2-cffi's own default profile, written out so an upgrade of the
library cannot change them silently. A hash made with other parameters still verifies
and is reported by `needs_rehash`, so a login can upgrade it.

Each hash or verify takes 64 MiB and tens of milliseconds of CPU, so the caller bounds
how many run at once (`services/auth/service.py`). Nothing here logs or returns a
password; error messages name the rule that failed only.
"""
import unicodedata

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

TIME_COST, MEMORY_KIB, PARALLELISM, HASH_LEN, SALT_LEN = 3, 64 * 1024, 4, 32, 16
MIN_LENGTH, MAX_LENGTH = 12, 256  # characters after NFC normalisation; the maximum bounds hashing input


class PasswordRuleError(ValueError):
    """A new password breaks a rule; the message names the rule, never the password."""


def normalise(password):
    """NFC form, so the same typed text hashes alike whatever the input method composed."""
    if not isinstance(password, str):
        raise PasswordRuleError('password must be text')
    return unicodedata.normalize('NFC', password)


def check_new_password(password, username=None):
    """The rules for a password being set (not for one being tried at login)."""
    password = normalise(password)
    if not MIN_LENGTH <= len(password) <= MAX_LENGTH:
        raise PasswordRuleError(f'password must be {MIN_LENGTH} to {MAX_LENGTH} characters')
    if any(unicodedata.category(c) in ('Cc', 'Cs') for c in password):
        raise PasswordRuleError('password must not contain control characters')
    if password.strip() != password:
        raise PasswordRuleError('password must not start or end with white space')
    if username is not None and username.casefold() in password.casefold():
        raise PasswordRuleError('password must not contain the username')
    return password


class Argon2Passwords:
    """`hash(password)` for a checked new password; `verify(hash, password)` -> bool for a
    login attempt (never raises for a wrong password or a damaged stored hash);
    `needs_rehash(hash)`. `dummy_hash` is verified when the username is unknown, so an
    unknown name costs the same time as a wrong password."""

    def __init__(self, time_cost=TIME_COST, memory_kib=MEMORY_KIB, parallelism=PARALLELISM):
        self._hasher = PasswordHasher(time_cost=time_cost, memory_cost=memory_kib, parallelism=parallelism,
                                      hash_len=HASH_LEN, salt_len=SALT_LEN, type=Type.ID)
        self.parameters = {'type': 'argon2id', 'time_cost': time_cost, 'memory_kib': memory_kib,
                           'parallelism': parallelism, 'hash_len': HASH_LEN, 'salt_len': SALT_LEN}
        self.dummy_hash = self._hasher.hash('ava-dummy-password-for-unknown-users')

    def hash(self, password):
        return self._hasher.hash(normalise(password))

    def verify(self, stored_hash, password):
        if not isinstance(stored_hash, str) or not isinstance(password, str) or len(password) > MAX_LENGTH * 4:
            return False
        try:
            return self._hasher.verify(stored_hash, normalise(password))
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, stored_hash):
        try:
            return self._hasher.check_needs_rehash(stored_hash)
        except (InvalidHashError, ValueError):
            return False
