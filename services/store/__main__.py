"""Deployment set-up for the PostgreSQL store (backlog 5.1; PRD FR-A3, §6.3).

    python -m services.store --database-url-env AVA_DATABASE_URL reader-role \
        --role ava_reader --password-env AVA_DB_READER_PASSWORD

Connects as the database owner (the URL in the named environment variable; libpq reads
PGPASSWORD, so the owner password need not be in the URL), applies pending migrations,
then creates or updates the read-only history role (`roles.ensure_history_reader`).
Safe to run at every start: the Compose `db-setup` job and the k3s API init container
do. The reader password comes only from the named environment variable. Neither the URL
nor a password is ever printed. One JSON record on stdout; a refusal exits 2 and a
failure (database unreachable, a lock or statement timeout, unexpected error) exits 1,
each with one JSON record on stderr naming only a fixed reason or an exception type.
Both role and password rules are checked before connecting. Lock waits are bounded at
60 s and statements at 120 s.
"""
import argparse
import json
import os
import re
import sys

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
CONNECT_TIMEOUT_S = 10
LOCK_TIMEOUT_MS, STATEMENT_TIMEOUT_MS = 60_000, 120_000


def _env_name(text):
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]{0,63}$', text):
        raise argparse.ArgumentTypeError('must be an environment variable name')
    return text


def build_parser():
    parser = argparse.ArgumentParser(prog='python -m services.store', description='AVA store set-up')
    parser.add_argument('--database-url-env', type=_env_name, required=True,
                        help='environment variable with the owner\'s PostgreSQL URL')
    commands = parser.add_subparsers(dest='command', required=True)
    reader = commands.add_parser('reader-role', help='migrate, then create or update the read-only history role')
    reader.add_argument('--role', required=True, help='role name, e.g. ava_reader')
    reader.add_argument('--password-env', type=_env_name, required=True,
                        help='environment variable with the role\'s password')
    reader.add_argument('--connection-limit', type=int, default=8, help='1-100 (default 8)')
    return parser


def _connect(dsn):
    """psycopg connection with a connect timeout unless the URL sets one. Raises
    ImportError, ValueError (unparsable URL) or a psycopg error."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    try:
        given = conninfo_to_dict(dsn)
    except psycopg.ProgrammingError as error:
        raise ValueError('database URL is not valid') from error
    extra = {} if 'connect_timeout' in given else {'connect_timeout': CONNECT_TIMEOUT_S}
    return psycopg.connect(dsn, **extra)


def main(argv=None, *, environ=None, stdout=None, stderr=None, connect=None):
    environ = os.environ if environ is None else environ
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    args = build_parser().parse_args(argv)

    def say(stream, record):
        stream.write(json.dumps(record, sort_keys=True) + '\n')
        stream.flush()

    def refuse(reason):
        say(stderr, {'event': 'refused', 'command': args.command, 'reason': reason})
        return EXIT_REFUSED

    def fail(reason):
        say(stderr, {'event': 'failed', 'command': args.command, 'reason': reason})
        return EXIT_FAILED

    try:
        from services.store.migrate import MigrationError, apply_migrations
        from services.store.roles import RoleError, check_password, check_role_name
    except ImportError as error:  # pragma: no cover - the module itself is stdlib-only
        return refuse(f'missing dependency: {error.name or error}')
    try:
        check_role_name(args.role)
    except RoleError as error:
        return refuse(str(error))
    if not 1 <= args.connection_limit <= 100:
        return refuse('connection limit must be from 1 to 100')
    dsn = environ.get(args.database_url_env)
    if not dsn:
        return refuse(f'database URL not set: put it in the {args.database_url_env} environment variable')
    password = environ.get(args.password_env)
    if not password:
        return refuse(f'reader password not set: put it in the {args.password_env} environment variable')
    try:
        check_password(password)
    except RoleError as error:
        return refuse(str(error))
    from services.store.roles import ensure_history_reader
    try:
        connection = (connect or _connect)(dsn)
    except ImportError as error:
        return refuse(f'missing dependency: {error.name or error} (requirements-store.txt)')
    except ValueError:
        return refuse('database URL is not valid')
    except Exception as error:  # unreachable, authentication: the type only, never the URL
        return fail(f'connect: {type(error).__name__}')
    try:
        with connection:
            # Review L2: a stalled migration or lock elsewhere ends the job with a failure
            # record instead of an init container that waits forever with no log line.
            for name, value in (('lock_timeout', LOCK_TIMEOUT_MS), ('statement_timeout', STATEMENT_TIMEOUT_MS)):
                connection.execute(f'SET {name} = {int(value)}')  # names and values from the constants
            connection.commit()
            applied = apply_migrations(connection)
            result = ensure_history_reader(connection, args.role, password,
                                           connection_limit=args.connection_limit)
    except MigrationError as error:
        return refuse(f'migration: {error}')
    except RoleError as error:
        return refuse(f'{error.reason}: {error}')
    except Exception as error:
        return fail(type(error).__name__)
    say(stdout, {'event': 'reader_role', 'migrated': applied, **result})
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
