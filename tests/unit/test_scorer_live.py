"""Live call table: admission, call-scoped config, ordered publishing, cleanup and
late-result discard (backlog 5.2/5.3 partial; FR-I4, FR-I9, FR-I11, FR-A4).

Claude-authored test design, 14 November 2026. Real CallGate, EventHub, scorer and
publisher; `ema_alpha=1.0` so EMA equals the score.
"""
import itertools
import threading
import unittest

from services.events.fanout import EventHub
from services.inference.capacity import CallGate
from services.scorer.config import ScoringConfig
from services.scorer.live import LiveCalls

CALLER = ScoringConfig(ema_alpha=1.0)


class Configs:
    def __init__(self, config=CALLER):
        self.config, self.fail = config, False

    def for_new_call(self):
        if self.fail:
            raise RuntimeError('config store down')
        return self.config


def make(max_calls=8, configs=None):
    hub = EventHub()
    sub = hub.subscribe(buffer=4096)
    ids = (f'alert-{i}' for i in itertools.count(1))
    gate = CallGate(max_calls)
    lock = threading.Lock()

    def new_id():
        with lock:
            return next(ids)

    live = LiveCalls(configs or Configs(), hub, gate, new_alert_id=new_id)
    return live, hub, sub, gate


def drain(sub):
    envelopes, dropped = sub.drain()
    assert dropped == 0
    return envelopes


class AdmissionTests(unittest.TestCase):
    def test_start_publishes_once_and_a_refused_call_gets_a_capacity_health_event(self):
        live, hub, sub, gate = make(max_calls=1)
        self.assertTrue(live.start('a', trunk='lab'))
        self.assertTrue(live.start('a'), 'repeat start is a no-op')
        self.assertFalse(live.start('b'))
        events = drain(sub)
        self.assertEqual([(e.type, e.call_id) for e in events], [('call.started', 'a'), ('system.health', 'b')])
        self.assertEqual(events[0].data, {'trunk': 'lab'})
        self.assertEqual(events[1].data, {'reason': 'capacity', 'call_id': 'b', 'active_calls': 1, 'max_calls': 1})
        self.assertFalse(live.is_active('b'))
        live.end('a', 'bye', speech_seconds={'caller': 1.0})
        self.assertTrue(live.start('b'), 'the slot is free after the end')
        self.assertEqual((live.counters['started'], live.counters['refused'], gate.active), (2, 1, 1))

    def test_config_is_the_snapshot_current_at_call_start(self):
        configs = Configs(ScoringConfig(ema_alpha=1.0, min_windows=1))
        live, hub, sub, gate = make(configs=configs)
        live.start('early')
        configs.config = ScoringConfig(ema_alpha=1.0, min_windows=3)  # reload after 'early' started
        live.start('late')
        for call_id in ('early', 'late'):
            live.result(call_id, 'caller', 0, 0.9, effective_stride_s=1.0)
        states = {e.call_id: e.data['state'] for e in drain(sub) if e.type == 'score.updated'}
        self.assertEqual(states, {'early': 'suspicious', 'late': 'monitoring'})

    def test_a_failing_start_releases_the_gate_slot(self):
        configs = Configs()
        live, hub, sub, gate = make(max_calls=1, configs=configs)
        configs.fail = True
        with self.assertRaises(RuntimeError):
            live.start('x')
        self.assertEqual(gate.active, 0)
        configs.fail = False
        self.assertTrue(live.start('y'))


