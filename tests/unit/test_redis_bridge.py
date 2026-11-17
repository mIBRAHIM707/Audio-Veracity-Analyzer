"""Claude-authored tests (15 November 2026) for services/events/redis_bridge.py and the
relay timestamp option of EventHub.publish (backlog 5.1/5.2; PRD FR-A4; architecture
§2.4, §2.5, §2.7).

Authored by Claude (Claude Code), not by GPT-6 Astra. Pure tests use a fake Redis
client and always run. Real-Redis tests run only when AVA_TEST_REDIS_URL names a
disposable Redis 7 (for example a local `redis:7-alpine` container; see
services/events/README.md); AVA_REQUIRE_REDIS=1 turns the skip into a failure.
"""
import json
import os
import time
import unittest
import uuid

from services.events.contract import EventContractError, make_envelope
from services.events.fanout import EventHub
from services.events.redis_bridge import (CHANNEL, MAX_MESSAGE_BYTES, RedisForwarder, RedisRelay, encode_message,
                                          parse_message)

REDIS_URL = os.environ.get('AVA_TEST_REDIS_URL')
REAL = unittest.skipIf(not REDIS_URL and os.environ.get('AVA_REQUIRE_REDIS') != '1',
                       'set AVA_TEST_REDIS_URL to a disposable Redis 7')
T0 = 1_763_200_000.0


def score_data(value=0.4):
    return {'score': value, 'state': 'monitoring', 'degraded': False, 'effective_stride_s': 2.0, 'note': 'اردو'}


def wire(event_type, call_id, data, seq, ts=T0):
    return make_envelope(event_type, call_id, data, seq=seq, ts=ts).to_json().encode('utf-8')


class FakePubSub:
    def __init__(self, messages, fail_after=None):
        self.messages, self.fail_after, self.reads, self.subscribed, self.closed = list(messages), fail_after, 0, [], False

    def subscribe(self, channel):
        self.subscribed.append(channel)

    def get_message(self, timeout=0.0):
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            raise ConnectionError('connection reset')
        return self.messages.pop(0) if self.messages else None

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, pubsub=None, fail_on=(), receivers=1):
        self._pubsub, self.fail_on, self.published, self.calls = pubsub, set(fail_on), [], 0
        self.receivers = receivers

    def pubsub(self, ignore_subscribe_messages=False):
        assert ignore_subscribe_messages
        return self._pubsub

    def publish(self, channel, message):
        self.calls += 1
        if self.calls in self.fail_on:
            raise ConnectionError('down')
        self.published.append((channel, message))
        return self.receivers


def message(payload):
    return {'type': 'message', 'channel': CHANNEL.encode(), 'data': payload, 'pattern': None}


class HubTimestampTests(unittest.TestCase):
    def test_given_ts_is_kept_and_seq_stays_local(self):
        hub = EventHub(clock=lambda: 5.0)
        a = hub.publish('call.started', 'c1', {}, ts=T0 + 3)
        b = hub.publish('call.started', 'c2', {})
        self.assertEqual((a.ts, a.seq, b.ts, b.seq), (T0 + 3, 1, 5.0, 2))

    def test_invalid_ts_is_a_contract_error_and_uses_no_seq(self):
        hub = EventHub(clock=lambda: 5.0)
        for bad in (-1, float('nan'), float('inf'), 'now', True):
            with self.assertRaises(EventContractError, msg=repr(bad)):
                hub.publish('call.started', 'c1', {}, ts=bad)
        self.assertEqual(hub.publish('call.started', 'c1', {}).seq, 1)


