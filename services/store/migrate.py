"""Versioned PostgreSQL schema migrations for AVA (backlog 5.1; architecture §2.7, ADR-7).

Plain numbered SQL files (`migrations/NNNN_name.sql`, contiguous from 0001), no ORM or
migration framework. `apply_migrations` runs every pending file and its bookkeeping
row in ONE transaction under a transaction-scoped advisory lock, so two starting
services cannot both apply it and a failure leaves the database unchanged
(PostgreSQL DDL is transactional). An applied file whose name or SHA-256 changed is
refused (`MigrationError`): add a new file instead of editing an old one. A database
that has versions this checkout does not know is refused too (no downgrade). Files
are hashed and run with LF line endings (CRLF checkouts match), and statements that
would end or escape the transaction (BEGIN/COMMIT/ROLLBACK/END/SAVEPOINT/RELEASE/
VACUUM, CONCURRENTLY) are refused at load. The connection must be idle (no open
transaction), or psycopg would nest a savepoint and commit nothing.

`plan` and `load_migrations` are pure and tested without a database; the connection
is any psycopg 3 connection (injected, so tests can use a disposable container).
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

MIGRATIONS_DIR = Path(__file__).resolve().parent / 'migrations'
NAME = re.compile(r'^(\d{4})_([a-z0-9_]{1,64})\.sql$')
MAX_FILE_BYTES = 256 * 1024
# pg_advisory_xact_lock key: ASCII 'AVAm' (fixed so every service uses the same lock).
LOCK_KEY = 0x4156416D
BOOKKEEPING = '''CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (char_length(sha256) = 64),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now())'''


# Statements that end or nest the migration transaction, or cannot run inside one.
TRANSACTION_CONTROL = re.compile(r'(?:^|;)\s*(BEGIN|COMMIT|ROLLBACK|ABORT|START\s+TRANSACTION|'
                                 r'PREPARE\s+TRANSACTION|END|SAVEPOINT|RELEASE|VACUUM)\b|\b(CONCURRENTLY)\b',
                                 re.IGNORECASE)


def _without_comments(sql):
    """Comments removed for the transaction-control check. Not a SQL lexer: `--` inside
    a string literal is treated as a comment too (keep such text out of migrations)."""
    sql = re.sub(r'/\*.*?\*/', ' ', sql, flags=re.DOTALL)
    return re.sub(r'--[^\n]*', ' ', sql)


class MigrationError(RuntimeError):
    """Invalid migration files, or a database that does not match them."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sha256: str
    sql: str


def load_migrations(directory=MIGRATIONS_DIR):
    """Read `NNNN_name.sql` files in version order. Refuses other files ending in
    .sql, gaps or duplicates in the numbering, empty or oversized files and non-UTF-8."""
    directory = Path(directory)
    found = {}
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() != '.sql':
            continue
        match = NAME.match(path.name)
        if not match or not path.is_file():
            raise MigrationError(f'{path.name}: expected a file named NNNN_lowercase_name.sql')
        version = int(match.group(1))
        if version in found:
            raise MigrationError(f'two migrations claim version {version}')
        raw = path.read_bytes()
        if not raw.strip() or len(raw) > MAX_FILE_BYTES:
            raise MigrationError(f'{path.name}: empty or larger than {MAX_FILE_BYTES} bytes')
        # Hash and run LF text: a Windows checkout with CRLF endings must match the
        # database a Linux checkout migrated.
        raw = raw.replace(b'\r\n', b'\n')
        try:
            sql = raw.decode('utf-8')
        except UnicodeDecodeError:
            raise MigrationError(f'{path.name}: not UTF-8') from None
        control = TRANSACTION_CONTROL.search(_without_comments(sql))
        if control:
            word = (control.group(1) or control.group(2)).upper()
            raise MigrationError(f'{path.name}: {word} would break the single migration transaction '
                                 '(also refused inside function bodies; split such a file deliberately)')
        found[version] = Migration(version, path.name, hashlib.sha256(raw).hexdigest(), sql)
    versions = sorted(found)
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f'migration versions must run 1..n without gaps, found {versions}')
    return [found[v] for v in versions]


def plan(applied, migrations):
    """Pure: `applied` rows `(version, name, sha256)` from the database -> the
    migrations still to apply. Raises MigrationError if the database does not match
    the files it claims to have applied, or has versions beyond them."""
    by_version = {m.version: m for m in migrations}
    applied = sorted(applied)
    for position, (version, name, sha256) in enumerate(applied, start=1):
        if version != position:
            raise MigrationError(f'database migration history has a gap or bad version at {version}')
        known = by_version.get(version)
        if known is None:
            raise MigrationError(f'database has migration {version} ({name}) that this checkout does not know')
        if (name, sha256) != (known.name, known.sha256):
            raise MigrationError(f'migration {version} changed after it was applied '
                                 f'(database {name} {sha256[:12]}, file {known.name} {known.sha256[:12]})')
    return [m for m in migrations if m.version > len(applied)]


def apply_migrations(connection, migrations=None):
    """Apply pending migrations in one transaction. Returns the applied versions
    (empty when the database is current)."""
    migrations = load_migrations() if migrations is None else list(migrations)
    status = getattr(connection.info.transaction_status, 'name', None)
    if status != 'IDLE':  # psycopg would nest a savepoint: nothing committed, lock held
        raise MigrationError(f'connection must be idle, not {status}: commit or roll back first')
    with connection.transaction():
        connection.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_KEY,))
        connection.execute(BOOKKEEPING)
        applied = connection.execute('SELECT version, name, sha256 FROM schema_migrations').fetchall()
        pending = plan([tuple(row) for row in applied], migrations)
        for migration in pending:
            connection.execute(migration.sql.encode('utf-8'))
            connection.execute('INSERT INTO schema_migrations (version, name, sha256) VALUES (%s, %s, %s)',
                               (migration.version, migration.name, migration.sha256))
    return [m.version for m in pending]
