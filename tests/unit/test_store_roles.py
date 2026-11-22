"""Claude-authored tests (20 November 2026) for the least-privilege history reader role
(services/store/roles.py) and `python -m services.store reader-role` (backlog 5.1
"Compose/k3s wiring with a read-only role"; PRD FR-A3, §6.3; architecture §2.7, §7.2).

Authored by Claude (Claude Code), not by GPT-6 Astra. Groups:
- pure tests (always run): role-name and password rules, the SCRAM-SHA-256 verifier
  checked against the RFC 7677 worked example (StoredKey through the client proof,
  ServerKey through the server signature), and CLI refusals that need no database;
- PostgreSQL tests, run only when AVA_TEST_POSTGRES_DSN names a disposable PostgreSQL 16
  database (AVA_REQUIRE_POSTGRES=1 makes the skip a failure). The password checks need
  the server to use password authentication (SCRAM), not `trust`; under `trust` those
  checks skip with that reason. Each test uses its own schema and role names and drops
  both. Logging in as the role is the evidence that the verifier is accepted.
Storage and privilege evidence only: no audio and no detector are involved.
"""
import base64
import hashlib
import hmac
import io
import json
import os
import re
import threading
import unittest
import uuid

from services.events.contract import make_envelope
from services.store import migrate
from services.store.__main__ import EXIT_FAILED, EXIT_OK, EXIT_REFUSED, main
from services.store.roles import (READER_TABLES, RoleError, check_password, check_role_name,
                                  ensure_history_reader, scram_verifier)

DSN = os.environ.get('AVA_TEST_POSTGRES_DSN')
REAL = unittest.skipIf(not DSN and os.environ.get('AVA_REQUIRE_POSTGRES') != '1',
                       'set AVA_TEST_POSTGRES_DSN to a disposable PostgreSQL 16 database')
PASSWORD = 'reader-pass-0123456789'
NEW_PASSWORD = 'rotated-pass-9876543210'
OWNER_PASSWORD_IN_URL = 'hunter2-owner-secret'
VERIFIER = re.compile(r'^SCRAM-SHA-256\$(\d+):([A-Za-z0-9+/=]+)\$([A-Za-z0-9+/=]+):([A-Za-z0-9+/=]+)$')


# -- pure -----------------------------------------------------------------------------

class NamesAndPasswordsTests(unittest.TestCase):
    def test_role_names(self):
        for good in ('ava_reader', '_r', 'r', 'a' * 63, 'reader_2'):
            self.assertEqual(check_role_name(good), good)
        for bad in ('', 'A', 'Ava', '1abc', 'pg_reader', 'a' * 64, 'ava-reader', 'ava reader', 'ava"x',
                    'ava;drop', 'ریڈر', None, 5):
            with self.subTest(bad=bad), self.assertRaises(RoleError) as caught:
                check_role_name(bad)
            self.assertEqual(caught.exception.reason, 'invalid')

    def test_passwords(self):
        self.assertEqual(check_password('x' * 12), 'x' * 12)
        self.assertEqual(check_password('x' * 1024), 'x' * 1024)
        for bad in ('x' * 11, 'x' * 1025, 'abcdefghijk\x00l', None, b'bytes-password-x',
                    'пароль-кириллица-12', 'tab\tinside-password', 'café-password-12'):
            with self.subTest(bad=bad), self.assertRaises(RoleError):
                check_password(bad)