class ParseMessageTests(unittest.TestCase):
    def test_round_trip_keeps_fields_and_utf8(self):
        envelope = parse_message(wire('score.updated', 'c1', score_data(), 7))
        self.assertEqual((envelope.type, envelope.call_id, envelope.seq, envelope.ts), ('score.updated', 'c1', 7, T0))
        self.assertEqual(envelope.data, score_data())
        self.assertEqual(parse_message(wire('system.health', None, {'reason': 'capacity'}, 1).decode()).call_id, None)

    def test_untrusted_payloads_refused(self):
        good = json.loads(wire('call.started', 'c1', {}, 1))
        cases = {
            'oversize': b' ' * (MAX_MESSAGE_BYTES + 1),
            'not utf-8': b'\xff\xfe{}',
            'not json': b'{nope',
            'NaN': json.dumps({**good, 'ts': 'X'}).replace('"X"', 'NaN').encode(),
            'Infinity': json.dumps({**good, 'seq': 'X'}).replace('"X"', 'Infinity').encode(),
            'list': b'[]',
            'extra key': json.dumps({**good, 'extra': 1}).encode(),
            'missing key': json.dumps({k: v for k, v in good.items() if k != 'seq'}).encode(),
            'type not str': json.dumps({**good, 'type': 5}).encode(),
            'unknown type': json.dumps({**good, 'type': 'state.changed'}).encode(),
            'negative seq': json.dumps({**good, 'seq': -1}).encode(),
            'float seq': json.dumps({**good, 'seq': 1.0}).encode(),
            'call.ended without its fields': wire('call.started', 'c1', {}, 1).replace(b'"call.started"', b'"call.ended"'),
            'no call id': json.dumps({**good, 'call_id': None}).encode(),
            'payload type': 12345,
            'duplicate key': wire('call.started', 'c1', {}, 1).replace(b'{', b'{"seq": 9, ', 1),
            'seq beyond 2**63': json.dumps({**good, 'seq': 2 ** 63}).encode(),
            'seq with 4000 digits': json.dumps({**good, 'seq': 'X'}).replace('"X"', '9' * 4000).encode(),
        }
        for label, payload in cases.items():
            with self.assertRaises(EventContractError, msg=label):
                parse_message(payload)


class ForwarderTests(unittest.TestCase):
    def test_forwards_in_order_and_counts_failures_without_raising(self):
        hub = EventHub(clock=lambda: T0)
        forwarder_sub = hub.subscribe()
        client = FakeClient(fail_on={2})
        forwarder = RedisForwarder(forwarder_sub, client)
        sent = [hub.publish('call.started', f'c{i}', {}) for i in range(3)]
        # Re-review A-M1: after the first failure the rest of the batch is not attempted.
        self.assertEqual(forwarder.pump(), 1)
        self.assertEqual(client.calls, 2)
        # 17 Nov: the wire form adds the hub's epoch as `source`; the exact bytes for a fixed
        # epoch are pinned in test_wire_bytes_are_pinned below.
        self.assertEqual(client.published, [(CHANNEL, encode_message(sent[0], hub.epoch))])
        self.assertEqual(json.loads(client.published[0][1]), {**json.loads(sent[0].to_json()), 'source': hub.epoch})
        self.assertEqual(forwarder.stats, {'forwarded': 1, 'publish_failures': 2, 'hub_dropped': 0,
                                           'no_receivers': 0})
        later = hub.publish('call.started', 'c9', {})
        self.assertEqual(forwarder.pump(), 1)  # the next pump tries again
        self.assertEqual(client.published[-1], (CHANNEL, encode_message(later, hub.epoch)))
        self.assertEqual(forwarder.pump(), 0)

    def test_wire_bytes_are_pinned(self):
        # Review L6 (17 Nov): one literal payload for a fixed epoch (key order, spacing, UTF-8).
        hub = EventHub(clock=lambda: T0, epoch='0123456789abcdef' * 2)
        client = FakeClient()
        forwarder = RedisForwarder(hub.subscribe(), client)
        hub.publish('call.started', 'c1', {'from': 'اردو'})
        forwarder.pump()
        self.assertEqual(client.published[0][1],
                         '{"call_id": "c1", "data": {"from": "اردو"}, "seq": 1, '
                         '"source": "0123456789abcdef0123456789abcdef", "ts": 1763200000.0, "type": "call.started"}')

    def test_unreachable_redis_costs_one_attempt_per_pump(self):
        hub = EventHub(clock=lambda: T0)
        client = FakeClient(fail_on=set(range(1, 100)))
        forwarder = RedisForwarder(hub.subscribe(), client)
        for i in range(50):
            hub.publish('call.started', f'c{i}', {})
        self.assertEqual(forwarder.pump(), 0)
        self.assertEqual((client.calls, forwarder.stats['publish_failures']), (1, 50))

    def test_no_receivers_counted_and_filtered_subscription_refused(self):
        hub = EventHub(clock=lambda: T0)
        forwarder = RedisForwarder(hub.subscribe(), FakeClient(receivers=0))
        hub.publish('call.started', 'c1', {})
        self.assertEqual(forwarder.pump(), 1)
        self.assertEqual(forwarder.stats['no_receivers'], 1)
        with self.assertRaisesRegex(ValueError, 'unfiltered'):
            RedisForwarder(hub.subscribe(call_id='c1'), FakeClient())

    def test_hub_overflow_is_counted_and_batch_bounded(self):
        hub = EventHub(clock=lambda: T0)
        forwarder = RedisForwarder(hub.subscribe(buffer=2), FakeClient(), batch=1)
        for i in range(4):
            hub.publish('call.started', f'c{i}', {})
        self.assertEqual(forwarder.pump(), 1)
        self.assertEqual(forwarder.pump(), 1)
        self.assertEqual(forwarder.stats['hub_dropped'], 2)
        with self.assertRaises(ValueError):
            RedisForwarder(hub.subscribe(), FakeClient(), batch=0)


