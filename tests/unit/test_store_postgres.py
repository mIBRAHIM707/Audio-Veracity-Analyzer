"""Claude-authored tests (15 November 2026) for services/store (backlog 5.1; PRD FR-I11,
FR-A3; architecture §2.4, §2.7, ADR-7).

Authored by Claude (Claude Code), not by GPT-6 Astra. Two groups:
- pure tests (always run): migration file rules, the migration plan against a recorded
  history, store behaviour without a database (types not stored, connect failure);
- PostgreSQL tests, run only when AVA_TEST_POSTGRES_DSN names a disposable database
  (for example a local `postgres:16-alpine` container; see services/store/README.md).
  Each test works in its own schema, dropped afterwards. AVA_REQUIRE_POSTGRES=1 turns
  the skip into a failure. No audio is involved; these are storage checks.
"""
import os
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from services.events.contract import make_envelope
from services.store import migrate
from services.store.events_pg import PostgresEventStore, StoreError

DSN = os.environ.get('AVA_TEST_POSTGRES_DSN')
REAL = unittest.skipIf(not DSN and os.environ.get('AVA_REQUIRE_POSTGRES') != '1',
                       'set AVA_TEST_POSTGRES_DSN to a disposable PostgreSQL 16 database')
T0 = 1_763_200_000.0  # 15 Nov 2025 UTC, any fixed time


def env(event_type, call_id, data, seq, ts):
    return make_envelope(event_type, call_id, data, seq=seq, ts=ts)


def score(call_id, seq, ts, window, value, ema, state='monitoring', direction='caller', **extra):
    data = {'score': value, 'ema': ema, 'provisional': window == 0, 'direction': direction,
            'direction_state': state, 'direction_alert_id': None, 'window': window, 'scored': True,
            'state': state, 'driving_direction': direction, 'quality': 'ok', 'degraded': False,
            'effective_stride_s': 2.0, **extra}
    return env('score.updated', call_id, data, seq, ts)


def ended(call_id, seq, ts, final_state='monitoring', reason='bye', open_ids=None):
    return env('call.ended', call_id, {'final_state': final_state, 'reason': reason, 'speech_seconds': 7.5,
                                       'verdict': 'genuine', 'ended_open_alert_ids': open_ids or {}}, seq, ts)


class MigrationFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def write(self, name, content=b'CREATE TABLE x (a int);'):
        (self.dir / name).write_bytes(content)

    def test_repository_migrations_load_in_order(self):
        loaded = migrate.load_migrations()
        self.assertEqual([m.version for m in loaded], list(range(1, len(loaded) + 1)))
        self.assertEqual(loaded[0].name, '0001_live_records.sql')
        self.assertEqual(len(loaded[0].sha256), 64)
        self.assertIn('CREATE TABLE calls', loaded[0].sql)

    def test_valid_set_and_ignored_files(self):
        self.write('0002_second.sql')
        self.write('0001_first.sql')
        self.write('README.md', b'not sql')
        self.assertEqual([m.name for m in migrate.load_migrations(self.dir)], ['0001_first.sql', '0002_second.sql'])

    def test_refusals(self):
        cases = {
            'gap': [('0001_a.sql', None), ('0003_c.sql', None)],
            'starts at 2': [('0002_b.sql', None)],
            'duplicate version': [('0001_a.sql', None), ('0001_b.sql', None)],
            'bad name': [('0001_A.sql', None)],
            'no number': [('first.sql', None)],
            'empty': [('0001_a.sql', b'  \n')],
            'not utf-8': [('0001_a.sql', b'\xff\xfe CREATE')],
            'too large': [('0001_a.sql', b'-' * (migrate.MAX_FILE_BYTES + 1))],
        }
        for label, files in cases.items():
            with tempfile.TemporaryDirectory() as folder:
                for name, content in files:
                    Path(folder, name).write_bytes(content if content is not None else b'SELECT 1;')
                with self.assertRaises(migrate.MigrationError, msg=label):
                    migrate.load_migrations(folder)

    def test_crlf_checkout_hashes_like_lf(self):
        # Review M4 (15 Nov): a Windows checkout must match a database migrated from Linux.
        self.write('0001_a.sql', b'CREATE TABLE x (a int);\nCREATE TABLE y (b int);\n')
        lf = migrate.load_migrations(self.dir)[0]
        self.write('0001_a.sql', b'CREATE TABLE x (a int);\r\nCREATE TABLE y (b int);\r\n')
        crlf = migrate.load_migrations(self.dir)[0]
        self.assertEqual((crlf.sha256, crlf.sql), (lf.sha256, lf.sql))

    def test_transaction_control_refused_but_comments_allowed(self):
        # Review L6 (15 Nov): such statements would break the one-transaction apply.
        for sql in (b'BEGIN; CREATE TABLE x (a int);', b'CREATE TABLE x (a int);\nCOMMIT;',
                    b'CREATE TABLE x (a int); rollback;', b'CREATE INDEX CONCURRENTLY i ON x (a);',
                    b'SELECT 1;\n  VACUUM;', b'CREATE TABLE x (a int); END;', b'START TRANSACTION;',
                    b'CREATE TABLE x (a int); ABORT;', b"SELECT 1; PREPARE TRANSACTION 'x';"):
            with tempfile.TemporaryDirectory() as folder:
                Path(folder, '0001_a.sql').write_bytes(sql)
                with self.assertRaisesRegex(migrate.MigrationError, 'single migration transaction', msg=sql):
                    migrate.load_migrations(folder)
        self.write('0001_a.sql', b'-- COMMIT is fine in a comment\n/* BEGIN; */ CREATE TABLE ended (end_at int);')
        self.assertEqual(len(migrate.load_migrations(self.dir)), 1)

    def test_plan_against_recorded_history(self):
        a = migrate.Migration(1, '0001_a.sql', 'a' * 64, 'SELECT 1')
        b = migrate.Migration(2, '0002_b.sql', 'b' * 64, 'SELECT 2')
        self.assertEqual(migrate.plan([], [a, b]), [a, b])
        self.assertEqual(migrate.plan([(1, a.name, a.sha256)], [a, b]), [b])
        self.assertEqual(migrate.plan([(2, b.name, b.sha256), (1, a.name, a.sha256)], [a, b]), [])
        bad = {'changed hash': [(1, a.name, 'c' * 64)], 'renamed': [(1, '0001_x.sql', a.sha256)],
               'unknown version': [(1, a.name, a.sha256), (2, b.name, b.sha256), (3, 'z', 'z' * 64)],
               'gap in history': [(2, b.name, b.sha256)]}
        for label, history in bad.items():
            with self.assertRaises(migrate.MigrationError, msg=label):
                migrate.plan(history, [a, b])


class StoreWithoutDatabaseTests(unittest.TestCase):
    def test_health_is_not_stored_and_needs_no_connection(self):
        def connect():
            raise AssertionError('must not connect')
        store = PostgresEventStore(connect)
        self.assertEqual(store.record(env('system.health', None, {'reason': 'capacity'}, 1, T0)), 'not_stored')
        self.assertEqual(store.counts['not_stored'], 1)

    def test_connect_failure_becomes_store_error_and_is_retried(self):
        calls = []

        def connect():
            calls.append(1)
            raise OSError('refused')
        store = PostgresEventStore(connect)
        for _ in range(2):
            with self.assertRaisesRegex(StoreError, 'OSError') as caught:
                store.record(env('call.started', 'c1', {}, 1, T0))
            self.assertTrue(caught.exception.retryable)
        self.assertEqual((len(calls), store.failures, store.counts['stored']), (2, 2, 0))