class ScramVerifierTests(unittest.TestCase):
    # RFC 7677 section 3 worked example (user "user", password "pencil").
    SALT = base64.b64decode('W22ZaJ0SNY7soEsUEjb6gQ==')
    AUTH_MESSAGE = ('n=user,r=rOprNGfwEbeRWgbNEkqO,'
                    'r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,'
                    'c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0').encode()
    PROOF = base64.b64decode('dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=')
    SERVER_SIGNATURE = base64.b64decode('6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4=')

    def parts(self, verifier):
        match = VERIFIER.match(verifier)
        self.assertIsNotNone(match, verifier)
        iterations, salt, stored, server = match.groups()
        return int(iterations), base64.b64decode(salt), base64.b64decode(stored), base64.b64decode(server)

    def test_the_rfc_7677_example_is_reproduced(self):
        # "pencil" is shorter than the deployment minimum, so the arithmetic is checked
        # through the same code path with the length rule lifted for this one case.
        from services.store import roles
        original = roles.MIN_PASSWORD
        roles.MIN_PASSWORD = 1
        try:
            verifier = scram_verifier('pencil', salt=self.SALT, iterations=4096)
        finally:
            roles.MIN_PASSWORD = original
        iterations, salt, stored_key, server_key = self.parts(verifier)
        self.assertEqual((iterations, salt), (4096, self.SALT))
        # ServerSignature = HMAC(ServerKey, AuthMessage)
        self.assertEqual(hmac.new(server_key, self.AUTH_MESSAGE, hashlib.sha256).digest(), self.SERVER_SIGNATURE)
        # ClientKey = ClientProof XOR HMAC(StoredKey, AuthMessage); StoredKey = H(ClientKey)
        signature = hmac.new(stored_key, self.AUTH_MESSAGE, hashlib.sha256).digest()
        client_key = bytes(a ^ b for a, b in zip(self.PROOF, signature))
        self.assertEqual(hashlib.sha256(client_key).digest(), stored_key)

    def test_salt_is_random_and_inputs_are_checked(self):
        first, second = scram_verifier(PASSWORD), scram_verifier(PASSWORD)
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.parts(first)[1]), 16)
        self.assertNotIn(PASSWORD, first)
        fixed = b'0123456789abcdef'
        self.assertEqual(scram_verifier(PASSWORD, salt=fixed), scram_verifier(PASSWORD, salt=fixed))
        for bad in ('пароль-кириллица-12', 'tab\tinside-password', 'newline\npassword-x'):
            with self.subTest(bad=bad), self.assertRaises(RoleError):
                scram_verifier(bad)
        for iterations in (4095, 1_000_001, True, 4096.0):
            with self.subTest(iterations=iterations), self.assertRaises(ValueError):
                scram_verifier(PASSWORD, iterations=iterations)
        for salt in (b'short', 'not-bytes-at-all-x'):
            with self.subTest(salt=salt), self.assertRaises(ValueError):
                scram_verifier(PASSWORD, salt=salt)


class FakeInfo:
    def __init__(self, status):
        self.transaction_status = type('Status', (), {'name': status})()


class BusyConnection:
    info = FakeInfo('INTRANS')

    def execute(self, *args):  # pragma: no cover - must never be reached
        raise AssertionError('must not run SQL on a busy connection')


class EnsureWithoutDatabaseTests(unittest.TestCase):
    def test_invalid_arguments_and_a_busy_connection_run_no_sql(self):
        connection = BusyConnection()
        with self.assertRaises(RoleError) as caught:
            ensure_history_reader(connection, 'ava_reader', PASSWORD)
        self.assertEqual(caught.exception.reason, 'connection')
        for role, password in (('Bad', PASSWORD), ('ava_reader', 'short')):
            with self.assertRaises(RoleError):
                ensure_history_reader(connection, role, password)
        for limit in (0, 101, True, '8'):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                ensure_history_reader(connection, 'ava_reader', PASSWORD, connection_limit=limit)


