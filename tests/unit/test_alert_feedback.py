"""Claude-authored tests (19 November 2026, attempt 8) for PRD FR-A7 alert feedback:
services/store/feedback.py, migration 0004, `PostgresAuthStore.transaction`, and the
API routes `POST`/`GET /v1/alerts/{id}/feedback` (services/api/app.py, __main__.py).

Authored by Claude (Claude Code), not by GPT-6 Astra. Groups:
- pure tests (always run): label and ID rules, the window span shared with the FR-A6
  peak, checks made before any database work;
- API tests with a fake feedback store and the fake account store of
  test_local_auth (need requirements-api.txt): sessions, refusals, audit rows, metrics;
- PostgreSQL tests, run only when AVA_TEST_POSTGRES_DSN names a disposable database
  (AVA_REQUIRE_POSTGRES=1 makes the skip a failure): alerts written by the real
  `PostgresEventStore` from FR-A4 envelopes, then labelled; the window copy, the
  one-transaction audit, relabelling, the cap, schema checks, constraints, and the API
  end to end with real accounts (also needs requirements-auth.txt).
Storage evidence only: no audio and no detector are involved, and a label is not
evidence that an alert was right.
"""
import json
import re
import threading
import unittest
import unittest.mock
import uuid

from services.auth.store import AuthStoreError, Principal
from services.store import feedback as feedback_module
from services.store import history as history_module
from services.store.feedback import LABELS, AlertFeedbackStore, check_feedback
from tests.unit import test_local_auth as auth_fixture
from tests.unit import test_store_history as base
from tests.unit.test_store_history import DSN, REAL, T0, env, ended, score

TestClient = base.TestClient
if TestClient is not None:
    from services.api.app import create_app, route_label
    from services.inference import analysis
    from tests.unit import test_analysis_api as api_fixture
API = unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')

ANA = Principal(1, 'ana', 'analyst', 1e9)
FAKE_HASH = '$argon2id$v=19$m=8,t=1,p=1$c2FsdHNhbHQ$aGFzaGhhc2hoYXNo'  # shape only: nobody logs in with it


# -- pure -----------------------------------------------------------------------------

class RecordingAuthStore:
    """Counts transactions; a feedback call that is refused early never reaches it."""

    def __init__(self):
        self.transactions = 0

    def transaction(self, work, *, required_migration):
        self.transactions += 1
        raise AssertionError('no database work expected')


