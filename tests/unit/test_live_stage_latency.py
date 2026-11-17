"""FR-O6 "latency per stage" and "alerts per minute" for the live loop (backlog 7.2/8.1;
PRD FR-O6; architecture §2.3 latency budget per hop).

Claude-authored test design, 17 November 2026. Real LiveCalls, CallGate, EventHub,
MicroBatcher and LivePipeline with the constant-window fake detector of
test_scorer_pipeline. Loop times and timer readings are chosen as exact binary
fractions so every expected duration is computed by hand without rounding. Infrastructure
evidence only: these are measurements of the loop's own stages, not product latency
targets (no telephony, no model).
"""
import types
import unittest

from services.scorer import metrics_text
from services.scorer.pipeline import STAGE_BUCKETS_S, STAGES, LivePipeline
from tests.unit.test_api_readiness_metrics import parse_metrics
from tests.unit.test_scorer_pipeline import events, make, window

TICK = 0.0078125  # 2 ** -7 s


class Timer:
    """Returns the scripted readings in order; raises once they run out (a test that
    reads more than it scripted then shows as a stage timer failure)."""

    def __init__(self, *readings):
        self.readings, self.reads = list(readings), 0

    def __call__(self):
        self.reads += 1
        if not self.readings:
            raise RuntimeError('timer read not scripted')
        return self.readings.pop(0)


def timed(timer, max_pending=4):
    pipe, live, detector, sub = make(max_pending=max_pending)
    return LivePipeline(live, pipe.batcher, detector, pipe.config, max_pending=max_pending, timer=timer), live, \
        detector, sub


def stage(pipe, name):
    return pipe.metrics_snapshot()['stage_seconds']['series'][name]


def expected_buckets(values):
    return [sum(v <= bound for v in values) for bound in STAGE_BUCKETS_S]


