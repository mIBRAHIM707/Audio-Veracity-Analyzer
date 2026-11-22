"""FR-O6 stage-latency histograms and live-call metrics on `/metrics` (backlog 7.2).

Claude-authored test design, 14 November 2026. Real AnalysisService with a fake
detector, real EventHub and call registry; strict exposition parser from the 12 Nov
readiness tests. Needs requirements-api.txt for the API cases.
"""
import math
import re
import time
import unittest

from services.events.fanout import EventHub
from services.inference import analysis
from tests.unit import test_analysis_api as api_fixture
from tests.unit.test_api_readiness_metrics import parse_metrics

TestClient = api_fixture.TestClient
if TestClient is not None:
    from services.api.app import ANALYSIS_STAGES, LATENCY_BUCKETS_S, Histogram, create_app


class SlowDetector:
    def __init__(self, seconds=0.02):
        self.seconds, self.calls = seconds, 0

    def __call__(self, samples):
        self.calls += 1
        time.sleep(self.seconds)
        return [0.0, 1.0]


def buckets(samples, family, label=''):
    """`le` -> cumulative count for one series, in exposition order."""
    pattern = re.compile(rf'{family}_bucket\{{{label}le="([^"]+)"\}}')
    return [(m.group(1), value) for key, value in samples.items() if (m := pattern.fullmatch(key))]


@unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
class HistogramTests(unittest.TestCase):
    def test_cumulative_buckets_sum_count_and_refusals(self):
        h = Histogram(('a', 'b'))
        for value in (0.0, 0.01, 0.3, 7.0, 1000.0):
            self.assertTrue(h.observe(value, 'a'))
        for bad, label in ((math.nan, 'a'), (-1.0, 'a'), (True, 'a'), ('1', 'a'), (1.0, 'c'), (math.inf, 'a')):
            self.assertFalse(h.observe(bad, label), (bad, label))
        a = h.series['a']
        expected = [sum(v <= bound for v in (0.0, 0.01, 0.3, 7.0, 1000.0)) for bound in LATENCY_BUCKETS_S]
        self.assertEqual(a['buckets'], expected)
        self.assertEqual((a['count'], a['sum']), (5, 1007.31))
        lines = h.render('x_seconds', 'stage')
        self.assertIn('x_seconds_bucket{stage="a",le="+Inf"} 5', lines)
        self.assertIn('x_seconds_count{stage="b"} 0', lines)
        self.assertIn('x_seconds_bucket{stage="a",le="0.01"} 2', lines, 'le bound is inclusive')


class StageTimingTests(unittest.TestCase):
    def test_exact_stage_times_with_a_stepping_clock_and_lazy_model_load(self):
        """Every clock read advances 1 s, so each timed span is exactly one step and the
        model load (first analysis, no warm-up) must not leak into any stage."""
        ticks = iter(range(10_000))
        detector = SlowDetector(0.0)
        service = analysis.AnalysisService(lambda: detector, model_id='fx', clock=lambda: float(next(ticks)))
        result = service.analyze_wav(api_fixture.wav(9.0))
        n = detector.calls
        self.assertGreater(n, 1)
        self.assertEqual(result['stage_seconds'], {'decode': 1.0, 'condition': 1.0, 'inference': float(n)})
        self.assertEqual(result['model_load_seconds'], 1.0)
        # reads: start, decoded, conditioned, load start/end, 2 per window, final
        self.assertEqual(result['processing_seconds'], (5 + 2 * n) - 0 - 1.0)


@unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
class ApiMetricsTests(unittest.TestCase):
    def make(self, hub=None, detector=None):
        service = analysis.AnalysisService(lambda: detector or SlowDetector(), model_id='fx-model')
        client = TestClient(create_app(service, hub=hub))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def test_analysis_reports_stage_seconds_and_histograms_observe_them(self):
        detector = SlowDetector(0.02)
        client = self.make(detector=detector)
        response = client.post('/v1/analyze', files={'file': ('fx.wav', api_fixture.wav(12.0), 'audio/wav')})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        stages = result['stage_seconds']
        self.assertEqual(set(stages), set(ANALYSIS_STAGES))
        self.assertTrue(all(v >= 0 for v in stages.values()))
        self.assertGreaterEqual(stages['inference'], detector.calls * 0.02 * 0.9)
        self.assertLessEqual(sum(stages.values()), result['processing_seconds'] + 1e-6)
        samples, declared = parse_metrics(client.get('/metrics').text)
        self.assertEqual((declared['ava_analysis_seconds'], declared['ava_analysis_stage_seconds']),
                         ('histogram', 'histogram'))
        self.assertEqual(samples['ava_analysis_seconds_count'], 1.0)
        series = buckets(samples, 'ava_analysis_seconds')
        self.assertEqual([le for le, _ in series], [repr(b) for b in LATENCY_BUCKETS_S] + ['+Inf'])
        counts = [n for _, n in series]
        self.assertEqual(counts, sorted(counts), 'cumulative buckets never decrease')
        self.assertEqual(counts[-1], 1.0)
        for stage in ANALYSIS_STAGES:
            self.assertEqual(samples[f'ava_analysis_stage_seconds_count{{stage="{stage}"}}'], 1.0)
        self.assertAlmostEqual(samples['ava_analysis_stage_seconds_sum{stage="inference"}'], stages['inference'])
        self.assertNotIn('ava_calls_active', declared, 'no live families without an event hub')

    def test_stale_gauge_counts_calls_idle_past_the_timeout(self):
        now = [0.0]
        hub = EventHub(clock=lambda: now[0])
        client = self.make(hub=hub)
        hub.publish('call.started', 'old', {})
        now[0] = 400.0  # past the 300 s idle timeout of event time
        hub.publish('call.started', 'new', {})
        samples, _ = parse_metrics(client.get('/metrics').text)
        self.assertEqual((samples['ava_calls_active'], samples['ava_calls_stale']), (2.0, 1.0))
        self.assertEqual(samples['ava_calls_idle_expired_total'], 0.0, 'no slot pressure, nothing expired')

    def test_live_call_metrics_come_from_the_registry_without_call_ids(self):
        hub = EventHub()
        client = self.make(hub=hub)
        hub.publish('call.started', 'secret-call-7', {})
        hub.publish('alert.raised', 'secret-call-7', {'alert_id': 'secret-alert-9'})
        hub.publish('call.started', 'quiet', {})
        hub.publish('call.ended', 'quiet', {'final_state': 'monitoring', 'reason': 'bye', 'speech_seconds': 1.0})
        text = client.get('/metrics').text
        samples, declared = parse_metrics(text)
        self.assertEqual(declared['ava_alerts_raised_total'], 'counter')
        self.assertEqual((samples['ava_calls_active'], samples['ava_alerts_open'], samples['ava_alerts_raised_total'],
                          samples['ava_calls_stale']), (1.0, 1.0, 1.0, 0.0))
        self.assertEqual(samples['ava_event_subscribers'], 1.0)
        self.assertEqual(samples['ava_call_registry_events_total{outcome="applied"}'], 4.0)
        self.assertEqual(samples['ava_call_registry_lost_events_total'], 0.0)
        self.assertNotIn('secret', text)
        hub.publish('call.ended', 'quiet', {'final_state': 'monitoring', 'reason': 'bye', 'speech_seconds': 1.0})
        hub.publish('system.health', None, {'reason': 'capacity'})
        samples, _ = parse_metrics(client.get('/metrics').text)
        outcomes = {k: v for k, v in samples.items() if k.startswith('ava_call_registry_events_total{')}
        self.assertEqual(sum(outcomes.values()), 6.0, 'exclusive outcomes sum to the envelopes received')
        self.assertEqual((outcomes['ava_call_registry_events_total{outcome="after_end"}'],
                          outcomes['ava_call_registry_events_total{outcome="ignored_system_health"}']), (1.0, 1.0))
        with client.websocket_connect('/v1/events'):
            deadline = time.monotonic() + 5
            while hub.subscriber_count < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            samples, _ = parse_metrics(client.get('/metrics').text)
            self.assertEqual(samples['ava_event_subscribers'], 2.0)


if __name__ == '__main__':
    unittest.main()
