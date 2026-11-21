"""Claude-authored tests (16 November 2026) for upstream loss reporting through the
event hub, the Redis relay thread (services/events/relay_loop.py) and its wiring into
the analysis API (`create_app(..., relay=)`, `stream.gap`, relay metrics).

Authored by Claude (Claude Code), not by GPT-6 Astra. Backlog 5.2 ("the relay thread
in the API process, `stream.gap` on relay loss"); PRD FR-A4, FR-O6; architecture §2.5
("WebSocket /v1/events fans out Redis pub/sub events").

Pure tests use fake Redis clients and always run (the API cases need
requirements-api.txt). Real-Redis cases run only when AVA_TEST_REDIS_URL names a
disposable Redis 7 (see services/events/README.md); AVA_REQUIRE_REDIS=1 turns the
skip into a failure. They also need requirements-api.txt and requirements-redis.txt.
"""
import json
import os
import queue
import threading
import time
import unittest
import uuid

from services.events.fanout import EventHub
from services.events.redis_bridge import CHANNEL, RedisForwarder, RedisRelay
from services.events.relay_loop import RelayLoop
from services.inference import analysis
from tests.unit import test_analysis_api as api_fixture
from tests.unit.test_redis_bridge import FakeClient, FakePubSub, message, score_data, wire

TestClient = api_fixture.TestClient
if TestClient is not None:
    from services.api import app as app_module
    from services.api.app import create_app, gap_message

REDIS_URL = os.environ.get('AVA_TEST_REDIS_URL')
REAL = unittest.skipIf(not REDIS_URL and os.environ.get('AVA_REQUIRE_REDIS') != '1',
                       'set AVA_TEST_REDIS_URL to a disposable Redis 7')
ENDED = {'final_state': 'monitoring', 'reason': 'bye', 'speech_seconds': 1.0}


def wait_for(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)
    return predicate()


class HubUpstreamLossTests(unittest.TestCase):
    def test_every_subscriber_is_told_once_and_no_seq_is_used(self):
        hub = EventHub(clock=lambda: 1.0)
        everything, only_c1 = hub.subscribe(), hub.subscribe(call_id='c1')
        hub.publish('call.started', 'c2', {})
        hub.report_upstream_loss(missing=3)
        hub.report_upstream_loss(interruptions=1)
        later = hub.subscribe()  # joined after the loss: nothing to report to it
        self.assertEqual(hub.publish('call.started', 'c1', {}).seq, 2)
        first = everything.drain_batch()
        self.assertEqual(([e.seq for e in first.envelopes], first.dropped, first.upstream_missing,
                          first.upstream_interruptions), ([1, 2], 0, 3, 1))
        self.assertEqual(everything.drain_batch()[1:], (0, 0, 0), 'taken once')
        filtered = only_c1.drain_batch()
        self.assertEqual(([e.call_id for e in filtered.envelopes], filtered.upstream_missing,
                          filtered.upstream_interruptions), (['c1'], 3, 1))
        self.assertEqual(later.drain_batch()[2:], (0, 0))
        self.assertEqual((hub.upstream_missing_total, hub.upstream_interruptions_total), (3, 1))

    def test_drain_leaves_upstream_loss_for_drain_batch_and_local_drops_stay_separate(self):
        hub = EventHub()
        subscription = hub.subscribe(buffer=1)
        hub.publish('call.started', 'a', {})
        hub.publish('call.started', 'b', {})  # drops 'a'
        hub.report_upstream_loss(missing=2)
        envelopes, dropped = subscription.drain()
        self.assertEqual(([e.call_id for e in envelopes], dropped), (['b'], 1))
        self.assertEqual(subscription.drain_batch(), ([], 0, 2, 0))

    def test_closed_subscription_is_not_told_and_bad_reports_are_refused(self):
        hub = EventHub()
        gone = hub.subscribe()
        gone.close()
        hub.report_upstream_loss(missing=1)
        self.assertEqual(gone.drain_batch()[2:], (0, 0))
        for kwargs in ({}, {'missing': 0, 'interruptions': 0}, {'missing': -1}, {'missing': 1.0},
                       {'missing': True}, {'interruptions': '1'}):
            with self.assertRaises(ValueError, msg=repr(kwargs)):
                hub.report_upstream_loss(**kwargs)
        with self.assertRaises(ValueError):
            hub.subscribe().drain_batch(0)
        self.assertEqual((hub.upstream_missing_total, hub.upstream_interruptions_total), (1, 0))