class StageTimingTests(unittest.TestCase):
    def assert_stage(self, pipe, name, values):
        series = stage(pipe, name)
        self.assertEqual(series['count'], len(values), name)
        self.assertEqual(series['sum'], sum(values), name)
        self.assertEqual(series['buckets'], expected_buckets(values), name)

    def test_one_window_hand_worked_timeline(self):
        # offer 10.0, schedule 10.5, step 10.625; timer 0 -> inference end 0.25 -> applied 0.25 + TICK.
        pipe, live, _, _ = timed(Timer(0.0, 0.25, 0.25 + TICK))
        live.start('c')
        pipe.offer('c', 'caller', 0, window(0.4), 10.0)
        self.assertEqual(pipe.schedule(10.5), 1)
        self.assertEqual(pipe.step(10.625), 1)
        self.assert_stage(pipe, 'queue_wait', [0.5])
        self.assert_stage(pipe, 'batch_wait', [0.125])
        self.assert_stage(pipe, 'apply', [TICK])
        self.assert_stage(pipe, 'window_to_result', [0.625 + 0.25 + TICK])
        self.assertEqual(pipe.metrics_snapshot()['inference_seconds']['series']['ok']['sum'], 0.25)
        self.assertEqual((pipe.counts['stage_timer_failures'], pipe.counts['timer_failures']), (0, 0))
        self.assertEqual(pipe._offered, {}, 'the offer time is released with the result')

    def test_windows_in_one_batch_apply_in_turn(self):
        # a offered at 0, b at 0.25; both scheduled at 0.25, stepped at 0.375. Inference
        # 0 -> 0.5, then a applied at 0.5 + TICK, b at 0.5 + 2 TICK: b's result waits for a's.
        pipe, live, detector, _ = timed(Timer(0.0, 0.5, 0.5 + TICK, 0.5 + 2 * TICK))
        for call_id in ('a', 'b'):
            live.start(call_id)
        pipe.offer('a', 'caller', 0, window(0.2), 0.0)
        pipe.offer('b', 'caller', 0, window(0.3), 0.25)
        self.assertEqual(pipe.schedule(0.25), 2)
        self.assertEqual(pipe.step(0.375), 2)
        self.assertEqual(len(detector.batches), 1)
        self.assert_stage(pipe, 'queue_wait', [0.25, 0.0])
        self.assert_stage(pipe, 'batch_wait', [0.125, 0.125])
        self.assert_stage(pipe, 'apply', [TICK, TICK])
        self.assert_stage(pipe, 'window_to_result', [0.375 + 0.5 + TICK, 0.125 + 0.5 + 2 * TICK])

    def test_queue_wait_grows_while_a_direction_is_behind(self):
        # Window 1 waits behind window 0 (in flight) and is handed over only after 0 is scored.
        pipe, live, _, _ = timed(Timer(0.0, TICK, TICK, 0.0, TICK, TICK))
        live.start('c')
        pipe.offer('c', 'caller', 0, window(0.4), 0.0)
        pipe.schedule(0.0)
        pipe.offer('c', 'caller', 1, window(0.4), 2.0)
        self.assertEqual(pipe.schedule(2.0), 0, 'one window per direction in flight')
        self.assertEqual(pipe.step(2.25), 1)
        self.assertEqual(pipe.schedule(3.0), 1)
        self.assertEqual(pipe.step(3.125), 1)
        self.assert_stage(pipe, 'queue_wait', [0.0, 1.0])
        self.assert_stage(pipe, 'batch_wait', [2.25, 0.125])
        self.assert_stage(pipe, 'window_to_result', [2.25 + TICK, 1.125 + TICK])

    def test_buckets_include_the_architecture_budgets_and_are_inclusive(self):
        for budget in (0.02, 0.1, 1.2, 1.6):
            self.assertIn(budget, STAGE_BUCKETS_S)
        self.assertEqual(list(STAGE_BUCKETS_S), sorted(set(STAGE_BUCKETS_S)))
        self.assertEqual(STAGES, metrics_text.STAGES, 'renderer and pipeline name the same stages')
        pipe, live, _, _ = timed(Timer(0.0, 0.5, 0.5))
        live.start('c')
        pipe.offer('c', 'caller', 0, window(0.4), 0.0)
        pipe.schedule(0.0)
        pipe.step(0.1)  # batch wait exactly on the 0.1 bound (0.1 - 0.0 is exact)
        series = stage(pipe, 'batch_wait')
        self.assertEqual(series['buckets'][STAGE_BUCKETS_S.index(0.1)], 1, 'le="0.1" includes 0.1')
        self.assertEqual(series['buckets'][STAGE_BUCKETS_S.index(0.05)], 0)
        self.assertEqual(stage(pipe, 'apply')['buckets'][0], 1, 'an apply time of 0 is in the first bucket')

    def test_unscored_windows_record_no_result_time_and_release_their_offer_time(self):
        pipe, live, detector, sub = make(max_pending=2)
        for call_id in ('a', 'b', 'c'):
            live.start(call_id)
        for i in range(10):  # 'a' never scheduled: the stride grows and its oldest windows are dropped
            pipe.offer('a', 'caller', i, window(0.4), 2.0 * i)
        self.assertGreaterEqual(pipe.counts['dropped_pending'], 1)
        dropped = pipe.counts['dropped_pending']
        # Review M2: dropped windows release their offer time at once, not at the call's end.
        self.assertEqual(len(pipe._offered), pipe.metrics_snapshot()['waiting'])
        self.assertEqual(len(pipe._offered), 2)
        pipe.end_call('a', 'bye', speech_seconds={'caller': 1.0})  # the two waiting are cancelled
        detector.fail = 'raise'
        pipe.offer('b', 'caller', 0, window(0.4), 24.0)
        pipe.schedule(24.0)
        pipe.step(24.125)  # detector failure loses the window
        detector.fail = None
        real = live.result
        live.result = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('publish fault'))
        pipe.offer('b', 'caller', 1, window(0.4), 26.0)
        pipe.schedule(26.0)
        pipe.step(26.125)  # result error
        live.result = lambda *a, **k: types.SimpleNamespace(applied=False)
        pipe.offer('b', 'caller', 2, window(0.4), 28.0)
        pipe.schedule(28.0)
        pipe.step(28.125)  # not applied
        live.result = real
        pipe.offer('c', 'caller', 0, window(0.4), 29.0)
        pipe.schedule(29.0)
        pipe.end_call('c', 'bye', speech_seconds={'caller': 1.0})
        pipe.step(29.125)  # skipped: its call ended while batched
        c = pipe.counts
        self.assertEqual((c['dropped_pending'], c['cancelled_pending'], c['windows_lost_to_detector'],
                          c['result_errors'], c['result_not_applied'], c['skipped_ended'], c['scored']),
                         (dropped, 2, 1, 1, 1, 1, 0))
        self.assertEqual(stage(pipe, 'window_to_result')['count'], 0)
        self.assertEqual(stage(pipe, 'queue_wait')['count'], 4, 'every hand-over to the batcher')
        self.assertEqual(stage(pipe, 'batch_wait')['count'], 3, 'every batched window taken for a live call')
        self.assertEqual(stage(pipe, 'apply')['count'], 1, 'only results LiveCalls returned normally')
        self.assertEqual(pipe._offered, {}, 'no offer time left behind on any path')
        self.assertEqual(pipe.counts['stage_timer_failures'], 0)

    def test_reused_call_id_uses_the_new_calls_offer_time(self):
        pipe, live, _, sub = timed(Timer(0.0, 0.25, 0.25))
        live.start('b')
        pipe.offer('b', 'caller', 0, window(0.5), 1.0)
        pipe.schedule(1.0)  # batched for the first call
        # Waiting; review M2: an index the new call never uses, so a leak cannot be overwritten.
        self.assertTrue(pipe.offer('b', 'caller', 7, window(0.5), 1.5)['accepted'])
        self.assertEqual(len(pipe._offered), 2)
        live.end_all('monitor_lost', speech_seconds_for=lambda call_id, directions: 0.0)
        live.start('b')
        pipe.offer('b', 'caller', 0, window(0.9), 5.0)  # the same key and index 0 for the new call
        self.assertEqual(list(pipe._offered.values()), [5.0], 'the old call offer times are gone')
        pipe.schedule(5.0)
        self.assertEqual(pipe.step(5.125), 1)
        self.assertEqual(pipe.counts['skipped_ended'], 1)
        self.assert_stage(pipe, 'window_to_result', [0.125 + 0.25])
        self.assertEqual(pipe._offered, {})

    def test_timer_faults_never_stop_scoring_and_loop_clock_stages_still_count(self):
        # Expected stage timer failures (review L5): apply and window_to_result each miss once,
        # except when only the batch start is missing (apply is still timed from inference end).
        for name, timer, failures in (('raises', lambda: (_ for _ in ()).throw(OSError('no clock')), 2),
                                      ('nan', lambda: float('nan'), 2),
                                      ('backwards apply', Timer(0.0, 0.5, 0.25), 2),
                                      ('missing start', Timer(float('nan'), 0.5, 0.5 + TICK), 1)):
            with self.subTest(timer=name):
                pipe, live, _, _ = timed(timer)
                live.start('c')
                pipe.offer('c', 'caller', 0, window(0.4), 0.0)
                pipe.schedule(0.0)
                self.assertEqual(pipe.step(0.125), 1)
                self.assertEqual(stage(pipe, 'queue_wait')['count'], 1)
                self.assertEqual(stage(pipe, 'batch_wait')['count'], 1)
                self.assertEqual(stage(pipe, 'window_to_result')['count'], 0)
                self.assertEqual(pipe.counts['stage_timer_failures'], failures)
                if name == 'missing start':  # inference unmeasured; the apply after it still is
                    self.assertEqual((pipe.counts['timer_failures'], stage(pipe, 'apply')['sum']), (1, TICK))
                else:
                    self.assertEqual(stage(pipe, 'apply')['count'], 0)


