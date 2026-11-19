"""Operator commands for local accounts (backlog 7.3; PRD FR-U7, §6.3).

    python -m services.auth --database-url-env AVA_DATABASE_URL migrate
    python -m services.auth --database-url-env AVA_DATABASE_URL add-user NAME --role analyst|admin
    python -m services.auth --database-url-env AVA_DATABASE_URL set-password NAME
    python -m services.auth --database-url-env AVA_DATABASE_URL disable NAME | enable NAME
    python -m services.auth --database-url-env AVA_DATABASE_URL set-role NAME --role analyst|admin
    python -m services.auth --database-url-env AVA_DATABASE_URL list-users
    python -m services.auth --database-url-env AVA_DATABASE_URL audit [--limit N]

There is no default account and no password on the command line: `add-user` and
`set-password` read the new password as the first line of standard input (a terminal
prompts without echo), checked against the rules in `passwords.py`. The database URL
comes from the named environment variable and is never printed. Every change is
written to the audit log with actor `system:cli`; disabling a user or setting a
password revokes its sessions. `migrate` applies pending migrations (the API never
migrates). One JSON record on stdout; refusals exit 2 with a JSON record on stderr.
"""
import argparse
import getpass
import json
import os
import re
import sys

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
ACTOR = 'system:cli'


def _env_name(text):
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]{0,63}$', text):
        raise argparse.ArgumentTypeError('must be an environment variable name')
    return text


def build_parser():
    parser = argparse.ArgumentParser(prog='python -m services.auth', description='AVA local accounts')
    parser.add_argument('--database-url-env', type=_env_name, required=True,
                        help='environment variable with the PostgreSQL URL')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('migrate', help='apply pending schema migrations')
    add = commands.add_parser('add-user', help='create a user (password on stdin)')
    add.add_argument('username')
    add.add_argument('--role', choices=('analyst', 'admin'), required=True)
    commands.add_parser('set-password', help='set a password (stdin); revokes sessions').add_argument('username')
    commands.add_parser('disable', help='disable a user; revokes sessions').add_argument('username')
    commands.add_parser('enable', help='enable a user').add_argument('username')
    role = commands.add_parser('set-role', help='change a role; revokes sessions')
    role.add_argument('username')
    role.add_argument('--role', choices=('analyst', 'admin'), required=True)
    commands.add_parser('list-users', help='users with role, state and active sessions')
    audit = commands.add_parser('audit', help='newest audit rows')
    audit.add_argument('--limit', type=int, default=50)
    return parser


def read_password(stdin):
    """First line of stdin without its line ending; a terminal prompts twice, no echo."""
    if stdin.isatty():
        first = getpass.getpass('New password: ')
        if getpass.getpass('Repeat: ') != first:
            raise ValueError('the two passwords differ')
        return first
    line = stdin.readline()
    if not line:
        raise ValueError('no password on standard input')
    return line[:-2] if line.endswith('\r\n') else line[:-1] if line.endswith('\n') else line


def _connect_factory(dsn):
    """The API's factory: connect timeout, TCP keepalives, statement and lock timeouts."""
    from services.api.__main__ import _connect_factory as api_factory
    return api_factory(dsn)


def main(argv=None, *, environ=None, stdin=None, stdout=None, stderr=None, passwords=None):
    environ = os.environ if environ is None else environ
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    args = build_parser().parse_args(argv)

    def say(stream, record):
        stream.write(json.dumps(record, sort_keys=True) + '\n')
        stream.flush()

    def refuse(reason):
        say(stderr, {'event': 'refused', 'command': args.command, 'reason': reason})
        return EXIT_REFUSED

    dsn = environ.get(args.database_url_env)
    if not dsn:
        return refuse(f'database URL not set: put it in the {args.database_url_env} environment variable')
    try:
        connect = _connect_factory(dsn)
    except ImportError as error:
        return refuse(f'missing dependency: {error.name or error} (requirements-store.txt)')
    except ValueError:
        return refuse('database URL is not valid')

    if args.command == 'migrate':
        from services.store.migrate import MigrationError, apply_migrations
        try:
            with connect() as connection:
                applied = apply_migrations(connection)
        except MigrationError as error:
            return refuse(str(error))
        except Exception as error:
            say(stderr, {'event': 'failed', 'command': 'migrate', 'reason': type(error).__name__})
            return EXIT_FAILED
        say(stdout, {'event': 'migrated', 'applied': applied})
        return EXIT_OK

    from services.auth.store import AuthStoreError, PostgresAuthStore
    hashing = args.command in ('add-user', 'set-password')
    if hashing:
        try:
            from services.auth.passwords import Argon2Passwords, PasswordRuleError, check_new_password
        except ImportError as error:
            return refuse(f'missing dependency: {error.name or error} (requirements-auth.txt)')
        try:
            password = check_new_password(read_password(stdin), args.username)
        except (ValueError, PasswordRuleError) as error:
            return refuse(str(error))
        password_hash = (passwords or Argon2Passwords()).hash(password)
        del password
    store = PostgresAuthStore(connect)
    try:
        if args.command == 'add-user':
            store.add_user(args.username, password_hash, args.role, actor=ACTOR)
            record = {'event': 'user_added', 'username': args.username, 'role': args.role}
        elif args.command == 'set-password':
            revoked = store.set_password(args.username, password_hash, actor=ACTOR)
            record = {'event': 'password_set', 'username': args.username, 'sessions_revoked': revoked}
        elif args.command in ('disable', 'enable'):
            revoked = store.set_disabled(args.username, args.command == 'disable', actor=ACTOR)
            record = {'event': f'user_{args.command}d', 'username': args.username, 'sessions_revoked': revoked}
        elif args.command == 'set-role':
            revoked = store.set_role(args.username, args.role, actor=ACTOR)
            record = {'event': 'role_set', 'username': args.username, 'role': args.role, 'sessions_revoked': revoked}
        elif args.command == 'list-users':
            record = {'event': 'users', 'users': store.list_users()}
        else:
            record = {'event': 'audit', 'rows': store.recent_audit(args.limit)}
    except ValueError as error:
        return refuse(str(error))
    except AuthStoreError as error:
        if error.reason in ('conflict', 'not_found', 'schema'):
            return refuse({'conflict': 'a user with this name exists', 'not_found': 'no such user',
                           'schema': 'the database is not migrated: run the migrate command'}[error.reason])
        say(stderr, {'event': 'failed', 'command': args.command, 'reason': error.reason})
        return EXIT_FAILED
    finally:
        store.close()
    say(stdout, record)
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
