"""Claude-authored tests (19 November 2026, attempt 5) for PRD FR-A6 alert search and CSV
export: `PostgresCallHistory.alerts()` and its cursor/filter helpers
(services/store/history.py), `services/api/alert_csv.py` and `GET /v1/alerts`
(services/api/app.py).

Authored by Claude (Claude Code), not by GPT-6 Astra. Groups:
- pure tests (always run): cursor round trip and refusals, filter checks, CSV text;
- API tests with a fake history (need requirements-api.txt);
- PostgreSQL tests, run only when AVA_TEST_POSTGRES_DSN names a disposable database
  (AVA_REQUIRE_POSTGRES=1 makes the skip a failure). Rows are written by the real
  `PostgresEventStore` from FR-A4 envelopes. Each test uses its own schema.
Storage evidence only: no audio and no detector are involved. "Confidence" is the
uncalibrated EMA at which an alert was raised.
"""
import csv
from datetime import datetime, timedelta, timezone
import io
import json
import re
import unittest

from services.api.alert_csv import COLUMNS, alerts_csv
from services.store import history as history_module
from services.store.history import (EPOCH, HistoryError, PostgresCallHistory, check_alert_filters, decode_cursor,
                                    encode_cursor)
from tests.unit import test_store_history as base
from tests.unit.test_store_history import DSN, REAL, T0, env, ended, score

TestClient = base.TestClient
if TestClient is not None:
    from services.api import app as app_module
    from services.api.app import create_app
    from services.inference import analysis
    from tests.unit import test_analysis_api as api_fixture


def b64(text):
    import base64
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip('=')


def alert(alert_id='a1', call_id='c1', **extra):
    row = {'alert_id': alert_id, 'direction': 'caller', 'state': 'open', 'raised_ts': T0, 'raised_window': 3,
           'raised_ema': 0.75, 'peak_ema': 0.8, 'closed_ts': None, 'cleared_window': None, 'cleared_ema': None,
           'call_id': call_id, 'session': 7}
    row.update(extra)
    return row


# -- pure -----------------------------------------------------------------------------

class CursorTests(unittest.TestCase):
    def test_round_trip_is_exact_and_url_and_header_safe(self):
        for raised_at, alert_id in ((EPOCH + timedelta(seconds=T0, microseconds=123_457), 'c1-caller-a1'),
                                    (EPOCH, 'a:b:c'),  # a colon inside the ID
                                    (datetime(2026, 11, 19, 4, 5, 6, 999_999, timezone.utc), 'ٹیسٹ' * 32),
                                    (EPOCH - timedelta(microseconds=1), 'x')):
            cursor = encode_cursor(raised_at, alert_id)
            self.assertRegex(cursor, r'^[A-Za-z0-9_-]+$')
            self.assertEqual(decode_cursor(cursor), (raised_at, alert_id))

    def test_anything_else_is_refused(self):
        for cursor in ('', None, 5, 'x' * 1025, '!!!!', b64('abc'), b64('12:'), b64('x:id'), b64('1.5:id'),
                       b64('12:' + 'x' * 129), b64('12:a\x00b'), b64('9' * 18 + ':id'),
                       'gA',  # one byte 0x80: not UTF-8
                       b64(' 12:id'),
                       b64('12:id') + '=', 'MD ph', 'MD+p/h'):  # padding, other alphabets: review nit

            with self.assertRaises(ValueError, msg=repr(cursor)):
                decode_cursor(cursor)