class AlertCountTests(unittest.TestCase):
    def test_published_alert_raised_events_are_counted(self):
        pipe, live, _, sub = make()
        for call_id in ('a', 'b'):
            live.start(call_id)
        now = 0.0
        for i in range(3):  # 0.9 three times raises one alert per call (test_scorer_pipeline FlowTests)
            now = 2.0 * i
            for call_id in ('a', 'b'):
                pipe.offer(call_id, 'caller', i, window(0.9), now)
            pipe.schedule(now)
            pipe.step(now + 0.125)
        raised = [e for e in events(sub) if e.type == 'alert.raised']
        self.assertEqual(len(raised), 2)
        self.assertEqual(pipe.counts['alerts_raised'], 2)
        self.assertEqual(pipe.metrics_snapshot()['counts']['alerts_raised'], 2)

    def test_malformed_outcome_counts_nothing_and_is_not_an_error(self):
        pipe, live, _, _ = make()
        live.start('c')
        live.result = lambda *a, **k: types.SimpleNamespace(applied=True)  # no envelopes
        pipe.offer('c', 'caller', 0, window(0.9), 0.0)
        pipe.schedule(0.0)
        self.assertEqual(pipe.step(0.125), 1)
        self.assertEqual((pipe.counts['alerts_raised'], pipe.counts['result_errors']), (0, 0))