class RelayTests(unittest.TestCase):
    def test_relays_valid_events_with_source_time_and_counts_the_rest(self):
        payloads = [{'type': 'subscribe', 'data': 1},  # confirmations are skipped
                    message(wire('call.started', 'c1', {}, 1, T0)),
                    message(b'{garbage'),
                    message(wire('score.updated', 'c1', score_data(0.9), 2, T0 + 2)),
                    message(wire('score.updated', 'c1', score_data(0.8), 5, T0 + 4)),  # 3 and 4 lost
                    message(wire('call.started', 'c9', {}, 1, T0 + 5))]  # source restarted
        pubsub = FakePubSub(payloads)
        hub = EventHub(clock=lambda: 99.0)
        local = hub.subscribe()
        relay = RedisRelay(lambda: FakeClient(pubsub), hub)
        self.assertEqual(relay.pump(timeout=0.1), 4)
        self.assertEqual(pubsub.subscribed, [CHANNEL])
        got, dropped = local.drain()
        self.assertEqual(dropped, 0)
        self.assertEqual([(e.type, e.call_id, e.seq, e.ts) for e in got],
                         [('call.started', 'c1', 1, T0), ('score.updated', 'c1', 2, T0 + 2),
                          ('score.updated', 'c1', 3, T0 + 4), ('call.started', 'c9', 4, T0 + 5)])
        self.assertEqual(got[1].data, score_data(0.9))
        self.assertEqual({k: relay.stats[k] for k in ('relayed', 'invalid', 'gaps', 'missing', 'restarts')},
                         {'relayed': 4, 'invalid': 1, 'gaps': 1, 'missing': 2, 'restarts': 1})

    def test_restart_counts_the_events_missed_since_it(self):
        payloads = [message(wire('call.started', 'c1', {}, 7)), message(wire('call.started', 'c2', {}, 3))]
        relay = RedisRelay(lambda: FakeClient(FakePubSub(payloads)), EventHub())
        self.assertEqual(relay.pump(), 2)
        self.assertEqual({k: relay.stats[k] for k in ('restarts', 'gaps', 'missing')},
                         {'restarts': 1, 'gaps': 1, 'missing': 2})

    def test_subscribe_failure_closes_the_pubsub(self):
        class FailingSubscribe(FakePubSub):
            def subscribe(self, channel):
                raise ConnectionError('refused during subscribe')
        pubsub = FailingSubscribe([])
        relay = RedisRelay(lambda: FakeClient(pubsub), EventHub())
        self.assertEqual(relay.pump(), 0)
        self.assertEqual((relay.stats['connection_errors'], pubsub.closed, relay.subscribed), (1, True, False))

    def test_unexpected_parser_fault_is_invalid_not_a_connection_error(self):
        pubsub = FakePubSub([message(object()), message(wire('call.started', 'c1', {}, 1))])
        relay = RedisRelay(lambda: FakeClient(pubsub), EventHub())
        self.assertEqual(relay.pump(), 1)
        self.assertEqual((relay.stats['invalid'], relay.stats['connection_errors'], relay.subscribed), (1, 0, True))

    def test_connection_error_resubscribes_on_the_next_pump(self):
        first = FakePubSub([message(wire('call.started', 'c1', {}, 1))], fail_after=1)
        second = FakePubSub([message(wire('call.started', 'c2', {}, 2))])
        clients = [FakeClient(first), FakeClient(second)]
        hub = EventHub(clock=lambda: 1.0)
        relay = RedisRelay(lambda: clients.pop(0), hub)
        self.assertEqual(relay.pump(), 1)  # one message, then the read fails
        self.assertEqual((relay.stats['connection_errors'], relay.subscribed, first.closed), (1, False, True))
        self.assertEqual(relay.pump(), 1)
        self.assertEqual(relay.stats['relayed'], 2)

    def test_connect_failure_and_hub_failure_are_contained(self):
        def refuse():
            raise ConnectionError('refused')
        relay = RedisRelay(refuse, EventHub())
        self.assertEqual(relay.pump(), 0)
        self.assertEqual(relay.stats['connection_errors'], 1)

        class BrokenHub:
            def publish(self, *args, **kwargs):
                raise RuntimeError('hub down')
        relay = RedisRelay(lambda: FakeClient(FakePubSub([message(wire('call.started', 'c1', {}, 1))])), BrokenHub())
        self.assertEqual(relay.pump(), 0)
        self.assertEqual((relay.stats['hub_errors'], relay.stats['relayed']), (1, 0))
        with self.assertRaises(ValueError):
            relay.pump(limit=0)


