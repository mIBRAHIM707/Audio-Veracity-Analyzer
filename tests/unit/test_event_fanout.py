"""FR-A4 event envelope and bounded non-blocking fan-out (backlog 5.1 partial; PRD
FR-A4, FR-I6, FR-I10; architecture §2.4, §2.5).

Claude-authored test cases, 10 November 2026. In-process only: no Redis, WebSocket
or PostgreSQL (backlog 5.2).
"""
import itertools
import json
import threading
import unittest

from services.events.contract import (MAX_EVENT_BYTES, PUBLISHED_TYPES, EventContractError,
                                      make_envelope)
from services.events.fanout import MAX_BUFFER, EventHub, EventHubError, HubFull

VALID = {
    'call.started': {},
    'score.updated': {'score': 0.7, 'state': 'suspicious', 'degraded': False, 'effective_stride_s': 2.0},
    'alert.raised': {'alert_id': 'a-1'},
    'alert.cleared': {'alert_id': 'a-1'},
    'call.ended': {'final_state': 'alert', 'reason': 'bye', 'speech_seconds': 12.5},
    'system.health': {'reason': 'media_quality'},
}


def nested(levels):
    data = {}
    for _ in range(levels):
        data = {'n': data}
    return data


def clock():
    ticks = itertools.count(1_700_000_000)
    return lambda: float(next(ticks))


class EnvelopeTests(unittest.TestCase):
    def test_every_fr_a4_type_builds_and_serializes(self):
        self.assertEqual(set(PUBLISHED_TYPES), {'call.started', 'score.updated', 'alert.raised',
                                                'alert.cleared', 'call.ended', 'system.health'})
        for event_type, data in VALID.items():
            with self.subTest(event_type):
                call_id = None if event_type == 'system.health' else 'call-1'
                envelope = make_envelope(event_type, call_id, data, seq=3, ts=1.5)
                self.assertEqual(json.loads(envelope.to_json()),
                                 {'type': event_type, 'call_id': call_id, 'seq': 3, 'ts': 1.5, 'data': data})
        make_envelope('system.health', 'call-1', VALID['system.health'], seq=0, ts=0)  # call-scoped health

    def test_internal_scorer_events_are_not_publishable(self):
        for event_type in ('state.changed', 'call.state', 'call.quality', 'call.summary', 'score', ''):
            with self.subTest(event_type):
                with self.assertRaisesRegex(EventContractError, 'not a published'):
                    make_envelope(event_type, 'c', {}, seq=1, ts=1)

    def test_required_fields(self):
        for event_type, data in VALID.items():
            for missing in data:
                with self.subTest(event_type=event_type, missing=missing):
                    with self.assertRaisesRegex(EventContractError, 'missing'):
                        make_envelope(event_type, 'c', {k: v for k, v in data.items() if k != missing},
                                      seq=1, ts=1)
        with self.assertRaisesRegex(EventContractError, 'reason'):
            make_envelope('call.ended', 'c', {**VALID['call.ended'], 'reason': 'hangup'}, seq=1, ts=1)
        with self.assertRaisesRegex(EventContractError, 'degraded'):
            make_envelope('score.updated', 'c', {**VALID['score.updated'], 'degraded': 0}, seq=1, ts=1)

    def test_identity_sequence_and_time_validated(self):
        cases = [('call.started', None, 1, 1), ('call.started', '', 1, 1), ('call.started', 7, 1, 1),
                 ('system.health', '', 1, 1), ('call.started', 'c', -1, 1), ('call.started', 'c', 1.0, 1),
                 ('call.started', 'c', True, 1), ('call.started', 'c', 1, -1), ('call.started', 'c', 1, float('nan')),
                 ('call.started', 'c', 1, '1'), ('call.started', 'c', 1, 10 ** 400), ('call.started', 'c', 1, True),
                 ('call.started', 'c' * 129, 1, 1), ('system.health', 'c' * 129, 1, 1)]
        make_envelope('call.started', 'c' * 128, {}, seq=1, ts=1)
        for event_type, call_id, seq, ts in cases:
            with self.subTest(call_id=call_id, seq=seq, ts=ts):
                with self.assertRaises(EventContractError):
                    make_envelope(event_type, call_id, {}, seq=seq, ts=ts)

    def test_data_must_be_strict_bounded_json(self):
        for name, data in (('nan', {'x': float('nan')}), ('inf', {'x': float('inf')}),
                           ('object', {'x': object()}), ('bytes', {'x': b'pcm'}), ('list', [1]),
                           ('int_key', {1: 'a'}), ('nested_tuple_key', {'x': {(1, 2): 3}}),
                           ('nested_int_key', {'x': {1: 'a'}}), ('deep', nested(10000)),
                           ('too_deep', nested(33)), ('too_big', {'x': 'a' * MAX_EVENT_BYTES})):
            with self.subTest(name):
                with self.assertRaises(EventContractError):
                    make_envelope('call.started', 'c', data, seq=1, ts=1)
        make_envelope('call.started', 'c', nested(32), seq=1, ts=1)
        # Exact size boundary: '{"x": "..."}' adds 9 bytes around the string.
        make_envelope('call.started', 'c', {'x': 'a' * (MAX_EVENT_BYTES - 9)}, seq=1, ts=1)
        with self.assertRaisesRegex(EventContractError, 'exceeds'):
            make_envelope('call.started', 'c', {'x': 'a' * (MAX_EVENT_BYTES - 8)}, seq=1, ts=1)

    def test_urdu_text_is_counted_as_utf8_not_escapes(self):
        text = 'آواز' * 1000  # 4 letters x 2 UTF-8 bytes x 1000 = 8000 bytes, under the cap
        envelope = make_envelope('call.started', 'c', {'note': text}, seq=1, ts=1)
        self.assertEqual(envelope.data['note'], text)
        self.assertIn('آواز', envelope.to_json())

    def test_value_checks(self):
        bad = [('score.updated', {**VALID['score.updated'], 'effective_stride_s': 0}),
               ('score.updated', {**VALID['score.updated'], 'effective_stride_s': float('inf')}),
               ('score.updated', {**VALID['score.updated'], 'score': 1.5}),
               ('score.updated', {**VALID['score.updated'], 'score': '0.5'}),
               ('score.updated', {**VALID['score.updated'], 'state': 'genuine'}),
               ('call.ended', {**VALID['call.ended'], 'final_state': 'undetermined'}),
               ('call.ended', {**VALID['call.ended'], 'speech_seconds': -1}),
               ('call.ended', {**VALID['call.ended'], 'speech_seconds': None})]
        for event_type, data in bad:
            with self.subTest(event_type=event_type, data=data):
                with self.assertRaises(EventContractError):
                    make_envelope(event_type, 'c', data, seq=1, ts=1)
        make_envelope('score.updated', 'c', {**VALID['score.updated'], 'score': None, 'state': 'unmonitored'},
                      seq=1, ts=1)

    def test_envelope_data_is_isolated(self):
        data = {'meta': {'trunk': 'lab'}}
        envelope = make_envelope('call.started', 'c', data, seq=1, ts=1)
        data['meta']['trunk'] = 'changed'
        copy = envelope.data
        copy['meta']['trunk'] = 'tampered'
        self.assertEqual(envelope.data, {'meta': {'trunk': 'lab'}})
        with self.assertRaises(AttributeError):
            envelope.seq = 9


