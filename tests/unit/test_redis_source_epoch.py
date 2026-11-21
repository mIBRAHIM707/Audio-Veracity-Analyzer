"""Source epoch on the Redis event bridge (backlog 5.2; PRD FR-A4; architecture §2.4,
§2.5; closes the 16 Nov review L2 "a restart with lost first events cannot be
detected").

Claude-authored test design, 17 November 2026. Real EventHubs, RedisForwarder and
RedisRelay with an in-memory bus standing in for Redis pub/sub (always run), plus one
real-Redis case when AVA_TEST_REDIS_URL names a disposable Redis 7. Covers the hub
epoch, the wire form, untrusted `source` values, every relay rule (same epoch in order,
gap, new epoch with and without lost first events, a non-advancing seq within one
epoch, messages without `source` from an older forwarder and transitions to and from
them) and what WebSocket clients are told through `drain_batch`.
"""
import json
import time
import unittest
import uuid

from services.events.contract import EventContractError, make_envelope
from services.events.fanout import EventHub
from services.events.redis_bridge import (CHANNEL, RedisForwarder, RedisRelay, encode_message,
                                          parse_message_with_source)
from tests.unit.test_redis_bridge import REAL, REDIS_URL, T0, FakeClient, FakePubSub, message

EPOCH_A, EPOCH_B = 'a' * 32, 'b' * 32


def wire(seq, source, call_id='c1'):
    envelope = make_envelope('call.started', call_id, {}, seq=seq, ts=T0)
    return (encode_message(envelope, source) if source else envelope.to_json()).encode('utf-8')


def relay_messages(payloads):
    """Relay the payloads into a fresh hub; returns (relay, the hub subscription's
    drain_batch, the relayed seqs as seen by the local hub's subscribers in order)."""
    hub = EventHub(clock=lambda: 1.0)
    local = hub.subscribe(buffer=4096)
    relay = RedisRelay(lambda: FakeClient(FakePubSub([message(p) for p in payloads])), hub)
    relay.pump(limit=len(payloads) + 1)
    return relay, local.drain_batch()


class Bus:
    """In-memory pub/sub: what a forwarder publishes, a relay reads (one channel)."""

    def __init__(self):
        self.queue = []

    def publish(self, channel, payload):
        self.queue.append(message(payload.encode('utf-8')))
        return 1

    def pubsub(self, ignore_subscribe_messages=False):
        return self

    def subscribe(self, channel):
        pass

    def get_message(self, timeout=0.0):
        return self.queue.pop(0) if self.queue else None

    def close(self):
        pass