class RuleTests(unittest.TestCase):
    def test_labels_and_alert_ids(self):
        self.assertEqual(LABELS, ('true_positive', 'false_positive'))
        for label in LABELS:
            check_feedback('a1', label)
        check_feedback('x' * 128, 'true_positive')
        check_feedback('انتباہ:۱', 'false_positive')  # any text the alerts table accepts
        for label in ('TRUE_POSITIVE', 'tp', 'true positive', '', None, 1, True, ['true_positive']):
            with self.assertRaisesRegex(ValueError, 'label'):
                check_feedback('a1', label)
        for alert_id in ('', 'x' * 129, 'a\x00b', None, 7):
            with self.assertRaisesRegex(ValueError, 'alert ID'):
                check_feedback(alert_id, 'true_positive')

    def test_the_window_span_is_the_one_the_alert_peak_reads(self):
        # FR-A6's confidence (history.PEAK_EMA) and the FR-A7 copy must cover the same windows.
        for clause in ('(a.direction IS NULL OR w.direction = a.direction)', 'w.scored_at >= a.raised_at',
                       'w.scored_at <= COALESCE(a.closed_at, c.last_event_at)', 'w.call_pk = a.call_pk'):
            self.assertIn(clause, feedback_module.SPAN)
            self.assertIn(clause, history_module.PEAK_EMA)

    def test_refusals_happen_before_any_database_work(self):
        store = RecordingAuthStore()
        feedback = AlertFeedbackStore(store)
        with self.assertRaises(ValueError):
            feedback.record(ANA, 'a1', 'maybe')
        with self.assertRaises(ValueError):
            feedback.record(ANA, 'x' * 129, 'true_positive')
        with self.assertRaisesRegex(ValueError, 'audit actor'):
            feedback.record(Principal(9, 'Not A Name', 'analyst', 1e9), 'a1', 'true_positive')
        for limit in (0, 101, True, 1.0):
            with self.assertRaises(ValueError):
                feedback.list('a1', limit)
        with self.assertRaises(ValueError):
            feedback.list('', 10)
        self.assertEqual(store.transactions, 0)
        for cap in (0, -1, feedback_module.MAX_WINDOWS + 1, True, 2.0):
            with self.assertRaises(ValueError):
                AlertFeedbackStore(store, max_windows=cap)

    def test_the_account_store_transaction_needs_a_migration_of_at_least_0003(self):
        from services.auth.store import PostgresAuthStore

        def connect():  # pragma: no cover - must not be reached
            raise AssertionError('no connection expected')
        store = PostgresAuthStore(connect)
        for version in (2, 0, True, '4', 4.0, None):
            with self.assertRaises(ValueError):
                store.transaction(lambda db: None, required_migration=version)

    @API
    def test_route_labels_stay_bounded(self):
        self.assertEqual(route_label('/v1/alerts/0b6a-uuid/feedback'), '/v1/alerts/:id/feedback')
        self.assertEqual(route_label('/v1/alerts'), '/v1/alerts')
        self.assertEqual(route_label('/v1/alerts/0b6a-uuid'), 'other')
        for path in ('/v1/alerts/a/b/feedback', '/v1/alerts//feedback', '/v1/alerts/feedback'):
            self.assertEqual(route_label(path), 'other', path)


# -- API with fakes -------------------------------------------------------------------

class FakeFeedback:
    def __init__(self):
        self.calls, self.fail, self.labels = [], None, {}

    def record(self, principal, alert_id, label):
        self.calls.append(('record', principal.username, alert_id, label))
        if self.fail:
            raise AuthStoreError('down', reason=self.fail)
        if alert_id not in self.labels:
            raise AuthStoreError('no such alert', reason='not_found')
        self.labels[alert_id].insert(0, label)
        return {'feedback_id': len(self.calls), 'alert_id': alert_id, 'label': label,
                'username': principal.username, 'ts': 5.0, 'alert_state': 'cleared', 'windows_stored': 3,
                'windows_total': 3}

    def list(self, alert_id, limit=100):
        self.calls.append(('list', alert_id))
        if self.fail:
            raise AuthStoreError('down', reason=self.fail)
        if alert_id not in self.labels:
            raise AuthStoreError('no such alert', reason='not_found')
        labels = self.labels[alert_id]
        return {'alert_id': alert_id, 'current': labels[0] if labels else None,
                'feedback': [{'label': label} for label in labels], 'feedback_total': len(labels),
                'feedback_truncated': False}