class RecordingHub(EventHub):
    """A real hub that also records the order of loss reports and publishes."""

    def __init__(self):
        super().__init__(clock=lambda: 1.0)
        self.log = []

    def report_upstream_loss(self, **kwargs):
        self.log.append(('loss', kwargs))
        super().report_upstream_loss(**kwargs)

    def publish(self, event_type, call_id, data, *, ts=None):
        self.log.append(('publish', call_id))
        return super().publish(event_type, call_id, data, ts=ts)


class RelayLossReportTests(unittest.TestCase):
    def test_gap_is_reported_before_the_event_after_it(self):
        hub = RecordingHub()
        payloads = [message(wire('call.started', 'c1', {}, 1)), message(wire('call.started', 'c4', {}, 4))]
        relay = RedisRelay(lambda: FakeClient(FakePubSub(payloads)), hub)
        self.assertEqual(relay.pump(), 2)
        self.assertEqual(hub.log, [('publish', 'c1'), ('loss', {'missing': 2, 'interruptions': 0}),
                                   ('publish', 'c4')])
        self.assertEqual((relay.stats['gaps'], relay.stats['missing']), (1, 2))

    def test_restarts_are_interruptions_and_count_what_is_known(self):
        hub = EventHub()
        watcher = hub.subscribe()
        payloads = [message(wire('call.started', 'a', {}, 5)), message(wire('call.started', 'b', {}, 1)),
                    message(wire('call.started', 'c', {}, 2)), message(wire('call.started', 'd', {}, 1)),
                    message(wire('call.started', 'e', {}, 4))]
        relay = RedisRelay(lambda: FakeClient(FakePubSub(payloads)), hub)
        self.assertEqual(relay.pump(), 5)
        batch = watcher.drain_batch()
        # 5 -> 1: restart (unknown loss); 2 contiguous; 2 -> 1: restart; 1 -> 4: gap of 2.
        self.assertEqual((len(batch.envelopes), batch.upstream_missing, batch.upstream_interruptions), (5, 2, 2))
        self.assertEqual({k: relay.stats[k] for k in ('restarts', 'gaps', 'missing')},
                         {'restarts': 2, 'gaps': 1, 'missing': 2})

    def test_the_first_message_and_contiguous_messages_report_nothing(self):
        hub = EventHub()
        watcher = hub.subscribe()
        payloads = [message(wire('call.started', 'c', {}, n)) for n in (40, 41, 42)]
        RedisRelay(lambda: FakeClient(FakePubSub(payloads)), hub).pump()
        self.assertEqual(watcher.drain_batch()[1:], (0, 0, 0))

    def test_an_outage_is_one_interruption_until_a_subscription_succeeds(self):
        hub = EventHub()
        watcher = hub.subscribe()
        attempts = []

        def connect():
            attempts.append(1)
            if len(attempts) in (1, 2, 3, 5):
                raise ConnectionError('refused')
            return FakeClient(FakePubSub([]))
        relay = RedisRelay(connect, hub)
        for _ in range(3):
            relay.pump()
        self.assertEqual((relay.stats['connection_errors'], relay.stats['interruptions']), (3, 1))
        self.assertEqual(watcher.drain_batch()[3], 1)
        relay.pump()  # attempt 4 subscribes
        self.assertTrue(relay.subscribed)
        relay._pubsub.fail_after = relay._pubsub.reads  # the subscribed connection then fails
        relay.pump()
        relay.pump()  # attempt 5 refused: the same outage
        self.assertEqual((relay.stats['connection_errors'], relay.stats['interruptions']), (5, 2))
        self.assertEqual(watcher.drain_batch()[3], 1)

    def test_an_event_the_hub_refuses_is_reported_missing(self):
        hub = RecordingHub()
        watcher = hub.subscribe()

        def refusing_publish(*args, **kwargs):
            raise RuntimeError('hub clock failed')
        hub.publish = refusing_publish
        relay = RedisRelay(lambda: FakeClient(FakePubSub([message(wire('call.started', 'c', {}, 1))])), hub)
        self.assertEqual(relay.pump(), 0)
        self.assertEqual((relay.stats['hub_errors'], watcher.drain_batch().upstream_missing), (1, 1))

    def test_a_hub_without_loss_reports_is_counted_and_relaying_goes_on(self):
        class PlainHub:
            def __init__(self):
                self.published = []

            def publish(self, event_type, call_id, data, *, ts=None):
                self.published.append(call_id)
        hub = PlainHub()
        payloads = [message(wire('call.started', 'a', {}, 1)), message(wire('call.started', 'b', {}, 3))]
        relay = RedisRelay(lambda: FakeClient(FakePubSub(payloads)), hub)
        self.assertEqual(relay.pump(), 2)
        self.assertEqual((hub.published, relay.stats['loss_report_errors']), (['a', 'b'], 1))