@REAL
class RealRedisTests(unittest.TestCase):
    def setUp(self):
        import redis
        self.redis = redis
        self.channel = f'ava:test:{uuid.uuid4().hex[:12]}'
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()

    def client(self, url=None):
        client = self.redis.Redis.from_url(url or REDIS_URL, socket_timeout=5, socket_connect_timeout=5)
        self.clients.append(client)
        return client

    def relay_until(self, relay, count, seconds=10):
        deadline, total = time.monotonic() + seconds, 0
        while total < count and time.monotonic() < deadline:
            total += relay.pump(timeout=0.2)
        return total

    def test_events_cross_redis_in_order_with_source_time(self):
        source, destination = EventHub(clock=time.time), EventHub(clock=lambda: 1.0)
        forwarder = RedisForwarder(source.subscribe(buffer=4096), self.client(), channel=self.channel)
        relay = RedisRelay(self.client, destination, channel=self.channel)
        relay.pump(timeout=0.5)  # subscribe before anything is published (pub/sub keeps no backlog)
        local = destination.subscribe(buffer=4096)
        sent = [source.publish('call.started', 'c1', {})]
        sent += [source.publish('score.updated', 'c1', score_data(i / 100)) for i in range(50)]
        sent.append(source.publish('call.ended', 'c1', {'final_state': 'monitoring', 'reason': 'bye',
                                                          'speech_seconds': 100.0}))
        self.assertEqual(forwarder.pump(), len(sent))
        self.assertEqual(self.relay_until(relay, len(sent)), len(sent))
        got, dropped = local.drain()
        self.assertEqual(dropped, 0)
        self.assertEqual([(e.type, e.call_id, e.ts, e.data) for e in got],
                         [(e.type, e.call_id, e.ts, e.data) for e in sent])
        self.assertEqual([e.seq for e in got], list(range(1, len(sent) + 1)))
        self.assertEqual((relay.stats['gaps'], relay.stats['invalid']), (0, 0))

    def test_events_published_while_the_relay_is_down_show_as_a_gap(self):
        source, destination = EventHub(clock=time.time), EventHub()
        forwarder = RedisForwarder(source.subscribe(), self.client(), channel=self.channel)
        relay = RedisRelay(self.client, destination, channel=self.channel)
        relay.pump(timeout=0.5)
        source.publish('call.started', 'c1', {})
        forwarder.pump()
        self.assertEqual(self.relay_until(relay, 1), 1)
        admin = self.client()
        killed = admin.execute_command('CLIENT', 'KILL', 'TYPE', 'pubsub')
        self.assertGreaterEqual(killed, 1)
        for i in range(3):  # nobody is subscribed: Redis drops these
            source.publish('score.updated', 'c1', score_data())
            forwarder.pump()
        deadline = time.monotonic() + 10
        while relay.stats['connection_errors'] == 0 and time.monotonic() < deadline:
            relay.pump(timeout=0.2)
        self.assertEqual(relay.stats['connection_errors'], 1)
        relay.pump(timeout=0.5)  # resubscribe
        source.publish('call.ended', 'c1', {'final_state': 'monitoring', 'reason': 'bye', 'speech_seconds': 1.0})
        forwarder.pump()
        self.assertEqual(self.relay_until(relay, 1), 1)
        self.assertEqual((relay.stats['gaps'], relay.stats['missing'], relay.stats['relayed']), (1, 3, 2))

    def test_forwarder_with_redis_unreachable_counts_and_continues(self):
        hub = EventHub()
        dead = self.redis.Redis(host='127.0.0.1', port=1, socket_connect_timeout=1, socket_timeout=1)
        self.clients.append(dead)
        forwarder = RedisForwarder(hub.subscribe(), dead, channel=self.channel)
        hub.publish('call.started', 'c1', {})
        hub.publish('call.started', 'c2', {})
        self.assertEqual(forwarder.pump(), 0)
        self.assertEqual(forwarder.stats['publish_failures'], 2)


if __name__ == '__main__':
    unittest.main()