class EpochTests(unittest.TestCase):
    def test_every_hub_gets_a_new_random_epoch(self):
        epochs = {EventHub().epoch for _ in range(50)}
        self.assertEqual(len(epochs), 50)
        for epoch in epochs:
            self.assertRegex(epoch, r'^[0-9a-f]{32}$')
        hub = EventHub(epoch=EPOCH_A)
        self.assertEqual((hub.epoch, hub.subscribe().hub_epoch), (EPOCH_A, EPOCH_A))
        for bad in ('A' * 32, 'a' * 31, 'a' * 33, 'g' * 32, 32, b'a' * 32, '', 'a' * 32 + '\n'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                EventHub(epoch=bad)

    def test_forwarder_sends_its_hubs_epoch_as_source(self):
        hub, client = EventHub(clock=lambda: T0), FakeClient()
        forwarder = RedisForwarder(hub.subscribe(), client)
        sent = hub.publish('call.started', 'c1', {'from': '1000'})
        forwarder.pump()
        envelope, source = parse_message_with_source(client.published[0][1])
        self.assertEqual(source, hub.epoch)
        self.assertEqual(forwarder.source, hub.epoch)
        self.assertEqual((envelope.type, envelope.call_id, envelope.seq, envelope.ts, envelope.data),
                         (sent.type, sent.call_id, sent.seq, sent.ts, sent.data))

    def test_a_subscription_without_an_epoch_gets_a_random_one(self):
        class Plain:
            call_id = None

            def drain(self, limit):
                return [], 0
        first, second = RedisForwarder(Plain(), FakeClient()), RedisForwarder(Plain(), FakeClient())
        self.assertRegex(first.source, r'^[0-9a-f]{32}$')
        self.assertNotEqual(first.source, second.source)

    def test_untrusted_source_values_are_refused(self):
        good = json.loads(wire(1, EPOCH_A))
        for label, value in (('uppercase', 'A' * 32), ('short', 'a' * 31), ('number', 7), ('null', None),
                             ('list', [EPOCH_A]), ('non-hex', 'z' * 32), ('trailing newline', EPOCH_A + '\n')):
            with self.subTest(label), self.assertRaises(EventContractError):
                parse_message_with_source(json.dumps({**good, 'source': value}).encode())
        with self.assertRaises(EventContractError):
            parse_message_with_source(json.dumps({**good, 'source2': EPOCH_A}).encode())
        self.assertEqual(parse_message_with_source(wire(1, None))[1], None, 'older messages have no source')


class RelayRuleTests(unittest.TestCase):
    def test_same_epoch_in_order_and_with_a_gap(self):
        relay, batch = relay_messages([wire(1, EPOCH_A), wire(2, EPOCH_A), wire(5, EPOCH_A)])
        self.assertEqual((relay.stats['relayed'], relay.stats['gaps'], relay.stats['missing'],
                          relay.stats['restarts']), (3, 1, 2, 0))
        self.assertEqual((batch.upstream_missing, batch.upstream_interruptions), (2, 0))

    def test_new_epoch_from_seq_1_is_a_restart(self):
        relay, batch = relay_messages([wire(1, EPOCH_A), wire(2, EPOCH_A), wire(1, EPOCH_B), wire(2, EPOCH_B)])
        self.assertEqual((relay.stats['restarts'], relay.stats['missing'], relay.stats['relayed']), (1, 0, 4))
        self.assertEqual((batch.upstream_missing, batch.upstream_interruptions), (0, 1))

    def test_restart_with_lost_first_events_is_counted_exactly(self):
        # The 16 Nov L2 case: the old epoch ended at seq 2; the new hub's events 1-3 were
        # lost, so its first seen seq (4) is above the old last. Without the epoch this
        # looked like a gap of 1 and no restart.
        relay, batch = relay_messages([wire(1, EPOCH_A), wire(2, EPOCH_A), wire(4, EPOCH_B)])
        self.assertEqual((relay.stats['restarts'], relay.stats['gaps'], relay.stats['missing']), (1, 1, 3))
        self.assertEqual((batch.upstream_missing, batch.upstream_interruptions), (3, 1))
        relay, _ = relay_messages([wire(1, None), wire(2, None), wire(4, None)])
        self.assertEqual((relay.stats['restarts'], relay.stats['missing']), (0, 1), 'what the old rule saw')
        relay, batch = relay_messages([wire(1, EPOCH_A), wire(2, EPOCH_A), wire(10, EPOCH_B), wire(11, EPOCH_B)])
        self.assertEqual((relay.stats['restarts'], relay.stats['missing'], batch.upstream_missing), (1, 9, 9))

    def test_a_seq_that_does_not_advance_within_an_epoch_is_dropped_not_a_restart(self):
        relay, batch = relay_messages([wire(5, EPOCH_A), wire(6, EPOCH_A), wire(6, EPOCH_A), wire(3, EPOCH_A),
                                       wire(7, EPOCH_A)])
        self.assertEqual((relay.stats['out_of_order'], relay.stats['restarts'], relay.stats['relayed'],
                          relay.stats['missing']), (2, 0, 3, 0), 'seq 7 after the dropped ones is no gap')
        self.assertEqual(len(batch.envelopes), 3)
        # Review M2: one interruption per run of dropped messages (6, 3 is one run).
        self.assertEqual((batch.upstream_missing, batch.upstream_interruptions), (0, 1))
        relay, batch = relay_messages([wire(5, EPOCH_A), wire(5, EPOCH_A), wire(6, EPOCH_A), wire(6, EPOCH_A)])
        self.assertEqual((relay.stats['out_of_order'], batch.upstream_interruptions), (2, 2), 'two separate runs')

    def test_a_restarted_hub_that_reused_its_epoch_is_loud_not_silent(self):
        # Review M2: epochs must be unique; if one is reused, the new hub's events (seq 1,
        # 2, 3 after an old 5) are dropped, and subscribers are told once at the start.
        relay, batch = relay_messages([wire(4, EPOCH_A), wire(5, EPOCH_A), wire(1, EPOCH_A), wire(2, EPOCH_A),
                                       wire(3, EPOCH_A), wire(6, EPOCH_A)])
        self.assertEqual((relay.stats['out_of_order'], relay.stats['restarts'], relay.stats['relayed']), (3, 0, 3))
        self.assertEqual((batch.upstream_missing, batch.upstream_interruptions), (0, 1))

    def test_seq_zero_is_refused_on_the_wire(self):
        # Review L4: a restart at seq 0 would have counted -1 missing.
        relay, batch = relay_messages([wire(3, EPOCH_A), wire(0, EPOCH_B), wire(1, EPOCH_B)])
        self.assertEqual((relay.stats['invalid'], relay.stats['restarts'], relay.stats['missing']), (1, 1, 0))
        self.assertEqual((batch.upstream_missing, batch.upstream_interruptions), (0, 1))

    def test_restart_during_a_redis_outage_reports_both_interruptions(self):
        # Review L5: the outage and the restart are separate unknown-size losses.
        hub = EventHub(clock=lambda: 1.0)
        local = hub.subscribe(buffer=4096)
        pubsubs = [FakePubSub([message(wire(1, EPOCH_A)), message(wire(2, EPOCH_A))], fail_after=2),
                   FakePubSub([message(wire(5, EPOCH_B))])]
        relay = RedisRelay(lambda: FakeClient(pubsubs.pop(0)), hub)
        self.assertEqual(relay.pump(limit=10), 2)  # then the connection drops
        self.assertEqual(relay.stats['connection_errors'], 1)
        self.assertEqual(relay.pump(limit=10), 1)  # resubscribed: the scorer restarted meanwhile
        batch = local.drain_batch()
        self.assertEqual((relay.stats['interruptions'], relay.stats['restarts'], relay.stats['missing']), (1, 1, 4))
        self.assertEqual((batch.upstream_missing, batch.upstream_interruptions), (4, 2))

    def test_messages_without_source_keep_the_earlier_rule(self):
        relay, batch = relay_messages([wire(1, None), wire(2, None), wire(1, None)])
        self.assertEqual((relay.stats['restarts'], relay.stats['out_of_order']), (1, 0))
        self.assertEqual(batch.upstream_interruptions, 1)

    def test_switching_to_or_from_an_epoch_is_a_restart(self):
        for label, payloads in (('older forwarder replaced', [wire(1, None), wire(2, None), wire(3, EPOCH_A)]),
                                ('rolled back', [wire(1, EPOCH_A), wire(2, EPOCH_A), wire(3, None)])):
            with self.subTest(label):
                relay, batch = relay_messages(payloads)
                self.assertEqual((relay.stats['restarts'], relay.stats['missing']), (1, 2))
                self.assertEqual(batch.upstream_interruptions, 1)

    def test_the_first_message_ever_counts_no_loss(self):
        relay, batch = relay_messages([wire(50, EPOCH_A), wire(51, EPOCH_A)])
        self.assertEqual((relay.stats['missing'], relay.stats['restarts']), (0, 0))
        self.assertEqual((batch.upstream_missing, batch.upstream_interruptions), (0, 0))


class EndToEndTests(unittest.TestCase):
    def test_a_source_restart_through_forwarders_and_relay(self):
        bus, destination = Bus(), EventHub(clock=lambda: 1.0)
        local = destination.subscribe(buffer=4096)
        relay = RedisRelay(lambda: bus, destination)
        first = EventHub(clock=lambda: T0)
        forwarder = RedisForwarder(first.subscribe(), bus)
        for i in range(3):
            first.publish('call.started', f'c{i}', {})
        forwarder.pump()
        self.assertEqual(relay.pump(limit=10), 3)
        # The scorer process restarts: a new hub, and its first three events never reach
        # Redis. Its next event has seq 4, exactly what the old epoch's seq 3 predicts, so
        # without the epoch nothing at all would look lost.
        second = EventHub(clock=lambda: T0)
        lost = second.subscribe()
        for call_id in ('c6', 'c7', 'c8'):
            second.publish('call.started', call_id, {})
        forwarder = RedisForwarder(lost, bus)
        lost.drain()  # never forwarded
        second.publish('call.started', 'c9', {})
        forwarder.pump()
        self.assertEqual(relay.pump(limit=10), 1)
        batch = local.drain_batch()
        self.assertEqual([e.call_id for e in batch.envelopes], ['c0', 'c1', 'c2', 'c9'])
        self.assertEqual((batch.upstream_missing, batch.upstream_interruptions), (3, 1))
        self.assertEqual((relay.stats['restarts'], relay.stats['missing']), (1, 3))


class ApiMetricsTests(unittest.TestCase):
    def test_out_of_order_is_an_exported_relay_outcome(self):
        try:
            from services.api.app import RELAY_OUTCOMES, relay_lines
        except ImportError:
            self.skipTest('requirements-api.txt not installed')
        self.assertIn('out_of_order', RELAY_OUTCOMES)
        relay, _ = relay_messages([wire(2, EPOCH_A), wire(2, EPOCH_A)])
        lines = relay_lines({'relay': relay.stats, 'subscribed': True, 'alive': True, 'faults': 0})
        self.assertIn('ava_event_relay_messages_total{outcome="out_of_order"} 1', lines)


@REAL
class RealRedisEpochTests(unittest.TestCase):
    def test_restart_with_lost_first_events_over_real_redis(self):
        import redis
        channel = f'ava:test:{uuid.uuid4().hex[:12]}'
        clients = []

        def client():
            made = redis.Redis.from_url(REDIS_URL, socket_timeout=5, socket_connect_timeout=5)
            clients.append(made)
            return made
        self.addCleanup(lambda: [c.close() for c in clients])
        destination = EventHub(clock=lambda: 1.0)
        local = destination.subscribe(buffer=4096)
        relay = RedisRelay(client, destination, channel=channel)
        relay.pump(timeout=0.5)  # subscribed before anything is published
        self.addCleanup(relay.close)
        first = EventHub(clock=time.time)
        forwarder = RedisForwarder(first.subscribe(), client(), channel=channel)
        for i in range(2):
            first.publish('call.started', f'c{i}', {})
        self.assertEqual(forwarder.pump(), 2)
        second = EventHub(clock=time.time)
        for i in range(3):
            second.publish('call.started', f'd{i}', {})  # published before any forwarder: lost
        forwarder = RedisForwarder(second.subscribe(), client(), channel=channel)
        second.publish('call.started', 'd3', {})
        self.assertEqual(forwarder.pump(), 1)
        deadline, relayed = time.monotonic() + 10, 0
        while relayed < 3 and time.monotonic() < deadline:
            relayed += relay.pump(timeout=0.2)
        self.assertEqual(relayed, 3)
        batch = local.drain_batch()
        self.assertEqual([e.call_id for e in batch.envelopes], ['c0', 'c1', 'd3'])
        self.assertEqual((batch.upstream_missing, batch.upstream_interruptions), (3, 1))
        self.assertEqual(CHANNEL, 'ava:events:v1', 'the production channel is unchanged')


if __name__ == '__main__':
    unittest.main()
