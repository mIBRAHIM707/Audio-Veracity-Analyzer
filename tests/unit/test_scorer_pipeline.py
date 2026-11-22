"""Single-owner live inference loop (backlog 8.1/8.2 wiring; FR-I9-FR-I11).

Claude-authored test design, 14 November 2026. Real LiveCalls, CallGate, EventHub,
MicroBatcher, PendingWindows and StrideController; the detector is a fake that turns
a window filled with value v into logits whose softmax gives exactly p = v.
"""
import math
import unittest

import numpy as np

from services.events.fanout import EventHub
from services.inference.analysis import WINDOW_SAMPLES
from services.inference.capacity import CallGate, MicroBatcher
from services.scorer.config import ScoringConfig
from services.scorer.live import LiveCalls
from services.scorer.pipeline import LivePipeline

CONFIG = ScoringConfig(ema_alpha=1.0)


class Configs:
    def for_new_call(self):
        return CONFIG


class Detector:
    """p_synthetic = window value (the window is constant)."""

    def __init__(self):
        self.batches, self.fail = [], None

    def __call__(self, batch):
        self.batches.append(batch.shape)
        if self.fail == 'raise':
            raise RuntimeError('kernel fault')
        values = batch[:, 0].astype(np.float64)
        logits = np.stack([np.log(values / (1 - values)), np.zeros_like(values)], axis=1)
        return logits[:-1] if self.fail == 'short' else logits


def window(value):
    return np.full(WINDOW_SAMPLES, value, dtype=np.float32)


def make(max_pending=4, max_pending_live=256):
    hub = EventHub()
    sub = hub.subscribe(buffer=4096)
    live = LiveCalls(Configs(), hub, CallGate(8), new_alert_id=iter(f'alert-{i}' for i in range(1, 99)).__next__)
    batcher = MicroBatcher(capacity_windows_per_s=10.0, collect_s=0.1, max_live=4, max_pending_live=max_pending_live)
    detector = Detector()
    return LivePipeline(live, batcher, detector, CONFIG, max_pending=max_pending), live, detector, sub


def events(sub):
    envelopes, dropped = sub.drain()
    assert dropped == 0
    return envelopes


class FlowTests(unittest.TestCase):
    def test_windows_flow_to_scores_and_an_alert(self):
        pipe, live, detector, sub = make()
        live.start('c')
        for i in range(3):
            now = 2.0 * i
            self.assertTrue(pipe.offer('c', 'caller', i, window(0.9), now)['accepted'])
            self.assertEqual(pipe.schedule(now), 1)
            self.assertEqual(pipe.step(now), 0, 'not due before the collection interval')
            self.assertEqual(pipe.step(now + 0.1), 1)
        published = events(sub)
        scores = [e.data for e in published if e.type == 'score.updated']
        self.assertEqual([round(s['score'], 6) for s in scores], [0.9, 0.9, 0.9])
        self.assertEqual({(s['degraded'], s['effective_stride_s']) for s in scores}, {(False, 2.0)})
        self.assertEqual([e.data['alert_id'] for e in published if e.type == 'alert.raised'], ['alert-1'])
        self.assertEqual(pipe.counts['scored'], 3)

    def test_windows_of_two_calls_share_one_batch(self):
        pipe, live, detector, sub = make()
        for call_id, value in (('a', 0.2), ('b', 0.7)):
            live.start(call_id)
            pipe.offer(call_id, 'caller', 0, window(value), 0.0)
        self.assertEqual(pipe.schedule(0.0), 2)
        self.assertEqual(pipe.step(0.1), 2)
        self.assertEqual(detector.batches, [(2, WINDOW_SAMPLES)])
        got = {e.call_id: round(e.data['score'], 6) for e in events(sub) if e.type == 'score.updated'}
        self.assertEqual(got, {'a': 0.2, 'b': 0.7})