class QueuePubSub:
    """Thread-safe fake pub/sub: `get_message(timeout)` blocks on a queue like redis-py."""

    def __init__(self):
        self.queue, self.closed, self.subscribed = queue.Queue(), False, []

    def subscribe(self, channel):
        self.subscribed.append(channel)

    def get_message(self, timeout=0.0):
        if self.closed:
            raise ConnectionError('closed')
        try:
            item = self.queue.get(timeout=timeout) if timeout else self.queue.get_nowait()
        except queue.Empty:
            return None
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class FakeRelay:
    def __init__(self, pump, subscribed=True):
        self._pump, self.subscribed, self.closed, self.timeouts = pump, subscribed, 0, []

    def pump(self, timeout=0.0):
        self.timeouts.append(timeout)
        return self._pump()

    def close(self):
        self.closed += 1


class RelayLoopTests(unittest.TestCase):
    def test_moves_events_into_the_hub_and_closes_the_relay_after_the_thread_ends(self):
        hub, pubsub = EventHub(), QueuePubSub()
        watcher = hub.subscribe()
        loop = RelayLoop(RedisRelay(lambda: FakeClient(pubsub), hub), wait_s=0.1)
        loop.start()
        for n in (1, 2, 3):
            pubsub.queue.put(message(wire('call.started', f'c{n}', {}, n)))
        self.assertTrue(wait_for(lambda: loop.stats['relayed'] == 3))
        self.assertEqual([e.call_id for e in watcher.drain()[0]], ['c1', 'c2', 'c3'])
        self.assertEqual(pubsub.subscribed, [CHANNEL])
        self.assertTrue(loop.stop())
        self.assertEqual((loop.alive, pubsub.closed, loop.snapshot()['subscribed'], loop.stats['closed']),
                         (False, True, False, True))
        self.assertTrue(loop.stop(), 'a second stop is harmless')
        with self.assertRaises(RuntimeError):
            loop.start()

    def test_unreachable_redis_backs_off_doubling_to_the_cap_and_stop_wakes_the_wait(self):
        waits = []
        relay = FakeRelay(lambda: 0, subscribed=False)
        loop = RelayLoop(relay, wait_s=0.1, backoff_s=0.01, max_backoff_s=0.04)
        original = loop._stop.wait

        def recording_wait(seconds):
            waits.append(seconds)
            return original(seconds)
        loop._stop.wait = recording_wait
        loop.start()
        self.assertTrue(wait_for(lambda: len(waits) >= 5))
        self.assertEqual(waits[:5], [0.01, 0.02, 0.04, 0.04, 0.04])
        self.assertTrue(all(t == 0.1 for t in relay.timeouts))
        self.assertTrue(loop.stop())
        self.assertEqual(relay.closed, 1)

        slow = RelayLoop(FakeRelay(lambda: 0, subscribed=False), backoff_s=60, max_backoff_s=60)
        slow.start()
        self.assertTrue(wait_for(lambda: slow.stats['backoff_waits'] >= 1))
        started = time.monotonic()
        self.assertTrue(slow.stop())
        self.assertLess(time.monotonic() - started, 5.0, 'stop must not wait out a 60 s backoff')

    def test_backoff_resets_after_a_subscription(self):
        states = iter([False, False, True, False] + [True] * 10_000)
        waits = []
        relay = FakeRelay(lambda: 0)

        def pump():
            relay.subscribed = next(states)
            return 0
        relay._pump = pump
        loop = RelayLoop(relay, wait_s=0.1, backoff_s=0.01, max_backoff_s=1.0)
        original = loop._stop.wait

        def recording_wait(seconds):
            waits.append(seconds)
            return original(seconds)
        loop._stop.wait = recording_wait
        loop.start()
        self.assertTrue(wait_for(lambda: len(waits) >= 5))
        loop.stop()
        from services.events.relay_loop import IDLE_S
        self.assertEqual(waits[:5], [0.01, 0.02, IDLE_S, 0.01, IDLE_S])

    def test_a_raising_pump_is_counted_and_the_loop_goes_on(self):
        calls = []

        def pump():
            calls.append(1)
            if len(calls) <= 2:
                raise KeyError('secret-host:6379')
            return 1 if len(calls) == 3 else 0
        loop = RelayLoop(FakeRelay(pump), wait_s=0.1)
        loop.start()
        self.assertTrue(wait_for(lambda: loop.stats['relayed'] == 1))
        loop.stop()
        self.assertEqual((loop.stats['faults'], loop.stats['last_fault']), (2, 'KeyError'))

    def test_a_stuck_pump_is_left_behind_and_the_relay_is_not_closed_under_it(self):
        release = threading.Event()
        self.addCleanup(release.set)
        relay = FakeRelay(lambda: release.wait(10) and 0)
        loop = RelayLoop(relay, wait_s=0.1)
        loop.start()
        self.assertTrue(wait_for(lambda: relay.timeouts))
        self.assertFalse(loop.stop(join_timeout_s=0.1))
        self.assertEqual((loop.stats['stuck'], relay.closed, loop.alive), (True, 0, True))
        release.set()
        self.assertTrue(wait_for(lambda: not loop.alive))
        self.assertTrue(loop.stop(join_timeout_s=1))
        self.assertEqual(relay.closed, 1)

    def test_default_stop_outlasts_a_reconnect_attempt_at_the_client_timeout(self):
        from services.events.relay_loop import CLIENT_TIMEOUT_S
        entered = threading.Event()

        def slow_connect_attempt():
            entered.set()
            time.sleep(CLIENT_TIMEOUT_S)  # a refused connect that waits out the client timeout
            return 0
        relay = FakeRelay(slow_connect_attempt, subscribed=False)
        loop = RelayLoop(relay, wait_s=0.5)
        loop.start()
        self.assertTrue(entered.wait(5))
        self.assertTrue(loop.stop(), 'the default join covers one attempt')
        self.assertEqual((loop.stats['stuck'], relay.closed), (False, 1))

    def test_arguments_are_validated(self):
        relay = FakeRelay(lambda: 0)
        for kwargs in ({'wait_s': 0}, {'wait_s': 6}, {'wait_s': True}, {'backoff_s': 0}, {'max_backoff_s': 301},
                       {'backoff_s': 2, 'max_backoff_s': 1}, {'wait_s': '1'}):
            with self.assertRaises(ValueError, msg=repr(kwargs)):
                RelayLoop(relay, **kwargs)
        with self.assertRaises(ValueError):
            RelayLoop(object())
        loop = RelayLoop(relay)
        for bad in (-1, 301, True, 'x'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                loop.stop(join_timeout_s=bad)
        self.assertTrue(loop.stop(join_timeout_s=0), 'never started: closes the relay')
        self.assertEqual(relay.closed, 1)


@unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
class ApiRelayTests(unittest.TestCase):
    def make(self, hub, relay):
        service = analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')
        client = TestClient(create_app(service, hub=hub, relay=relay))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def test_gap_message_shapes(self):
        from services.events.fanout import Drained
        self.assertIsNone(gap_message(Drained([], 0, 0, 0)))
        self.assertEqual(json.loads(gap_message(Drained([], 3, 0, 0))), {'type': 'stream.gap', 'dropped': 3})
        self.assertEqual(json.loads(gap_message(Drained([], 0, 2, 1))),
                         {'type': 'stream.gap', 'dropped': 0, 'upstream_missing': 2, 'upstream_interruptions': 1})
        self.assertEqual(json.loads(gap_message(Drained([], 0, 0, 1))),
                         {'type': 'stream.gap', 'dropped': 0, 'upstream_interruptions': 1})

    def test_relay_must_feed_the_apps_hub(self):
        service = analysis.AnalysisService(lambda: None, model_id='fx')
        relay = RedisRelay(lambda: FakeClient(FakePubSub([])), EventHub())
        with self.assertRaisesRegex(ValueError, 'event hub'):
            create_app(service, relay=relay)
        with self.assertRaisesRegex(ValueError, 'event hub'):
            create_app(service, hub=EventHub(), relay=relay)

    def test_relayed_events_reach_websockets_and_calls_with_upstream_gaps_and_metrics(self):
        hub, pubsub = EventHub(), QueuePubSub()
        client = self.make(hub, RedisRelay(lambda: FakeClient(pubsub), hub))
        self.assertTrue(wait_for(lambda: pubsub.subscribed == [CHANNEL]))
        with client.websocket_connect('/v1/events?call_id=c1') as ws:
            self.assertTrue(wait_for(lambda: hub.subscriber_count == 2))  # registry feed + socket
            pubsub.queue.put(message(wire('call.started', 'c1', {}, 1)))
            pubsub.queue.put(message(wire('score.updated', 'c1', score_data(0.6), 2)))
            got = [json.loads(ws.receive_text()) for _ in range(2)]
            self.assertEqual([(e['type'], e['seq']) for e in got], [('call.started', 1), ('score.updated', 2)])
            pubsub.queue.put(message(wire('call.ended', 'c1', ENDED, 6)))  # 3, 4 and 5 lost upstream
            following = [json.loads(ws.receive_text()) for _ in range(2)]
        self.assertEqual(following[0], {'type': 'stream.gap', 'dropped': 0, 'upstream_missing': 3})
        self.assertEqual((following[1]['type'], following[1]['seq']), ('call.ended', 3), 'local seq stays gap-free')
        self.assertTrue(wait_for(lambda: client.get('/v1/calls/c1').json().get('status') == 'ended'))
        registry = client.get('/v1/calls').json()['registry']  # REST says its view may be stale
        self.assertEqual((registry['upstream_missing'], registry['upstream_interruptions']), (3, 0))
        metrics = client.get('/metrics').text
        for line in ('ava_event_relay_messages_total{outcome="relayed"} 3',
                     'ava_event_relay_missing_events_total 3', 'ava_event_relay_gaps_total 1',
                     'ava_event_relay_subscribed 1', 'ava_event_relay_running 1',
                     'ava_event_relay_interruptions_total 0', 'ava_event_relay_thread_faults_total 0',
                     'ava_event_relay_loss_report_errors_total 0'):
            self.assertIn(line + '\n', metrics)
        client.__exit__(None, None, None)
        self.assertTrue(pubsub.closed, 'shutdown stops the thread and closes the subscription')

    def test_a_relay_outage_reaches_open_websockets_as_an_interruption(self):
        hub, pubsubs = EventHub(), [QueuePubSub(), QueuePubSub()]
        made = []

        def connect():
            made.append(1)
            return FakeClient(pubsubs[min(len(made), 2) - 1])
        self.addCleanup(setattr, app_module, 'RelayLoop', app_module.RelayLoop)
        app_module.RelayLoop = lambda relay: RelayLoop(relay, wait_s=0.05, backoff_s=0.01)
        client = self.make(hub, RedisRelay(connect, hub))
        with client.websocket_connect('/v1/events') as ws:
            self.assertTrue(wait_for(lambda: hub.subscriber_count == 2))
            pubsubs[0].queue.put(message(wire('call.started', 'c1', {}, 1)))
            self.assertEqual(json.loads(ws.receive_text())['call_id'], 'c1')
            pubsubs[0].queue.put(ConnectionError('connection reset'))
            gap = json.loads(ws.receive_text())
            self.assertEqual(gap, {'type': 'stream.gap', 'dropped': 0, 'upstream_interruptions': 1})
            self.assertTrue(wait_for(lambda: pubsubs[1].subscribed == [CHANNEL]), 'resubscribed')
            pubsubs[1].queue.put(message(wire('call.ended', 'c1', ENDED, 2)))
            self.assertEqual(json.loads(ws.receive_text())['type'], 'call.ended')
        metrics = client.get('/metrics').text
        self.assertIn('ava_event_relay_connection_errors_total 1\n', metrics)
        self.assertIn('ava_event_relay_interruptions_total 1\n', metrics)
        self.assertEqual(client.get('/v1/calls').json()['registry']['upstream_interruptions'], 1)

    def test_relay_and_registry_restart_with_a_second_lifespan(self):
        # Only the relay/registry part restarts: the job worker stays closed (as before).
        hub, made = EventHub(), []

        def connect():
            made.append(QueuePubSub())
            return FakeClient(made[-1])
        service = analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')
        app = create_app(service, hub=hub, relay=RedisRelay(connect, hub))
        for run, call_id in ((1, 'first'), (2, 'second')):
            with TestClient(app) as client:
                self.assertTrue(wait_for(lambda: len(made) == run and made[-1].subscribed == [CHANNEL]))
                made[-1].queue.put(message(wire('call.started', call_id, {}, run)))
                self.assertTrue(wait_for(lambda: client.get(f'/v1/calls/{call_id}').status_code == 200))
                self.assertIn('ava_event_relay_running 1\n', client.get('/metrics').text)
            self.assertTrue(made[-1].closed, f'run {run} closed its subscription')

    def test_a_stuck_relay_thread_is_not_joined_by_a_second_one(self):
        release = threading.Event()
        self.addCleanup(release.set)
        hub = EventHub()
        relay = FakeRelay(lambda: release.wait(10) and 0)
        relay.hub, relay.stats = hub, {}
        self.addCleanup(setattr, app_module, 'RelayLoop', app_module.RelayLoop)
        started = []

        def short_stop_loop(given):
            loop = RelayLoop(given, wait_s=0.05)
            loop.stop = lambda join_timeout_s=0.1, _stop=loop.stop: _stop(join_timeout_s)
            started.append(loop)
            return loop
        app_module.RelayLoop = short_stop_loop
        service = analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')
        app = create_app(service, hub=hub, relay=relay)
        with TestClient(app):
            self.assertTrue(wait_for(lambda: relay.timeouts))
        self.assertEqual((len(started), started[0].stats['stuck'], relay.closed), (1, True, 0))
        with TestClient(app) as client:  # the first thread is still inside pump()
            self.assertIn('ava_event_relay_running 1\n', client.get('/metrics').text)
            self.assertEqual(len(started), 1, 'no second thread on the same relay')
            release.set()
            self.assertTrue(wait_for(lambda: not started[0].alive))
        self.assertEqual(relay.closed, 1, 'closed once the stuck thread ended')
        with TestClient(app):
            self.assertTrue(wait_for(lambda: len(started) == 2 and started[1].alive))
        self.assertEqual(relay.closed, 2)


@REAL
@unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
class RealRedisApiRelayTests(unittest.TestCase):
    """The scorer side (hub -> RedisForwarder) and the API side (RedisRelay thread ->
    hub -> WebSocket) through a real Redis 7."""

    def test_events_cross_real_redis_to_a_websocket_and_an_outage_becomes_stream_gap(self):
        import redis

        from services.events.relay_loop import open_relay_client
        channel = f'ava:test:{uuid.uuid4().hex[:12]}'
        clients = [open_relay_client(REDIS_URL), redis.Redis.from_url(REDIS_URL, socket_timeout=5)]
        for made in clients:
            self.addCleanup(made.close)
        relay_client, publisher = clients
        source, hub = EventHub(), EventHub()
        forwarder = RedisForwarder(source.subscribe(buffer=4096), publisher, channel=channel)
        relay = RedisRelay(lambda: relay_client, hub, channel=channel)
        service = analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')
        client = TestClient(create_app(service, hub=hub, relay=relay))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        self.assertTrue(wait_for(lambda: relay.subscribed and publisher.pubsub_numsub(channel)[0][1] == 1, 10))
        with client.websocket_connect('/v1/events') as ws:
            self.assertTrue(wait_for(lambda: hub.subscriber_count == 2))
            source.publish('call.started', 'r1', {})
            source.publish('score.updated', 'r1', score_data(0.2))
            self.assertEqual(forwarder.pump(), 2)
            got = [json.loads(ws.receive_text()) for _ in range(2)]
            self.assertEqual([e['type'] for e in got], ['call.started', 'score.updated'])
            self.assertGreaterEqual(publisher.execute_command('CLIENT', 'KILL', 'TYPE', 'pubsub'), 1)
            for _ in range(3):  # nobody is subscribed now: Redis drops these
                source.publish('score.updated', 'r1', score_data())
                forwarder.pump()
            interruption = json.loads(ws.receive_text())
            self.assertEqual(interruption, {'type': 'stream.gap', 'dropped': 0, 'upstream_interruptions': 1})
            self.assertTrue(wait_for(lambda: relay.subscribed and publisher.pubsub_numsub(channel)[0][1] == 1, 15))
            source.publish('call.ended', 'r1', ENDED)
            forwarder.pump()
            counted = json.loads(ws.receive_text())
            ended = json.loads(ws.receive_text())
        self.assertEqual(counted, {'type': 'stream.gap', 'dropped': 0, 'upstream_missing': 3})
        self.assertEqual((ended['type'], ended['call_id']), ('call.ended', 'r1'))
        self.assertTrue(wait_for(lambda: client.get('/v1/calls/r1').json().get('status') == 'ended'))
        metrics = client.get('/metrics').text
        self.assertIn('ava_event_relay_messages_total{outcome="relayed"} 3\n', metrics)
        self.assertIn('ava_event_relay_missing_events_total 3\n', metrics)
        self.assertIn('ava_event_relay_interruptions_total 1\n', metrics)


if __name__ == '__main__':
    unittest.main()