class FilterTests(unittest.TestCase):
    def test_valid_filters_become_exact_utc_times(self):
        self.assertEqual(check_alert_filters(), (None, None, None))
        start, end, after = check_alert_filters('ended_open', 1.5, T0, 0, encode_cursor(EPOCH, 'a'))
        self.assertEqual((start, end, after),
                         (EPOCH + timedelta(seconds=1.5), EPOCH + timedelta(seconds=T0), (EPOCH, 'a')))
        self.assertEqual(start.tzinfo, timezone.utc)
        for confidence in (0, 1, 0.5, 1.0):
            check_alert_filters(min_confidence=confidence)

    def test_bad_filters_are_refused(self):
        nan, inf = float('nan'), float('inf')
        for kwargs in ({'state': 'raised'}, {'state': None}, {'since': -1}, {'since': nan}, {'until': inf},
                       {'since': True}, {'since': '5'}, {'until': 4e10}, {'since': 5, 'until': 5},
                       {'since': 6, 'until': 5}, {'min_confidence': -0.01}, {'min_confidence': 1.01},
                       {'min_confidence': nan}, {'min_confidence': True}, {'min_confidence': '0.5'},
                       {'cursor': 'not a cursor'}):
            with self.assertRaises(ValueError, msg=kwargs):
                check_alert_filters(**kwargs)

    def test_the_reader_refuses_bad_arguments_before_connecting(self):
        def connect():
            raise AssertionError('must not connect')
        history = PostgresCallHistory(connect)
        for kwargs in ({'limit': 0}, {'limit': 10_001}, {'limit': True}, {'limit': 2.0}, {'state': 'x'},
                       {'cursor': '***'}, {'since': 10, 'until': 1}):
            with self.assertRaises(ValueError, msg=kwargs):
                history.alerts(**kwargs)


class CsvTests(unittest.TestCase):
    def test_header_line_ends_times_numbers_and_missing_values(self):
        text = alerts_csv([alert(), alert('a2', state='cleared', closed_ts=T0 + 4.000001, cleared_window=5,
                                         cleared_ema=0.25, raised_ema=None, peak_ema=None, direction=None),
                           alert('a3', raised_ts=-1.5)])  # review L: negative times (OSError on Windows)
        self.assertTrue(text.endswith('\r\n'))
        self.assertEqual(text.count('\r\n'), 4)
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(tuple(rows[0]), COLUMNS)
        self.assertEqual(rows[1], ['a1', 'c1', '7', 'caller', 'open', '2025-11-18T21:06:40.000000Z', '3', '0.75',
                                   '0.8', '', '', ''])
        self.assertEqual(rows[2], ['a2', 'c1', '7', '', 'cleared', '2025-11-18T21:06:40.000000Z', '3', '', '',
                                   '2025-11-18T21:06:44.000001Z', '5', '0.25'])
        self.assertEqual(rows[3][5], '1969-12-31T23:59:58.500000Z')
        self.assertEqual(alerts_csv([]), ','.join(COLUMNS) + '\r\n')

    def test_ids_a_spreadsheet_would_run_as_formulas_are_neutralised(self):
        hostile = ['=HYPERLINK("http://x")', '+1', '-2', '@SUM(A1)', '\tx', '\rx']
        rows = list(csv.reader(io.StringIO(alerts_csv([alert(call_id=v, alert_id=v) for v in hostile]),
                                           newline='')))[1:]
        for value, row in zip(hostile, rows):
            self.assertEqual((row[0], row[1]), ("'" + value, "'" + value))

    def test_commas_quotes_newlines_and_urdu_survive(self):
        odd = 'کال, "one"\nline'
        rows = list(csv.reader(io.StringIO(alerts_csv([alert(call_id=odd)]), newline='')))
        self.assertEqual(rows[1][1], odd)


# -- API with a fake history ----------------------------------------------------------

class FakeAlerts(base.FakeHistory):
    def __init__(self, page=None, error=None):
        super().__init__(error=error)
        self.page = page or {'alerts': [], 'next_cursor': None}

    def alerts(self, state='all', since=None, until=None, min_confidence=None, limit=100, cursor=None):
        self.seen.append(('alerts', state, since, until, min_confidence, limit, cursor))
        if self.error:
            raise self.error
        return self.page


@unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
class AlertRouteTests(unittest.TestCase):
    def make(self, history=None):
        service = analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')
        client = TestClient(create_app(service, history=history))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def test_without_a_store_it_answers_503(self):
        response = self.make().get('/v1/alerts')
        self.assertEqual((response.status_code, response.json()['detail']),
                         (503, 'No call history store is configured'))

    def test_bad_parameters_are_422_before_any_read(self):
        fake = FakeAlerts()
        client = self.make(fake)
        cursor = encode_cursor(EPOCH, 'a')
        for query in ('state=raised', 'since=nan', 'since=inf', 'since=-1', 'until=x', 'since=5&until=5',
                      'min_confidence=1.5', 'min_confidence=nan', 'limit=0', 'limit=1001', 'limit=1.5',
                      'format=csv&limit=10001', 'format=xml', 'cursor=***', f'cursor={cursor}x'):
            response = client.get(f'/v1/alerts?{query}')
            self.assertEqual(response.status_code, 422, query)
        self.assertEqual(fake.seen, [])
        self.assertIn('state must be one of all, open, cleared, ended_open',
                      client.get('/v1/alerts?state=x').json()['detail'])

    def test_json_page_with_defaults_and_filters_passed_through(self):
        page = {'alerts': [alert()], 'next_cursor': encode_cursor(EPOCH, 'a1')}
        fake = FakeAlerts(page)
        client = self.make(fake)
        self.assertEqual(client.get('/v1/alerts').json(), page)
        cursor = page['next_cursor']
        client.get(f'/v1/alerts?state=open&since=10&until=20.5&min_confidence=0.6&limit=1000&cursor={cursor}')
        self.assertEqual(fake.seen, [('alerts', 'all', None, None, None, 100, None),
                                     ('alerts', 'open', 10.0, 20.5, 0.6, 1000, cursor)])
        metrics = client.get('/metrics').text
        # Review L: alert searches have their own counter; the history counter is untouched.
        self.assertIn('ava_alert_search_reads_total{outcome="ok"} 2\n', metrics)
        self.assertIn('ava_call_history_reads_total{outcome="ok"} 0\n', metrics)
        self.assertNotIn('ava_alert_search_reads_total{outcome="not_found"}', metrics)
        self.assertIn('ava_api_requests_total{route="/v1/alerts",status="2xx"} 2', metrics)
        self.assertEqual(app_module.route_label('/v1/alerts'), '/v1/alerts')

    def test_csv_export_is_a_download_with_the_next_cursor_in_a_header(self):
        cursor = encode_cursor(EPOCH + timedelta(seconds=T0), 'ٹیسٹ')  # non-Latin ID: header still ASCII
        fake = FakeAlerts({'alerts': [alert(), alert('a0', call_id='=1+1')], 'next_cursor': cursor})
        client = self.make(fake)
        response = client.get('/v1/alerts?format=csv&state=ended_open')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['content-type'], 'text/csv; charset=utf-8')
        self.assertEqual(response.headers['content-disposition'], 'attachment; filename="ava-alerts.csv"')
        self.assertEqual(response.headers['x-ava-next-cursor'], cursor)
        rows = list(csv.reader(io.StringIO(response.text, newline='')))
        self.assertEqual([r[:2] for r in rows[1:]], [['a1', 'c1'], ['a0', "'=1+1"]])
        self.assertEqual(fake.seen[-1][5], 1000, 'the CSV default page is larger')
        client.get('/v1/alerts?format=csv&limit=10000')
        self.assertEqual(fake.seen[-1][5], 10_000)
        fake.page = {'alerts': [], 'next_cursor': None}
        last = client.get('/v1/alerts?format=csv')
        self.assertNotIn('x-ava-next-cursor', last.headers)
        self.assertEqual(last.text, ','.join(COLUMNS) + '\r\n')

    def test_store_failures_are_503_with_a_fixed_reason_in_both_formats(self):
        for reason in history_module.REASONS:
            client = self.make(FakeAlerts(error=HistoryError('OperationalError at secret-host', reason=reason)))
            for query in ('', '?format=csv'):
                response = client.get(f'/v1/alerts{query}')
                self.assertEqual((response.status_code, response.json()['detail']),
                                 (503, f'Call history store unavailable ({reason})'))
                self.assertNotIn('secret-host', response.text)
            self.assertIn(f'ava_alert_search_reads_total{{outcome="{reason}"}} 2\n', client.get('/metrics').text)

    def test_a_page_the_csv_writer_cannot_read_is_a_counted_503(self):
        for broken in ({'alert_id': 'a1'},  # KeyError
                       alert(raised_ts=1e300)):  # OverflowError (an ArithmeticError)
            client = self.make(FakeAlerts({'alerts': [broken], 'next_cursor': None}))
            response = client.get('/v1/alerts?format=csv')
            self.assertEqual((response.status_code, response.json()['detail']),
                             (503, 'Call history store unavailable (error)'))
            self.assertIn('ava_alert_search_reads_total{outcome="error"} 1\n', client.get('/metrics').text)