class DegradedTests(unittest.TestCase):
    def test_backlog_grows_the_stride_then_publishes_unmonitored_once(self):
        pipe, live, detector, sub = make(max_pending=2)
        live.start('c')
        accepted = []
        for i in range(40):  # inference never runs: the per-call queue stays behind
            outcome = pipe.offer('c', 'caller', i, window(0.5), 2.0 * i)
            if outcome['accepted']:
                accepted.append(i)
        self.assertEqual(accepted[:5], [0, 1, 3, 6, 10], 'stride grows by one hop per behind decision')
        unmonitored = [e for e in events(sub) if e.type == 'score.updated' and e.data['state'] == 'unmonitored']
        self.assertEqual(len(unmonitored), 1)
        self.assertEqual((unmonitored[0].data['degraded'], unmonitored[0].data['effective_stride_s'],
                          unmonitored[0].data['reason']), (True, 8.0, 'stride_cap'))
        self.assertGreater(pipe.counts['dropped_pending'], 0)
        snap = pipe.snapshot()['directions']['c/caller']
        self.assertEqual((snap['waiting'], snap['unmonitored']), (2, True))

    def test_results_carry_the_degraded_stride_in_force(self):
        pipe, live, detector, sub = make()
        live.start('c')
        pipe.offer('c', 'caller', 0, window(0.3), 0.0)
        pipe.offer('c', 'caller', 1, window(0.3), 2.0)  # behind: stride now 2 hops
        self.assertEqual(pipe.schedule(2.0), 1, 'one window per direction in flight')
        self.assertEqual(pipe.step(2.1), 1)
        scores = [e.data for e in events(sub) if e.type == 'score.updated']
        self.assertEqual({(s['degraded'], s['effective_stride_s']) for s in scores}, {(True, 4.0)})

    def test_realistic_loop_with_slow_inference_grows_the_stride_not_the_batcher(self):
        """Review H1: offer and schedule every hop, inference never finishes."""
        pipe, live, detector, sub = make()
        live.start('c')
        for i in range(3):
            pipe.offer('c', 'caller', i, window(0.3), 2.0 * i)
            pipe.schedule(2.0 * i)
        # window 0 in flight; window 1 waited without being "behind" (nothing was waiting
        # yet); window 2 found window 1 waiting -> behind -> stride grew to 2 hops.
        snap = pipe.snapshot()['directions']['c/caller']
        self.assertEqual((snap['in_flight'], snap['waiting'], snap['effective_stride_s']), (True, 2, 4.0))
        self.assertEqual(pipe.batcher.pending()['live'], 1, 'the shared batcher holds one window, not a backlog')

    def test_schedule_leaves_backlog_in_the_call_queue_when_the_batcher_is_full(self):
        pipe, live, detector, sub = make(max_pending_live=1)
        for call_id in ('a', 'b'):
            live.start(call_id)
            pipe.offer(call_id, 'caller', 0, window(0.3), 0.0)
        self.assertEqual(pipe.schedule(0.0), 1)
        waiting = sorted(v['waiting'] for v in pipe.snapshot()['directions'].values())
        self.assertEqual(waiting, [0, 1])

    def test_schedule_rotates_so_every_call_is_served(self):
        pipe, live, detector, sub = make(max_pending_live=1)
        calls = ('a', 'b', 'c')
        for call_id in calls:
            live.start(call_id)
            pipe.offer(call_id, 'caller', 0, window(0.3), 0.0)
        now = 0.0
        for _ in calls:
            pipe.schedule(now)
            now += 0.1
            self.assertEqual(pipe.step(now), 1)
        served = {e.call_id for e in events(sub) if e.type == 'score.updated'}
        self.assertEqual(served, set(calls))


