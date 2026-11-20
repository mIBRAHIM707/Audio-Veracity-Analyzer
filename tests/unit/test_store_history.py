"""Claude-authored tests (19 November 2026) for the read-only PostgreSQL call history
(services/store/history.py), its API routes (`GET /v1/calls/{id}/history`,
`GET /v1/history/calls`) and the `--database-url-env` option of `python -m services.api`
(backlog 5.1/5.2; PRD FR-A3 "full history in PostgreSQL"; architecture §2.5, §2.7).

Authored by Claude (Claude Code), not by GPT-6 Astra. Groups:
- pure tests (always run): argument checks, connection failures, a factory that hands
  over a connection inside a transaction;
- API and CLI tests with a fake history (need requirements-api.txt);
- PostgreSQL tests, run only when AVA_TEST_POSTGRES_DSN names a disposable database
  (see services/store/README.md; AVA_REQUIRE_POSTGRES=1 makes the skip a failure). The
  rows are written by the real `PostgresEventStore` from FR-A4 envelopes, so these
  check the reader against what the writer actually stores, including a session
  superseded after an abrupt scorer stop. Each test uses its own schema.
Storage evidence only: no audio and no detector are involved.
"""
import io
import json
import os
import threading
import unittest
import unittest.mock
import uuid

from services.events.contract import make_envelope
from services.inference import analysis
from services.store import history as history_module
from services.store import migrate
from services.store.events_pg import PostgresEventStore
from services.store.history import HistoryError, PostgresCallHistory, valid_call_id
from tests.unit import test_analysis_api as api_fixture

TestClient = api_fixture.TestClient
if TestClient is not None:
    from services.api import app as app_module
    from services.api.__main__ import EXIT_OK, EXIT_REFUSED, main
    from services.api.app import create_app

DSN = os.environ.get('AVA_TEST_POSTGRES_DSN')
REAL = unittest.skipIf(not DSN and os.environ.get('AVA_REQUIRE_POSTGRES') != '1',
                       'set AVA_TEST_POSTGRES_DSN to a disposable PostgreSQL 16 database')
T0 = 1_763_500_000.0  # any fixed time (Nov 2025 UTC)
SECRET_DSN = 'postgresql://ava:hunter2@secret-host:5432/ava'


def env(event_type, call_id, data, seq, ts):
    return make_envelope(event_type, call_id, data, seq=seq, ts=ts)


def score(call_id, seq, ts, window, value, ema, state='monitoring', direction='caller', **extra):
    data = {'score': value, 'ema': ema, 'provisional': window == 0, 'direction': direction,
            'direction_state': state, 'direction_alert_id': None, 'window': window, 'scored': value is not None,
            'state': state, 'driving_direction': direction, 'quality': 'ok', 'degraded': False,
            'effective_stride_s': 2.0, **extra}
    return env('score.updated', call_id, data, seq, ts)


def ended(call_id, seq, ts, final_state='monitoring', reason='bye'):
    return env('call.ended', call_id, {'final_state': final_state, 'reason': reason, 'speech_seconds': 7.5,
                                       'verdict': 'genuine', 'ended_open_alert_ids': {}}, seq, ts)


# -- pure -----------------------------------------------------------------------------

class FakeInfo:
    def __init__(self, status):
        self.transaction_status = type('Status', (), {'name': status})()


class InTransactionConnection:
    """A misconfigured factory's connection: a transaction is already open."""

    def __init__(self):
        self.info, self.closed, self.close_calls = FakeInfo('INTRANS'), False, 0

    def close(self):
        self.closed, self.close_calls = True, self.close_calls + 1


