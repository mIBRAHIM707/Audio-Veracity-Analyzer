"""Least-privilege PostgreSQL login for the read-only call history (backlog 5.1 "Compose/
k3s wiring with a read-only role"; PRD FR-A3 "full history in PostgreSQL", FR-A6, §6.3;
architecture §2.5, §2.7, §7.2).

The live service writes calls as the database owner. The API only reads them, so in a
deployment it logs in as a separate role limited to reading the history tables.
`ensure_history_reader` creates or updates that role in one transaction and is safe to
run at every start:

- attributes: LOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOINHERIT, NOREPLICATION,
  NOBYPASSRLS, a connection limit, and `default_transaction_read_only = on` (earlier
  per-role settings are reset first);
- privileges in the current schema: USAGE on the schema and SELECT on exactly the tables
  the history reader uses (`READER_TABLES`); every other table and sequence privilege is
  revoked, so the accounts, sessions, audit and feedback tables (migrations 0003/0004,
  which hold password hashes) stay out of reach; on the database, every privilege
  granted to the role itself is revoked;
- review M1 (20 Nov): in the current database, PUBLIC loses TEMP and the functions that
  create large objects (`PUBLIC_LO_FUNCTIONS`), so the reader cannot write temporary
  tables or large objects either (the AVA owner is a superuser and is not affected);
- the password is sent as a SCRAM-SHA-256 verifier computed here, so the plain text
  never reaches a server log (the verifier may, with `log_statement=ddl`; treat it like
  a password hash); rerunning with a new password rotates it.

It refuses (RoleError, nothing changed) a role that is the connected user, a superuser
or replication role, a member of any other role, the owner of anything in the database
(including large objects), or the holder of rights outside the history schema (other
schemas, functions, default privileges, other databases), and a schema without the
history tables (run the migrations first). A connected user without the rights to do
all this (CREATEROLE, and a superuser for the PUBLIC revocations) is a refusal too.
The PUBLIC revocations apply to every non-superuser role in the database, and their
outcome is checked. Not covered: CONNECT on the cluster's other databases, which PUBLIC
has by default (restrict it in pg_hba.conf), and the advisory-lock functions: a
compromised reader could hold the migration lock and delay the next start's migration
(bounded at 60 s for this job; calls are never affected). Pure helpers are tested without a database; the
connection is injected (any idle psycopg 3 connection).
"""
import base64
import hashlib
import hmac
import re
import secrets

READER_TABLES = ('schema_migrations', 'calls', 'window_scores', 'alerts')
ROLE_NAME = re.compile(r'^[a-z_][a-z0-9_]{0,62}$')
RESERVED_PREFIX = 'pg_'  # PostgreSQL reserves role names starting with pg_
MIN_PASSWORD, MAX_PASSWORD = 12, 1024
SCRAM_ITERATIONS = 4096  # PostgreSQL's default scram_iterations
DEFAULT_CONNECTION_LIMIT = 8
# pg_advisory_xact_lock key: ASCII 'AVAr', so two starting jobs cannot interleave.
LOCK_KEY = 0x41564172
# Review M1: PUBLIC may execute these by default, and a large object needs no other
# privilege to create and write. (lo_import is superuser-only already.)
PUBLIC_LO_FUNCTIONS = ('pg_catalog.lo_creat(integer)', 'pg_catalog.lo_create(oid)',
                       'pg_catalog.lo_from_bytea(oid, bytea)')