class CancellationAndFailureTests(unittest.TestCase):
    def test_end_call_cancels_waiting_windows_and_skips_batched_ones(self):
        pipe, live, detector, sub = make(max_pending=4)
        live.start('c')
        pipe.offer('c', 'caller', 0, window(0.4), 0.0)
        pipe.schedule(0.0)  # now in the batcher
        pipe.offer('c', 'caller', 1, window(0.4), 2.0)  # waiting in the call queue
        pipe.end_call('c', 'bye', speech_seconds={'caller': 3.0})
        self.assertEqual(pipe.step(2.1), 0)
        self.assertEqual((pipe.counts['skipped_ended'], pipe.counts['cancelled_pending']), (1, 1))
        self.assertEqual(detector.batches, [], 'no inference for an ended call')
        self.assertEqual([e.type for e in events(sub)], ['call.started', 'call.ended'])
        self.assertIsNone(pipe.offer('c', 'caller', 2, window(0.4), 4.0))
        self.assertEqual(pipe.counts['offered_inactive'], 1)

    def test_calls_ended_elsewhere_are_cleaned_up_at_schedule(self):
        pipe, live, detector, sub = make()
        live.start('c')
        pipe.offer('c', 'caller', 0, window(0.4), 0.0)
        live.end_all('monitor_lost', speech_seconds_for=lambda call_id, directions: {d: 0.0 for d in directions})
        self.assertEqual(pipe.schedule(0.0), 0)
        self.assertEqual((pipe.counts['cancelled_pending'], pipe.snapshot()['directions']), (1, {}))

    def test_a_batched_window_of_a_call_ended_elsewhere_skips_inference(self):
        pipe, live, detector, sub = make()
        live.start('c')
        pipe.offer('c', 'caller', 0, window(0.4), 0.0)
        pipe.schedule(0.0)
        live.end_all('monitor_lost', speech_seconds_for=lambda call_id, directions: {d: 0.0 for d in directions})
        self.assertEqual(pipe.step(0.1), 0)  # before any schedule cleans up
        self.assertEqual((pipe.counts['skipped_ended'], detector.batches), (1, []))

    def test_detector_faults_lose_one_batch_and_the_calls_continue(self):
        for fail in ('raise', 'short'):
            with self.subTest(fail=fail):
                pipe, live, detector, sub = make()
                live.start('c')
                detector.fail = fail
                pipe.offer('c', 'caller', 0, window(0.6), 0.0)
                pipe.schedule(0.0)
                self.assertEqual(pipe.step(0.1), 0)
                self.assertEqual((pipe.counts['detector_failures'], pipe.counts['windows_lost_to_detector']), (1, 1))
                detector.fail = None
                pipe.offer('c', 'caller', 1, window(0.6), 2.0)
                pipe.schedule(2.0)
                self.assertEqual(pipe.step(2.1), 1)
                self.assertTrue(live.is_active('c'))

    def test_nan_logits_count_as_a_detector_fault(self):
        pipe, live, detector, sub = make()
        live.start('c')
        pipe.detector = lambda batch: [[math.nan, 0.0]]
        pipe.offer('c', 'caller', 0, window(0.6), 0.0)
        pipe.schedule(0.0)
        self.assertEqual(pipe.step(0.1), 0)
        self.assertEqual(pipe.counts['detector_failures'], 1)

    def test_invalid_windows_are_refused_before_any_change(self):
        pipe, live, detector, sub = make()
        live.start('c')
        for bad in (np.zeros(10, np.float32), np.zeros(WINDOW_SAMPLES, np.float64),
                    np.full(WINDOW_SAMPLES, np.nan, np.float32)):
            with self.assertRaises(ValueError):
                pipe.offer('c', 'caller', 0, bad, 0.0)
        with self.assertRaises(ValueError):
            pipe.offer('c', 'bogus', 0, window(0.5), 0.0)
        pipe.offer('c', 'caller', 5, window(0.5), 0.0)
        with self.assertRaises(ValueError):
            pipe.offer('c', 'caller', 5, window(0.5), 1.0)  # index not increasing
        self.assertEqual(pipe.snapshot()['directions']['c/caller']['accepted'], 1)
        with self.assertRaises(ValueError):
            LivePipeline(live, None, None, CONFIG, max_pending=0)

    def test_a_backwards_clock_raises_before_losing_a_window(self):
        pipe, live, detector, sub = make()
        live.start('c')
        pipe.offer('c', 'caller', 0, window(0.5), 2.0)
        for call in (lambda: pipe.schedule(1.0), lambda: pipe.step(1.0),
                     lambda: pipe.offer('c', 'caller', 1, window(0.5), 1.0)):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(pipe.snapshot()['directions']['c/caller']['waiting'], 1)
        self.assertEqual(pipe.schedule(2.0), 1)

    def test_a_reused_call_id_is_not_fed_the_old_calls_windows(self):
        """Review M6: an old batched window must not make the new call's indexes stale."""
        pipe, live, detector, sub = make()
        live.start('c')
        pipe.offer('c', 'caller', 50, window(0.9), 0.0)
        pipe.schedule(0.0)  # index 50 sits in the batcher
        pipe.end_call('c', 'bye', speech_seconds={'caller': 1.0})
        live.start('c')
        pipe.offer('c', 'caller', 0, window(0.2), 1.0)
        pipe.schedule(1.0)
        self.assertEqual(pipe.step(1.1), 1)
        self.assertEqual(pipe.counts['skipped_ended'], 1)
        last = [e for e in events(sub) if e.type == 'score.updated'][-1]
        self.assertAlmostEqual(last.data['score'], 0.2, places=6)

    def test_one_failing_result_does_not_stop_the_rest_of_the_batch(self):
        pipe, live, detector, sub = make()
        for call_id in ('a', 'b'):
            live.start(call_id)
            pipe.offer(call_id, 'caller', 0, window(0.4), 0.0)
        original = live.result

        def flaky(call_id, *args, **kwargs):
            if call_id == 'a':
                raise RuntimeError('scorer fault')
            return original(call_id, *args, **kwargs)

        live.result = flaky
        pipe.schedule(0.0)
        self.assertEqual(pipe.step(0.1), 1)
        self.assertEqual(pipe.counts['result_errors'], 1)

    def test_file_batches_are_counted_not_run_and_windows_are_copied(self):
        pipe, live, detector, sub = make()
        pipe.batcher.submit('file-item', kind='file', now=0.0)
        self.assertEqual(pipe.step(0.2), 0)
        self.assertEqual(pipe.counts['file_batches_not_run'], 1)
        live.start('c')
        buffer = window(0.3)
        pipe.offer('c', 'caller', 0, buffer, 0.3)
        buffer[:] = 0.8  # the builder reuses its buffer
        pipe.schedule(0.3)
        pipe.step(0.4)
        [score] = [e.data['score'] for e in events(sub) if e.type == 'score.updated']
        self.assertAlmostEqual(score, 0.3, places=6)


if __name__ == '__main__':
    unittest.main()