class CliWithoutDatabaseTests(unittest.TestCase):
    URL = f'postgresql://ava:{OWNER_PASSWORD_IN_URL}@db.invalid:5432/ava'

    def run_main(self, argv, environ, connect=None):
        out, err = io.StringIO(), io.StringIO()
        code = main(['--database-url-env', 'OWNER_URL', *argv], environ=environ, stdout=out, stderr=err,
                    connect=connect)
        text = out.getvalue() + err.getvalue()
        for secret in (OWNER_PASSWORD_IN_URL, PASSWORD, 'db.invalid'):
            self.assertNotIn(secret, text)
        return code, out.getvalue(), err.getvalue()

    def never(self, dsn):  # pragma: no cover - must never be reached
        raise AssertionError('must not connect')

    def test_refusals_before_connecting(self):
        good = {'OWNER_URL': self.URL, 'READER_PW': PASSWORD}
        cases = [
            (['reader-role', '--role', 'Bad-Name', '--password-env', 'READER_PW'], good, 'role must be'),
            (['reader-role', '--role', 'ava_reader', '--password-env', 'READER_PW'], {'READER_PW': PASSWORD},
             'database URL not set'),
            (['reader-role', '--role', 'ava_reader', '--password-env', 'READER_PW'], {'OWNER_URL': self.URL},
             'reader password not set'),
            (['reader-role', '--role', 'ava_reader', '--password-env', 'READER_PW'],
             {'OWNER_URL': self.URL, 'READER_PW': 'short'}, 'password must be'),
            # Review L1: refused before connecting (and so before any migration).
            (['reader-role', '--role', 'ava_reader', '--password-env', 'READER_PW'],
             {'OWNER_URL': self.URL, 'READER_PW': 'café-password-12'}, 'printable ASCII'),
            (['reader-role', '--role', 'ava_reader', '--password-env', 'READER_PW', '--connection-limit', '0'],
             good, 'connection limit'),
        ]
        for argv, environ, message in cases:
            with self.subTest(message=message):
                code, out, err = self.run_main(argv, environ, connect=self.never)
                self.assertEqual(code, EXIT_REFUSED)
                self.assertEqual(out, '')
                record = json.loads(err)
                self.assertEqual((record['event'], record['command']), ('refused', 'reader-role'))
                self.assertIn(message, record['reason'])

    def test_an_unreachable_database_is_a_failure_naming_only_the_error_type(self):
        def unreachable(dsn):
            raise ConnectionRefusedError(f'could not connect to {dsn}')
        code, out, err = self.run_main(['reader-role', '--role', 'ava_reader', '--password-env', 'READER_PW'],
                                       {'OWNER_URL': self.URL, 'READER_PW': PASSWORD}, connect=unreachable)
        self.assertEqual((code, out), (EXIT_FAILED, ''))
        self.assertEqual(json.loads(err), {'event': 'failed', 'command': 'reader-role',
                                           'reason': 'connect: ConnectionRefusedError'})

    def test_bad_option_values_are_usage_errors(self):
        for argv in (['--database-url-env', '1BAD', 'reader-role', '--role', 'r', '--password-env', 'P'],
                     ['--database-url-env', 'U', 'reader-role', '--role', 'r', '--password-env', 'bad-name']):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as caught:
                main(argv, environ={}, stdout=io.StringIO(), stderr=io.StringIO())
            self.assertEqual(caught.exception.code, 2)


# -- PostgreSQL -----------------------------------------------------------------------

