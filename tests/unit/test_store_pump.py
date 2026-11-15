"""Claude-authored tests (15 November 2026) for services/store/pump.py (backlog
5.1/5.2; PRD FR-I11, FR-A3). Authored by Claude (Claude Code), not by GPT-6 Astra.
Pure tests use a real EventHub and a scripted store; the PostgreSQL case runs only with
AVA_TEST_POSTGRES_DSN (see test_store_postgres.py) and stops the database connection
mid-stream to show the backlog catches up in order.
"""
import unittest

from services.events.fanout import EventHub
from services.store.events_pg import StoreError
from services.store.pump import StorePump
from tests.unit.test_store_postgres import DSN, REAL, T0


class ScriptedStore:
    """Records envelopes; `script` maps a seq to an exception raised once (or always)."""

    def __init__(self, script=None, always=(), faulty=()):
        self.recorded, self.script, self.always, self.faulty = [], dict(script or {}), set(always), set(faulty)

    def record(self, envelope):
        if envelope.seq in self.always:
            raise StoreError('refused', retryable=False)
        if envelope.seq in self.faulty:
            raise RuntimeError('driver bug')
        failure = self.script.pop(envelope.seq, None)
        if failure is not None:
            raise failure
        self.recorded.append(envelope.seq)
        return 'stored'


def publish(hub, count, start=0):
    for i in range(count):
        hub.publish('call.started', f'c{start + i}', {})


class StorePumpTests(unittest.TestCase):
    def test_in_order_and_outcomes_counted(self):
        hub = EventHub(clock=lambda: T0)
        store = ScriptedStore()
        pump = StorePump(hub.subscribe(), store)
        publish(hub, 5)
        self.assertEqual(pump.pump(), 5)
        self.assertEqual(store.recorded, [1, 2, 3, 4, 5])
        self.assertEqual((pump.counts['stored'], pump.backlog), (5, 0))

    def test_retryable_outage_keeps_order_and_catches_up(self):
        hub = EventHub(clock=lambda: T0)
        store = ScriptedStore({3: StoreError('down', retryable=True)})
        pump = StorePump(hub.subscribe(), store)
        publish(hub, 5)
        self.assertEqual(pump.pump(), 2)  # 1, 2 stored; 3 waits with 4, 5 behind it
        self.assertEqual((store.recorded, pump.backlog, pump.counts['retry_waits']), ([1, 2], 3, 1))
        publish(hub, 2, start=5)
        self.assertEqual(pump.pump(), 5)
        self.assertEqual(store.recorded, [1, 2, 3, 4, 5, 6, 7])

    def test_permanent_refusal_is_skipped_and_misconfiguration_waits(self):
        hub = EventHub(clock=lambda: T0)
        store = ScriptedStore({2: ValueError('connection has a transaction open')}, always={4})
        pump = StorePump(hub.subscribe(), store)
        publish(hub, 5)
        pump.pump()
        self.assertEqual((store.recorded, pump.counts['store_misconfigured']), ([1], 1))
        pump.pump()
        self.assertEqual(store.recorded, [1, 2, 3, 5])
        self.assertEqual((pump.counts['refused'], pump.backlog), (1, 0))

    def test_backlog_is_bounded_and_a_persistently_faulty_head_is_skipped(self):
        # Re-review findings 2 and 3: only what fits is taken from the hub (nothing is
        # dropped by the pump), and a deterministic fault cannot stall storage.
        hub = EventHub(clock=lambda: T0)
        store = ScriptedStore(faulty={1})
        pump = StorePump(hub.subscribe(buffer=4096), store, max_backlog=3)
        publish(hub, 6)
        for turn in range(4):
            self.assertEqual(pump.pump(), 0)  # nothing raised; the head waits
            self.assertEqual(pump.backlog, 3)
        self.assertEqual((pump.counts['faults'], pump.consecutive_failures), (4, 4))
        pump.pump()  # 5th fault: the head is skipped, the rest flow
        self.assertEqual(store.recorded, [2, 3, 4, 5, 6])
        self.assertEqual((pump.counts['skipped_faulty'], pump.counts['hub_dropped'], pump.consecutive_failures),
                         (1, 0, 0))

    def test_an_outage_leaves_new_events_in_the_hub_buffer(self):
        hub = EventHub(clock=lambda: T0)
        store = ScriptedStore({1: StoreError('down', retryable=True)})
        pump = StorePump(hub.subscribe(buffer=4), store, max_backlog=100)
        publish(hub, 2)
        pump.pump()  # 1 fails: 1 and 2 wait in the backlog
        publish(hub, 6, start=2)  # the hub buffer (4) keeps the newest 4, drops 2
        pump.pump()
        self.assertEqual(store.recorded, [1, 2, 5, 6, 7, 8])
        self.assertEqual(pump.counts['hub_dropped'], 2)
        with self.assertRaisesRegex(ValueError, 'unfiltered'):
            StorePump(hub.subscribe(call_id='c1'), store)
        for bad in ({'max_backlog': 0}, {'batch': 0}):
            with self.assertRaises(ValueError):
                StorePump(hub.subscribe(), store, **bad)


@REAL
class PostgresPumpTests(unittest.TestCase):
    def test_lost_connection_mid_stream_catches_up_in_order(self):
        import uuid

        import psycopg

        from services.store import migrate
        from services.store.events_pg import PostgresEventStore
        schema = f'ava_test_{uuid.uuid4().hex[:12]}'
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'CREATE SCHEMA {schema}')
        connections = []

        def connect():
            connection = psycopg.connect(DSN, options=f'-c search_path={schema}')
            connections.append(connection)
            return connection

        def cleanup():
            for connection in connections:
                connection.close()
            with psycopg.connect(DSN, autocommit=True) as admin:
                admin.execute(f'DROP SCHEMA {schema} CASCADE')
        self.addCleanup(cleanup)
        migrate.apply_migrations(connect())
        hub, store = EventHub(clock=lambda: T0), PostgresEventStore(connect)
        pump = StorePump(hub.subscribe(), store)
        hub.publish('call.started', 'p1', {})
        pump.pump()
        with psycopg.connect(DSN, autocommit=True) as admin:  # the store's connection dies
            admin.execute('SELECT pg_terminate_backend(%s)', (store._connection.info.backend_pid,))
        for window in range(3):
            hub.publish('score.updated', 'p1', {'score': 0.1, 'ema': 0.1, 'provisional': False,
                                                'direction': 'caller', 'window': window, 'scored': True,
                                                'state': 'monitoring', 'degraded': False,
                                                'effective_stride_s': 2.0})
        hub.publish('call.ended', 'p1', {'final_state': 'monitoring', 'reason': 'bye', 'speech_seconds': 6.0})
        pump.pump()  # the first write fails (retryable) and everything waits
        self.assertGreaterEqual(pump.counts['retry_waits'], 1)
        pump.pump()  # reconnected: caught up in order
        self.assertEqual(pump.backlog, 0)
        with psycopg.connect(DSN, options=f'-c search_path={schema}') as reader:
            rows = reader.execute('SELECT count(*) FROM window_scores').fetchone()[0]
            ended = reader.execute('SELECT end_reason FROM calls').fetchall()
        self.assertEqual((rows, ended), (3, [('bye',)]))


if __name__ == '__main__':
    unittest.main()