class RoleError(RuntimeError):
    """The role cannot be set up safely. `reason` is a fixed word for logs."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def check_role_name(role):
    if not isinstance(role, str) or not ROLE_NAME.match(role) or role.startswith(RESERVED_PREFIX):
        raise RoleError('invalid', 'role must be 1-63 lowercase letters, digits or _, not starting '
                                   'with a digit or pg_')
    return role


def check_password(password):
    """Review L1: the whole rule, so a caller can refuse before connecting. Printable
    ASCII only: PostgreSQL applies SASLprep to the password, which leaves ASCII
    unchanged, so a verifier computed here always matches what the server checks."""
    if (not isinstance(password, str) or not MIN_PASSWORD <= len(password) <= MAX_PASSWORD
            or not password.isascii() or not password.isprintable()):
        raise RoleError('invalid', f'password must be {MIN_PASSWORD}-{MAX_PASSWORD} printable ASCII characters')
    return password


def scram_verifier(password, *, salt=None, iterations=SCRAM_ITERATIONS):
    """Pure: the SCRAM-SHA-256 verifier PostgreSQL stores for `password` (RFC 5802/7677:
    SaltedPassword = PBKDF2-HMAC-SHA-256; StoredKey = H(HMAC(SP, 'Client Key'));
    ServerKey = HMAC(SP, 'Server Key')). The password is used as UTF-8; PostgreSQL also
    applies SASLprep, which leaves ASCII unchanged, so non-ASCII passwords are refused
    here rather than risk a verifier the server would not match."""
    check_password(password)
    if not isinstance(iterations, int) or isinstance(iterations, bool) or not 4096 <= iterations <= 1_000_000:
        raise ValueError('iterations must be from 4096 to 1,000,000')
    salt = secrets.token_bytes(16) if salt is None else salt
    if not isinstance(salt, bytes) or len(salt) < 16:
        raise ValueError('salt must be at least 16 bytes')
    salted = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
    stored = hashlib.sha256(hmac.new(salted, b'Client Key', hashlib.sha256).digest()).digest()
    server = hmac.new(salted, b'Server Key', hashlib.sha256).digest()
    b64 = lambda raw: base64.b64encode(raw).decode('ascii')  # noqa: E731
    return f'SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored)}:{b64(server)}'


def _status(connection):
    return getattr(connection.info.transaction_status, 'name', None)


def ensure_history_reader(connection, role, password, *, connection_limit=DEFAULT_CONNECTION_LIMIT):
    """Create or update `role` as the history reader in the connection's current schema.
    Returns {'role', 'created', 'schema', 'tables'}. Raises RoleError (nothing changed)."""
    check_role_name(role)
    verifier = scram_verifier(password)
    if not isinstance(connection_limit, int) or isinstance(connection_limit, bool) or not 1 <= connection_limit <= 100:
        raise ValueError('connection_limit must be from 1 to 100')
    status = _status(connection)
    if status != 'IDLE':  # psycopg would nest a savepoint and commit nothing
        raise RoleError('connection', f'connection must be idle, not {status}')
    from psycopg import errors, sql  # imported here, after the checks, so they need no driver
    try:
        with connection.transaction():
            created, schema = _ensure(connection, sql, role, verifier, connection_limit)
    except errors.InsufficientPrivilege as error:  # review N3: a configuration error, not a fault
        raise RoleError('refused', 'the connected user lacks the rights to set up the role (needs CREATEROLE, '
                                   f'and a superuser to revoke the PUBLIC rights): {type(error).__name__}') from None
    return {'role': role, 'created': created, 'schema': schema, 'tables': list(READER_TABLES)}


def _ensure(connection, sql, role, verifier, connection_limit):
    """The transaction body of `ensure_history_reader`; returns (created, schema)."""
    ident = sql.Identifier(role)
    connection.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_KEY,))
    user, schema, database = connection.execute('SELECT current_user, current_schema(), current_database()').fetchone()
    if schema is None:
        raise RoleError('schema', 'no current schema (check search_path)')
    if role == user:
        raise RoleError('refused', 'the reader role must not be the connected (owner) user')
    missing = [t for t in READER_TABLES if connection.execute(
        'SELECT to_regclass(%s)', (f'{_quote(schema)}.{_quote(t)}',)).fetchone()[0] is None]
    if missing:
        raise RoleError('schema', f'history tables missing ({", ".join(missing)}): apply the migrations first')
    row = connection.execute('SELECT oid, rolsuper, rolreplication FROM pg_roles WHERE rolname = %s',
                             (role,)).fetchone()
    created = row is None
    if not created:
        oid, superuser, replication = row
        if superuser or replication:
            raise RoleError('refused', 'the role exists as a superuser or replication role')
        member_of = connection.execute('SELECT count(*) FROM pg_auth_members WHERE member = %s',
                                       (oid,)).fetchone()[0]
        if member_of:
            raise RoleError('refused', 'the role is a member of another role and could inherit its rights')
        owns = connection.execute(
            'SELECT (SELECT count(*) FROM pg_class WHERE relowner = %(o)s) '
            '+ (SELECT count(*) FROM pg_namespace WHERE nspowner = %(o)s) '
            '+ (SELECT count(*) FROM pg_proc WHERE proowner = %(o)s) '
            '+ (SELECT count(*) FROM pg_type WHERE typowner = %(o)s) '
            '+ (SELECT count(*) FROM pg_largeobject_metadata WHERE lomowner = %(o)s) '
            '+ (SELECT count(*) FROM pg_database WHERE datdba = %(o)s)', {'o': oid}).fetchone()[0]
        if owns:
            raise RoleError('refused', 'the role owns database objects; an owner bypasses grants')
    verb = sql.SQL('CREATE ROLE' if created else 'ALTER ROLE')
    connection.execute(sql.SQL(
        '{} {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS '
        'CONNECTION LIMIT {} PASSWORD {}').format(verb, ident, sql.Literal(connection_limit),
                                                  sql.Literal(verifier)))
    # Review M2: settings and database rights left from an earlier set-up are removed.
    connection.execute(sql.SQL('ALTER ROLE {} RESET ALL').format(ident))
    # Re-review: per-database settings override role-wide ones, so reset them too.
    connection.execute(sql.SQL('ALTER ROLE {} IN DATABASE {} RESET ALL').format(ident, sql.Identifier(database)))
    connection.execute(sql.SQL('ALTER ROLE {} SET default_transaction_read_only = on').format(ident))
    schema_ident = sql.Identifier(schema)
    for statement in ('REVOKE ALL ON DATABASE {d} FROM {r}',
                      'REVOKE ALL ON ALL TABLES IN SCHEMA {s} FROM {r}',
                      'REVOKE ALL ON ALL SEQUENCES IN SCHEMA {s} FROM {r}',
                      'REVOKE CREATE ON SCHEMA {s} FROM {r}'):
        connection.execute(sql.SQL(statement).format(d=sql.Identifier(database), s=schema_ident, r=ident))
    if not created:
        _refuse_rights_elsewhere(connection, role, schema)
    # Review M1: PUBLIC's default rights to write temporary tables and large objects.
    connection.execute(sql.SQL('REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC').format(sql.Identifier(database)))
    connection.execute(sql.SQL('REVOKE EXECUTE ON FUNCTION {} FROM PUBLIC').format(
        sql.SQL(', ').join(sql.SQL(signature) for signature in PUBLIC_LO_FUNCTIONS)))
    # Re-review: a REVOKE by a non-owner is only a WARNING ("no privileges could be
    # revoked"), so check the outcome instead of trusting the statements.
    kept = connection.execute(
        'SELECT has_database_privilege(%(r)s, %(d)s, \'TEMP\'), '
        + ', '.join(f'has_function_privilege(%(r)s, %(f{i})s, \'EXECUTE\')' for i in range(len(PUBLIC_LO_FUNCTIONS))),
        {'r': role, 'd': database, **{f'f{i}': f for i, f in enumerate(PUBLIC_LO_FUNCTIONS)}}).fetchone()
    if any(kept):
        raise RoleError('refused', 'the role could still create temporary tables or large objects: the PUBLIC '
                                   'rights can only be revoked by a superuser (or the objects\' owners)')
    connection.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(schema_ident, ident))
    connection.execute(sql.SQL('GRANT SELECT ON {} TO {}').format(
        sql.SQL(', ').join(sql.Identifier(schema, t) for t in READER_TABLES), ident))
    return created, schema


# Review M2: rights an existing role may hold that the revocations above do not reach.
# Each query counts ACL entries naming the role (aclexplode skips NULL, the defaults).
RIGHTS_ELSEWHERE = {
    'other schemas': 'SELECT count(*) FROM pg_namespace n, aclexplode(n.nspacl) a '
                     'WHERE a.grantee = %(o)s AND n.nspname <> %(s)s',
    'tables in other schemas': 'SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, '
                               'aclexplode(c.relacl) a WHERE a.grantee = %(o)s AND n.nspname <> %(s)s',
    'columns': 'SELECT count(*) FROM pg_attribute t, aclexplode(t.attacl) a WHERE a.grantee = %(o)s',
    'functions': 'SELECT count(*) FROM pg_proc p, aclexplode(p.proacl) a WHERE a.grantee = %(o)s',
    'default privileges': 'SELECT count(*) FROM pg_default_acl d WHERE d.defaclrole = %(o)s OR EXISTS '
                          '(SELECT 1 FROM aclexplode(d.defaclacl) a WHERE a.grantee = %(o)s)',
    'large objects': 'SELECT count(*) FROM pg_largeobject_metadata m, aclexplode(m.lomacl) a '
                     'WHERE a.grantee = %(o)s',
    'other databases': 'SELECT count(*) FROM pg_database d, aclexplode(d.datacl) a WHERE a.grantee = %(o)s',
}


def _refuse_rights_elsewhere(connection, role, schema):
    oid = connection.execute('SELECT oid FROM pg_roles WHERE rolname = %s', (role,)).fetchone()[0]
    found = [name for name, query in RIGHTS_ELSEWHERE.items()
             if connection.execute(query, {'o': oid, 's': schema}).fetchone()[0]]
    if found:
        raise RoleError('refused', f'the role holds rights outside the history tables ({", ".join(found)}); '
                                   'revoke them or use a new role name')


def _quote(name):
    """Double-quoted identifier text for to_regclass (schema and table names)."""
    return '"' + name.replace('"', '""') + '"'
