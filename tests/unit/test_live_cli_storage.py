"""Claude-authored tests (16 November 2026): event storage and Redis publishing in the
live service command line (backlog 5.1/5.2/8.1; PRD FR-I11 "flushed to PostgreSQL
every hop", FR-A4, FR-O4 start-up refusal; architecture §2.4). Authored by Claude
(Claude Code), not by GPT-6 Astra. Injected factories with a real EventHub and real
pump threads; the PostgreSQL case runs the real `_open_store` (migrations, psycopg)
only with AVA_TEST_POSTGRES_DSN (see test_store_postgres.py), the Redis case only with
AVA_TEST_REDIS_URL.
"""
import json
import os
import threading
import time
import unittest

from services.events.fanout import EventHub
from services.events.redis_bridge import CHANNEL
from services.live import __main__ as cli
from services.live.__main__ import EXIT_INTERRUPTED, EXIT_OK, EXIT_REFUSED
from services.store.migrate import MigrationError
from tests.unit.test_live_cli import BASE, SECRET, FakeService, Harness, run
from tests.unit.test_store_postgres import DSN, REAL

DB_SECRET = 'postgresql://ava:db-pw-91c2@db.internal:5432/ava'
REDIS_SECRET = 'redis://:redis-pw-3d8e@cache.internal:6379/0'
ENV = {'AVA_ARI_PASSWORD': SECRET, 'AVA_DB': DB_SECRET, 'AVA_REDIS': REDIS_SECRET}
# 17 Nov: the FR-O6 event log (on by default) writes JSON lines between the start and
# stop records; these tests index those two records by position, so it is off here
# (tests/unit/test_live_eventlog.py covers it).
QUIET = BASE + ['--event-log', 'off']
WITH_DB = QUIET + ['--database-url-env', 'AVA_DB']
WITH_REDIS = QUIET + ['--redis-url-env', 'AVA_REDIS']
REDIS_URL = os.environ.get('AVA_TEST_REDIS_URL')


class FakeStore:
    def __init__(self, block=None):
        self.recorded, self.closed, self.block = [], False, block

    def record(self, envelope):
        if self.block is not None:
            self.block.wait(30)
        self.recorded.append((envelope.type, envelope.call_id))
        return 'stored'

    def close(self):
        self.closed = True


class FakeRedis:
    def __init__(self, fail=False):
        self.published, self.closed, self.fail = [], False, fail

    def publish(self, channel, payload):
        if self.fail:
            raise ConnectionError('cache.internal:6379 refused')
        self.published.append((channel, json.loads(payload)['type']))
        return 1

    def close(self):
        self.closed = True