class HistoryWithoutDatabaseTests(unittest.TestCase):
    def test_call_ids_the_store_could_never_hold_are_not_looked_up(self):
        def connect():
            raise AssertionError('must not connect')
        history = PostgresCallHistory(connect)
        for call_id in ('', 'x' * 129, 'a\x00b', None, 7):
            self.assertIsNone(history.call(call_id), repr(call_id))
        self.assertTrue(valid_call_id('x' * 128))
        self.assertTrue(valid_call_id('ٹیسٹ-کال'))  # Urdu text is a valid ID

    def test_argument_checks(self):
        history = PostgresCallHistory(lambda: None)
        for kwargs in ({'status': 'open'}, {'limit': 0}, {'limit': 201}, {'limit': True}, {'limit': 2.0},
                       {'before': 0}, {'before': -1}, {'before': 2 ** 63}, {'before': '5'}):
            with self.assertRaises(ValueError, msg=kwargs):
                history.recent(**kwargs)
        for kwargs in ({'max_sessions': 0}, {'max_alerts': 0}, {'max_windows': 0}, {'max_windows': 1.5}):
            with self.assertRaises(ValueError, msg=kwargs):
                PostgresCallHistory(lambda: None, **kwargs)

    def test_a_connect_failure_is_unavailable_and_retried_on_the_next_read(self):
        attempts = []

        def connect():
            attempts.append(1)
            raise OSError('refused by secret-host')
        history = PostgresCallHistory(connect)
        for _ in range(2):
            with self.assertRaises(HistoryError) as caught:
                history.call('c1')
            self.assertEqual(caught.exception.reason, 'unavailable')
            self.assertNotIn('secret-host', str(caught.exception), 'only the error class is reported')
        self.assertEqual((len(attempts), history.failures), (2, 2))

    def test_a_read_waits_a_bounded_time_for_the_connection_then_is_busy(self):
        # Review M2 (19 Nov): waiting readers must not pile up in the API's thread pool.
        def connect():
            raise AssertionError('a busy read never connects')
        history = PostgresCallHistory(connect, wait_s=0.05)
        history._lock.acquire()  # another read in progress
        try:
            with self.assertRaises(HistoryError) as caught:
                history.recent()
        finally:
            history._lock.release()
        self.assertEqual(caught.exception.reason, 'busy')

    def test_close_during_a_read_closes_the_connection_when_the_read_ends(self):
        # Review L6: shutdown does not wait long on a read in progress.
        connection = InTransactionConnection()
        history = PostgresCallHistory(lambda: connection, wait_s=1)
        history._connection = connection
        history._lock.acquire()  # a read in progress
        with unittest.mock.patch.object(history_module, 'CLOSE_WAIT_S', 0.05):
            history.close()
        self.assertEqual(connection.close_calls, 0, 'not closed under the reader')
        history._lock.release()
        with self.assertRaises(HistoryError) as caught:
            history.recent()  # the next read finds the history closed and closes the connection
        self.assertEqual((caught.exception.reason, connection.close_calls), ('unavailable', 1))

    def test_non_finite_numbers_are_served_as_null(self):
        self.assertEqual([history_module._num(v) for v in (float('nan'), float('inf'), 0.5, 3, None)],
                         [None, None, 0.5, 3, None])

    def test_a_connection_inside_a_transaction_is_refused_and_closed(self):
        made = []

        def connect():
            made.append(InTransactionConnection())
            return made[-1]
        history = PostgresCallHistory(connect)
        with self.assertRaises(HistoryError) as caught:
            history.recent()
        self.assertEqual(caught.exception.reason, 'error')
        self.assertEqual((made[0].close_calls, history.failures), (1, 1))
        with self.assertRaises(HistoryError):
            history.recent()
        self.assertEqual(len(made), 2, 'a fresh connection is asked for after a refusal')


# -- API routes and command line with a fake history ---------------------------------

class FakeHistory:
    def __init__(self, calls=None, error=None):
        self.calls, self.error, self.seen = calls or {}, error, []

    def call(self, call_id):
        self.seen.append(('call', call_id))
        if self.error:
            raise self.error
        return self.calls.get(call_id)

    def recent(self, status='all', limit=50, before=None):
        self.seen.append(('recent', status, limit, before))
        if self.error:
            raise self.error
        return {'calls': [], 'next_before': None}


@unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
class HistoryRouteTests(unittest.TestCase):
    def make(self, history=None, hub=None):
        service = analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')
        client = TestClient(create_app(service, hub=hub, history=history))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def test_without_a_history_store_the_routes_answer_503(self):
        client = self.make()
        for path in ('/v1/calls/c1/history', '/v1/history/calls'):
            response = client.get(path)
            self.assertEqual((response.status_code, response.json()['detail']),
                             (503, 'No call history store is configured'), path)
        self.assertNotIn('ava_call_history_reads_total', client.get('/metrics').text)

    def test_found_missing_and_paged_reads_without_a_live_hub(self):
        stored = {'c1': {'call_id': 'c1', 'sessions': [], 'sessions_total': 1, 'sessions_truncated': False}}
        fake = FakeHistory(stored)
        client = self.make(fake)
        self.assertEqual(client.get('/v1/calls/c1/history').json(), stored['c1'])
        self.assertEqual(client.get('/v1/calls/none/history').status_code, 404)
        self.assertEqual(client.get('/v1/calls/c1').status_code, 503, 'the live registry still needs a hub')
        self.assertEqual(client.get('/v1/history/calls?status=ended&limit=10&before=99').json(),
                         {'calls': [], 'next_before': None})
        self.assertIn(('recent', 'ended', 10, 99), fake.seen)
        # A call ID that is literally "history" is a detail lookup, not the history route.
        self.assertEqual(app_module.route_label('/v1/calls/history'), '/v1/calls/:id')
        self.assertEqual(app_module.route_label('/v1/calls/x/history'), '/v1/calls/:id/history')
        metrics = client.get('/metrics').text
        self.assertIn('ava_call_history_reads_total{outcome="ok"} 2\n', metrics)
        self.assertIn('ava_call_history_reads_total{outcome="not_found"} 1\n', metrics)
        self.assertIn('ava_api_requests_total{route="/v1/calls/:id/history",status="4xx"} 1', metrics)
        self.assertIn('ava_api_requests_total{route="/v1/history/calls",status="2xx"} 1', metrics)

    def test_bad_parameters_are_422_before_any_read(self):
        fake = FakeHistory()
        client = self.make(fake)
        for query in ('status=open', 'limit=0', 'limit=201', 'limit=x', 'before=0', 'before=-3',
                      f'before={2 ** 63}', 'before=1.5'):
            self.assertEqual(client.get(f'/v1/history/calls?{query}').status_code, 422, query)
        self.assertEqual(fake.seen, [])

    def test_store_failures_are_503_with_a_fixed_reason_and_counted(self):
        for reason in history_module.REASONS:
            client = self.make(FakeHistory(error=HistoryError('OperationalError at secret-host', reason=reason)))
            for path in ('/v1/calls/c1/history', '/v1/history/calls'):
                response = client.get(path)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()['detail'], f'Call history store unavailable ({reason})')
                self.assertNotIn('secret-host', response.text)
            self.assertIn(f'ava_call_history_reads_total{{outcome="{reason}"}} 2\n', client.get('/metrics').text)


    def test_an_unencodable_answer_is_a_counted_503_not_a_500(self):
        # Re-review L1: e.g. a later column type JSON cannot hold.
        from decimal import Decimal
        client = self.make(FakeHistory({'c1': {'call_id': 'c1', 'odd': Decimal('1.5')}}))
        response = client.get('/v1/calls/c1/history')
        self.assertEqual((response.status_code, response.json()['detail']),
                         (503, 'Call history store unavailable (error)'))
        self.assertIn('ava_call_history_reads_total{outcome="error"} 1\n', client.get('/metrics').text)

    def test_history_requests_beyond_the_cap_are_busy_not_queued(self):
        # Re-review L2: waiting history reads must not take every worker thread.
        entered, release = threading.Event(), threading.Event()

        class Blocking(FakeHistory):
            def call(self, call_id):
                entered.set()
                release.wait(10)
                return {'call_id': call_id, 'sessions': []}
        with unittest.mock.patch.object(app_module, 'MAX_HISTORY_REQUESTS', 1):
            client = self.make(Blocking())
        first = []
        worker = threading.Thread(target=lambda: first.append(client.get('/v1/calls/c1/history').status_code))
        worker.start()
        try:
            self.assertTrue(entered.wait(10))
            second = client.get('/v1/calls/c2/history')
            self.assertEqual((second.status_code, second.json()['detail']),
                             (503, 'Call history store unavailable (busy)'))
            self.assertEqual(client.get('/healthz').status_code, 200)
        finally:
            release.set()
            worker.join(10)
        self.assertEqual(first, [200])
        self.assertEqual(client.get('/v1/calls/c3/history').status_code, 200, 'the slot is free again')


@unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
class HistoryCliTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.model = Path(folder.name)
        (self.model / 'model.safetensors').write_bytes(b'')

    def run_main(self, extra, environ, opener, serve=lambda app, sock: None):
        stderr = io.StringIO()
        service = lambda args: analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')  # noqa: E731
        code = main(['--model-root', str(self.model), '--port', '0', '--event-log', 'off',
                     '--database-url-env', 'AVA_DATABASE_URL', *extra], environ=environ, stderr=stderr,
                    made={'service': service, 'history': opener, 'serve': serve})
        return code, [json.loads(line) for line in stderr.getvalue().splitlines()], stderr.getvalue()

    def test_refusals_never_print_the_url(self):
        def raises(error):
            def opener(dsn):
                raise error
            return opener
        env = {'AVA_DATABASE_URL': SECRET_DSN}
        cases = [({}, raises(AssertionError('not called')), 'AVA_DATABASE_URL environment variable'),
                 (env, raises(ValueError(SECRET_DSN)), 'database URL is not valid'),
                 (env, raises(ImportError('psycopg', name='psycopg')), 'requirements-store.txt'),
                 (env, raises(RuntimeError(SECRET_DSN)), 'database client: RuntimeError')]
        for environ, opener, expected in cases:
            code, records, text = self.run_main([], environ, opener)
            self.assertEqual((code, len(records), records[0]['event']), (EXIT_REFUSED, 1, 'refused'), expected)
            self.assertIn(expected, records[0]['reason'])
            self.assertNotIn('hunter2', text)
            self.assertNotIn('secret-host', text)

    def test_the_history_is_served_and_closed_and_reachability_reported(self):
        closed = []

        class Closable(FakeHistory):
            def close(self):
                closed.append(1)
        fake = Closable({'c1': {'call_id': 'c1', 'sessions': []}})
        seen = {}

        def serve(app, sock):
            with TestClient(app) as client:
                seen['status'] = client.get('/v1/calls/c1/history').status_code
                seen['closed_while_serving'] = len(closed)
        for reachable in (True, 'schema'):
            closed.clear()
            code, records, _ = self.run_main([], {'AVA_DATABASE_URL': SECRET_DSN}, lambda dsn: (fake, reachable),
                                             serve)
            self.assertEqual(code, EXIT_OK)
            self.assertEqual((records[0]['call_history'], records[0]['history_reachable']), (True, reachable))
            self.assertEqual((seen, len(closed)), ({'status': 200, 'closed_while_serving': 0}, 1))


# -- PostgreSQL -----------------------------------------------------------------------

