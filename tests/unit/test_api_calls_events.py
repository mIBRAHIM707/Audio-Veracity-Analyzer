"""FR-A3 `/v1/calls` and FR-A4 `WS /v1/events` on the analysis API (backlog 5.2 partial).

Claude-authored test design, 14 November 2026. In-process TestClient with a real
`EventHub`; events are published as the scorer would publish them. Needs
requirements-api.txt; skipped without FastAPI.
"""
import json
import re
import time
import unittest

from services.calls.registry import CallRegistry
from services.events.fanout import EventHub
from services.inference import analysis
from tests.unit import test_analysis_api as api_fixture

TestClient = api_fixture.TestClient
if TestClient is not None:
    from starlette.websockets import WebSocketDisconnect

    from services.api import app as app_module
    from services.api.app import create_app

SCORE = {'score': 0.7, 'state': 'suspicious', 'degraded': False, 'effective_stride_s': 1.0}


@unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
class ApiCallsEventsTests(unittest.TestCase):
    def make(self, hub=None, **kwargs):
        service = analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')
        client = TestClient(create_app(service, hub=hub, **kwargs))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def test_calls_routes_answer_503_and_events_close_1011_without_a_hub(self):
        client = self.make()
        self.assertEqual(client.get('/v1/calls').status_code, 503)
        self.assertEqual(client.get('/v1/calls/x').status_code, 503)
        with client.websocket_connect('/v1/events') as ws, self.assertRaises(WebSocketDisconnect) as caught:
            ws.receive_text()
        self.assertEqual(caught.exception.code, 1011)
        with self.assertRaisesRegex(ValueError, 'event hub'):
            create_app(analysis.AnalysisService(lambda: None, model_id='fx'), registry=CallRegistry())

    def test_calls_list_and_detail_reflect_published_events_immediately(self):
        hub = EventHub()
        client = self.make(hub)
        hub.publish('call.started', 'c1', {})
        hub.publish('score.updated', 'c1', {**SCORE, 'direction': 'caller'})
        hub.publish('alert.raised', 'c1', {'alert_id': 'a1'})
        hub.publish('call.started', 'c2', {})
        hub.publish('call.ended', 'c2', {'final_state': 'monitoring', 'reason': 'bye', 'speech_seconds': 3.0})
        body = client.get('/v1/calls').json()
        self.assertEqual([(c['call_id'], c['status'], c['state']) for c in body['calls']],
                         [('c1', 'active', 'alert'), ('c2', 'ended', 'monitoring')])
        self.assertEqual(body['registry']['applied'], 5)
        self.assertEqual([c['call_id'] for c in client.get('/v1/calls?status=ended').json()['calls']], ['c2'])
        self.assertEqual(client.get('/v1/calls?status=open').status_code, 422)
        detail = client.get('/v1/calls/c1').json()
        self.assertEqual(detail['alerts'][0]['alert_id'], 'a1')
        self.assertEqual(detail['timeline']['full_resolution'][0]['direction'], 'caller')
        self.assertEqual(client.get('/v1/calls/none').status_code, 404)
        metrics = client.get('/metrics').text
        self.assertIn('ava_api_requests_total{route="/v1/calls/:id",status="4xx"} 1', metrics)
        self.assertIn('ava_api_requests_total{route="/v1/calls",status="2xx"} 2', metrics)
        labels = set(re.findall(r'route="([^"]*)"', metrics))
        self.assertLessEqual(labels, set(app_module.ROUTES) | {'other'}, 'call IDs never become labels')
        self.assertEqual(client.get('/v1/calls').json()['registry']['pump_errors'], 0)

    def test_background_pump_keeps_registry_current_without_requests(self):
        hub, registry = EventHub(), CallRegistry()
        self.make(hub, registry=registry)
        hub.publish('call.started', 'bg', {})
        deadline = time.monotonic() + 5
        while registry.get_call('bg') is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(registry.get_call('bg'))

    def test_websocket_streams_envelopes_with_call_filter_and_health_broadcast(self):
        hub = EventHub()
        client = self.make(hub)
        with client.websocket_connect('/v1/events') as everything, \
                client.websocket_connect('/v1/events?call_id=c1') as only_c1:
            self._wait_subscribers(hub, 3)  # registry feed + two sockets
            hub.publish('call.started', 'c2', {})
            hub.publish('score.updated', 'c1', SCORE)
            hub.publish('system.health', None, {'reason': 'capacity'})
            got_all = [json.loads(everything.receive_text()) for _ in range(3)]
            got_c1 = [json.loads(only_c1.receive_text()) for _ in range(2)]
        self.assertEqual([(e['type'], e['call_id'], e['seq']) for e in got_all],
                         [('call.started', 'c2', 1), ('score.updated', 'c1', 2), ('system.health', None, 3)])
        self.assertEqual([e['type'] for e in got_c1], ['score.updated', 'system.health'])
        self.assertEqual(got_c1[0]['data'], SCORE)
        self._wait_subscribers(hub, 1)  # sockets unsubscribed on disconnect

    def test_slow_client_gets_a_gap_message_and_the_newest_events(self):
        hub = EventHub()
        client = self.make(hub)
        for name, value in (('WS_BUFFER', 2), ('WS_POLL_S', 3.0)):
            self.addCleanup(setattr, app_module, name, getattr(app_module, name))
            setattr(app_module, name, value)
        with client.websocket_connect('/v1/events?call_id=c') as ws:
            self._wait_subscribers(hub, 2)
            # The socket's first (empty) drain follows its subscription at once; the next is
            # 3 s away, so all five publishes land in the 2-envelope buffer before it.
            time.sleep(0.5)
            for i in range(5):
                hub.publish('score.updated', 'c', {**SCORE, 'score': i / 10})
            messages = [json.loads(ws.receive_text()) for _ in range(3)]
        gap, *events = messages
        self.assertEqual(gap, {'type': 'stream.gap', 'dropped': 3})
        self.assertEqual([e['data']['score'] for e in events], [0.3, 0.4])

    def test_subscriber_limit_closes_with_1013_and_client_messages_are_ignored(self):
        hub = EventHub(max_subscribers=2)
        client = self.make(hub)  # the registry feed takes one slot
        with client.websocket_connect('/v1/events') as first:
            self._wait_subscribers(hub, 2)
            with client.websocket_connect('/v1/events') as second, \
                    self.assertRaises(WebSocketDisconnect) as caught:
                second.receive_text()
            self.assertEqual(caught.exception.code, 1013)
            first.send_text('hello')  # must not end the stream
            hub.publish('call.started', 'z', {})
            self.assertEqual(json.loads(first.receive_text())['call_id'], 'z')

    def _wait_subscribers(self, hub, count):
        deadline = time.monotonic() + 5
        while hub.subscriber_count != count and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(hub.subscriber_count, count)


if __name__ == '__main__':
    unittest.main()