@REAL
class PostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        self.psycopg = psycopg
        self.schema = f'ava_test_{uuid.uuid4().hex[:12]}'
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'CREATE SCHEMA {self.schema}')
        self.addCleanup(self.drop_schema)
        self.connections = []

    def drop_schema(self):
        for connection in self.connections:
            connection.close()
        with self.psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA {self.schema} CASCADE')

    def connect(self):
        connection = self.psycopg.connect(DSN, options=f'-c search_path={self.schema}')
        self.connections.append(connection)
        return connection

    def migrated_store(self):
        migrate.apply_migrations(self.connect())
        return PostgresEventStore(self.connect)

    def query(self, sql, params=()):
        with self.psycopg.connect(DSN, options=f'-c search_path={self.schema}') as connection:
            return connection.execute(sql, params).fetchall()

    # -- migrations ------------------------------------------------------------------

    def test_apply_is_idempotent_and_records_hashes(self):
        connection = self.connect()
        files = migrate.load_migrations()
        self.assertEqual(migrate.apply_migrations(connection), [m.version for m in files])
        self.assertEqual(migrate.apply_migrations(connection), [])
        rows = self.query('SELECT version, name, sha256 FROM schema_migrations ORDER BY version')
        self.assertEqual(rows, [(m.version, m.name, m.sha256) for m in files])
        self.assertEqual(rows[:2], [(1, '0001_live_records.sql', files[0].sha256),
                                    (2, '0002_inferred_end.sql', files[1].sha256)])
        tables = {r[0] for r in self.query('SELECT table_name FROM information_schema.tables WHERE table_schema = %s',
                                           (self.schema,))}
        self.assertEqual(tables, {'schema_migrations', 'calls', 'window_scores', 'alerts'})

    def test_changed_applied_migration_is_refused_without_changes(self):
        connection = self.connect()
        migrate.apply_migrations(connection)
        with self.psycopg.connect(DSN, options=f'-c search_path={self.schema}', autocommit=True) as other:
            other.execute("UPDATE schema_migrations SET sha256 = repeat('0', 64)")
        with self.assertRaisesRegex(migrate.MigrationError, 'changed after it was applied'):
            migrate.apply_migrations(connection)

    def test_failing_migration_leaves_nothing_behind(self):
        good = migrate.load_migrations()
        broken = migrate.Migration(len(good) + 1, f'{len(good) + 1:04d}_broken.sql', 'e' * 64,
                                   'CREATE TABLE half_done (a int); SELECT no_such_function();')
        connection = self.connect()
        with self.assertRaises(self.psycopg.Error):
            migrate.apply_migrations(connection, [*good, broken])
        connection.rollback()
        tables = {r[0] for r in self.query('SELECT table_name FROM information_schema.tables WHERE table_schema = %s',
                                           (self.schema,))}
        self.assertEqual(tables, set(), 'one transaction: neither 0001 nor the broken file may remain')
        self.assertEqual(migrate.apply_migrations(connection, good), [m.version for m in good])

    def test_concurrent_starts_apply_once(self):
        results, errors = [], []
        barrier = threading.Barrier(4)

        def worker():
            try:
                connection = self.connect()
                barrier.wait(timeout=10)
                results.append(migrate.apply_migrations(connection))
            except Exception as error:  # reported below
                errors.append(error)
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [[], [], [], [m.version for m in migrate.load_migrations()]])

    # -- event store -----------------------------------------------------------------

    def test_call_lifecycle_is_flushed_every_hop(self):
        store = self.migrated_store()
        events = [
            env('call.started', 'c1', {'from': 'lab'}, 1, T0),
            score('c1', 2, T0 + 4, 0, 0.2, 0.2),
            score('c1', 3, T0 + 6, 1, 0.9, 0.62, state='suspicious'),
            env('alert.raised', 'c1', {'alert_id': 'a1', 'direction': 'caller', 'ema': 0.8, 'window': 2}, 4, T0 + 8),
            score('c1', 5, T0 + 8, 2, 0.95, 0.8, state='alert'),
            env('alert.cleared', 'c1', {'alert_id': 'a1', 'direction': 'caller', 'ema': 0.5, 'window': 3}, 6, T0 + 10),
            score('c1', 7, T0 + 10, 3, 0.1, 0.5, state='suspicious'),
            env('alert.raised', 'c1', {'alert_id': 'a2', 'direction': 'caller', 'ema': 0.8, 'window': 4}, 8, T0 + 12),
            ended('c1', 9, T0 + 13, final_state='alert', open_ids={'caller': 'a2'}),
        ]
        self.assertEqual([store.record(e) for e in events], ['stored'] * len(events))
        call = self.query('SELECT call_id, implicit_start, start_data, state, final_state, end_reason, speech_seconds, '
                          'verdict, last_seq, extract(epoch FROM ended_at - started_at) FROM calls')
        self.assertEqual(call, [('c1', False, {'from': 'lab'}, 'alert', 'alert', 'bye', 7.5, 'genuine', 9, 13)])
        windows = self.query('SELECT window_idx, score, ema, call_state, provisional FROM window_scores '
                             'ORDER BY window_idx')
        self.assertEqual(windows, [(0, 0.2, 0.2, 'monitoring', True), (1, 0.9, 0.62, 'suspicious', False),
                                   (2, 0.95, 0.8, 'alert', False), (3, 0.1, 0.5, 'suspicious', False)])
        alerts = self.query('SELECT alert_id, status, raised_window, cleared_window, closed_at IS NULL '
                            'FROM alerts ORDER BY alert_id')
        self.assertEqual(alerts, [('a1', 'cleared', 2, 3, False), ('a2', 'ended_open', 4, None, False)])

    def test_mid_call_state_is_readable_after_each_hop(self):
        # FR-I11: a scorer restart must find the last flushed state while the call runs.
        store = self.migrated_store()
        store.record(env('call.started', 'c2', {}, 1, T0))
        store.record(score('c2', 2, T0 + 4, 0, 0.7, 0.7, state='suspicious', degraded=True, effective_stride_s=4.0))
        fresh = PostgresEventStore(self.connect)  # a new process would open its own connection
        self.assertEqual(fresh.record(score('c2', 3, T0 + 6, 1, 0.1, 0.4)), 'stored')
        rows = self.query('SELECT state, degraded, effective_stride_s, last_seq, ended_at FROM calls')
        self.assertEqual(rows, [('monitoring', False, 2.0, 3, None)])
        self.assertEqual(self.query('SELECT count(*) FROM window_scores'), [(2,)])

    def test_replays_are_idempotent_and_do_not_move_state_back(self):
        store = self.migrated_store()
        start = env('call.started', 'c3', {}, 1, T0)
        first, second = score('c3', 2, T0 + 4, 0, 0.9, 0.9, state='suspicious'), score('c3', 3, T0 + 6, 1, 0.1, 0.5)
        for event in (start, first, second):
            store.record(event)
        raised = env('alert.raised', 'c3', {'alert_id': 'r1', 'direction': 'caller', 'ema': 0.8, 'window': 1}, 4, T0 + 6)
        cleared = env('alert.cleared', 'c3', {'alert_id': 'r1', 'direction': 'caller', 'ema': 0.4, 'window': 2}, 5, T0 + 8)
        store.record(raised)
        store.record(cleared)
        self.assertEqual([store.record(e) for e in (start, first, raised, cleared)], ['duplicate'] * 4)
        self.assertEqual(self.query('SELECT state, last_seq FROM calls'), [('monitoring', 5)])
        self.assertEqual(self.query('SELECT count(*) FROM window_scores'), [(2,)])
        end = ended('c3', 6, T0 + 9)
        self.assertEqual(store.record(end), 'stored')
        # After the end, every replay (including the end and the start) is stale.
        self.assertEqual([store.record(e) for e in (start, first, raised, end)], ['after_end'] * 4)
        self.assertEqual(self.query('SELECT count(*) FROM calls'), [(1,)])
        self.assertEqual(store.counts['after_end'], 4)

    def test_implicit_start_reuse_after_end_status_and_unknown_alert(self):
        store = self.migrated_store()
        self.assertEqual(store.record(score('c4', 1, T0, 0, 0.3, 0.3)), 'stored')  # no call.started seen
        status = {'score': None, 'ema': None, 'provisional': False, 'direction': None, 'scored': False,
                  'state': 'no_speech', 'driving_direction': None, 'quality': 'ok', 'degraded': False,
                  'effective_stride_s': 2.0}
        self.assertEqual(store.record(env('score.updated', 'c4', status, 2, T0 + 12)), 'stored')
        self.assertEqual(self.query('SELECT implicit_start, state FROM calls'), [(True, 'no_speech')])
        self.assertEqual(self.query('SELECT count(*) FROM window_scores'), [(1,)])  # status: no window row
        self.assertEqual(store.record(env('alert.cleared', 'c4', {'alert_id': 'nope'}, 3, T0 + 13)), 'unknown_alert')
        store.record(ended('c4', 4, T0 + 14))
        self.assertEqual(store.record(score('c4', 5, T0 + 14, 1, 0.5, 0.4)), 'after_end')  # FR-I11 late result
        # Review M2 (15 Nov): a status published after the end, with a LATER time, must
        # not reopen the call (the registry ignores it too).
        self.assertEqual(store.record(env('score.updated', 'c4', status, 6, T0 + 30)), 'after_end')
        self.assertEqual(store.record(score('c4', 7, T0 + 31, 9, 0.5, 0.4)), 'after_end')
        self.assertEqual(store.record(env('call.started', 'c4', {}, 8, T0 + 14)), 'after_end')  # same time as the end
        self.assertEqual(self.query('SELECT count(*) FROM calls'), [(1,)])
        self.assertEqual(store.record(env('call.started', 'c4', {}, 9, T0 + 40)), 'stored')  # ID reused later
        rows = self.query('SELECT implicit_start, ended_at IS NULL FROM calls WHERE call_id = %s ORDER BY id', ('c4',))
        self.assertEqual(rows, [(True, False), (False, True)])

    def test_late_start_completes_an_implicit_start(self):
        # Review L1 (15 Nov): start data is kept. 19 Nov (Claude-authored change): a
        # session's start is published before its other events, so a late start carries
        # a time not later than the first event seen; one later than that is a new
        # session (see the supersede tests below).
        store = self.migrated_store()
        self.assertEqual(store.record(score('c8', 2, T0 + 4, 0, 0.3, 0.3)), 'stored')
        late_start = env('call.started', 'c8', {'from': 'lab'}, 1, T0 + 3)
        self.assertEqual(store.record(late_start), 'stored')
        self.assertEqual(store.record(late_start), 'duplicate')  # a replay
        self.assertEqual(store.record(env('call.started', 'c8', {'from': 'other'}, 3, T0 + 2)), 'duplicate')
        rows = self.query('SELECT implicit_start, start_data, extract(epoch FROM started_at), '
                          'extract(epoch FROM start_event_at), last_seq FROM calls')
        self.assertEqual(rows, [(False, {'from': 'lab'}, T0 + 3, T0 + 3, 2)])
        self.assertEqual(store.superseded, 0)

    def test_start_at_the_first_event_time_completes_an_implicit_start(self):
        # Claude-authored, 19 Nov: the boundary is strict (equal time = same session).
        store = self.migrated_store()
        store.record(score('c13', 2, T0 + 4, 0, 0.3, 0.3))
        self.assertEqual(store.record(env('call.started', 'c13', {}, 1, T0 + 4)), 'stored')
        self.assertEqual(self.query('SELECT implicit_start, ended_at FROM calls'), [(False, None)])
        self.assertEqual(store.superseded, 0)

    # -- abrupt live stop: a later start supersedes the open session (Claude-authored,
    #    19 Nov 2026; backlog 5.QA; PRD FR-A3, FR-I11; architecture §2.4) ------------

    def test_a_later_start_supersedes_a_session_that_never_ended(self):
        store = self.migrated_store()
        old = [env('call.started', 'k1', {'from': 'first'}, 1, T0),
               score('k1', 2, T0 + 4, 0, 0.9, 0.9, state='suspicious'),
               env('alert.raised', 'k1', {'alert_id': 'k1-a', 'direction': 'caller', 'ema': 0.85, 'window': 1},
                   3, T0 + 6),
               score('k1', 4, T0 + 6, 1, 0.95, 0.85, state='alert')]
        self.assertEqual([store.record(e) for e in old], ['stored'] * 4)
        # The scorer is killed (no call.ended). The restarted one has a new hub: seq 1 again.
        restart = env('call.started', 'k1', {'from': 'second'}, 1, T0 + 20)
        self.assertEqual(store.record(restart), 'stored')
        self.assertEqual((store.superseded, store.counts['stored']), (1, 5))
        rows = self.query('SELECT start_data, ended_at IS NULL, extract(epoch FROM ended_at), final_state, end_reason, '
                          'end_inferred, speech_seconds, verdict, end_data FROM calls ORDER BY id')
        self.assertEqual(rows, [({'from': 'first'}, False, T0 + 6, 'alert', 'monitor_lost', True, None, None, None),
                                ({'from': 'second'}, True, None, None, None, False, None, None, None)])
        self.assertEqual(self.query("SELECT status, extract(epoch FROM closed_at) FROM alerts"), [('ended_open', T0 + 6)])
        # The new session's events go to the new row; its window 0 is not a duplicate.
        self.assertEqual(store.record(score('k1', 2, T0 + 24, 0, 0.1, 0.1)), 'stored')
        self.assertEqual(store.record(restart), 'duplicate')  # a retried write of the same start
        self.assertEqual(store.superseded, 1)
        self.assertEqual(self.query('SELECT c.start_data, count(w.*) FROM calls c JOIN window_scores w ON w.call_pk = c.id '
                                    'GROUP BY c.id ORDER BY c.id'), [({'from': 'first'}, 2), ({'from': 'second'}, 1)])
        self.assertEqual(store.record(ended('k1', 3, T0 + 30)), 'stored')
        self.assertEqual(self.query('SELECT end_reason, end_inferred, speech_seconds FROM calls ORDER BY id'),
                         [('monitor_lost', True, None), ('bye', False, 7.5)])
        # A replay of the old session's start is after both ends.
        self.assertEqual(store.record(old[0]), 'after_end')

    def test_an_implicit_session_is_superseded_by_a_later_start(self):
        store = self.migrated_store()
        store.record(score('k2', 5, T0 + 4, 0, 0.3, 0.3))  # its start was never stored
        store.record(score('k2', 6, T0 + 6, 1, 0.2, 0.25))
        self.assertEqual(store.record(env('call.started', 'k2', {}, 1, T0 + 10)), 'stored')
        self.assertEqual(self.query('SELECT implicit_start, extract(epoch FROM ended_at), end_reason, end_inferred '
                                    'FROM calls ORDER BY id'),
                         [(True, T0 + 6, 'monitor_lost', True), (False, None, None, False)])
        self.assertEqual(store.superseded, 1)

    def test_superseded_end_is_never_after_the_new_start_and_equal_starts_do_not_supersede(self):
        store = self.migrated_store()
        store.record(env('call.started', 'k3', {}, 1, T0))
        self.assertEqual(store.record(env('call.started', 'k3', {'other': True}, 9, T0)), 'duplicate')  # same time
        self.assertEqual(store.record(env('call.started', 'k3', {}, 9, T0 - 1)), 'duplicate')  # earlier
        store.record(score('k3', 2, T0 + 30, 0, 0.3, 0.3))  # e.g. the clock stepped back before the restart
        store.record(env('alert.raised', 'k3', {'alert_id': 'k3-a', 'direction': 'caller', 'ema': 0.9, 'window': 0},
                         3, T0 + 30))
        self.assertEqual(store.record(env('call.started', 'k3', {}, 1, T0 + 20)), 'stored')
        self.assertEqual(self.query('SELECT extract(epoch FROM ended_at) FROM calls WHERE ended_at IS NOT NULL'),
                         [(T0 + 20,)])
        # Review L3: an alert is never closed before it was raised.
        self.assertEqual(self.query('SELECT status, extract(epoch FROM closed_at) FROM alerts'), [('ended_open', T0 + 30)])
        self.assertEqual(store.superseded, 1)

    def test_an_open_row_without_a_start_event_time_is_not_superseded(self):
        # Defensive: this code always writes `start_event_at` and migration 0002 fills it
        # for older open rows. Without it a later start cannot be told from a late
        # start of the same session, so it stays a duplicate.
        store = self.migrated_store()
        store.record(env('call.started', 'k4', {}, 1, T0))
        with self.psycopg.connect(DSN, options=f'-c search_path={self.schema}', autocommit=True) as other:
            other.execute('UPDATE calls SET start_event_at = NULL')
        self.assertEqual(store.record(env('call.started', 'k4', {}, 1, T0 + 20)), 'duplicate')
        self.assertEqual(self.query('SELECT count(*), bool_and(ended_at IS NULL) FROM calls'), [(1, True)])

    def test_upgrading_a_database_with_rows_to_migration_2(self):
        # Review M2: 0001 alone, rows written, then the rest; the data and the checks
        # survive, and an open row from before can be superseded by a re-tap.
        connection = self.connect()
        files = migrate.load_migrations()
        self.assertEqual(migrate.apply_migrations(connection, files[:1]), [1])
        with self.psycopg.connect(DSN, options=f'-c search_path={self.schema}') as old:
            old.execute("INSERT INTO calls (call_id, started_at, last_event_at, last_seq, ended_at, final_state, "
                        "end_reason, speech_seconds) VALUES ('u1', to_timestamp(%s), to_timestamp(%s), 4, "
                        "to_timestamp(%s), 'monitoring', 'bye', 12.5)", (T0, T0 + 9, T0 + 10))
            old.execute("INSERT INTO calls (call_id, started_at, last_event_at, last_seq) "
                        "VALUES ('u2', to_timestamp(%s), to_timestamp(%s), 3)", (T0, T0 + 5))
            old.execute("INSERT INTO calls (call_id, started_at, implicit_start, last_event_at, last_seq) "
                        "VALUES ('u3', to_timestamp(%s), TRUE, to_timestamp(%s), 2)", (T0 + 1, T0 + 5))
        self.assertEqual(migrate.apply_migrations(connection), [m.version for m in files[1:]])
        rows = self.query('SELECT call_id, end_reason, speech_seconds, end_inferred, '
                          'extract(epoch FROM start_event_at) FROM calls ORDER BY id')
        self.assertEqual(rows, [('u1', 'bye', 12.5, False, None), ('u2', None, None, False, T0),
                                ('u3', None, None, False, None)])
        names = {r[0] for r in self.query("SELECT conname FROM pg_constraint WHERE conrelid = 'calls'::regclass "
                                          "AND contype = 'c'")}
        self.assertNotIn('calls_check2', names)
        self.assertLessEqual({'calls_speech_seconds_reported', 'calls_inferred_end_shape'}, names)
        store = PostgresEventStore(self.connect)
        self.assertEqual(store.record(env('call.started', 'u2', {}, 1, T0 + 20)), 'stored')
        self.assertEqual(store.record(env('call.started', 'u3', {}, 1, T0 + 20)), 'stored')
        self.assertEqual(store.superseded, 2)
        self.assertEqual(self.query("SELECT call_id, end_reason, end_inferred FROM calls WHERE call_id <> 'u1' "
                                    'AND ended_at IS NOT NULL ORDER BY call_id'),
                         [('u2', 'monitor_lost', True), ('u3', 'monitor_lost', True)])

    def test_migration_2_allows_only_honest_inferred_ends(self):
        migrate.apply_migrations(self.connect())
        names = {r[0] for r in self.query("SELECT conname FROM pg_constraint WHERE conrelid = 'calls'::regclass "
                                          "AND contype = 'c'")}
        self.assertNotIn('calls_check2', names)
        self.assertLessEqual({'calls_check', 'calls_check1', 'calls_speech_seconds_reported',
                              'calls_inferred_end_shape'}, names)
        insert = ("INSERT INTO calls (call_id, started_at, last_event_at, last_seq, ended_at, final_state, end_reason, "
                  "speech_seconds, end_inferred) VALUES ('x', now(), now(), 1, now(), 'monitoring', %s, %s, %s)")
        refused = {'reported end without speech seconds': ('bye', None, False),
                   'inferred end with a reported reason': ('bye', None, True),
                   'inferred end with invented speech seconds': ('monitor_lost', 0.0, True)}
        for label, values in refused.items():
            with self.assertRaises(self.psycopg.errors.CheckViolation, msg=label):
                self.query(insert, values)
        with self.assertRaises(self.psycopg.errors.CheckViolation, msg='open row marked inferred'):
            self.query("INSERT INTO calls (call_id, started_at, last_event_at, last_seq, end_inferred) "
                       "VALUES ('y', now(), now(), 1, TRUE)")
        self.query(insert + ' RETURNING id', ('monitor_lost', None, True))
        self.query(insert.replace("'x'", "'z'") + ' RETURNING id', ('bye', 3.0, False))

    def test_connection_with_an_open_transaction_is_refused(self):
        # Review M1 (15 Nov): psycopg would nest a savepoint and commit nothing.
        migrate.apply_migrations(self.connect())

        def busy():
            connection = self.connect()
            connection.execute("SET application_name = 'ava-test'")  # opens a transaction
            return connection
        store = PostgresEventStore(busy)
        # Re-review B-L2: a misconfigured factory is not a bad envelope, so it is not a
        # StoreError a caller might answer by discarding events.
        with self.assertRaisesRegex(ValueError, 'transaction open'):
            store.record(env('call.started', 'c9', {}, 1, T0))
        self.assertEqual(self.query('SELECT count(*) FROM calls'), [(0,)])
        with self.assertRaisesRegex(migrate.MigrationError, 'idle'):
            migrate.apply_migrations(busy())

    def test_refused_envelopes_are_not_retryable(self):
        # Review M3 (15 Nov): values the database will never accept are permanent.
        store = self.migrated_store()
        cases = {'NUL in call ID': env('call.started', 'c\x00x', {}, 1, T0),
                 'milliseconds as seconds': env('call.started', 'c10', {}, 2, T0 * 1000),
                 'ema above 1': score('c11', 3, T0, 0, 0.5, 1.5)}
        for label, envelope in cases.items():
            with self.assertRaises(StoreError, msg=label) as caught:
                store.record(envelope)
            self.assertFalse(caught.exception.retryable, label)
        self.assertEqual(self.query('SELECT count(*) FROM calls'), [(0,)])
        self.assertEqual(store.record(env('call.started', 'c12', {}, 4, T0)), 'stored')

    def test_unscored_quality_window_is_stored_with_nulls(self):
        store = self.migrated_store()
        data = {'score': None, 'ema': None, 'provisional': False, 'direction': 'agent', 'direction_state': None,
                'direction_alert_id': None, 'window': 0, 'scored': False, 'concealed_fraction': 0.25,
                'state': 'monitoring', 'driving_direction': None, 'quality': 'low', 'degraded': False,
                'effective_stride_s': 2.0}
        self.assertEqual(store.record(env('score.updated', 'c5', data, 1, T0)), 'stored')
        self.assertEqual(self.query('SELECT direction, scored, score, ema, concealed_fraction, quality FROM window_scores'),
                         [('agent', False, None, None, 0.25, 'low')])

    def test_refused_write_rolls_back_and_the_store_recovers(self):
        store = self.migrated_store()
        bad = score('c6', 1, T0, 0, 0.5, 0.5, direction='operator')  # not a scored direction: CHECK fails
        with self.assertRaisesRegex(StoreError, 'CheckViolation') as caught:
            store.record(bad)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.query('SELECT count(*) FROM calls'), [(0,)], 'the implicit start was rolled back')
        self.assertEqual(store.record(score('c6', 2, T0 + 2, 0, 0.5, 0.5)), 'stored')
        self.assertEqual((store.failures, store.counts['stored']), (1, 1))

    def test_lost_connection_fails_one_write_then_reconnects(self):
        store = self.migrated_store()
        store.record(env('call.started', 'c7', {}, 1, T0))
        backend = store._connection.info.backend_pid
        with self.psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute('SELECT pg_terminate_backend(%s)', (backend,))
        with self.assertRaises(StoreError) as caught:
            store.record(score('c7', 2, T0 + 4, 0, 0.2, 0.2))
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(store.record(score('c7', 2, T0 + 4, 0, 0.2, 0.2)), 'stored')  # same envelope, retried
        self.assertNotEqual(store._connection.info.backend_pid, backend)
        self.assertEqual(self.query('SELECT count(*) FROM window_scores'), [(1,)])


if __name__ == '__main__':
    unittest.main()