class RenderTests(unittest.TestCase):
    def scored_pipe(self):
        pipe, live, _, _ = timed(Timer(0.0, 0.25, 0.25 + TICK))
        live.start('secret-call')
        pipe.offer('secret-call', 'caller', 0, window(0.4), 10.0)
        pipe.schedule(10.5)
        pipe.step(10.625)
        return pipe

    def test_stage_histogram_and_alert_counter_render(self):
        pipe = self.scored_pipe()
        snap = pipe.metrics_snapshot()
        text = '\n'.join(metrics_text.pipeline_lines(snap, 0.0))
        self.assertNotIn('secret-call', text)
        samples, declared = parse_metrics(text)
        self.assertEqual(declared['ava_live_stage_seconds'], 'histogram')
        self.assertEqual(declared['ava_live_alerts_raised_total'], 'counter')
        self.assertEqual(declared['ava_live_stage_timer_failures_total'], 'counter')
        for name in STAGES:
            series = snap['stage_seconds']['series'][name]
            rendered = [v for k, v in samples.items() if k.startswith(f'ava_live_stage_seconds_bucket{{stage="{name}"')]
            self.assertEqual(rendered, series['buckets'] + [series['count']], name)
            self.assertEqual(samples[f'ava_live_stage_seconds_count{{stage="{name}"}}'], 1)
        self.assertEqual(samples['ava_live_stage_seconds_sum{stage="window_to_result"}'], 0.625 + 0.25 + TICK)
        self.assertEqual(samples['ava_live_stage_seconds_bucket{stage="queue_wait",le="0.5"}'], 1)
        self.assertEqual(samples['ava_live_stage_seconds_bucket{stage="queue_wait",le="0.25"}'], 0)

    def test_malformed_stage_histogram_is_refused(self):
        pipe = self.scored_pipe()
        mutations = (lambda h: h['series'].pop('apply'), lambda h: h['series'].update(extra=h['series']['apply']),
                     lambda h: h['bounds'].reverse(), lambda h: h['series']['apply'].update(count=0),
                     lambda h: h['series']['apply']['buckets'].pop(), lambda h: h['series']['apply'].update(sum=-1.0))
        for i, mutate in enumerate(mutations):
            with self.subTest(mutation=i):
                snap = pipe.metrics_snapshot()
                mutate(snap['stage_seconds'])
                with self.assertRaises(ValueError):
                    metrics_text.pipeline_lines(snap, 0.0)
        snap = pipe.metrics_snapshot()
        del snap['stage_seconds']
        with self.assertRaises(KeyError):
            metrics_text.pipeline_lines(snap, 0.0)


if __name__ == '__main__':
    unittest.main()
