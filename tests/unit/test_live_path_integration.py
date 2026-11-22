"""In-process integration of the live path (backlog 5.QA partial; FR-A3, FR-A4, FR-A8,
FR-I5/FR-I7 (alert raise), FR-I9 (capacity refusal), FR-I11 (end reasons, late
results)). Stride growth, agent direction, alert clear and latency histograms are
covered by the unit modules, not here.

Claude-authored test design, 14 November 2026. Every component is real except the
detector (a fake whose p_synthetic equals the constant window value) and the absence
of telephony: LivePipeline -> LiveCalls (CallGate, OrderedCallScorer, publisher) ->
EventHub -> API (call registry, `/v1/calls`, `WS /v1/events`, `/metrics`). This is
infrastructure evidence, not model or telephony acceptance.
"""
import json
import threading
import time
import unittest

import numpy as np

from services.events.fanout import EventHub
from services.inference import analysis
from services.inference.analysis import WINDOW_SAMPLES
from services.inference.capacity import CallGate, MicroBatcher
from services.scorer.config import ScoringConfig
from services.scorer.live import LiveCalls
from services.scorer.pipeline import LivePipeline
from tests.unit import test_analysis_api as api_fixture
from tests.unit.test_api_readiness_metrics import parse_metrics

TestClient = api_fixture.TestClient
if TestClient is not None:
    from services.api.app import create_app

CONFIG = ScoringConfig(ema_alpha=1.0)


class Configs:
    def for_new_call(self):
        return CONFIG


def detector(batch):
    values = batch[:, 0].astype(np.float64)
    return np.stack([np.log(values / (1 - values)), np.zeros_like(values)], axis=1)


def window(value):
    return np.full(WINDOW_SAMPLES, value, dtype=np.float32)


@unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
class LivePathIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.hub = EventHub()
        ids = iter(f'alert-{i}' for i in range(1, 99))
        self.live = LiveCalls(Configs(), self.hub, CallGate(2), new_alert_id=lambda: next(ids))
        self.pipe = LivePipeline(self.live, MicroBatcher(capacity_windows_per_s=10.0, collect_s=0.1),
                                 detector, CONFIG)
        service = analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')
        self.client = TestClient(create_app(service, hub=self.hub))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.now = 0.0

    def hop(self, call_scores):
        """One 2 s hop: offer one window per call, schedule, run the due batch."""
        for call_id, (index, value) in call_scores.items():
            self.pipe.offer(call_id, 'caller', index, window(value), self.now)
        self.pipe.schedule(self.now)
        applied = self.pipe.step(self.now + 0.1)
        self.now += 2.0
        return applied

    def receive(self, ws, count, timeout=5.0):
        """Read `count` messages, failing (not hanging) if they do not arrive."""
        out = []
        reader = threading.Thread(target=lambda: out.extend(json.loads(ws.receive_text()) for _ in range(count)),
                                  daemon=True)
        reader.start()
        reader.join(timeout)
        self.assertFalse(reader.is_alive(), f'only {len(out)} of {count} WebSocket messages arrived')
        return out

    def test_alert_lifecycle_reaches_rest_websocket_and_metrics(self):
        everything = self.hub.subscribe(buffer=1024)  # sees the call-less and other-call events
        with self.client.websocket_connect('/v1/events?call_id=suspect') as ws:
            deadline = time.monotonic() + 5
            while self.hub.subscriber_count < 3 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(self.hub.subscriber_count, 3, 'registry feed, test subscriber, WebSocket')
            self.assertTrue(self.live.start('suspect'))
            self.assertTrue(self.live.start('normal'))
            self.assertFalse(self.live.start('third'), 'CallGate(2): the third call is not tapped')
            for i, value in enumerate((0.9, 0.9, 0.9, 0.95)):
                self.assertEqual(self.hop({'suspect': (i, value), 'normal': (i, 0.1)}), 2)
            received = self.receive(ws, 6)
        self.assertEqual([m['type'] for m in received],
                         ['call.started', 'score.updated', 'score.updated', 'score.updated', 'alert.raised',
                          'score.updated'])
        seqs = [m['seq'] for m in received]
        self.assertTrue(all(a < b for a, b in zip(seqs, seqs[1:])), seqs)
        self.assertEqual({m['call_id'] for m in received}, {'suspect'})
        self.assertEqual(received[4]['data']['alert_id'], 'alert-1')
        health = [e for e in everything.drain()[0] if e.type == 'system.health']
        self.assertEqual([(e.call_id, e.data['reason']) for e in health], [('third', 'capacity')])

        detail = self.client.get('/v1/calls/suspect').json()
        self.assertEqual((detail['status'], detail['state'], detail['alerts_open']), ('active', 'alert', 1))
        self.assertEqual([round(p['score'], 6) for p in detail['timeline']['full_resolution']],
                         [0.9, 0.9, 0.9, 0.95])
        normal = self.client.get('/v1/calls/normal').json()
        self.assertEqual((normal['state'], normal['alerts_total']), ('monitoring', 0))
        self.assertEqual(self.client.get('/v1/calls/third').status_code, 404)

        samples, _ = parse_metrics(self.client.get('/metrics').text)
        self.assertEqual((samples['ava_calls_active'], samples['ava_alerts_open'], samples['ava_alerts_raised_total']),
                         (2.0, 1.0, 1.0))

        self.pipe.end_call('normal', 'bye', speech_seconds={'caller': 8.0})
        self.assertEqual(self.live.end_all('monitor_lost', speech_seconds_for=lambda c, d: {x: 6.0 for x in d}), 1)
        calls = {c['call_id']: c for c in self.client.get('/v1/calls').json()['calls']}
        self.assertEqual((calls['normal']['end_reason'], calls['suspect']['end_reason']), ('bye', 'monitor_lost'))
        suspect = self.client.get('/v1/calls/suspect').json()
        self.assertEqual((suspect['final_state'], suspect['alerts'][0]['state'], suspect['speech_seconds']),
                         ('alert', 'ended_open', 6.0))
        body = self.client.get('/v1/calls').json()['registry']
        self.assertEqual((body['pump_errors'], body['lost_events'], body['apply_errors']), (0, 0, 0))
        samples, _ = parse_metrics(self.client.get('/metrics').text)
        self.assertEqual((samples['ava_calls_active'], samples['ava_alerts_open']), (0.0, 0.0),
                         'an ended_open alert is not counted as open')
        self.assertTrue(self.live.start('third'), 'slots are free again after the ends')

    def test_a_late_result_after_monitor_lost_publishes_nothing(self):
        self.live.start('c')
        self.pipe.offer('c', 'caller', 0, window(0.6), self.now)
        self.pipe.schedule(self.now)  # in the batcher when the tap receiver restarts
        self.live.end_all('monitor_lost', speech_seconds_for=lambda c, d: {x: 0.0 for x in d})
        self.assertEqual(self.pipe.step(self.now + 0.1), 0)
        self.assertEqual(self.pipe.counts['skipped_ended'], 1)
        detail = self.client.get('/v1/calls/c').json()
        self.assertEqual((detail['end_reason'], detail['timeline']['full_resolution']), ('monitor_lost', []))

    def test_a_detector_outage_leaves_calls_and_api_running(self):
        self.live.start('c')
        self.hop({'c': (0, 0.3)})
        self.pipe.detector = lambda batch: (_ for _ in ()).throw(RuntimeError('model crashed'))
        self.assertEqual(self.hop({'c': (1, 0.3)}), 0)
        self.assertEqual((self.pipe.counts['detector_failures'], self.pipe.counts['windows_lost_to_detector']),
                         (1, 1))
        self.pipe.detector = detector
        self.assertEqual(self.hop({'c': (2, 0.3)}), 1)
        detail = self.client.get('/v1/calls/c').json()
        self.assertEqual(detail['status'], 'active')
        self.assertEqual(len(detail['timeline']['full_resolution']), 2, 'index 1 was lost, 0 and 2 scored')
        self.assertEqual(self.pipe.counts['scored'], 2)


if __name__ == '__main__':
    unittest.main()