# -- PostgreSQL -----------------------------------------------------------------------

@REAL
class PostgresAlertSearchTests(base.PostgresHistoryTests):
    """Reuses the schema-per-test fixture; the inherited history tests are not rerun."""

    def setUp(self):
        super().setUp()
        # Stored alerts (raised time, EMA at raise, peak EMA = confidence, final state):
        #   k1-a1  T0+6     0.55  0.55  ended_open (session killed, superseded by the re-tap)
        #   k1-a2  T0+24    0.70  0.80  cleared at T0+28 (its window at T0+26 peaks at 0.8; the
        #                               0.85 window at T0+29 is after the clear)
        #   o-a1   T0+54    0.90  0.95  open; caller window 0.95 at T0+56 (the 0.99 window at T0+52
        #                               is before the raise, the agent's 0.97 another direction)
        #   o-a0   T0+54    none  0.97  open, no direction: every direction from the raise; same
        #                               raised time as o-a1 (tie broken by ID)
        #   d-a1   T0+60.000001  0.61  0.61  ended_open, one microsecond after a whole second
        self.killed_and_retapped_call()  # k1-a1, then session 2 ends with bye at T0+30
        outcomes = self.store(
            env('call.started', 'k2', {'from': '+923001234567'}, 1, T0 + 20),
            env('alert.raised', 'k2', {'alert_id': 'k1-a2', 'direction': 'caller', 'ema': 0.7, 'window': 2}, 2,
                T0 + 24),
            score('k2', 3, T0 + 26, 3, 0.9, 0.8, state='alert'),
            env('alert.cleared', 'k2', {'alert_id': 'k1-a2', 'direction': 'caller', 'ema': 0.3, 'window': 4}, 4,
                T0 + 28),
            score('k2', 5, T0 + 29, 4, 0.9, 0.85),
            env('call.started', 'open1', {}, 1, T0 + 50),
            score('open1', 2, T0 + 52, 0, 0.99, 0.99),
            env('alert.raised', 'open1', {'alert_id': 'o-a1', 'direction': 'caller', 'ema': 0.9, 'window': 3}, 3,
                T0 + 54),
            env('alert.raised', 'open1', {'alert_id': 'o-a0'}, 4, T0 + 54),
            score('open1', 5, T0 + 56, 1, 0.97, 0.95, state='alert'),
            score('open1', 6, T0 + 58, 0, 0.97, 0.97, state='alert', direction='agent'),
            env('call.started', 'done1', {}, 1, T0 + 59),
            env('alert.raised', 'done1', {'alert_id': 'd-a1', 'direction': 'agent', 'ema': 0.61, 'window': 0}, 2,
                T0 + 60.000001),
            ended('done1', 3, T0 + 61, final_state='alert'))
        self.assertEqual(outcomes, ['stored'] * 14)

    def ids(self, **kwargs):
        return [a['alert_id'] for a in self.history().alerts(**kwargs)['alerts']]

    def test_newest_raised_first_with_ties_by_id_and_every_field(self):
        page = self.history().alerts()
        self.assertEqual([a['alert_id'] for a in page['alerts']], ['d-a1', 'o-a1', 'o-a0', 'k1-a2', 'k1-a1'])
        self.assertIsNone(page['next_cursor'])
        by_id = {a['alert_id']: a for a in page['alerts']}
        self.assertEqual(by_id['k1-a2'], {'alert_id': 'k1-a2', 'direction': 'caller', 'state': 'cleared',
                                          'raised_ts': T0 + 24, 'raised_window': 2, 'raised_ema': 0.7,
                                          'peak_ema': 0.8, 'closed_ts': T0 + 28, 'cleared_window': 4,
                                          'cleared_ema': 0.3, 'call_id': 'k2', 'session': by_id['k1-a2']['session']})
        self.assertEqual((by_id['k1-a1']['call_id'], by_id['k1-a1']['state']), ('k1', 'ended_open'))
        self.assertEqual((by_id['o-a0']['raised_ema'], by_id['o-a0']['direction']), (None, None))
        self.assertEqual({k: a['peak_ema'] for k, a in by_id.items()},
                         {'d-a1': 0.61, 'o-a1': 0.95, 'o-a0': 0.97, 'k1-a2': 0.8, 'k1-a1': 0.55})
        self.assertNotIn('+923001234567', json.dumps(page), 'start data are never served')

    def test_state_filters(self):
        self.assertEqual(self.ids(state='open'), ['o-a1', 'o-a0'])
        self.assertEqual(self.ids(state='cleared'), ['k1-a2'])
        self.assertEqual(self.ids(state='ended_open'), ['d-a1', 'k1-a1'])

    def test_time_range_is_since_inclusive_until_exclusive_to_the_microsecond(self):
        self.assertEqual(self.ids(since=T0 + 24, until=T0 + 54), ['k1-a2'])
        self.assertEqual(self.ids(since=T0 + 24.000001), ['d-a1', 'o-a1', 'o-a0'])
        self.assertEqual(self.ids(until=T0 + 60.000001), ['o-a1', 'o-a0', 'k1-a2', 'k1-a1'])
        self.assertEqual(self.ids(since=T0 + 60.000001), ['d-a1'])
        self.assertEqual(self.ids(since=T0 + 61), [])

    def test_minimum_confidence_is_the_inclusive_peak_ema(self):
        self.assertEqual(self.ids(min_confidence=0.61), ['d-a1', 'o-a1', 'o-a0', 'k1-a2'])
        self.assertEqual(self.ids(min_confidence=0.6100001), ['o-a1', 'o-a0', 'k1-a2'])
        self.assertEqual(self.ids(min_confidence=0.8), ['o-a1', 'o-a0', 'k1-a2'], 'the cleared alert peaked at 0.8')
        self.assertEqual(self.ids(min_confidence=0.96), ['o-a0'], 'other directions count only without one')
        self.assertEqual(self.ids(state='open', min_confidence=0.98), [], 'the 0.99 before the raise is not counted')
        self.assertEqual(self.ids(min_confidence=0.95, cursor=encode_cursor(EPOCH + timedelta(seconds=T0 + 54),
                                                                            'o-a1')), ['o-a0'])

    def test_an_alert_with_no_number_never_matches_a_minimum(self):
        # A raised EMA outside 0-1 or NaN (that column has no range check; review L) is not
        # a confidence: PostgreSQL sorts NaN above every number, so `>=` alone would match it.
        self.store(env('call.started', 'bare', {}, 1, T0 + 100),
                   env('alert.raised', 'bare', {'alert_id': 'b-none'}, 2, T0 + 101),
                   env('alert.raised', 'bare', {'alert_id': 'b-nan', 'ema': 0.9}, 3, T0 + 102),
                   env('alert.raised', 'bare', {'alert_id': 'b-big', 'ema': 0.9}, 4, T0 + 103))
        with self.psycopg.connect(DSN, options=f'-c search_path={self.schema}', autocommit=True) as admin:
            admin.execute("UPDATE alerts SET raised_ema = 'NaN' WHERE alert_id = 'b-nan'")
            admin.execute("UPDATE alerts SET raised_ema = 5 WHERE alert_id = 'b-big'")
        page = self.history().alerts(since=T0 + 100)['alerts']
        self.assertEqual([(a['alert_id'], a['raised_ema'], a['peak_ema']) for a in page],
                         [('b-big', 5.0, None), ('b-nan', None, None), ('b-none', None, None)])
        self.assertEqual(self.ids(since=T0 + 100, min_confidence=0), [])
        self.assertEqual(self.ids(min_confidence=0.99), [])

    def test_paging_one_by_one_visits_every_alert_once_in_order(self):
        history = self.history()
        for filters, expected in (({}, ['d-a1', 'o-a1', 'o-a0', 'k1-a2', 'k1-a1']),
                                  ({'state': 'open'}, ['o-a1', 'o-a0']),
                                  ({'since': T0 + 10, 'min_confidence': 0.5}, ['d-a1', 'o-a1', 'o-a0', 'k1-a2'])):
            seen, cursor = [], None
            for _ in range(10):
                page = history.alerts(limit=1, cursor=cursor, **filters)
                seen += [a['alert_id'] for a in page['alerts']]
                cursor = page['next_cursor']
                if cursor is None:
                    break
            self.assertEqual(seen, expected, filters)
        two = history.alerts(limit=2)
        rest = history.alerts(limit=10, cursor=two['next_cursor'])
        self.assertEqual([a['alert_id'] for a in rest['alerts']], ['o-a0', 'k1-a2', 'k1-a1'])
        self.assertEqual(decode_cursor(two['next_cursor']),
                         (EPOCH + timedelta(seconds=T0 + 54), 'o-a1'), 'the cursor holds the exact stored time')

    def test_an_alert_raised_after_a_page_was_read_does_not_shift_the_next_page(self):
        history = self.history()
        first = history.alerts(limit=2)
        self.store(env('alert.raised', 'open1', {'alert_id': 'o-new', 'ema': 0.99}, 4, T0 + 70))
        self.assertEqual([a['alert_id'] for a in history.alerts(limit=10, cursor=first['next_cursor'])['alerts']],
                         ['o-a0', 'k1-a2', 'k1-a1'])
        self.assertEqual(self.ids(limit=1), ['o-new'])

    def test_an_unmigrated_database_is_a_schema_failure(self):
        fresh = f'{self.schema}_empty'
        with self.psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'CREATE SCHEMA {fresh}')
        self.addCleanup(self._drop, fresh)
        history = PostgresCallHistory(lambda: self._connect_to(fresh))
        self.addCleanup(history.close)
        with self.assertRaises(HistoryError) as caught:
            history.alerts()
        self.assertEqual(caught.exception.reason, 'schema')

    def _connect_to(self, schema):
        connection = self.psycopg.connect(DSN, options=f'-c search_path={schema}')
        self.connections.append(connection)
        return connection

    def _drop(self, schema):
        with self.psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA {schema} CASCADE')

    @unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
    def test_the_api_pages_a_csv_export_through_the_header_cursor(self):
        with TestClient(create_app(analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model'),
                                   history=self.history())) as client:
            rows, query = [], '/v1/alerts?format=csv&limit=2&min_confidence=0.5'
            for _ in range(5):
                response = client.get(query)
                self.assertEqual(response.status_code, 200)
                page = list(csv.reader(io.StringIO(response.text, newline='')))
                self.assertEqual(tuple(page[0]), COLUMNS)
                rows += page[1:]
                cursor = response.headers.get('x-ava-next-cursor')
                if cursor is None:
                    break
                query = f'/v1/alerts?format=csv&limit=2&min_confidence=0.5&cursor={cursor}'
            self.assertEqual([(r[0], r[1], r[4], r[7], r[8]) for r in rows],
                             [('d-a1', 'done1', 'ended_open', '0.61', '0.61'), ('o-a1', 'open1', 'open', '0.9', '0.95'),
                              ('o-a0', 'open1', 'open', '', '0.97'), ('k1-a2', 'k2', 'cleared', '0.7', '0.8'),
                              ('k1-a1', 'k1', 'ended_open', '0.55', '0.55')])
            self.assertTrue(re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.000001Z', rows[0][5]), rows[0][5])
            body = client.get('/v1/alerts?state=open').json()
            self.assertEqual([a['alert_id'] for a in body['alerts']], ['o-a1', 'o-a0'])


# The inherited PostgresHistoryTests cases run once, in test_store_history.
for _name in [n for n in vars(base.PostgresHistoryTests) if n.startswith('test_')]:
    setattr(PostgresAlertSearchTests, _name, None)


if __name__ == '__main__':
    unittest.main()