def seqs(subscription, limit=None):
    envelopes, _ = subscription.drain(limit)
    return [e.seq for e in envelopes]


class HubTests(unittest.TestCase):
    def test_delivery_order_sequence_and_filters(self):
        hub = EventHub(clock=clock())
        everything, call_a, call_b = hub.subscribe(), hub.subscribe(call_id='a'), hub.subscribe(call_id='b')
        hub.publish('call.started', 'a', {})
        hub.publish('call.started', 'b', {})
        hub.publish('system.health', None, {'reason': 'media_quality'})
        hub.publish('alert.raised', 'a', {'alert_id': 'x'})
        hub.publish('system.health', 'b', {'reason': 'media_quality'})  # call-scoped health
        envelopes, dropped = everything.drain()
        self.assertEqual([(e.seq, e.type, e.call_id) for e in envelopes],
                         [(1, 'call.started', 'a'), (2, 'call.started', 'b'), (3, 'system.health', None),
                          (4, 'alert.raised', 'a'), (5, 'system.health', 'b')])
        self.assertEqual(dropped, 0)
        self.assertEqual(seqs(call_a), [1, 3, 4])  # own call plus unscoped health; gaps are normal
        self.assertEqual(seqs(call_b), [2, 3, 5])
        self.assertEqual(everything.drain(), ([], 0))
        self.assertEqual([e.ts for e in envelopes], sorted(e.ts for e in envelopes))

    def test_full_buffer_drops_oldest_and_reports_per_drain(self):
        hub = EventHub(clock=clock())
        slow, fast = hub.subscribe(buffer=3), hub.subscribe(buffer=10)
        for n in range(7):
            hub.publish('call.started', f'c{n}', {})
        envelopes, dropped = slow.drain()
        self.assertEqual(([e.seq for e in envelopes], dropped), ([5, 6, 7], 4))
        hub.publish('call.started', 'c7', {})
        self.assertEqual((seqs(slow), slow.drain()[1], slow.dropped_total), ([8], 0, 4))  # reported once
        envelopes, dropped = fast.drain()
        self.assertEqual((len(envelopes), dropped), (8, 0))  # one slow consumer does not affect another

    def test_drain_limit(self):
        hub = EventHub(clock=clock())
        sub = hub.subscribe()
        for n in range(5):
            hub.publish('call.started', f'c{n}', {})
        self.assertEqual(seqs(sub, 2), [1, 2])
        self.assertEqual(seqs(sub, 10), [3, 4, 5])
        for bad in (0, -1, 1.5, True, '2'):
            with self.subTest(limit=bad):
                with self.assertRaises(ValueError):
                    sub.drain(bad)

    def test_invalid_publish_delivers_nothing_and_keeps_sequence(self):
        hub = EventHub(clock=clock())
        sub = hub.subscribe()
        with self.assertRaises(EventContractError):
            hub.publish('state.changed', 'c', {})
        with self.assertRaises(EventContractError):
            hub.publish('call.ended', 'c', {'reason': 'bye'})
        with self.assertRaises(EventContractError):
            hub.publish('call.started', 'c', nested(10000))  # no RecursionError escapes
        hub.publish('call.started', 'c', {})
        self.assertEqual(seqs(sub), [1])

    def test_clock_faults_are_hub_errors_and_deliver_nothing(self):
        def broken():
            raise OSError('clock unavailable')
        for bad_clock in (broken, lambda: float('nan'), lambda: -1.0, lambda: 'now', lambda: 10 ** 400):
            with self.subTest(clock=bad_clock):
                hub = EventHub(clock=bad_clock)
                sub = hub.subscribe()
                with self.assertRaises(EventHubError):
                    hub.publish('call.started', 'c', {})
                self.assertEqual(sub.drain(), ([], 0))
        ticks = iter([float('nan'), 5.0])
        hub = EventHub(clock=lambda: next(ticks))
        sub = hub.subscribe()
        with self.assertRaises(EventHubError):
            hub.publish('call.started', 'c', {})
        self.assertEqual(hub.publish('call.started', 'c', {}).seq, 1)  # failed publish used no seq

    def test_subscriber_limits_and_close(self):
        hub = EventHub(max_subscribers=2, clock=clock())
        first, _ = hub.subscribe(), hub.subscribe()
        with self.assertRaises(HubFull):
            hub.subscribe()
        hub.publish('call.started', 'c', {})
        first.close()
        first.close()  # idempotent
        self.assertEqual((hub.subscriber_count, first.drain()), (1, ([], 0)))
        hub.subscribe()  # a slot is free again
        for kwargs in (dict(buffer=0), dict(buffer=MAX_BUFFER + 1), dict(buffer=2.0), dict(call_id=''),
                       dict(call_id=5)):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    EventHub(clock=clock()).subscribe(**kwargs)
        for bad in (0, -1, 1.0, None):
            with self.subTest(max_subscribers=bad):
                with self.assertRaises(ValueError):
                    EventHub(max_subscribers=bad)

    def test_concurrent_publishers_give_unique_ordered_sequences(self):
        hub = EventHub()
        sub = hub.subscribe(buffer=MAX_BUFFER)
        start = threading.Barrier(4)

        def publisher(name):
            start.wait()
            for n in range(500):
                hub.publish('call.started', f'{name}-{n}', {})

        threads = [threading.Thread(target=publisher, args=(f'p{i}',)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive())
        events, dropped = sub.drain()
        self.assertEqual([e.seq for e in events], list(range(1, 2001)))  # unique, gap-free, in order
        self.assertEqual(dropped, 0)
        self.assertEqual(len({e.call_id for e in events}), 2000)

    def test_drain_and_close_during_publish_burst(self):
        hub = EventHub()
        steady = hub.subscribe(buffer=MAX_BUFFER)
        start, received, errors = threading.Barrier(3), [], []

        def publisher():
            start.wait()
            for n in range(1500):
                hub.publish('call.started', f'c{n}', {})

        def drainer():
            start.wait()
            for _ in range(3000):
                envelopes, dropped = steady.drain(7)
                received.extend(e.seq for e in envelopes)
                if dropped:
                    errors.append(dropped)

        def churner():
            start.wait()
            for _ in range(300):
                hub.subscribe(buffer=1).close()

        threads = [threading.Thread(target=f) for f in (publisher, drainer, churner)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive())
        received.extend(seqs(steady))
        self.assertEqual(received, list(range(1, 1501)))  # nothing lost, duplicated or reordered
        self.assertEqual(errors, [])
        self.assertEqual(hub.subscriber_count, 1)


if __name__ == '__main__':
    unittest.main()
