"""Claude-authored tests (17 November 2026) for services/api/oplog.py, the analysis API's
structured JSON operations log, and its wiring in services/api/app.py (backlog 7.2;
PRD FR-O6 "Structured JSON logs", §6.3 data minimisation).

Authored by Claude (Claude Code), not by GPT-6 Astra. ApiLog with a manual clock and an
in-memory stream; the app through FastAPI's TestClient with the fake detector of
test_analysis_api (skipped without requirements-api.txt). The real uvicorn child case
is in test_api_cli (ApiCliProcessTests).
"""
import io
import json
import threading
import time
import unittest

from services.api.oplog import ApiLog, PROBE_ROUTES

try:
    from fastapi.testclient import TestClient
    from services.api.app import create_app
    from tests.unit.test_analysis_api import Detector, wav
except ImportError:  # pragma: no cover - lightweight environment
    TestClient = None

from services.inference import analysis


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def lines(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


class BrokenStream:
    def write(self, text):
        raise OSError('stderr closed')

    def flush(self):
        pass


class ApiLogTests(unittest.TestCase):
    def make(self, **options):
        self.stream, self.clock = io.StringIO(), Clock()
        return ApiLog(self.stream, wall=lambda: 1_763_380_800.25, clock=self.clock, **options)

    def test_line_shape_and_allowed_values_only(self):
        log = self.make()
        log.emit('job_failed', 'warning', job_id='j\u0085' + 'x' * 200, status=500, odd=object(), nan=float('nan'))
        self.assertEqual(log.write_pending(), 1)
        text = self.stream.getvalue()
        self.assertTrue(text.isascii(), 'ASCII-escaped')
        [record] = lines(self.stream)
        self.assertEqual(list(record)[:3], ['ts', 'level', 'event'])
        self.assertEqual(record['ts'], '2025-11-17T12:00:00.250Z')
        self.assertEqual((record['level'], record['event'], record['status']), ('warning', 'job_failed', 500))
        self.assertEqual(len(record['job_id']), 128, 'strings cut to 128 characters')
        self.assertEqual((record['odd'], record['nan']), (None, None), 'only strings, finite numbers, bools, None')

    def test_request_lines_skip_probes_and_level_by_status(self):
        log = self.make()
        for route in sorted(PROBE_ROUTES):
            self.assertFalse(log.request(route, 'GET', 200, 0.001))
        self.assertTrue(log.request('/v1/analyze', 'POST', 200, 0.12345))
        self.assertTrue(log.request('/v1/jobs/:id', 'GET', 404, 0.0))
        self.assertTrue(log.request('/v1/calls', 'GET', 503, None))
        self.assertTrue(log.request('/v1/analyze', 'POST', 500, 0.5))
        log.write_pending()
        self.assertEqual([(r['route'], r['method'], r['status'], r['level'], r['duration_ms']) for r in lines(self.stream)],
                         [('/v1/analyze', 'POST', 200, 'info', 123.5), ('/v1/jobs/:id', 'GET', 404, 'warning', 0.0),
                          ('/v1/calls', 'GET', 503, 'warning', None),  # review L5: deliberate shedding
                          ('/v1/analyze', 'POST', 500, 'error', 500.0)])

    def test_rate_limit_drops_request_lines_but_not_job_lines_and_reports_them(self):
        log = self.make(max_lines_per_s=2)
        results = [log.request('/v1/analyze', 'POST', 200, 0.01) for _ in range(5)]
        self.assertEqual(results, [True, True, False, False, False])
        self.assertTrue(log.emit('job_queued', job_id='a'))  # never rate-limited
        log.write_pending()
        events = [(r['event'], r.get('count')) for r in lines(self.stream)]
        # Review L3: the drop report comes after the lines it follows (ts order).
        self.assertEqual(events, [('request', None), ('request', None), ('job_queued', None), ('log_dropped', 3)])
        self.clock.t += 0.5  # refills one token (2 per second)
        self.assertEqual([log.request('/v1/analyze', 'POST', 200, 0.01) for _ in range(2)], [True, False])
        self.assertEqual(log.report(), {'lines': 4, 'dropped': 4, 'write_failures': 0})

    def test_a_full_queue_drops_any_line_and_reports_it(self):
        log = self.make(max_queued=2)
        self.assertEqual([log.emit('job_done', job_id=str(i)) for i in range(4)], [True, True, False, False])
        self.assertEqual(log.write_pending(), 3)
        self.assertEqual([r['event'] for r in lines(self.stream)], ['job_done', 'job_done', 'log_dropped'])
        self.assertEqual(lines(self.stream)[-1]['count'], 2)

    def test_drop_reports_come_at_most_every_half_second(self):
        log = self.make(max_queued=1)
        log.emit('job_done', job_id='a')
        log.emit('job_done', job_id='b')  # dropped
        log.write_pending()  # first report is due
        log.emit('job_done', job_id='c')
        log.emit('job_done', job_id='d')  # dropped
        log.write_pending()  # 0 s later: not due, merged into a later report
        self.clock.t += 0.5
        log.write_pending()
        self.assertEqual([(r['event'], r.get('job_id'), r.get('count')) for r in lines(self.stream)],
                         [('job_done', 'a', None), ('log_dropped', None, 1), ('job_done', 'c', None),
                          ('log_dropped', None, 1)])

    def test_a_flood_with_the_writer_running_stays_near_the_limit(self):
        # Review M1: drop reports must not track the request rate.
        stream = io.StringIO()
        log = ApiLog(stream, max_lines_per_s=2)
        log.start()
        for _ in range(1000):
            log.request('/v1/analyze', 'POST', 429, 0.001)
        self.assertTrue(log.stop(timeout=5))
        records = lines(stream)
        drops = [r for r in records if r['event'] == 'log_dropped']
        self.assertEqual(sum(r['event'] == 'request' for r in records), 1000 - sum(r['count'] for r in drops))
        self.assertLessEqual(len(drops), 5, drops)
        self.assertEqual(sum(r['count'] for r in drops), log.report()['dropped'])
        self.assertGreaterEqual(log.report()['dropped'], 990)

    def test_lines_after_stop_are_counted_not_written(self):
        log = self.make()
        log.emit('job_done', job_id='a')
        self.assertTrue(log.stop())
        self.assertFalse(log.emit('job_done', job_id='late'))
        self.assertFalse(log.request('/v1/analyze', 'POST', 200, 0.1))
        self.assertEqual([r['job_id'] for r in lines(self.stream)], ['a'])
        self.assertEqual(log.report(), {'lines': 1, 'dropped': 2, 'write_failures': 0})

    def test_stop_reports_a_writer_blocked_by_its_stream(self):
        release = threading.Event()

        class Blocking(io.StringIO):
            def write(self, text):
                release.wait(10)
                return super().write(text)
        log = ApiLog(Blocking())
        log.start()
        self.addCleanup(release.set)
        log.emit('job_done', job_id='a')
        self.assertFalse(log.stop(timeout=0.2), 'not in time: the caller is told')
        release.set()
        log._thread.join(5)
        self.assertEqual(log.report()['lines'], 1)

    def test_detector_ready_lost_to_a_full_queue_is_tried_again(self):
        log = self.make(max_queued=1)
        log.emit('job_done', job_id='a')
        self.assertFalse(log.detector_ready('fx-model', 1.0))  # queue full
        log.write_pending()
        self.assertTrue(log.detector_ready('fx-model', 0.0))
        self.assertFalse(log.detector_ready('fx-model', 0.0))

    def test_detector_ready_is_logged_once(self):
        log = self.make()
        self.assertTrue(log.detector_ready('fx-model', 1.5))
        self.assertFalse(log.detector_ready('fx-model', 0.0))
        log.write_pending()
        self.assertEqual([(r['event'], r['model_id'], r['warmup_seconds']) for r in lines(self.stream)],
                         [('detector_ready', 'fx-model', 1.5)])

    def test_relay_losses_are_logged_as_deltas_after_a_baseline(self):
        log = self.make()
        self.assertEqual(log.relay_stats({'gaps': 2, 'interruptions': 1, 'relayed': 50}), 0, 'baseline only')
        self.assertEqual(log.relay_stats({'gaps': 2, 'interruptions': 1}), 0)
        self.assertEqual(log.relay_stats({'gaps': 5, 'interruptions': 2, 'restarts': 1, 'connection_errors': 0}), 3)
        self.assertEqual(log.relay_stats({'gaps': 4, 'interruptions': 'x', 'restarts': 1}), 0, 'no negative or odd')
        log.write_pending()
        self.assertEqual([(r['event'], r['count'], r['level']) for r in lines(self.stream)],
                         [('relay_gap', 3, 'warning'), ('relay_interruption', 1, 'warning'),
                          ('relay_source_restart', 1, 'warning')])

    def test_relay_stats_read_out_of_order_never_log_a_loss_twice(self):
        # Claude-authored, 18 Nov (re-review low 1): the pump and /metrics read 5 then 6
        # but store 6 then 5; the next read of 6 is not a new gap.
        log = self.make()
        log.relay_stats({'gaps': 4})
        self.assertEqual(log.relay_stats({'gaps': 6}), 1)
        self.assertEqual(log.relay_stats({'gaps': 5}), 0)
        self.assertEqual(log.relay_stats({'gaps': 6}), 0)
        self.assertEqual(log.relay_stats({'gaps': 7}), 1)
        log.write_pending()
        self.assertEqual([(r['event'], r['count']) for r in lines(self.stream)],
                         [('relay_gap', 2), ('relay_gap', 1)])

    def test_a_broken_stream_is_counted_never_raised(self):
        log = ApiLog(BrokenStream())
        log.emit('job_done', job_id='a')
        self.assertEqual(log.write_pending(), 0)
        self.assertEqual(log.report(), {'lines': 0, 'dropped': 0, 'write_failures': 1})

    def test_invalid_settings_are_refused(self):
        for options in ({'max_lines_per_s': 0}, {'max_lines_per_s': True}, {'max_queued': 0}, {'max_queued': 1.5}):
            with self.subTest(options), self.assertRaises(ValueError):
                ApiLog(io.StringIO(), **options)

    def test_the_writer_thread_writes_every_line_from_many_threads_and_stops(self):
        stream = io.StringIO()
        log = ApiLog(stream, max_queued=10_000)
        log.start()
        with self.assertRaises(RuntimeError):
            log.start()

        def produce(n):
            for i in range(200):
                log.emit('job_done', job_id=f'{n}-{i}')
        threads = [threading.Thread(target=produce, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(log.stop(timeout=5))
        records = lines(stream)
        self.assertEqual(len(records), 800)
        self.assertEqual(len({r['job_id'] for r in records}), 800)
        self.assertEqual(log.report()['lines'], 800)

    def test_stop_without_start_writes_what_is_queued(self):
        log = self.make()
        log.emit('job_queued', job_id='a')
        self.assertTrue(log.stop())
        self.assertEqual(len(lines(self.stream)), 1)


@unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
class ApiAppLogTests(unittest.TestCase):
    def make(self, detector=None):
        self.stream = io.StringIO()
        self.log = ApiLog(self.stream)
        service = analysis.AnalysisService(lambda: detector or Detector(), model_id='fx-model')
        client = TestClient(create_app(service, oplog=self.log))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def wait_job(self, client, job_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            record = client.get(f'/v1/jobs/{job_id}').json()
            if record['status'] in ('done', 'failed'):
                return record
            time.sleep(0.05)
        self.fail('job did not finish')

    def records(self):
        self.log.write_pending()
        return lines(self.stream)

    def test_requests_jobs_and_readiness_are_logged_without_ids_in_routes(self):
        client = self.make()
        self.assertEqual(client.get('/healthz').status_code, 200)
        self.assertEqual(client.get('/readyz').status_code, 200)
        self.assertEqual(client.get('/readyz').status_code, 200)
        response = client.post('/v1/analyze?name=secret-query', files={'file': ('secret-name.wav', wav(61.0),
                                                                                'audio/wav')})
        self.assertEqual(response.status_code, 202)
        job_id = response.json()['job_id']
        self.assertEqual(self.wait_job(client, job_id)['status'], 'done')
        self.assertEqual(client.get('/v1/calls').status_code, 503)
        records = self.records()
        text = self.stream.getvalue()
        self.assertEqual([r for r in records if r['event'] == 'detector_ready'][0]['model_id'], 'fx-model')
        self.assertEqual(sum(r['event'] == 'detector_ready' for r in records), 1)
        jobs = [(r['event'], r['job_id']) for r in records if r['event'].startswith('job_')]
        self.assertEqual(jobs, [('job_queued', job_id), ('job_done', job_id)])
        routes = [(r['route'], r['method'], r['status']) for r in records if r['event'] == 'request']
        self.assertIn(('/v1/analyze', 'POST', 202), routes)
        self.assertIn(('/v1/jobs/:id', 'GET', 200), routes)
        self.assertIn(('/v1/calls', 'GET', 503), routes)
        self.assertFalse([r for r in routes if r[0] in PROBE_ROUTES], 'probe routes are not logged')
        for leaked in ('secret-query', 'secret-name', 'genuine', 'synthetic', 'p_synthetic', f'/v1/jobs/{job_id}'):
            self.assertNotIn(leaked, text)

    def test_unknown_paths_and_methods_are_fixed_labels(self):
        client = self.make()
        client.request('PROPFIND', '/private/secret-path?token=secret-token')
        client.get('/v1/jobs/secret-job-id')
        records = [r for r in self.records() if r['event'] == 'request']
        self.assertEqual([(r['route'], r['method']) for r in records], [('other', 'other'), ('/v1/jobs/:id', 'GET')])
        self.assertNotIn('secret', self.stream.getvalue())

    def test_a_failed_job_logs_its_status_only(self):
        class Failing:
            def __call__(self, samples):
                raise RuntimeError('detector exploded: secret-detail')
        client = self.make(Failing())
        job_id = client.post('/v1/analyze', files={'file': ('a.wav', wav(61.0), 'audio/wav')}).json()['job_id']
        record = self.wait_job(client, job_id)
        self.assertEqual(record['status'], 'failed')
        failed = [r for r in self.records() if r['event'] == 'job_failed']
        self.assertEqual(failed, [{'ts': failed[0]['ts'], 'level': 'warning', 'event': 'job_failed', 'job_id': job_id,
                                   'status': record['error']['status']}])
        self.assertNotIn('secret-detail', self.stream.getvalue())

    def test_a_raising_log_never_fails_a_request(self):
        class Raising(ApiLog):
            def emit(self, *args, **kwargs):
                raise RuntimeError('log down')

            def request(self, *args, **kwargs):
                raise RuntimeError('log down')

            def detector_ready(self, *args, **kwargs):
                raise RuntimeError('log down')
        service = analysis.AnalysisService(lambda: Detector(), model_id='fx-model')
        with TestClient(create_app(service, oplog=Raising(io.StringIO()))) as client:
            self.assertEqual(client.get('/readyz').status_code, 200)
            response = client.post('/v1/analyze', files={'file': ('a.wav', wav(61.0), 'audio/wav')})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(self.wait_job(client, response.json()['job_id'])['status'], 'done')
            self.assertEqual(client.get('/v1/calls').status_code, 503)


if __name__ == '__main__':
    unittest.main()
