"""FR-A8 readiness (warm-up inference) and Prometheus metrics (backlogs 7.2 / 8.1).

Claude-authored test design, 12 November 2026. Fake detectors and generated PCM;
the API cases need requirements-api.txt and are skipped without FastAPI.
"""
import math
import re
import threading
import unittest

import numpy as np

from services.inference import analysis
from tests.unit import test_analysis_api as api_fixture

TestClient = api_fixture.TestClient


class CountingDetector:
    def __init__(self, logits=(2.0, 0.0)):
        self.calls, self.logits = [], list(logits)

    def __call__(self, samples):
        self.calls.append(samples)
        return self.logits


class WarmUpTests(unittest.TestCase):
    def test_warm_up_scores_one_silent_window_once(self):
        detector = CountingDetector()
        service = analysis.AnalysisService(lambda: detector, model_id='fx')
        self.assertFalse(service.ready)
        seconds = service.warm_up()
        self.assertTrue(service.ready and service.detector_loaded)
        self.assertGreaterEqual(seconds, 0)
        self.assertEqual(len(detector.calls), 1)
        window = detector.calls[0]
        self.assertEqual((window.dtype, window.shape), (np.float32, (analysis.WINDOW_SAMPLES,)))
        self.assertFalse(window.any())
        self.assertEqual(service.warm_up(), seconds)
        self.assertEqual(len(detector.calls), 1, 'readiness is sticky')

    def test_failures_leave_the_service_not_ready_and_retryable(self):
        attempts = []

        def factory():
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError('model volume missing')
            return CountingDetector()

        service = analysis.AnalysisService(factory, model_id='fx')
        with self.assertRaisesRegex(analysis.DetectorFailure, 'Detector unavailable'):
            service.warm_up()
        self.assertFalse(service.ready)
        service.warm_up()
        self.assertTrue(service.ready)
        for logits in ((math.nan, 0.0), (1.0,), ('a', 'b')):
            bad = analysis.AnalysisService(lambda: CountingDetector(logits), model_id='fx')
            with self.assertRaises(analysis.DetectorFailure):
                bad.warm_up()
            self.assertFalse(bad.ready)

        def broken(samples):
            raise RuntimeError('kernel fault')

        crashing = analysis.AnalysisService(lambda: broken, model_id='fx')
        with self.assertRaisesRegex(analysis.DetectorFailure, 'Warm-up inference failed: RuntimeError'):
            crashing.warm_up()

    def test_warm_up_refuses_while_an_analysis_holds_the_detector(self):
        service = analysis.AnalysisService(CountingDetector, model_id='fx')
        with service._lock:
            with self.assertRaises(analysis.AnalyzerBusy):
                service.warm_up()
        self.assertFalse(service.ready)

    def test_a_warmed_service_stays_ready_while_busy(self):
        service = analysis.AnalysisService(CountingDetector, model_id='fx')
        seconds = service.warm_up()
        with service._lock:
            self.assertEqual(service.warm_up(), seconds)


def parse_metrics(text):
    """Strict reader: every sample belongs to a family declared by HELP and TYPE first."""
    samples, declared, helped = {}, {}, set()
    for line in text.strip().split('\n'):
        if line.startswith('# HELP '):
            helped.add(line.split()[2])
        elif line.startswith('# TYPE '):
            _, _, name, kind = line.split()
            assert name in helped, f'TYPE before HELP for {name}'
            declared[name] = kind
        else:
            match = re.fullmatch(r'([a-z_]+)(\{[^}]*\})? (\S+)', line)
            assert match, line
            name = match.group(1)
            family = name if name in declared else re.sub(r'_(sum|count|bucket)$', '', name)
            assert family in declared, f'undeclared sample {name}'
            if declared[family] == 'counter':
                assert name.endswith('_total'), name
            if name.endswith('_bucket'):  # 14 Nov: histograms (FR-O6) need an `le` label
                assert declared[family] == 'histogram' and 'le="' in (match.group(2) or ''), line
            samples[name + (match.group(2) or '')] = float(match.group(3))
    return samples, declared


def post(client, payload):
    return client.post('/v1/analyze', files={'file': ('fx.wav', payload, 'audio/wav')})


@unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
class ReadinessAndMetricsApiTests(unittest.TestCase):
    def make(self, factory):
        service = analysis.AnalysisService(factory, model_id='fx-model')
        client = TestClient(api_fixture.create_app(service))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client, service

    def test_readyz_warms_up_once_then_stays_ready(self):
        detector = CountingDetector()
        client, _ = self.make(lambda: detector)
        first = client.get('/readyz')
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual((first.json()['status'], first.json()['model_id']), ('ready', 'fx-model'))
        self.assertEqual(client.get('/readyz').status_code, 200)
        self.assertEqual(len(detector.calls), 1)
        self.assertTrue(client.get('/healthz').json()['detector_loaded'])

    def test_readyz_reports_detector_failure_busy_and_stopped(self):
        client, service = self.make(lambda: CountingDetector((math.nan, 0.0)))
        response = client.get('/readyz')
        self.assertEqual(response.status_code, 503)
        self.assertIn('invalid logits', response.json()['reason'])
        client, service = self.make(CountingDetector)
        with service._lock:
            busy = client.get('/readyz')
        self.assertEqual((busy.status_code, busy.json()['reason']), (503, 'busy'))
        client.app.state.jobs.close()
        stopped = client.get('/readyz')
        self.assertEqual((stopped.status_code, stopped.json()['reason']), (503, 'job worker stopped'))

    def test_metrics_exposition_counts_routes_jobs_and_readiness(self):
        client, _ = self.make(CountingDetector)
        response = client.get('/metrics')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers['content-type'].startswith('text/plain; version=0.0.4'))
        samples, declared = parse_metrics(response.text)
        self.assertEqual((samples['ava_ready'], samples['ava_detector_loaded']), (0.0, 0.0))
        self.assertEqual(samples['ava_job_queue_capacity'], 4.0)
        # 14 Nov: the summary became a histogram (architecture: stage latencies as histograms).
        self.assertEqual((declared['ava_api_requests_total'], declared['ava_analysis_seconds']), ('counter', 'histogram'))

        self.assertEqual(post(client, api_fixture.wav(8.0)).status_code, 200)
        self.assertEqual(post(client, b'not a wav').status_code, 422)
        self.assertEqual(client.get('/v1/jobs/abc123').status_code, 404)
        self.assertEqual(client.post('/v1/analyze', content=b'x', headers={'content-length': str(200 << 20)}).status_code, 413)
        job = post(client, api_fixture.wav(70.0))
        self.assertEqual(job.status_code, 202, job.text)
        job_id = job.json()['job_id']
        api_fixture.AnalysisApiTests.wait_job(self, client, job_id)
        client.get('/readyz')
        text = client.get('/metrics').text
        samples, _ = parse_metrics(text)
        self.assertEqual(samples['ava_api_requests_total{route="/v1/analyze",status="2xx"}'], 2.0)
        self.assertEqual(samples['ava_api_requests_total{route="/v1/analyze",status="4xx"}'], 2.0)
        self.assertEqual(samples['ava_api_requests_total{route="/v1/jobs/:id",status="4xx"}'], 1.0)
        self.assertGreaterEqual(samples['ava_api_requests_total{route="/v1/jobs/:id",status="2xx"}'], 1.0)
        self.assertEqual(samples['ava_api_requests_total{route="/metrics",status="2xx"}'], 1.0)
        self.assertEqual(samples['ava_jobs{state="done"}'], 1.0)
        self.assertEqual(samples['ava_analysis_seconds_count'], 2.0)
        self.assertGreater(samples['ava_analysis_seconds_sum'], 0.0)
        self.assertEqual(samples['ava_ready'], 1.0)
        self.assertEqual(samples['ava_uploads_in_flight'], 0.0)
        self.assertNotIn(job_id, text, 'job IDs are never metric labels')
        self.assertNotIn('abc123', text)

    def test_readyz_stays_ready_while_an_analysis_runs(self):
        client, service = self.make(CountingDetector)
        self.assertEqual(client.get('/readyz').status_code, 200)
        with service._lock:
            self.assertEqual(client.get('/readyz').status_code, 200)

    def test_unhandled_errors_are_counted_as_5xx(self):
        class Exploding(analysis.AnalysisService):
            def analyze_wav(self, payload, max_seconds=analysis.MAX_SYNC_SECONDS):
                raise RuntimeError('unexpected')

        service = Exploding(CountingDetector, model_id='fx')
        client = TestClient(api_fixture.create_app(service), raise_server_exceptions=False)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        self.assertEqual(post(client, api_fixture.wav(8.0)).status_code, 500)
        samples, _ = parse_metrics(client.get('/metrics').text)
        self.assertEqual(samples['ava_api_requests_total{route="/v1/analyze",status="5xx"}'], 1.0)

    def test_unknown_paths_share_one_label(self):
        client, _ = self.make(CountingDetector)
        for path in ('/x', '/y/z', '/v1/other'):
            client.get(path)
        samples, _ = parse_metrics(client.get('/metrics').text)
        self.assertEqual(samples['ava_api_requests_total{route="other",status="4xx"}'], 3.0)
        self.assertFalse(any('/y/z' in name for name in samples))


class RouteLabelTests(unittest.TestCase):
    @unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
    def test_route_labels_are_bounded(self):
        from services.api.app import route_label
        self.assertEqual(route_label('/v1/jobs/0123abcd'), '/v1/jobs/:id')
        self.assertEqual(route_label('/healthz'), '/healthz')
        self.assertEqual(route_label('/healthz/extra'), 'other')


if __name__ == '__main__':
    unittest.main()