class CleanupTests(unittest.TestCase):
    def test_results_for_unknown_or_ended_calls_are_discarded_and_counted(self):
        live, hub, sub, gate = make()
        self.assertEqual(live.result('ghost', 'caller', 0, 0.5, effective_stride_s=1.0).reason, 'unknown_call')
        live.start('c')
        outcome = live.result('c', 'caller', 0, 0.5, effective_stride_s=1.0)
        self.assertEqual((outcome.applied, outcome.reason, [e.type for e in outcome.envelopes]),
                         (True, None, ['score.updated']))
        self.assertEqual(live.result('c', 'caller', 0, 0.5, effective_stride_s=1.0).reason, 'duplicate')
        [ended] = live.end('c', 'bye', speech_seconds={'caller': 3.0})
        self.assertEqual((ended.type, ended.data['reason']), ('call.ended', 'bye'))
        self.assertEqual(live.result('c', 'caller', 1, 0.5, effective_stride_s=1.0).reason, 'unknown_call')
        self.assertEqual(live.end('c', 'bye', speech_seconds={'caller': 3.0}), [], 'second end is a no-op')
        self.assertEqual(live.counters['discarded_unknown_call'], 2)
        self.assertEqual(gate.active, 0)

    def test_invalid_end_raises_and_leaves_the_call_running(self):
        live, hub, sub, gate = make()
        live.start('c')
        for bad in ({'reason': 'hangup', 'speech_seconds': {'caller': 1.0}},
                    {'reason': 'bye', 'speech_seconds': {'agent': 1.0}},
                    {'reason': 'bye', 'speech_seconds': {'caller': -1.0}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                live.end('c', bad['reason'], speech_seconds=bad['speech_seconds'])
        self.assertTrue(live.is_active('c'))
        self.assertEqual(gate.active, 1)
        self.assertTrue(live.result('c', 'caller', 0, 0.5, effective_stride_s=1.0).applied)

    def test_monitor_lost_ends_every_open_call_and_one_failure_does_not_stop_the_rest(self):
        live, hub, sub, gate = make()
        for call_id in ('a', 'b', 'c'):
            live.start(call_id)
        seen = []

        def speech(call_id, directions):
            seen.append((call_id, directions))
            if call_id == 'b':
                raise KeyError('no speech record')
            return {d: 2.0 for d in directions}

        self.assertEqual(live.end_all('monitor_lost', speech_seconds_for=speech), 3)
        self.assertEqual(sorted(seen), [('a', ('caller',)), ('b', ('caller',)), ('c', ('caller',))])
        ended = {e.call_id: e.data for e in drain(sub) if e.type == 'call.ended'}
        self.assertEqual({k: v['reason'] for k, v in ended.items()},
                         {'a': 'monitor_lost', 'b': 'monitor_lost', 'c': 'monitor_lost'})
        self.assertEqual((ended['b']['speech_seconds'], ended['b']['speech_seconds_known']), (0.0, False))
        self.assertEqual(ended['a']['speech_seconds'], 2.0)
        self.assertNotIn('speech_seconds_known', ended['a'])
        self.assertEqual(live.counters['speech_seconds_missing'], 1)
        self.assertEqual((gate.active, live.snapshot()['calls']), (0, {}))
        with self.assertRaises(ValueError):
            live.end_all('hangup', speech_seconds_for=speech)

    def test_end_all_reads_directions_with_the_end_so_a_late_agent_answer_is_included(self):
        live, hub, sub, gate = make(configs=Configs(ScoringConfig(ema_alpha=1.0,
                                                                  score_directions=('caller', 'agent'))))
        live.start('c')
        live.human_agent_answered('c')
        live.end_all('monitor_lost', speech_seconds_for=lambda call_id, directions: {d: 1.0 for d in directions})
        [ended] = [e for e in drain(sub) if e.type == 'call.ended']
        self.assertEqual(ended.data['speech_seconds_by_direction'], {'caller': 1.0, 'agent': 1.0})

    def test_end_all_survives_a_crashing_scorer_and_counts_ends_whose_publish_failed(self):
        live, hub, sub, gate = make()
        for call_id in ('a', 'b', 'c'):
            live.start(call_id)

        def crash(*args, **kwargs):
            raise RuntimeError('scorer bug')

        live._calls['b'].scorer.end = crash
        hub._clock = lambda: float('nan')  # every call.ended publish now fails
        speech = lambda call_id, directions: {d: 1.0 for d in directions}
        self.assertEqual(live.end_all('monitor_lost', speech_seconds_for=speech), 2)
        self.assertEqual(live.counters['end_all_failures'], 1)
        self.assertEqual((gate.active, live.snapshot()['calls']), (0, {}), "b's slot is released too")
        self.assertEqual(live.counters['publish_failures'], 2)

    def test_a_hub_failure_during_end_still_frees_the_slot_and_is_counted(self):
        live, hub, sub, gate = make()
        live.start('c')
        live.result('c', 'caller', 0, 0.5, effective_stride_s=1.0)
        hub._clock = lambda: float('nan')
        self.assertEqual(live.end('c', 'bye', speech_seconds={'caller': 1.0}), [])
        self.assertEqual((gate.active, live.is_active('c'), live.counters['publish_failures']), (0, False, 1))

    def test_call_started_is_published_before_any_racing_result_or_end(self):
        """Review M2 (start): block the hub inside call.started's publish and let
        another thread try to end the call meanwhile."""
        live, hub, sub, gate = make()
        inside, release = threading.Event(), threading.Event()
        original = hub.publish

        def slow_publish(event_type, call_id, data):
            if event_type == 'call.started':
                inside.set()
                release.wait(5)
            return original(event_type, call_id, data)

        hub.publish = slow_publish
        starter = threading.Thread(target=live.start, args=('c',))
        starter.start()
        self.assertTrue(inside.wait(5))
        ender = threading.Thread(target=live.end, args=('c', 'bye'), kwargs={'speech_seconds': {'caller': 1.0}})
        ender.start()
        ender.join(0.2)
        self.assertTrue(ender.is_alive(), 'end waits for the call lock held by start')
        release.set()
        starter.join(5)
        ender.join(5)
        self.assertEqual([e.type for e in drain(sub)], ['call.started', 'call.ended'])

    def test_status_and_agent_answer_for_unknown_calls_do_nothing(self):
        live, hub, sub, gate = make()
        self.assertEqual(live.status('none', 'unmonitored', degraded=True, effective_stride_s=4.0), [])
        self.assertFalse(live.human_agent_answered('none'))
        live.start('c')
        [env] = live.status('c', 'unmonitored', degraded=True, effective_stride_s=4.0)
        self.assertEqual((env.data['state'], env.data['degraded']), ('unmonitored', True))
        self.assertTrue(live.human_agent_answered('c'))
        self.assertIn('c', live.snapshot()['calls'])


class ConcurrencyTests(unittest.TestCase):
    def test_events_reach_the_hub_in_scorer_order_with_several_workers(self):
        """Review M2: apply and publish must be one critical section per call."""
        live, hub, sub, gate = make()
        live.start('c')
        indices = iter(range(4000))
        lock = threading.Lock()

        def worker():
            while True:
                with lock:
                    idx = next(indices, None)
                if idx is None:
                    return
                live.result('c', 'caller', idx, (idx % 7) / 7, effective_stride_s=1.0)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        windows = [e.data['window'] for e in drain(sub) if e.type == 'score.updated']
        self.assertGreater(len(windows), 100)
        self.assertEqual(windows, sorted(windows), 'published window numbers follow seq order')
        self.assertEqual(len(set(windows)), len(windows))

    def test_end_racing_with_results_publishes_nothing_for_the_call_after_call_ended(self):
        reasons_seen = set()
        for attempt in range(20):
            live, hub, sub, gate = make()
            live.start('c')
            go, warmed = threading.Event(), threading.Event()
            outcomes = []

            def worker(base):
                go.wait()
                for i in range(200):
                    outcomes.append(live.result('c', 'caller', base + i * 4, 0.5, effective_stride_s=1.0))
                    if i == 10:
                        warmed.set()  # end only once results are flowing

            threads = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
            for thread in threads:
                thread.start()
            go.set()
            self.assertTrue(warmed.wait(5))
            live.end('c', 'bye', speech_seconds={'caller': 1.0})
            for thread in threads:
                thread.join()
            types = [e.type for e in drain(sub)]
            self.assertEqual(types[-1], 'call.ended', f'attempt {attempt}')
            self.assertEqual(types.count('call.ended'), 1)
            applied = sum(o.applied for o in outcomes)
            self.assertEqual(types.count('score.updated'), applied, 'every applied result published, no other')
            self.assertEqual(gate.active, 0)
            reasons_seen.update(o.reason for o in outcomes)
        # The race really happened: some results were applied and some discarded late.
        self.assertIn(None, reasons_seen)
        self.assertTrue(reasons_seen & {'unknown_call', 'call_ended'})


if __name__ == '__main__':
    unittest.main()