@REAL
class ReaderRolePostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import errors
        from psycopg.conninfo import make_conninfo
        self.psycopg, self.errors, self.make_conninfo = psycopg, errors, make_conninfo
        token = uuid.uuid4().hex[:10]
        self.schema, self.role = f'ava_test_{token}', f'ava_r_{token}'
        self.extra_roles, self.extra_schemas = [], []
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'CREATE SCHEMA {self.schema}')
        self.connections = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for connection in self.connections:
            connection.close()
        with self.psycopg.connect(DSN, autocommit=True) as admin:
            for schema in (self.schema, *self.extra_schemas):
                admin.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
            for role in (self.role, *self.extra_roles):
                if admin.execute('SELECT 1 FROM pg_roles WHERE rolname = %s', (role,)).fetchone():
                    admin.execute(f'DROP OWNED BY {role}')
                    admin.execute(f'DROP ROLE {role}')

    def owner(self):
        connection = self.psycopg.connect(DSN, options=f'-c search_path={self.schema}')
        self.connections.append(connection)
        return connection

    def migrated_owner(self):
        connection = self.owner()
        migrate.apply_migrations(connection)
        return connection

    def as_reader(self, password=PASSWORD, role=None):
        dsn = self.make_conninfo(DSN, user=role or self.role, password=password,
                                 options=f'-c search_path={self.schema}')
        connection = self.psycopg.connect(dsn)
        self.connections.append(connection)
        return connection

    def password_auth_or_skip(self, role=None):
        """Under `trust` any password logs in; the password checks then prove nothing."""
        try:
            self.as_reader(password='certainly-not-the-password', role=role)
        except self.psycopg.OperationalError:
            return
        if os.environ.get('AVA_REQUIRE_POSTGRES') == '1':
            self.fail('the test server accepts any password (trust): run it with scram-sha-256')
        self.skipTest('the test server uses trust authentication')

    def role_row(self, role=None):
        with self.psycopg.connect(DSN) as admin:
            return admin.execute(
                'SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolinherit, rolreplication, '
                'rolbypassrls, rolconnlimit, rolconfig FROM pg_roles WHERE rolname = %s',
                (role or self.role,)).fetchone()

    def test_the_role_can_read_the_history_tables_and_nothing_else(self):
        result = ensure_history_reader(self.migrated_owner(), self.role, PASSWORD)
        self.assertEqual(result, {'role': self.role, 'created': True, 'schema': self.schema,
                                  'tables': list(READER_TABLES)})
        self.assertEqual(self.role_row(), (True, False, False, False, False, False, False, 8,
                                           ['default_transaction_read_only=on']))
        reader = self.as_reader()
        for table in READER_TABLES:
            reader.execute(f'SELECT count(*) FROM {table}').fetchone()
        self.assertEqual(reader.execute('SHOW transaction_read_only').fetchone()[0], 'on')
        reader.rollback()
        # Read-only is only a default the session can change; privileges are the boundary.
        reader.autocommit = True
        reader.execute('SET default_transaction_read_only = off')
        # (CREATE in the schema is checked in the rerun test, where a hand grant is the control.)
        writes = ["INSERT INTO calls (call_id) VALUES ('x')", 'UPDATE alerts SET status = status',
                  'DELETE FROM window_scores', 'TRUNCATE calls',
                  "INSERT INTO schema_migrations VALUES (99, 'x', repeat('0', 64))",
                  # Review M1: PUBLIC's default rights, revoked in this database.
                  'CREATE TEMP TABLE stolen_temp (x int)', 'SELECT lo_create(0)', 'SELECT lo_creat(-1)',
                  "SELECT lo_from_bytea(0, 'x'::bytea)", f'CREATE SCHEMA stolen_{self.role}']
        for statement in writes:
            with self.subTest(statement=statement), self.assertRaises(self.errors.InsufficientPrivilege):
                reader.execute(statement)
        for table in ('users', 'auth_sessions', 'audit_log', 'alert_feedback'):
            with self.subTest(table=table), self.assertRaises(self.errors.InsufficientPrivilege):
                reader.execute(f'SELECT * FROM {table}')
        sequences = reader.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                                   "WHERE n.nspname = %s AND c.relkind = 'S'", (self.schema,)).fetchall()
        self.assertTrue(sequences)
        for (name,) in sequences:
            with self.subTest(sequence=name), self.assertRaises(self.errors.InsufficientPrivilege):
                reader.execute('SELECT nextval(%s)', (f'{self.schema}.{name}',))

    def test_the_password_is_checked_and_rotated_and_never_sent_in_clear(self):
        owner = self.migrated_owner()
        ensure_history_reader(owner, self.role, PASSWORD)
        self.password_auth_or_skip()
        with self.psycopg.connect(DSN) as admin:
            stored = admin.execute('SELECT rolpassword FROM pg_authid WHERE rolname = %s', (self.role,)).fetchone()[0]
        self.assertRegex(stored, VERIFIER)
        self.assertNotIn(PASSWORD, stored)
        self.as_reader().close()
        result = ensure_history_reader(owner, self.role, NEW_PASSWORD)
        self.assertIs(result['created'], False)
        self.as_reader(NEW_PASSWORD).close()
        with self.assertRaises(self.psycopg.OperationalError):
            self.as_reader(PASSWORD)

    def test_the_real_history_reader_works_through_the_role(self):
        from services.store.events_pg import PostgresEventStore
        from services.store.history import PostgresCallHistory
        owner = self.migrated_owner()
        ensure_history_reader(owner, self.role, PASSWORD)
        store = PostgresEventStore(self.owner)
        store.record(make_envelope('call.started', 'call-r1', {}, seq=1, ts=1_763_500_000.0))
        history = PostgresCallHistory(lambda: self.as_reader())
        self.addCleanup(history.close)
        sessions = history.recent()
        self.assertEqual([s['call_id'] for s in sessions['calls']], ['call-r1'])
        self.assertEqual(history.call('call-r1')['sessions'][0]['call_id'], 'call-r1')

    def test_rerunning_removes_privileges_added_by_hand(self):
        owner = self.migrated_owner()
        ensure_history_reader(owner, self.role, PASSWORD)
        # DELETE needs only the table privilege (an INSERT would also need the id
        # sequence, which is revoked separately, and so could pass for the wrong reason).
        database = owner.execute('SELECT current_database()').fetchone()[0]
        for grant in ('DELETE ON calls', 'SELECT ON users', f'CREATE ON SCHEMA {self.schema}',
                      f'CREATE ON DATABASE {database}'):
            owner.execute(f'GRANT {grant} TO {self.role}')
        owner.execute(f"ALTER ROLE {self.role} SET work_mem = '64MB'")  # review M2: settings reset
        owner.commit()
        reader = self.as_reader()
        reader.autocommit = True
        reader.execute('SET default_transaction_read_only = off')
        reader.execute('DELETE FROM calls')  # control: the hand grants are in effect
        reader.execute('SELECT * FROM users')
        reader.execute('CREATE TABLE stolen_before (x int)')
        reader.execute('DROP TABLE stolen_before')
        reader.execute(f'CREATE SCHEMA stolen_before_{self.role}')
        reader.execute(f'DROP SCHEMA stolen_before_{self.role}')
        reader.close()
        ensure_history_reader(owner, self.role, PASSWORD)
        self.assertEqual(self.role_row()[8], ['default_transaction_read_only=on'], 'earlier settings are reset')
        reader = self.as_reader()
        reader.autocommit = True
        reader.execute('SET default_transaction_read_only = off')
        for statement in ('DELETE FROM calls', 'SELECT * FROM users', 'CREATE TABLE stolen (x int)',
                          f'CREATE SCHEMA stolen_{self.role}'):
            with self.subTest(statement=statement), self.assertRaises(self.errors.InsufficientPrivilege):
                reader.execute(statement)

    def test_unsafe_roles_are_refused_and_left_unchanged(self):
        owner = self.migrated_owner()
        with self.psycopg.connect(DSN, autocommit=True) as admin:
            user = admin.execute('SELECT current_user').fetchone()[0]
            cases = {}
            for kind, setup in (('super', 'CREATE ROLE {r} SUPERUSER LOGIN'),
                                ('replication', 'CREATE ROLE {r} REPLICATION LOGIN'),
                                ('member', 'CREATE ROLE {r} LOGIN; GRANT pg_read_all_data TO {r}'),
                                ('owner', 'CREATE ROLE {r} LOGIN')):
                role = f'{self.role}_{kind}'[:63]
                self.extra_roles.append(role)
                for statement in setup.format(r=role).split('; '):
                    admin.execute(statement)
                cases[kind] = role
            admin.execute(f'CREATE TABLE {self.schema}.owned_by_reader (x int)')
            admin.execute(f'ALTER TABLE {self.schema}.owned_by_reader OWNER TO {cases["owner"]}')
        expected = {'super': 'superuser', 'replication': 'replication', 'member': 'member of another role',
                    'owner': 'owns database objects'}
        for kind, role in cases.items():
            before = self.role_row(role)
            with self.subTest(kind=kind), self.assertRaises(RoleError) as caught:
                ensure_history_reader(owner, role, PASSWORD)
            self.assertEqual(caught.exception.reason, 'refused')
            self.assertIn(expected[kind], str(caught.exception))
            self.assertEqual(self.role_row(role), before, f'{kind}: nothing changed')
            self.assertEqual(owner.info.transaction_status.name, 'IDLE')
        with self.assertRaises(RoleError) as caught:
            ensure_history_reader(owner, user, PASSWORD)
        self.assertIn('connected (owner) user', str(caught.exception))

    def test_an_existing_role_with_rights_elsewhere_is_refused_and_left_unchanged(self):
        """Review M2: rights the per-schema revocations do not reach (another schema, its
        tables, functions, default privileges, another database) are refused, not kept."""
        owner = self.migrated_owner()
        other = f'{self.schema}_other'
        self.extra_schemas.append(other)
        with self.psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'CREATE SCHEMA {other}')
            admin.execute(f'CREATE TABLE {other}.t (x int)')
            admin.execute(f'CREATE FUNCTION {other}.f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$')
        cases = {'other schemas': 'GRANT USAGE ON SCHEMA {o} TO {r}',
                 'tables in other schemas': 'GRANT SELECT ON {o}.t TO {r}',
                 'functions': 'GRANT EXECUTE ON FUNCTION {o}.f() TO {r}',
                 'default privileges': 'ALTER DEFAULT PRIVILEGES IN SCHEMA {o} GRANT SELECT ON TABLES TO {r}',
                 'other databases': 'GRANT CONNECT ON DATABASE postgres TO {r}'}
        for index, (expected, grant) in enumerate(cases.items()):
            role = f'{self.role}_e{index}'
            self.extra_roles.append(role)
            ensure_history_reader(owner, role, PASSWORD)  # a clean reader first
            with self.psycopg.connect(DSN, autocommit=True) as admin:
                admin.execute(grant.format(o=other, r=role))
            with self.subTest(case=expected), self.assertRaises(RoleError) as caught:
                ensure_history_reader(owner, role, NEW_PASSWORD)
            self.assertEqual(caught.exception.reason, 'refused')
            self.assertIn(expected, str(caught.exception))
            self.assertEqual(owner.info.transaction_status.name, 'IDLE')
        # Nothing changed by the refusals: the first password still logs in (when checked).
        self.password_auth_or_skip(role=f'{self.role}_e0')
        self.as_reader(PASSWORD, role=f'{self.role}_e0').close()

    def test_a_large_object_owner_is_refused(self):
        owner = self.migrated_owner()
        ensure_history_reader(owner, self.role, PASSWORD)
        with self.psycopg.connect(DSN, autocommit=True) as admin:
            oid = admin.execute('SELECT lo_create(0)').fetchone()[0]
            self.addCleanup(self.unlink_large_object, oid)
            admin.execute(f'ALTER LARGE OBJECT {oid} OWNER TO {self.role}')
        with self.assertRaises(RoleError) as caught:
            ensure_history_reader(owner, self.role, PASSWORD)
        self.assertIn('owns database objects', str(caught.exception))

    def unlink_large_object(self, oid):
        with self.psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute('SELECT lo_unlink(%s) FROM pg_largeobject_metadata WHERE oid = %s', (oid, oid))

    def test_an_owner_without_the_rights_is_a_refusal_not_a_crash(self):
        """Review N3: a non-superuser owner without CREATEROLE gets a refusal (exit 2)."""
        weak, weak_schema = f'{self.role}_owner', f'{self.schema}_weak'
        self.extra_roles.append(weak)
        self.extra_schemas.append(weak_schema)
        with self.psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f"CREATE ROLE {weak} LOGIN PASSWORD '{PASSWORD}'")
            admin.execute(f'CREATE SCHEMA {weak_schema} AUTHORIZATION {weak}')
        connection = self.psycopg.connect(self.make_conninfo(DSN, user=weak, password=PASSWORD,
                                                             options=f'-c search_path={weak_schema}'))
        self.connections.append(connection)
        migrate.apply_migrations(connection)
        with self.assertRaises(RoleError) as caught:
            ensure_history_reader(connection, self.role, PASSWORD)
        self.assertEqual(caught.exception.reason, 'refused')
        self.assertIn('lacks the rights', str(caught.exception))
        self.assertIsNone(self.role_row(), 'no role created')

    def test_a_createrole_owner_that_cannot_revoke_the_public_rights_is_refused(self):
        """Re-review: a non-superuser owner with CREATEROLE gets only a WARNING from the
        PUBLIC revocations; the outcome check turns that into a refusal, not a success."""
        database = self.owner().execute('SELECT current_database()').fetchone()[0]
        with self.psycopg.connect(DSN, autocommit=True) as admin:  # PostgreSQL's defaults again
            admin.execute(f'GRANT TEMPORARY ON DATABASE {database} TO PUBLIC')
            admin.execute('GRANT EXECUTE ON FUNCTION pg_catalog.lo_creat(integer), pg_catalog.lo_create(oid), '
                          'pg_catalog.lo_from_bytea(oid, bytea) TO PUBLIC')
        weak, weak_schema = f'{self.role}_cr', f'{self.schema}_cr'
        self.extra_roles.append(weak)
        self.extra_schemas.append(weak_schema)
        with self.psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f"CREATE ROLE {weak} LOGIN CREATEROLE PASSWORD '{PASSWORD}'")
            admin.execute(f'CREATE SCHEMA {weak_schema} AUTHORIZATION {weak}')
        connection = self.psycopg.connect(self.make_conninfo(DSN, user=weak, password=PASSWORD,
                                                             options=f'-c search_path={weak_schema}'))
        self.connections.append(connection)
        migrate.apply_migrations(connection)
        with self.assertRaises(RoleError) as caught:
            ensure_history_reader(connection, self.role, PASSWORD)
        self.assertEqual(caught.exception.reason, 'refused')
        self.assertIn('could still create temporary tables', str(caught.exception))
        self.assertIsNone(self.role_row(), 'rolled back: no role created')
        # A superuser owner then revokes them, so the other tests' checks still hold.
        ensure_history_reader(self.migrated_owner(), self.role, PASSWORD)

    def test_per_database_settings_are_reset(self):
        owner = self.migrated_owner()
        ensure_history_reader(owner, self.role, PASSWORD)
        database = owner.execute('SELECT current_database()').fetchone()[0]
        owner.commit()
        with self.psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'ALTER ROLE {self.role} IN DATABASE {database} SET default_transaction_read_only = off')
        reader = self.as_reader()
        self.assertEqual(reader.execute('SHOW default_transaction_read_only').fetchone()[0], 'off', 'control')
        reader.close()
        ensure_history_reader(owner, self.role, PASSWORD)
        reader = self.as_reader()
        self.assertEqual(reader.execute('SHOW default_transaction_read_only').fetchone()[0], 'on')

    def test_an_unmigrated_schema_is_refused_and_no_role_is_created(self):
        owner = self.owner()
        with self.assertRaises(RoleError) as caught:
            ensure_history_reader(owner, self.role, PASSWORD)
        self.assertEqual(caught.exception.reason, 'schema')
        self.assertIn('calls', str(caught.exception))
        self.assertIsNone(self.role_row())
        migrate.apply_migrations(owner, migrate.load_migrations()[:1])  # 0001: the four tables exist
        self.assertIs(ensure_history_reader(owner, self.role, PASSWORD)['created'], True)

    def test_two_concurrent_runs_create_the_role_once(self):
        self.migrated_owner()
        barrier, results, errors = threading.Barrier(2), [], []

        def run():
            connection = self.owner()
            barrier.wait()
            try:
                results.append(ensure_history_reader(connection, self.role, PASSWORD)['created'])
            except Exception as error:  # pragma: no cover - reported below
                errors.append(error)
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, True])

    def test_the_command_line_migrates_then_sets_up_the_role(self):
        dsn = self.make_conninfo(DSN, options=f'-c search_path={self.schema}')
        environ = {'OWNER_URL': dsn, 'READER_PW': PASSWORD}
        argv = ['--database-url-env', 'OWNER_URL', 'reader-role', '--role', self.role, '--password-env',
                'READER_PW', '--connection-limit', '4']
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(main(argv, environ=environ, stdout=out, stderr=err), EXIT_OK, err.getvalue())
        record = json.loads(out.getvalue())
        self.assertEqual(record, {'event': 'reader_role', 'migrated': [m.version for m in migrate.load_migrations()],
                                  'role': self.role, 'created': True, 'schema': self.schema,
                                  'tables': list(READER_TABLES)})
        self.assertEqual(self.role_row()[7], 4)
        out = io.StringIO()
        self.assertEqual(main(argv, environ=environ, stdout=out, stderr=io.StringIO()), EXIT_OK)
        self.assertEqual((json.loads(out.getvalue())['migrated'], json.loads(out.getvalue())['created']), ([], False))
        for text in (out.getvalue(), err.getvalue()):
            self.assertNotIn(PASSWORD, text)
            self.assertNotIn(dsn, text)
        # An unsafe role is a refusal (exit 2) naming the reason, not a crash.
        err = io.StringIO()
        argv[argv.index(self.role)] = self.psycopg.conninfo.conninfo_to_dict(DSN).get('user', 'postgres')
        self.assertEqual(main(argv, environ=environ, stdout=io.StringIO(), stderr=err), EXIT_REFUSED)
        self.assertIn('refused:', json.loads(err.getvalue())['reason'])


if __name__ == '__main__':
    unittest.main()