class HubService(FakeService):
    """The fake service with a real hub; `run` publishes like the scorer would and, at
    stop, the monitor_lost end the real service publishes in its `finally`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hub = EventHub()

    def run(self, stop):
        self.hub.publish('call.started', 'c1', {})
        if self.on_run:
            self.on_run(stop)
        assert stop.is_set()
        self.hub.publish('call.ended', 'c1', {'final_state': 'monitoring', 'reason': 'monitor_lost',
                                              'speech_seconds': 0.0})


class StorageHarness(Harness):
    def __init__(self, *, store=None, store_error=None, redis=None, redis_error=None, reachable=True, **kwargs):
        super().__init__(**kwargs)
        self.store, self.store_error = store or FakeStore(), store_error
        self.redis, self.redis_error, self.reachable = redis or FakeRedis(), redis_error, reachable
        self.dsns, self.urls = [], []

    def open_store(self, dsn):
        self.dsns.append(dsn)
        if self.store_error:
            raise self.store_error
        return self.store, [1]

    def open_redis(self, url):
        self.urls.append(url)
        if self.redis_error:
            raise self.redis_error
        return self.redis, self.reachable

    def service(self, *args, **kwargs):
        made = HubService(*args, **kwargs)
        made.on_run = self.on_run
        self.services.append(made)
        return made

    def factories(self):
        return {**super().factories(), 'store': self.open_store, 'redis': self.open_redis}


def lines_without_secrets(test, text):
    for secret in (SECRET, DB_SECRET, 'db-pw-91c2', REDIS_SECRET, 'redis-pw-3d8e', 'db.internal', 'cache.internal'):
        test.assertNotIn(secret, text)


class StorageCliTests(unittest.TestCase):
    def test_events_are_stored_and_published_including_the_final_end(self):
        harness = StorageHarness()
        code, lines, text = run(WITH_DB + ['--redis-url-env', 'AVA_REDIS'], harness, ENV)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual((harness.dsns, harness.urls), ([DB_SECRET], [REDIS_SECRET]))
        expected = [('call.started', 'c1'), ('call.ended', 'c1')]
        self.assertEqual(harness.store.recorded, expected)  # the end published at stop is drained
        self.assertEqual(harness.redis.published, [(CHANNEL, 'call.started'), (CHANNEL, 'call.ended')])
        self.assertTrue(harness.store.closed and harness.redis.closed)
        start, stopped = lines
        self.assertEqual((start['database_migrations_applied'], start['redis_reachable']), ([1], True))
        self.assertEqual((stopped['store']['counts']['stored'], stopped['store']['drained'],
                          stopped['store']['stuck']), (2, True, False))
        self.assertEqual(stopped['redis']['counts']['forwarded'], 2)
        lines_without_secrets(self, text)

    def test_without_the_options_nothing_is_opened_and_records_are_unchanged(self):
        harness = StorageHarness()
        code, lines, _ = run(QUIET, harness, ENV)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual([line['event'] for line in lines], ['starting', 'stopped'])
        self.assertEqual((harness.dsns, harness.urls), ([], []))
        self.assertNotIn('database_migrations_applied', lines[0])
        self.assertNotIn('redis_reachable', lines[0])
        self.assertFalse({'store', 'redis'} & set(lines[1]))

    def test_storage_refusals_exit_2_without_secrets_and_close_what_was_opened(self):
        class OperationalError(Exception):
            pass
        cases = {
            'database variable unset': (WITH_DB, StorageHarness(), {'AVA_ARI_PASSWORD': SECRET}),
            'database unreachable': (WITH_DB, StorageHarness(
                store_error=OperationalError(f'connection to {DB_SECRET} failed')), ENV),
            'driver missing': (WITH_DB, StorageHarness(store_error=ImportError('no psycopg', name='psycopg')), ENV),
            'migration drift': (WITH_DB, StorageHarness(store_error=MigrationError('migration 1 changed')), ENV),
            'redis variable unset': (WITH_REDIS, StorageHarness(), {'AVA_ARI_PASSWORD': SECRET}),
            'redis url invalid': (WITH_REDIS, StorageHarness(redis_error=ValueError(REDIS_SECRET)), ENV),
            'redis driver missing': (WITH_REDIS, StorageHarness(redis_error=ImportError('x', name='redis')), ENV),
        }
        for label, (argv, harness, environ) in cases.items():
            with self.subTest(label):
                code, lines, text = run(argv, harness, environ)
                self.assertEqual((code, [line['event'] for line in lines]), (EXIT_REFUSED, ['refused']))
                self.assertEqual(harness.services, [])
                lines_without_secrets(self, text)
        reason = run(WITH_DB, StorageHarness(store_error=OperationalError(DB_SECRET)), ENV)[1][0]['reason']
        self.assertEqual(reason, 'database unreachable or refused the login: OperationalError')
        self.assertIn('migration 1 changed',
                      run(WITH_DB, StorageHarness(store_error=MigrationError('migration 1 changed')), ENV)[1][0]['reason'])
        # A refusal after the store and Redis opened (ARI credentials) closes both.
        from services.tap.ari import AriError
        harness = StorageHarness(ari_error=AriError('HTTP 401', 401))
        self.assertEqual(run(WITH_DB + ['--redis-url-env', 'AVA_REDIS'], harness, ENV)[0], EXIT_REFUSED)
        self.assertTrue(harness.store.closed and harness.redis.closed)

    def test_unreachable_redis_starts_and_counts_failures(self):
        harness = StorageHarness(redis=FakeRedis(fail=True), reachable=False)
        code, lines, text = run(WITH_REDIS, harness, ENV)
        self.assertEqual(code, EXIT_OK)
        self.assertFalse(lines[0]['redis_reachable'])
        self.assertEqual(lines[1]['redis']['counts']['publish_failures'], 2)
        self.assertEqual(lines[1]['redis']['last_fault'], None)  # the forwarder contains its own faults
        lines_without_secrets(self, text)

    def test_a_hung_database_never_holds_scoring_or_shutdown(self):
        release = threading.Event()
        self.addCleanup(release.set)
        harness = StorageHarness(store=FakeStore(block=release))
        published = []

        def scoring(stop):
            started = time.monotonic()
            for i in range(200):  # the scorer keeps publishing while the store is hung
                harness.services[0].hub.publish('call.started', f'x{i}', {})
            published.append(time.monotonic() - started)
            stop.set()
        harness.on_run = scoring
        started = time.monotonic()
        code, lines, _ = run(WITH_DB + ['--drain-timeout', '0.3'], harness, ENV)
        self.assertEqual(code, EXIT_OK)
        self.assertLess(published[0], 1.0)
        self.assertLess(time.monotonic() - started, 5.0)  # drain 0.3 s + join grace 1 s
        self.assertTrue(lines[1]['store']['stuck'])
        self.assertFalse(harness.store.closed)  # still in use by the stuck thread
        release.set()

    def test_forced_exit_skips_the_drain(self):
        import signal
        harness = StorageHarness()

        def stalled(stop):
            signal.raise_signal(signal.SIGINT)
            signal.raise_signal(signal.SIGINT)
        harness.on_run = stalled
        code, lines, _ = run(WITH_DB, harness, ENV)
        self.assertEqual(code, EXIT_INTERRUPTED)
        self.assertEqual((lines[-1]['forced'], lines[-1]['store']['drain_turns']), (True, 0))

    def test_database_outage_at_stop_is_reported_not_called_drained(self):
        # Review M2: the stop record says what was left, not "drained".
        from services.store.events_pg import StoreError

        class DownStore(FakeStore):
            def record(self, envelope):
                raise StoreError('database down', retryable=True)
        harness = StorageHarness(store=DownStore())
        code, lines, _ = run(WITH_DB + ['--drain-timeout', '0.3'], harness, ENV)
        self.assertEqual(code, EXIT_OK)
        store = lines[1]['store']
        self.assertEqual((store['drained'], store['left_pending'], store['backlog']), (False, 2, 2))
        self.assertGreater(store['consecutive_failures'], 0)
        self.assertGreater(store['counts']['retry_waits'], 0)

    def test_ctrl_c_during_the_drain_forces_the_exit_with_a_record(self):
        # Review M4: a second Ctrl+C while the pumps drain is a forced exit, not a traceback.
        import signal
        release = threading.Event()
        self.addCleanup(release.set)
        harness = StorageHarness(store=FakeStore(block=release))
        timer = []

        def interrupt_soon(stop):
            # Fires while main() joins the (blocked) store pump after run() returned.
            timer.append(threading.Timer(0.3, signal.raise_signal, (signal.SIGINT,)))
            stop.set()
        harness.on_run = interrupt_soon
        original = cli._stop_pumps

        def stop_pumps(pumps, drain, code):
            timer[0].start()
            return original(pumps, drain, code)
        from unittest import mock
        started = time.monotonic()
        with mock.patch.object(cli, '_stop_pumps', stop_pumps):
            code, lines, text = run(WITH_DB + ['--drain-timeout', '30'], harness, ENV)
        self.assertLess(time.monotonic() - started, 10)  # handled promptly, not after the 30 s drain
        self.assertEqual(code, EXIT_INTERRUPTED)
        self.assertEqual((lines[-1]['event'], lines[-1]['forced']), ('stopped', True))
        self.assertNotIn('Traceback', text)

    def test_a_loop_exception_still_stops_the_pumps_and_writes_the_record(self):
        # Review L7.
        harness = StorageHarness()

        def broken(stop):
            raise RuntimeError('loop bug')
        harness.on_run = broken
        code, lines, _ = run(WITH_DB, harness, ENV)
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertEqual((lines[-1]['event'], lines[-1]['error'], lines[-1]['store']['alive']),
                         ('stopped', 'RuntimeError', False))
        self.assertTrue(harness.store.closed)

    def test_unexpected_start_up_errors_close_what_was_opened(self):
        # Review L5: any Redis client error is refused by type name; Ctrl+C after the
        # store opened still closes it.
        harness = StorageHarness(redis_error=RuntimeError(REDIS_SECRET))
        code, lines, text = run(WITH_DB + ['--redis-url-env', 'AVA_REDIS'], harness, ENV)
        self.assertEqual((code, lines[0]['reason']), (EXIT_REFUSED, 'Redis client: RuntimeError'))
        self.assertTrue(harness.store.closed)
        lines_without_secrets(self, text)
        harness = StorageHarness()
        harness.ari = lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
        code, lines, _ = run(WITH_DB, harness, ENV)
        self.assertEqual((code, lines[0]['event']), (EXIT_INTERRUPTED, 'interrupted'))
        self.assertTrue(harness.store.closed)

    def test_bad_drain_timeout_is_a_usage_error(self):
        import contextlib
        import io
        for value in ('0', '-1', 'nan', 'soon', '301', 'inf'):
            with self.subTest(value), self.assertRaises(SystemExit) as raised, \
                    contextlib.redirect_stderr(io.StringIO()):
                cli.main(BASE + ['--drain-timeout', value], environ=ENV, factories=StorageHarness().factories())
            self.assertEqual(raised.exception.code, 2)
        for name in ('', 'A-B', '1X'):
            with self.subTest(name), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                cli.main(BASE + ['--database-url-env', name], environ=ENV, factories=StorageHarness().factories())


@REAL
class RealPostgresCliTests(unittest.TestCase):
    def test_real_store_applies_migrations_and_keeps_the_events(self):
        import uuid

        import psycopg
        from psycopg.conninfo import make_conninfo
        schema = f'ava_test_{uuid.uuid4().hex[:12]}'
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'CREATE SCHEMA {schema}')

        def drop():
            with psycopg.connect(DSN, autocommit=True) as admin:
                admin.execute(f'DROP SCHEMA {schema} CASCADE')
        self.addCleanup(drop)
        dsn = make_conninfo(DSN, options=f'-c search_path={schema}')
        harness = Harness()
        harness.service = lambda *a, **k: harness.services.append(HubService(*a, **k)) or harness.services[-1]
        factories = {**harness.factories(), 'store': cli._open_store}
        environ = {'AVA_ARI_PASSWORD': SECRET, 'AVA_DB': dsn}
        stop = threading.Event()
        stop.set()
        import io
        err = io.StringIO()
        code = cli.main(WITH_DB, environ=environ, factories=factories, stop_event=stop, stderr=err)
        self.assertEqual(code, EXIT_OK, err.getvalue())
        start, stopped = [json.loads(line) for line in err.getvalue().splitlines()]
        self.assertEqual(start['database_migrations_applied'], [1])
        self.assertEqual(stopped['store']['counts']['stored'], 2)
        with psycopg.connect(dsn) as reader:
            rows = reader.execute('SELECT call_id, end_reason FROM calls').fetchall()
        self.assertEqual(rows, [('c1', 'monitor_lost')])
        # A second start finds the schema current.
        err = io.StringIO()
        self.assertEqual(cli.main(WITH_DB, environ=environ, factories=factories, stop_event=stop, stderr=err), EXIT_OK)
        self.assertEqual(json.loads(err.getvalue().splitlines()[0])['database_migrations_applied'], [])
        self.assertNotIn(dsn, err.getvalue())

    def test_store_sessions_are_time_bounded_and_a_held_migration_lock_refuses(self):
        # Review M3: statement/lock timeouts on the store's connections; a migration lock
        # held elsewhere refuses the start within the lock timeout instead of hanging.
        import io
        import uuid
        from unittest import mock

        import psycopg
        from psycopg.conninfo import make_conninfo

        from services.events.contract import make_envelope
        from services.store.migrate import LOCK_KEY
        schema = f'ava_test_{uuid.uuid4().hex[:12]}'
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'CREATE SCHEMA {schema}')

        def drop():
            with psycopg.connect(DSN, autocommit=True) as admin:
                admin.execute(f'DROP SCHEMA {schema} CASCADE')
        self.addCleanup(drop)
        dsn = make_conninfo(DSN, options=f'-c search_path={schema}')
        store, applied = cli._open_store(dsn)
        self.addCleanup(store.close)
        self.assertEqual(applied, [1])
        self.assertEqual(store.record(make_envelope('call.started', 'c1', {}, seq=1, ts=1_763_200_000.0)), 'stored')
        connection = store._connection
        self.assertEqual(connection.execute('SHOW statement_timeout').fetchone()[0], '10s')
        self.assertEqual(connection.execute('SHOW lock_timeout').fetchone()[0], '10s')
        self.assertEqual(connection.execute('SHOW search_path').fetchone()[0], schema)  # options kept
        connection.rollback()
        holder = psycopg.connect(DSN)
        self.addCleanup(holder.close)
        holder.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_KEY,))  # held until rollback
        harness = Harness()
        harness.service = lambda *a, **k: harness.services.append(HubService(*a, **k)) or harness.services[-1]
        stop, err = threading.Event(), io.StringIO()
        stop.set()
        started = time.monotonic()
        with mock.patch.object(cli, 'MIGRATION_LOCK_TIMEOUT_MS', 500):
            code = cli.main(WITH_DB, environ={'AVA_ARI_PASSWORD': SECRET, 'AVA_DB': dsn},
                            factories={**harness.factories(), 'store': cli._open_store}, stop_event=stop,
                            stderr=err)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn('LockNotAvailable', err.getvalue())
        self.assertNotIn(dsn, err.getvalue())


@unittest.skipIf(not REDIS_URL, 'set AVA_TEST_REDIS_URL to a disposable Redis 7')
class RealRedisCliTests(unittest.TestCase):
    def test_real_redis_receives_the_events(self):
        import io

        import redis
        listener = redis.Redis.from_url(REDIS_URL, socket_timeout=5).pubsub()
        listener.subscribe(CHANNEL)
        self.addCleanup(listener.close)
        listener.get_message(timeout=2)  # the subscribe confirmation
        harness = Harness()
        harness.service = lambda *a, **k: harness.services.append(HubService(*a, **k)) or harness.services[-1]
        factories = {**harness.factories(), 'redis': cli._open_redis}
        stop = threading.Event()
        stop.set()
        err = io.StringIO()
        code = cli.main(WITH_REDIS, environ={'AVA_ARI_PASSWORD': SECRET, 'AVA_REDIS': REDIS_URL},
                        factories=factories, stop_event=stop, stderr=err)
        self.assertEqual(code, EXIT_OK, err.getvalue())
        types = []
        deadline = time.monotonic() + 5
        while len(types) < 2 and time.monotonic() < deadline:
            message = listener.get_message(timeout=0.5)
            if message and message['type'] == 'message':
                types.append(json.loads(message['data'])['type'])
        self.assertEqual(types, ['call.started', 'call.ended'])
        self.assertTrue(json.loads(err.getvalue().splitlines()[0])['redis_reachable'])


if __name__ == '__main__':
    unittest.main()
