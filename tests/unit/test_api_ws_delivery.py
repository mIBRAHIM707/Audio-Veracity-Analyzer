"""FR-O6 WebSocket delivery timing on the analysis API's `/metrics` (backlog 7.2).

Claude-authored test design, 18 November 2026 (Claude Code, not GPT-6 Astra).
`ava_ws_delivery_seconds` is the time from an envelope's `ts` (its publish time; kept
across the Redis relay) until `WS /v1/events` finished sending it. The cases use the
real app, a real `EventHub` and an injected wall clock, so every observed delay is
exact. The `Metrics` cases need no FastAPI; the app cases need requirements-api.txt.
"""
import asyncio
import errno
import json
import math
import threading
import time
import unittest
from unittest.mock import patch

from services.events.fanout import EventHub
from services.inference import analysis
from tests.unit import test_analysis_api as api_fixture
from tests.unit.test_api_readiness_metrics import parse_metrics

TestClient = api_fixture.TestClient
if TestClient is not None:
    from starlette.websockets import WebSocket, WebSocketDisconnect

    from services.api import app as app_module
    from services.api.app import LATENCY_BUCKETS_S, WS_DELIVERY_BUCKETS_S, Histogram, Metrics, create_app

SCORE = {'score': 0.7, 'state': 'suspicious', 'degraded': False, 'effective_stride_s': 1.0}


class SettableClock:
    """Wall clock the test sets; `fail` makes the next reads raise."""

    def __init__(self, now=0.0):
        self.now, self.fail, self.reads = now, False, 0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.reads += 1
            if self.fail:
                raise OSError('clock unavailable')
            return self.now


def ws_buckets(samples):
    return {key.split('le="')[1].rstrip('"}'): value for key, value in samples.items()
            if key.startswith('ava_ws_delivery_seconds_bucket{')}


@unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
class DeliveryMetricsUnitTests(unittest.TestCase):
    def test_histogram_takes_its_own_buckets_and_the_default_is_unchanged(self):
        self.assertEqual(Histogram().bounds, LATENCY_BUCKETS_S)
        h = Histogram(buckets=WS_DELIVERY_BUCKETS_S)
        for value in (0.0, 0.005, 0.2, 0.2000001, 31.0):
            self.assertTrue(h.observe(value))
        lines = h.render('x_seconds')
        self.assertEqual([line.split('le="')[1].split('"')[0] for line in lines if '_bucket' in line],
                         ['0.005', '0.01', '0.025', '0.05', '0.1', '0.2', '0.3', '0.5', '1.0', '2.5', '5.0',
                          '10.0', '30.0', '+Inf'])
        self.assertIn('x_seconds_bucket{le="0.005"} 2', lines, 'bounds are inclusive')
        self.assertIn('x_seconds_bucket{le="0.2"} 3', lines)
        self.assertIn('x_seconds_bucket{le="30.0"} 4', lines, '31 s only in +Inf')
        self.assertIn('x_seconds_bucket{le="+Inf"} 5', lines)
        self.assertIn(0.2, WS_DELIVERY_BUCKETS_S, 'the architecture §3 200 ms budget is a bound')

    def test_unusable_differences_are_unmeasured_never_observed(self):
        metrics = Metrics()
        metrics.observe_ws_delivery(100.25, 100.0)
        metrics.observe_ws_delivery(100.0, 100.0)  # zero is a valid delay
        for sent_at, ts in ((99.0, 100.0),          # sender clock ahead of ours
                            (None, 100.0),          # our clock failed
                            (math.nan, 1.0), (math.inf, 1.0), (math.inf, math.inf),
                            (True, 0.0), ('5', 1.0), (5.0, None)):
            metrics.observe_ws_delivery(sent_at, ts)
        self.assertEqual(metrics.ws_delivery.series[None]['count'], 2)
        self.assertEqual(metrics.ws_delivery.series[None]['sum'], 0.25)
        self.assertEqual(metrics.ws_unmeasured, 8)


@unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
class WebSocketDeliveryTests(unittest.TestCase):
    def make(self, hub, clock):
        service = analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')
        client = TestClient(create_app(service, hub=hub, wall_clock=clock))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def metrics(self, client):
        samples, declared = parse_metrics(client.get('/metrics').text)
        self.assertEqual(declared['ava_ws_delivery_seconds'], 'histogram')
        self.assertEqual(declared['ava_ws_delivery_unmeasured_total'], 'counter')
        self.assertEqual(declared['ava_ws_slow_client_closes_total'], 'counter')
        return samples

    def wait_for(self, predicate, what):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(predicate(), what)

    def test_each_sent_event_is_timed_from_its_ts_including_relayed_ones(self):
        hub, clock = EventHub(), SettableClock(1_000.3)
        client = self.make(hub, clock)
        before = self.metrics(client)
        self.assertEqual(before['ava_ws_delivery_seconds_count'], 0, 'present at zero before any client')
        with client.websocket_connect('/v1/events') as ws:
            self.wait_for(lambda: hub.subscriber_count == 2, 'socket subscribed')
            # Relayed events keep the scorer's ts (RedisRelay passes it to publish).
            hub.publish('call.started', 'c1', {}, ts=1_000.0)          # 0.3 s
            hub.publish('score.updated', 'c1', SCORE, ts=1_000.29)     # 0.01 s
            hub.publish('score.updated', 'c1', SCORE, ts=1_000.3)      # 0 s
            got = [json.loads(ws.receive_text()) for _ in range(3)]
            self.assertEqual([e['seq'] for e in got], [1, 2, 3])
            self.wait_for(lambda: self.metrics(client)['ava_ws_delivery_seconds_count'] == 3, 'three timed')
        samples = self.metrics(client)
        buckets = ws_buckets(samples)
        self.assertEqual((buckets['0.005'], buckets['0.01'], buckets['0.2'], buckets['0.5'], buckets['+Inf']),
                         (1, 2, 2, 3, 3))
        self.assertAlmostEqual(samples['ava_ws_delivery_seconds_sum'], 0.31, places=6)
        self.assertEqual(samples['ava_ws_delivery_unmeasured_total'], 0)

    def test_two_sockets_time_the_same_event_once_each(self):
        hub, clock = EventHub(), SettableClock(50.5)
        client = self.make(hub, clock)
        with client.websocket_connect('/v1/events') as a, client.websocket_connect('/v1/events?call_id=c') as b:
            self.wait_for(lambda: hub.subscriber_count == 3, 'both sockets subscribed')
            hub.publish('call.started', 'c', {}, ts=50.0)
            hub.publish('call.started', 'other', {}, ts=50.0)  # only socket a
            [a.receive_text() for _ in range(2)]
            b.receive_text()
            self.wait_for(lambda: self.metrics(client)['ava_ws_delivery_seconds_count'] == 3, 'three sends')
        self.assertAlmostEqual(self.metrics(client)['ava_ws_delivery_seconds_sum'], 1.5, places=6)

    def test_ts_ahead_of_our_clock_or_a_failing_clock_is_unmeasured_and_the_event_still_arrives(self):
        hub, clock = EventHub(), SettableClock(10.0)
        client = self.make(hub, clock)
        with client.websocket_connect('/v1/events') as ws:
            self.wait_for(lambda: hub.subscriber_count == 2, 'socket subscribed')
            hub.publish('call.started', 'ahead', {}, ts=10.5)  # another process's clock is ahead
            self.assertEqual(json.loads(ws.receive_text())['call_id'], 'ahead')
            self.wait_for(lambda: self.metrics(client)['ava_ws_delivery_unmeasured_total'] == 1, 'counted')
            clock.fail = True
            hub.publish('call.started', 'broken', {}, ts=1.0)
            self.assertEqual(json.loads(ws.receive_text())['call_id'], 'broken', 'a clock fault never blocks')
            self.wait_for(lambda: self.metrics(client)['ava_ws_delivery_unmeasured_total'] == 2, 'counted')
            clock.fail = False
            hub.publish('call.started', 'fine', {}, ts=9.0)
            self.assertEqual(json.loads(ws.receive_text())['call_id'], 'fine', 'the socket survived')
            self.wait_for(lambda: self.metrics(client)['ava_ws_delivery_seconds_count'] == 1, 'timed again')
        self.assertEqual(self.metrics(client)['ava_ws_delivery_seconds_sum'], 1.0)

    def test_gap_control_messages_are_not_timed(self):
        hub, clock = EventHub(), SettableClock(2.0)
        client = self.make(hub, clock)
        for name, value in (('WS_BUFFER', 2), ('WS_POLL_S', 3.0)):
            self.addCleanup(setattr, app_module, name, getattr(app_module, name))
            setattr(app_module, name, value)
        with client.websocket_connect('/v1/events?call_id=c') as ws:
            self.wait_for(lambda: hub.subscriber_count == 2, 'socket subscribed')
            # The socket's first empty drain follows its subscription at once; the next is
            # 3 s away. Timing-based (as in test_api_calls_events): a runner stalled for
            # 0.5 s would put the publishes before the first drain and change the gap.
            time.sleep(0.5)
            for i in range(5):
                hub.publish('score.updated', 'c', {**SCORE, 'score': i / 10}, ts=1.0)
            gap, *events = [json.loads(ws.receive_text()) for _ in range(3)]
            self.assertEqual(gap['type'], 'stream.gap')
            self.wait_for(lambda: self.metrics(client)['ava_ws_delivery_seconds_count'] == 2, 'two timed')
        samples = self.metrics(client)
        self.assertEqual((samples['ava_ws_delivery_seconds_count'], samples['ava_ws_delivery_unmeasured_total']),
                         (2, 0), 'dropped envelopes and the gap message are not delivery times')

    def test_the_clock_is_read_after_the_send_completes(self):
        hub, clock = EventHub(), SettableClock(7.0)
        client = self.make(hub, clock)
        original = WebSocket.send_text

        async def slow_send(websocket, text):  # the send itself takes 0.25 s of wall clock
            await original(websocket, text)
            clock.now += 0.25
        with patch.object(WebSocket, 'send_text', slow_send), client.websocket_connect('/v1/events') as ws:
            self.wait_for(lambda: hub.subscriber_count == 2, 'socket subscribed')
            hub.publish('call.started', 'c', {}, ts=7.0)
            ws.receive_text()
            self.wait_for(lambda: self.metrics(client)['ava_ws_delivery_seconds_count'] == 1, 'timed')
        self.assertEqual(self.metrics(client)['ava_ws_delivery_seconds_sum'], 0.25, 'read before the send: 0')

    def test_a_client_that_cannot_be_sent_to_is_closed_and_counted(self):
        hub, clock = EventHub(), SettableClock(5.0)
        client = self.make(hub, clock)
        # A send that never completes, as to a client that stopped reading; a short
        # timeout stands in for the 10 s one (review L, 18 Nov: no reliance on how
        # `asyncio.wait_for` treats a zero timeout, which changed in Python 3.12).
        self.addCleanup(setattr, app_module, 'WS_SEND_TIMEOUT_S', app_module.WS_SEND_TIMEOUT_S)
        app_module.WS_SEND_TIMEOUT_S = 0.05

        async def stuck_send(websocket, text):
            await asyncio.Event().wait()
        with patch.object(WebSocket, 'send_text', stuck_send), client.websocket_connect('/v1/events') as ws:
            self.wait_for(lambda: hub.subscriber_count == 2, 'socket subscribed')
            hub.publish('call.started', 'c', {}, ts=4.0)
            with self.assertRaises(WebSocketDisconnect) as caught:
                ws.receive_text()
            self.assertEqual(caught.exception.code, 1008)
        self.wait_for(lambda: hub.subscriber_count == 1, 'the closed socket unsubscribed')
        samples = self.metrics(client)
        self.assertEqual(samples['ava_ws_slow_client_closes_total'], 1)
        self.assertEqual(samples['ava_ws_delivery_seconds_count'], 0, 'an event not sent is not timed')

    def test_a_transport_timeout_error_is_a_departed_client_not_a_slow_one(self):
        # Review L1 (18 Nov): on Python 3.11 asyncio.TimeoutError is the built-in
        # TimeoutError, a subclass of OSError, so a transport ETIMEDOUT from the send used
        # to be counted as a slow-client close. It must be treated as the client leaving.
        hub, clock = EventHub(), SettableClock(5.0)
        client = self.make(hub, clock)

        async def failing_send(websocket, text):
            raise OSError(errno.ETIMEDOUT, 'connection timed out')
        self.assertIsInstance(OSError(errno.ETIMEDOUT, 'x'), TimeoutError, 'the case this test guards')
        with patch.object(WebSocket, 'send_text', failing_send), client.websocket_connect('/v1/events'):
            self.wait_for(lambda: hub.subscriber_count == 2, 'socket subscribed')
            hub.publish('call.started', 'c', {}, ts=4.0)
            self.wait_for(lambda: hub.subscriber_count == 1, 'the failed socket unsubscribed')
        samples = self.metrics(client)
        self.assertEqual(samples['ava_ws_slow_client_closes_total'], 0)
        self.assertEqual(samples['ava_ws_delivery_seconds_count'], 0, 'an event not sent is not timed')

    def test_without_a_hub_no_websocket_families_are_rendered(self):
        service = analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')
        with TestClient(create_app(service)) as client:
            text = client.get('/metrics').text
        self.assertNotIn('ava_ws_', text)


if __name__ == '__main__':
    unittest.main()