@API
class FeedbackApiTests(unittest.TestCase):
    def make(self, with_feedback=True):
        self.auth, self.store, _, _ = auth_fixture.authenticator()
        self.feedback = FakeFeedback()
        self.feedback.labels['a1'] = []
        service = analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')
        app = create_app(service, auth=self.auth, feedback=self.feedback if with_feedback else None)
        client = TestClient(app, base_url='https://testserver')
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def login(self, client):
        response = client.post('/v1/auth/login', json={'username': 'ana', 'password': auth_fixture.PASSWORD})
        self.assertEqual(response.status_code, 200)

    def audits(self, action='alerts.feedback'):
        return [row for row in self.store.audits if row[1] in (action, 'api.request')]

    def test_both_routes_need_a_session(self):
        client = self.make()
        self.assertEqual(client.post('/v1/alerts/a1/feedback', json={'label': 'true_positive'}).status_code, 401)
        self.assertEqual(client.get('/v1/alerts/a1/feedback').status_code, 401)
        self.assertEqual(self.feedback.calls, [])

    def test_a_label_is_stored_and_the_route_writes_no_second_audit_row(self):
        client = self.make()
        self.login(client)
        response = client.post('/v1/alerts/a1/feedback', json={'label': 'false_positive'})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['label'], 'false_positive')
        self.assertEqual(self.feedback.calls, [('record', 'ana', 'a1', 'false_positive')])
        # The store writes the ok row in the label's transaction; the middleware adds none.
        self.assertEqual(self.audits(), [])
        listed = client.get('/v1/alerts/a1/feedback')
        self.assertEqual((listed.status_code, listed.json()['current']), (200, 'false_positive'))
        client.post('/v1/alerts/a1/feedback', json={'label': 'true_positive'})
        self.assertEqual(client.get('/v1/alerts/a1/feedback').json()['current'], 'true_positive', 'newest wins')
        self.assertIn('ava_alert_feedback_writes_total{outcome="ok"} 2\n', client.get('/metrics').text)
        self.assertIn('ava_api_requests_total{route="/v1/alerts/:id/feedback",status="2xx"} 4\n',
                      client.get('/metrics').text)

    def test_malformed_submissions_are_refused_before_the_store_and_audited(self):
        client = self.make()
        self.login(client)
        bodies = [b'not json', b'[]', b'"true_positive"', json.dumps({}).encode(),
                  json.dumps({'label': 'true_positive', 'note': 'x'}).encode(),
                  json.dumps({'label': 'TRUE_POSITIVE'}).encode(), json.dumps({'label': 1}).encode(),
                  json.dumps({'label': None}).encode(), b'\xff\xfe']
        for body in bodies:
            response = client.post('/v1/alerts/a1/feedback', content=body,
                                   headers={'content-type': 'application/json'})
            self.assertEqual(response.status_code, 422, body)
        long_id = client.post('/v1/alerts/' + 'x' * 129 + '/feedback', json={'label': 'true_positive'})
        self.assertEqual(long_id.status_code, 422)
        big = client.post('/v1/alerts/a1/feedback', content=b'{"label": "' + b'x' * 2000 + b'"}',
                          headers={'content-type': 'application/json'})
        self.assertEqual(big.status_code, 413)
        self.assertEqual(self.feedback.calls, [], 'nothing reached the store')
        rows = self.audits()
        self.assertEqual(len(rows), len(bodies) + 2)
        self.assertEqual(rows[0], ('ana', 'alerts.feedback', 'refused',
                                   {'route': '/v1/alerts/:id/feedback', 'method': 'POST', 'status': 422}))
        self.assertEqual(rows[-1][3]['status'], 413)
        self.assertNotIn('note', json.dumps(rows), 'bodies are never audited')
        self.assertIn(f'ava_alert_feedback_writes_total{{outcome="invalid"}} {len(bodies) + 2}\n',
                      client.get('/metrics').text)
        self.assertEqual(client.get('/v1/alerts/' + 'x' * 129 + '/feedback').status_code, 422)

    def test_an_unknown_alert_is_404_and_store_failures_are_503_with_a_fixed_reason(self):
        client = self.make()
        self.login(client)
        missing = client.post('/v1/alerts/nope/feedback', json={'label': 'true_positive'})
        self.assertEqual((missing.status_code, missing.json()), (404, {'detail': 'No stored alert with this ID'}))
        self.assertEqual(client.get('/v1/alerts/nope/feedback').status_code, 404)
        self.assertEqual(self.audits()[-1][2:], ('refused', {'route': '/v1/alerts/:id/feedback', 'method': 'POST',
                                                             'status': 404}))
        self.feedback.fail = 'conflict'
        full = client.post('/v1/alerts/a1/feedback', json={'label': 'true_positive'})
        self.assertEqual((full.status_code, full.json()), (409, {'detail': 'This alert has the most labels allowed'}))
        self.assertEqual(self.audits()[-1][2], 'refused')
        for reason in ('unavailable', 'busy', 'timeout', 'schema', 'error'):
            self.feedback.fail = reason
            response = client.post('/v1/alerts/a1/feedback', json={'label': 'true_positive'})
            self.assertEqual((response.status_code, response.json()),
                             (503, {'detail': f'Feedback not stored ({reason})'}))
            self.assertEqual(self.audits()[-1][2], 'failed')
            read = client.get('/v1/alerts/a1/feedback')
            self.assertEqual((read.status_code, read.json()),
                             (503, {'detail': f'Feedback store unavailable ({reason})'}))
        metrics = client.get('/metrics').text
        self.assertIn('ava_alert_feedback_writes_total{outcome="not_found"} 1\n', metrics)
        self.assertIn('ava_alert_feedback_writes_total{outcome="schema"} 1\n', metrics)

    def test_a_cross_site_submission_is_refused_before_anything_runs(self):
        client = self.make()
        self.login(client)
        response = client.post('/v1/alerts/a1/feedback', json={'label': 'true_positive'},
                               headers={'origin': 'https://evil.example'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.feedback.calls, [])

    def test_without_a_feedback_store_both_routes_answer_503(self):
        client = self.make(with_feedback=False)
        self.login(client)
        for response in (client.post('/v1/alerts/a1/feedback', json={'label': 'true_positive'}),
                         client.get('/v1/alerts/a1/feedback')):
            self.assertEqual(response.status_code, 503)
        service = analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')
        with TestClient(create_app(service)) as plain:
            self.assertEqual(plain.post('/v1/alerts/a1/feedback', json={'label': 'true_positive'}).status_code, 503)
            self.assertNotIn('ava_alert_feedback', plain.get('/metrics').text)

    def test_feedback_needs_accounts(self):
        service = analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')
        with self.assertRaisesRegex(ValueError, 'local accounts'):
            create_app(service, feedback=FakeFeedback())


# -- PostgreSQL -----------------------------------------------------------------------

@REAL
class PostgresFeedbackTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from services.auth.store import PostgresAuthStore
        from services.store import migrate
        from services.store.events_pg import PostgresEventStore
        self.psycopg, self.migrate = psycopg, migrate
        self.schema = f'ava_test_{uuid.uuid4().hex[:12]}'
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'CREATE SCHEMA {self.schema}')
        self.connections = []
        self.addCleanup(self.drop_schema)
        migrate.apply_migrations(self.connect())
        self.auth_store = PostgresAuthStore(self.connect)
        self.addCleanup(self.auth_store.close)
        ana_id = self.auth_store.add_user('ana', FAKE_HASH, 'analyst', actor='system:test')
        boss_id = self.auth_store.add_user('boss', FAKE_HASH, 'admin', actor='system:test')
        self.ana = Principal(ana_id, 'ana', 'analyst', 1e10)
        self.boss = Principal(boss_id, 'boss', 'admin', 1e10)
        self.events = PostgresEventStore(self.connect)
        # Call f1: a caller alert raised at T0+4 and cleared at T0+8; the agent's window at
        # T0+5 is another direction; the caller's window at T0+10 is after the clear. Then an
        # open alert with no direction raised at T0+12 (every direction from its raise).
        outcomes = [self.events.record(e) for e in (
            env('call.started', 'f1', {'from': '+923001234567'}, 1, T0),
            score('f1', 2, T0 + 2, 0, 0.2, 0.2),
            score('f1', 3, T0 + 4, 1, 0.9, 0.6, state='alert'),
            env('alert.raised', 'f1', {'alert_id': 'f-a1', 'direction': 'caller', 'ema': 0.6, 'window': 1}, 4,
                T0 + 4),
            score('f1', 5, T0 + 5, 0, 0.95, 0.9, direction='agent'),
            score('f1', 6, T0 + 6, 2, None, None, quality='low'),
            score('f1', 7, T0 + 8, 3, 0.1, 0.4),
            env('alert.cleared', 'f1', {'alert_id': 'f-a1', 'direction': 'caller', 'ema': 0.4, 'window': 3}, 8,
                T0 + 8),
            score('f1', 9, T0 + 10, 4, 0.1, 0.3),
            env('alert.raised', 'f1', {'alert_id': 'f-a2'}, 10, T0 + 12),
            score('f1', 11, T0 + 12, 1, 0.8, 0.7, direction='agent'),
            score('f1', 12, T0 + 14, 5, 0.7, 0.6))]
        self.assertEqual(outcomes, ['stored'] * 12)

    def drop_schema(self):
        for connection in self.connections:
            connection.close()
        with self.psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA {self.schema} CASCADE')

    def connect(self):
        connection = self.psycopg.connect(DSN, options=f'-c search_path={self.schema}')
        self.connections.append(connection)
        return connection

    def sql(self, statement, *args):
        with self.psycopg.connect(DSN, options=f'-c search_path={self.schema}', autocommit=True) as connection:
            cursor = connection.execute(statement, args or None)
            return cursor.fetchall() if cursor.description else cursor.rowcount

    def feedback_audits(self):
        return [(r['actor'], r['outcome'], r['detail']) for r in reversed(self.auth_store.recent_audit())
                if r['action'] == 'alerts.feedback']

    def test_a_label_is_stored_with_the_alert_windows_and_its_audit_row(self):
        store = AlertFeedbackStore(self.auth_store)
        record = store.record(self.ana, 'f-a1', 'true_positive')
        self.assertEqual({k: record[k] for k in ('alert_id', 'label', 'username', 'alert_state', 'windows_stored',
                                                  'windows_total')},
                         {'alert_id': 'f-a1', 'label': 'true_positive', 'username': 'ana',
                          'alert_state': 'cleared', 'windows_stored': 3, 'windows_total': 3})
        (user_id, windows, total), = self.sql('SELECT user_id, windows, windows_total FROM alert_feedback')
        self.assertEqual((user_id, total), (self.ana.user_id, 3))
        self.assertEqual([(w['direction'], w['window'], w['ts'], w['scored'], w['score'], w['ema'], w['quality'])
                          for w in windows],
                         [('caller', 1, T0 + 4, True, 0.9, 0.6, 'ok'), ('caller', 2, T0 + 6, False, None, None, 'low'),
                          ('caller', 3, T0 + 8, True, 0.1, 0.4, 'ok')])
        self.assertEqual(set(windows[0]), {'direction', 'window', 'ts', 'scored', 'score', 'ema', 'provisional',
                                           'quality', 'degraded', 'concealed_fraction'})
        self.assertNotIn('+923001234567', json.dumps(windows), 'no start data copied')
        self.assertEqual(self.feedback_audits(),
                         [('ana', 'ok', {'alert_id': 'f-a1', 'label': 'true_positive',
                                         'feedback_id': record['feedback_id']})])

    def test_an_open_alert_without_a_direction_copies_every_direction_so_far(self):
        store = AlertFeedbackStore(self.auth_store)
        record = store.record(self.boss, 'f-a2', 'false_positive')
        self.assertEqual((record['alert_state'], record['windows_total'], record['username']), ('open', 2, 'boss'))
        windows = self.sql("SELECT windows FROM alert_feedback WHERE alert_id = 'f-a2'")[0][0]
        self.assertEqual([(w['direction'], w['window']) for w in windows], [('agent', 1), ('caller', 5)])
        # A window stored after the label is not in that label's copy; a new label sees it.
        self.events.record(score('f1', 13, T0 + 16, 6, 0.6, 0.55))
        again = store.record(self.ana, 'f-a2', 'true_positive')
        self.assertEqual(again['windows_total'], 3)
        self.assertEqual(self.sql("SELECT windows_total FROM alert_feedback WHERE alert_id = 'f-a2' ORDER BY id"),
                         [(2,), (3,)])

    def test_labels_are_appended_and_the_newest_is_current(self):
        store = AlertFeedbackStore(self.auth_store)
        self.assertEqual(store.list('f-a1'), {'alert_id': 'f-a1', 'current': None, 'feedback': [],
                                              'feedback_total': 0, 'feedback_truncated': False})
        first = store.record(self.ana, 'f-a1', 'true_positive')
        second = store.record(self.boss, 'f-a1', 'false_positive')
        listed = store.list('f-a1')
        self.assertEqual(listed['current'], 'false_positive')
        self.assertEqual([(f['feedback_id'], f['username'], f['label']) for f in listed['feedback']],
                         [(second['feedback_id'], 'boss', 'false_positive'),
                          (first['feedback_id'], 'ana', 'true_positive')])
        self.assertEqual(listed['feedback'][0]['windows_stored'], 3)
        short = store.list('f-a1', 1)
        self.assertEqual((len(short['feedback']), short['feedback_total'], short['feedback_truncated']),
                         (1, 2, True))
        self.assertEqual(len(self.feedback_audits()), 2)

    def test_an_unknown_alert_writes_nothing_and_keeps_the_connection(self):
        store = AlertFeedbackStore(self.auth_store)
        for call in (lambda: store.record(self.ana, 'missing', 'true_positive'), lambda: store.list('missing')):
            with self.assertRaises(AuthStoreError) as error:
                call()
            self.assertEqual(error.exception.reason, 'not_found')
        self.assertEqual(self.sql('SELECT count(*) FROM alert_feedback'), [(0,)])
        self.assertEqual(self.feedback_audits(), [])
        self.assertEqual(self.auth_store.failures, 0, 'not a store failure')

    def test_a_failed_audit_row_rolls_the_label_back(self):
        store = AlertFeedbackStore(self.auth_store)
        original = self.auth_store.audit_in

        def failing(db, *args, **kwargs):
            original(db, *args, **kwargs)
            raise RuntimeError('audit write failed after the insert')
        self.auth_store.audit_in = failing
        with self.assertRaises(AuthStoreError) as error:
            store.record(self.ana, 'f-a1', 'true_positive')
        self.assertEqual(error.exception.reason, 'error')
        del self.auth_store.audit_in
        self.assertEqual(self.sql('SELECT count(*) FROM alert_feedback'), [(0,)])
        self.assertEqual(self.feedback_audits(), [])
        self.assertEqual(store.record(self.ana, 'f-a1', 'true_positive')['label'], 'true_positive', 'reconnects')

    def test_a_clear_waits_for_a_label_being_written_so_status_and_copy_agree(self):
        # Review L1: the alert row is locked for the label's transaction. The clear of the
        # open alert f-a2 arrives while the label is between its window copy and its commit.
        store = AlertFeedbackStore(self.auth_store)
        inside, release, results = threading.Event(), threading.Event(), {}
        original = self.auth_store.audit_in

        def slow(db, *args, **kwargs):
            inside.set()
            release.wait(10)
            original(db, *args, **kwargs)
        self.auth_store.audit_in = slow
        labeller = threading.Thread(target=lambda: results.update(label=store.record(self.ana, 'f-a2',
                                                                                      'true_positive')))
        labeller.start()
        clear = env('alert.cleared', 'f1', {'alert_id': 'f-a2', 'ema': 0.2, 'window': 6}, 13, T0 + 16)
        clearer = threading.Thread(target=lambda: results.update(clear=self.events.record(clear)))
        try:
            self.assertTrue(inside.wait(10))
            clearer.start()
            clearer.join(1.0)
            waited = clearer.is_alive()
        finally:
            release.set()
            labeller.join(10)
            if clearer.is_alive() or clearer.ident is not None:
                clearer.join(10)
        self.assertTrue(waited, 'the clear waits for the label being written')
        self.assertEqual((results['label']['alert_state'], results['label']['windows_total'], results['clear']),
                         ('open', 2, 'stored'))
        self.assertEqual(self.sql("SELECT status FROM alerts WHERE alert_id = 'f-a2'"), [('cleared',)])

    def test_an_alert_closed_between_the_copy_and_the_lock_is_copied_again(self):
        # Re-review R1: the copy is made before the row lock; a clear committed in between
        # must not leave a copy to the old end with the new status.
        store = AlertFeedbackStore(self.auth_store)
        clear = env('alert.cleared', 'f1', {'alert_id': 'f-a2', 'ema': 0.2, 'window': 6}, 13, T0 + 13)
        outcomes = []
        store._between = lambda: outcomes.append(self.events.record(clear))
        record = store.record(self.ana, 'f-a2', 'true_positive')
        self.assertEqual(outcomes, ['stored'])
        self.assertEqual((record['alert_state'], record['windows_total'], record['windows_stored']), ('cleared', 1, 1))
        windows = self.sql("SELECT windows FROM alert_feedback WHERE alert_id = 'f-a2'")[0][0]
        self.assertEqual([(w['direction'], w['window']) for w in windows], [('agent', 1)], 'ends at the clear')

    def test_an_alert_keeps_at_most_max_labels(self):
        store = AlertFeedbackStore(self.auth_store)
        with unittest.mock.patch.object(feedback_module, 'MAX_LABELS', 3):
            for label in ('true_positive', 'false_positive', 'true_positive'):
                store.record(self.ana, 'f-a1', label)
            with self.assertRaises(AuthStoreError) as error:
                store.record(self.boss, 'f-a1', 'false_positive')
        self.assertEqual(error.exception.reason, 'conflict')
        self.assertEqual(self.sql("SELECT count(*) FROM alert_feedback WHERE alert_id = 'f-a1'"), [(3,)])
        self.assertEqual((len(self.feedback_audits()), self.auth_store.failures), (3, 0))
        self.assertEqual(store.record(self.ana, 'f-a2', 'true_positive')['label'], 'true_positive', 'per alert')

    def test_the_copy_keeps_the_oldest_windows_up_to_the_cap(self):
        record = AlertFeedbackStore(self.auth_store, max_windows=2).record(self.ana, 'f-a1', 'true_positive')
        self.assertEqual((record['windows_stored'], record['windows_total']), (2, 3))
        windows = self.sql('SELECT windows FROM alert_feedback')[0][0]
        self.assertEqual([w['window'] for w in windows], [1, 2])

    def test_a_database_before_0004_is_schema_until_it_is_migrated(self):
        schema = f'ava_test_{uuid.uuid4().hex[:12]}'
        with self.psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'CREATE SCHEMA {schema}')
        self.addCleanup(self._drop, schema)

        def connect():
            connection = self.psycopg.connect(DSN, options=f'-c search_path={schema}')
            self.connections.append(connection)
            return connection
        from services.auth.store import PostgresAuthStore
        self.migrate.apply_migrations(connect(), self.migrate.load_migrations()[:3])
        auth_store = PostgresAuthStore(connect)
        self.addCleanup(auth_store.close)
        self.assertIsNone(auth_store.find_user('ana'), 'accounts work on 0003')
        opened = len(self.connections)
        for _ in range(3):
            with self.assertRaises(AuthStoreError) as error:
                AlertFeedbackStore(auth_store).list('f-a1')
            self.assertEqual(error.exception.reason, 'schema')
        # Review L3: a database short of 0004 is not a broken connection; logins keep it.
        self.assertIsNone(auth_store.find_user('ana'))
        self.assertEqual((auth_store.failures, len(self.connections)), (0, opened), 'no reconnect, no failure')
        self.migrate.apply_migrations(connect())
        with self.assertRaises(AuthStoreError) as error:  # migrated now: the alert is simply not stored
            AlertFeedbackStore(auth_store).list('f-a1')
        self.assertEqual(error.exception.reason, 'not_found')

    def _drop(self, schema):
        with self.psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA {schema} CASCADE')

    def test_the_schema_refuses_bad_rows_and_follows_the_call(self):
        good = "('f-a1', 'true_positive', NULL, 'ana', 'cleared', '[]'::jsonb, 0)"
        columns = 'INSERT INTO alert_feedback (alert_id, label, user_id, username, alert_status, windows, windows_total)'
        self.assertEqual(self.sql(f'{columns} VALUES {good}'), 1)
        for bad in ("('f-a1', 'unsure', NULL, 'ana', 'cleared', '[]'::jsonb, 0)",
                    "('f-a1', 'true_positive', NULL, 'Ana Bad', 'cleared', '[]'::jsonb, 0)",
                    "('f-a1', 'true_positive', NULL, 'ana', 'closed', '[]'::jsonb, 0)",
                    "('f-a1', 'true_positive', NULL, 'ana', 'cleared', '{}'::jsonb, 0)",
                    "('f-a1', 'true_positive', NULL, 'ana', 'cleared', '[1, 2]'::jsonb, 1)",
                    "('f-a1', 'true_positive', NULL, 'ana', 'cleared', '[]'::jsonb, -1)",
                    "('nope', 'true_positive', NULL, 'ana', 'cleared', '[]'::jsonb, 0)"):
            with self.assertRaises(self.psycopg.errors.IntegrityError, msg=bad):
                self.sql(f'{columns} VALUES {bad}')
        self.sql("DELETE FROM calls WHERE call_id = 'f1'")
        self.assertEqual(self.sql('SELECT count(*) FROM alert_feedback'), [(0,)], 'deleted with its call')

    @auth_fixture.ARGON2
    def test_the_api_end_to_end_with_real_accounts(self):
        if TestClient is None:
            self.skipTest('requirements-api.txt not installed')
        from services.auth.passwords import Argon2Passwords
        from services.auth.service import Authenticator
        passwords = Argon2Passwords(time_cost=1, memory_kib=8192, parallelism=1)
        self.auth_store.set_password('ana', passwords.hash(auth_fixture.PASSWORD), actor='system:test')
        auth = Authenticator(self.auth_store, passwords)
        service = analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')
        app = create_app(service, auth=auth, feedback=AlertFeedbackStore(self.auth_store))
        with TestClient(app, base_url='https://testserver') as client:
            login = client.post('/v1/auth/login', json={'username': 'ana', 'password': auth_fixture.PASSWORD})
            self.assertEqual(login.status_code, 200)
            stored = client.post('/v1/alerts/f-a1/feedback', json={'label': 'false_positive'})
            self.assertEqual(stored.status_code, 201, stored.text)
            self.assertEqual(stored.json()['windows_total'], 3)
            self.assertEqual(client.post('/v1/alerts/none/feedback', json={'label': 'true_positive'}).status_code,
                             404)
            listed = client.get('/v1/alerts/f-a1/feedback').json()
            self.assertEqual((listed['current'], listed['feedback'][0]['username']), ('false_positive', 'ana'))
        rows = [(r['actor'], r['action'], r['outcome']) for r in reversed(self.auth_store.recent_audit())
                if r['action'] not in ('user.add', 'user.password')]
        self.assertEqual(rows, [('ana', 'auth.login', 'ok'), ('ana', 'alerts.feedback', 'ok'),
                                ('ana', 'alerts.feedback', 'refused')], 'one row per submission')
        self.assertTrue(re.fullmatch(r'\d+', str(stored.json()['feedback_id'])))


if __name__ == '__main__':
    unittest.main()