@REAL
class PostgresHistoryTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        self.psycopg = psycopg
        self.schema = f'ava_test_{uuid.uuid4().hex[:12]}'
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'CREATE SCHEMA {self.schema}')
        self.connections = []
        self.addCleanup(self.drop_schema)

    def drop_schema(self):
        for connection in self.connections:
            connection.close()
        with self.psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA {self.schema} CASCADE')

    def connect(self):
        connection = self.psycopg.connect(DSN, options=f'-c search_path={self.schema}')
        self.connections.append(connection)
        return connection

    def store(self, *envelopes):
        if not hasattr(self, '_store'):
            migrate.apply_migrations(self.connect())
            self._store = PostgresEventStore(self.connect)
        return [self._store.record(e) for e in envelopes]

    def history(self, **kwargs):
        history = PostgresCallHistory(self.connect, **kwargs)
        self.addCleanup(history.close)
        return history

    def killed_and_retapped_call(self):
        """Session 1: start, two windows, an alert, then the scorer is killed (no end).
        Session 2 (the re-tap): a later start, two windows from index 0, a status-only
        update, an end. Start data carries a caller number that must never be served."""
        outcomes = self.store(
            env('call.started', 'k1', {'from': '+923001234567'}, 1, T0),
            score('k1', 2, T0 + 4, 0, 0.2, 0.2),
            score('k1', 3, T0 + 6, 1, 0.9, 0.55, state='alert'),
            env('alert.raised', 'k1', {'alert_id': 'k1-a1', 'direction': 'caller', 'ema': 0.55, 'window': 1}, 4,
                T0 + 6),
            env('call.started', 'k1', {'from': '+923001234567'}, 1, T0 + 20),
            score('k1', 2, T0 + 24, 0, 0.1, 0.1),
            score('k1', 3, T0 + 26, 0, 0.3, 0.2, direction='agent'),
            score('k1', 4, T0 + 26, 1, None, None, quality='low'),
            ended('k1', 5, T0 + 30))
        self.assertEqual(outcomes, ['stored'] * 9)

    def test_every_session_with_alerts_and_windows_in_time_order(self):
        self.killed_and_retapped_call()
        result = self.history().call('k1')
        self.assertEqual((result['call_id'], result['sessions_total'], result['sessions_truncated']), ('k1', 2, False))
        new, old = result['sessions']
        self.assertGreater(new['session'], old['session'])
        self.assertEqual((old['status'], old['end_reason'], old['end_inferred'], old['speech_seconds'],
                          old['verdict'], old['started_ts'], old['ended_ts']),
                         ('ended', 'monitor_lost', True, None, None, T0, T0 + 6))
        self.assertEqual(old['alerts'], [{'alert_id': 'k1-a1', 'direction': 'caller', 'state': 'ended_open',
                                          'raised_ts': T0 + 6, 'raised_window': 1, 'raised_ema': 0.55,
                                          'closed_ts': T0 + 6, 'cleared_window': None, 'cleared_ema': None}])
        self.assertEqual([(w['window'], w['ts'], w['score'], w['ema'], w['state'], w['provisional'])
                          for w in old['windows']],
                         [(0, T0 + 4, 0.2, 0.2, 'monitoring', True), (1, T0 + 6, 0.9, 0.55, 'alert', False)])
        self.assertEqual((new['status'], new['end_reason'], new['end_inferred'], new['speech_seconds'],
                          new['verdict'], new['final_state'], new['started_seen']),
                         ('ended', 'bye', False, 7.5, 'genuine', 'monitoring', True))
        # Same timestamp: ordered by direction, then window index. The unscored low-quality
        # window keeps its row with null score and EMA.
        self.assertEqual([(w['direction'], w['window'], w['scored'], w['score'], w['quality'])
                          for w in new['windows']],
                         [('caller', 0, True, 0.1, 'ok'), ('agent', 0, True, 0.3, 'ok'),
                          ('caller', 1, False, None, 'low')])
        self.assertEqual((new['windows_total'], new['windows_truncated'], new['alerts'], new['alerts_total']),
                         (3, False, [], 0))
        self.assertNotIn('+923001234567', json.dumps(result), 'start/end data are never served')
        self.assertIsNone(self.history().call('never-stored'))

    def test_detected_codec_is_served_and_nothing_else_from_start_data(self):
        """Claude-authored, 20 Nov (FR-U5): `call.started` data `codec` as the writer stores
        it; only a format-name JSON string is served, by both reads."""
        cases = {'d1': ({'codec': 'alaw', 'from_number': '+923001234567', 'tap_format': 'ulaw'}, 'alaw'),
                 'd2': ({'codec': None}, None), 'd3': ({}, None), 'd4': ({'codec': '<b>x</b>'}, None),
                 'd5': ({'codec': True}, None), 'd6': ({'codec': ['alaw']}, None), 'd7': ({'codec': 'ulaw|alaw'},
                                                                                        'ulaw|alaw')}
        for k, (call_id, (data, _)) in enumerate(cases.items()):
            self.assertEqual(self.store(env('call.started', call_id, data, 1, T0 + k)), ['stored'])
        self.assertEqual(self.store(score('d8', 1, T0 + 9, 0, 0.2, 0.2)), ['stored'])  # implicit start: no data
        history = self.history()
        for call_id, (_, served) in cases.items():
            result = history.call(call_id)
            self.assertEqual(result['sessions'][0]['codec'], served, call_id)
            self.assertNotIn('+923001234567', json.dumps(result))
        self.assertIsNone(history.call('d8')['sessions'][0]['codec'])
        listed = {row['call_id']: row['codec'] for row in history.recent(limit=20)['calls']}
        self.assertEqual(listed, {**{c: s for c, (_, s) in cases.items()}, 'd8': None})

    def test_bounds_keep_the_newest_sessions_and_the_oldest_windows(self):
        self.killed_and_retapped_call()
        self.store(env('alert.raised', 'k1', {'alert_id': 'late'}, 9, T0 + 40))  # after the end: not stored
        result = self.history(max_sessions=1, max_windows=2, max_alerts=1).call('k1')
        self.assertEqual((result['sessions_total'], result['sessions_truncated'], len(result['sessions'])), (2, True, 1))
        session = result['sessions'][0]
        self.assertEqual((session['end_reason'], session['windows_total'], session['windows_truncated']),
                         ('bye', 3, True))
        self.assertEqual([(w['direction'], w['window']) for w in session['windows']], [('caller', 0), ('agent', 0)])
        # Review M1: the window budget is shared by the whole answer, newest session first.
        both = self.history(max_windows=4).call('k1')['sessions']
        self.assertEqual([(len(s['windows']), s['windows_total'], s['windows_truncated']) for s in both],
                         [(3, 3, False), (1, 2, True)])
        spent = self.history(max_windows=3).call('k1')['sessions']
        self.assertEqual([(len(s['windows']), s['windows_truncated']) for s in spent], [(3, False), (0, True)])
        self.store(env('call.started', 'm1', {}, 1, T0),
                   *[env('alert.raised', 'm1', {'alert_id': f'm1-a{i}'}, 2 + i, T0 + 1 + i) for i in range(3)])
        alerts = self.history(max_alerts=2).call('m1')['sessions'][0]
        self.assertEqual(([a['alert_id'] for a in alerts['alerts']], alerts['alerts_total'],
                          alerts['alerts_truncated']), (['m1-a0', 'm1-a1'], 3, True))

    def test_recent_sessions_page_newest_first_with_filters_and_counts(self):
        self.killed_and_retapped_call()  # two ended sessions of k1
        self.store(env('call.started', 'open1', {}, 1, T0 + 50), score('open1', 2, T0 + 54, 0, 0.8, 0.8),
                   env('alert.raised', 'open1', {'alert_id': 'o-a1'}, 3, T0 + 54),
                   env('call.started', 'done1', {}, 1, T0 + 60), ended('done1', 2, T0 + 61))
        history = self.history()
        first = history.recent(limit=2)
        self.assertEqual([(c['call_id'], c['status']) for c in first['calls']], [('done1', 'ended'),
                                                                                 ('open1', 'active')])
        self.assertEqual(first['next_before'], first['calls'][-1]['session'])
        second = history.recent(limit=2, before=first['next_before'])
        self.assertEqual([(c['call_id'], c['end_reason']) for c in second['calls']],
                         [('k1', 'bye'), ('k1', 'monitor_lost')])
        self.assertIsNone(second['next_before'], 'no further page')
        self.assertEqual(history.recent(limit=2, before=second['calls'][-1]['session']),
                         {'calls': [], 'next_before': None})
        active = history.recent(status='active')['calls']
        self.assertEqual([(c['call_id'], c['alerts_open'], c['alerts_total'], c['windows_total'], c['score_max'])
                          for c in active], [('open1', 1, 1, 1, 0.8)])
        ended_calls = history.recent(status='ended')['calls']
        self.assertEqual([c['call_id'] for c in ended_calls], ['done1', 'k1', 'k1'])
        k1_new = ended_calls[1]
        self.assertEqual((k1_new['windows_total'], k1_new['windows_scored'], k1_new['score_max']), (3, 2, 0.3))
        self.assertNotIn('windows', k1_new, 'summaries carry counts, not rows')

    def test_reads_cannot_write(self):
        self.killed_and_retapped_call()
        history = self.history()
        with self.assertRaises(HistoryError) as caught:
            history._read(lambda db: db.execute("DELETE FROM calls"))  # the guarantee itself, not a route
        self.assertIn('ReadOnlySqlTransaction', str(caught.exception))
        self.assertEqual(self.query_count(), 2)
        self.assertEqual(len(history.call('k1')['sessions']), 2, 'the next read reconnects and works')

    def query_count(self):
        with self.psycopg.connect(DSN, options=f'-c search_path={self.schema}') as connection:
            return connection.execute('SELECT count(*) FROM calls').fetchone()[0]

    def test_an_unmigrated_or_partly_migrated_database_is_a_schema_failure(self):
        history = self.history()
        with self.assertRaises(HistoryError) as caught:
            history.recent()
        self.assertEqual(caught.exception.reason, 'schema')  # no tables at all
        migrate.apply_migrations(self.connect(), migrate.load_migrations()[:1])
        with self.assertRaises(HistoryError) as caught:
            history.call('x')
        self.assertEqual(caught.exception.reason, 'schema')  # 0001 only: no end_inferred yet
        migrate.apply_migrations(self.connect())
        self.assertEqual(history.recent(), {'calls': [], 'next_before': None})

    def test_a_lost_connection_is_unavailable_then_recovers(self):
        self.killed_and_retapped_call()
        history = self.history()
        self.assertEqual(len(history.recent()['calls']), 2)
        pid = history._connection.info.backend_pid
        with self.psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute('SELECT pg_terminate_backend(%s)', (pid,))
        with self.assertRaises(HistoryError) as caught:
            history.recent()
        self.assertEqual(caught.exception.reason, 'unavailable')
        self.assertEqual(len(history.recent()['calls']), 2)
        self.assertNotEqual(history._connection.info.backend_pid, pid)

    def test_a_slow_statement_or_a_read_past_its_deadline_is_a_timeout(self):
        # Review L2/M2: neither is reported as "unavailable"; the next read works.
        self.killed_and_retapped_call()
        history = self.history(statement_timeout_ms=50)
        with self.assertRaises(HistoryError) as caught:
            history._read(lambda db: db.execute('SELECT pg_sleep(2)'))
        self.assertEqual(caught.exception.reason, 'timeout')
        self.assertIn('QueryCanceled', str(caught.exception))
        self.assertEqual(len(history.recent()['calls']), 2)
        ticks = iter([0.0] + [1_000.0] * 50)  # the deadline is set at 0 s, then time has passed
        late = self.history(clock=lambda: next(ticks))
        with self.assertRaises(HistoryError) as caught:
            late.call('k1')
        self.assertEqual(caught.exception.reason, 'timeout')

    def test_concurrent_reads_take_turns_and_all_succeed(self):
        self.killed_and_retapped_call()
        history = self.history(wait_s=30)
        results, errors = [], []

        def read():
            try:
                results.append(len(history.call('k1')['sessions']))
            except Exception as error:  # reported below
                errors.append(error)
        threads = [threading.Thread(target=read) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual((errors, results), ([], [2] * 6))

    def test_the_command_line_factory_sets_timeouts_and_probes(self):
        # Review L4: the real connection factory of `python -m services.api`.
        from psycopg.conninfo import make_conninfo
        from services.api import __main__ as cli
        dsn = make_conninfo(DSN, options=f'-c search_path={self.schema}')
        history, reachable = cli._open_history(dsn)
        self.addCleanup(history.close)
        self.assertEqual(reachable, 'schema', 'an unmigrated database is reported, not refused')
        self.store(env('call.started', 'f1', {}, 1, T0))
        history, reachable = cli._open_history(dsn)
        self.addCleanup(history.close)
        self.assertIs(reachable, True)
        settings = history._read(lambda db: [db.execute(f'SHOW {name}').fetchone()[0]
                                             for name in ('lock_timeout', 'transaction_read_only')])
        self.assertEqual(settings, ['10s', 'on'])
        self.assertEqual(history._connection.info.transaction_status.name, 'IDLE')
        with self.assertRaises(ValueError):
            cli._open_history("host='unterminated")  # does not parse

    @unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
    def test_the_api_serves_the_stored_history(self):
        self.killed_and_retapped_call()
        service = analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')
        with TestClient(create_app(service, history=self.history())) as client:
            body = client.get('/v1/calls/k1/history').json()
            self.assertEqual([s['end_reason'] for s in body['sessions']], ['bye', 'monitor_lost'])
            listed = client.get('/v1/history/calls?status=ended&limit=1').json()
            self.assertEqual(len(listed['calls']), 1)
            following = client.get(f'/v1/history/calls?status=ended&limit=1&before={listed["next_before"]}').json()
            self.assertEqual(following['calls'][0]['end_reason'], 'monitor_lost')
            self.assertEqual(client.get('/v1/calls/none/history').status_code, 404)


if __name__ == '__main__':
    unittest.main()
