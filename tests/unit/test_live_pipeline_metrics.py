"""FR-O6 live-pipeline metrics: window accounting, ID-free snapshot, thread-safe hand-over
and `/metrics` families (backlogs 7.2 and 8.2; PRD FR-O6, FR-I9-FR-I11, FR-A8).

Claude-authored test design, 14 November 2026. Real LiveCalls, CallGate, EventHub,
MicroBatcher and LivePipeline with the constant-window fake detector of
test_scorer_pipeline; the API cases need requirements-api.txt. Infrastructure evidence
only (no telephony, no model).
"""
import threading
import types
import unittest

from services.inference import analysis
from services.scorer.pipeline import PipelineStats
from tests.unit import test_analysis_api as api_fixture
from tests.unit.test_api_readiness_metrics import parse_metrics
from tests.unit.test_scorer_pipeline import events, make, window

TestClient = api_fixture.TestClient
if TestClient is not None:
    from services.api.app import PIPELINE_INTAKE, PIPELINE_RESOLVED, create_app

RESOLVED = ('dropped_pending', 'cancelled_pending', 'skipped_ended', 'scored', 'windows_lost_to_detector',
            'result_errors', 'result_not_applied')


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


class AccountingTests(unittest.TestCase):
    def assert_conserved(self, pipe, offered):
        snap = pipe.metrics_snapshot()
        c = snap['counts']
        self.assertEqual(c['accepted'], sum(c[k] for k in RESOLVED) + snap['waiting'] + snap['in_batcher'], c)
        self.assertEqual(offered, c['accepted'] + c['stride_skipped'] + c['offered_inactive'], c)
        return snap

    def test_every_accepted_window_is_accounted_for_exactly_once(self):
        pipe, live, detector, sub = make(max_pending=2)
        offered = 0

        def offer(call_id, direction, index, value, now):
            nonlocal offered
            pipe.offer(call_id, direction, index, window(value), now)
            offered += 1
            self.assert_conserved(pipe, offered)

        for call_id in ('secret-call-a', 'secret-call-b', 'secret-call-c'):
            live.start(call_id)
        now = 0.0
        for hop in range(12):  # call a keeps up; b falls behind (no schedule for it); c mixes
            now = 2.0 * hop
            offer('secret-call-a', 'caller', hop, 0.2, now)
            offer('secret-call-b', 'caller', hop, 0.6, now)
            if hop % 3 == 0:
                offer('secret-call-c', 'caller', hop, 0.4, now)
            if hop == 5:
                detector.fail = 'raise'
            if hop == 6:
                detector.fail = None
            pipe.schedule(now)
            self.assert_conserved(pipe, offered)
            pipe.step(now + 0.1)
            self.assert_conserved(pipe, offered)
            if hop == 8:  # end c while one of its windows may be in the batcher
                pipe.end_call('secret-call-c', 'bye', speech_seconds={'caller': 1.0})
        offer('secret-call-c', 'caller', 99, 0.4, now + 0.2)  # inactive now
        for i in range(20):  # inference stalls for b: stride grows, the queue drops its oldest
            now += 2.0
            offer('secret-call-b', 'caller', 100 + i, 0.6, now)
        live.end_all('monitor_lost', speech_seconds_for=lambda call_id, directions: 0.0)  # cleaned at schedule
        pipe.schedule(now + 1.0)
        pipe.step(now + 1.2)
        snap = self.assert_conserved(pipe, offered)
        c = snap['counts']
        for key in ('scored', 'dropped_pending', 'stride_skipped', 'windows_lost_to_detector', 'offered_inactive',
                    'cancelled_pending', 'unmonitored_entered'):
            self.assertGreater(c[key], 0, key)
        self.assertEqual((snap['waiting'], snap['in_batcher'], snap['directions']), (0, 0, 0))
        self.assertNotIn('secret-call', repr(snap), 'no call IDs in the metrics snapshot')
        self.assertEqual(c['detector_failures'], 1)

    def test_conservation_on_end_reuse_detector_and_result_fault_paths(self):
        # Review M2: paths the scenario above does not reach.
        pipe, live, detector, sub = make()
        offered = 0

        def offer(call_id, index, now):
            nonlocal offered
            pipe.offer(call_id, 'caller', index, window(0.5), now)
            offered += 1

        live.start('a')
        offer('a', 0, 0.0)
        pipe.schedule(0.0)
        pipe.end_call('a', 'bye', speech_seconds={'caller': 1.0})  # window in the batcher
        self.assertEqual(self.assert_conserved(pipe, offered)['in_batcher'], 1)
        pipe.step(0.1)
        self.assertEqual(self.assert_conserved(pipe, offered)['counts']['skipped_ended'], 1)
        # Reused ID after end_all, offered before any schedule (review L5): the earlier
        # call's batched and waiting windows never reach the new call.
        live.start('b')
        offer('b', 0, 1.0)
        pipe.schedule(1.0)
        offer('b', 1, 1.01)  # waits behind the batched one
        live.end_all('monitor_lost', speech_seconds_for=lambda call_id, directions: 0.0)
        live.start('b')
        pipe.offer('b', 'caller', 0, window(0.9), 1.1)  # the new call starts its indexes again
        offered += 1
        snap = self.assert_conserved(pipe, offered)
        self.assertEqual((snap['counts']['cancelled_pending'], snap['waiting']), (1, 1))
        events(sub)
        pipe.schedule(1.1)  # the new window joins the old batched item in one batch
        self.assertEqual(pipe.step(1.2), 1)
        snap = self.assert_conserved(pipe, offered)
        self.assertEqual((snap['counts']['skipped_ended'], snap['counts']['scored']), (2, 1))
        scored = [e for e in events(sub) if e.type == 'score.updated']
        self.assertEqual([(e.call_id, round(e.data['score'], 6)) for e in scored], [('b', 0.9)],
                         'only the new call window (0.9) is scored, never the old 0.5 ones')
        self.assert_conserved(pipe, offered)
        # Short detector output loses the batch; a failing result is counted per window.
        for index, fail in ((1, 'short'), (2, None)):
            detector.fail = fail
            offer('b', index, 2.0 * index + 2)
            pipe.schedule(2.0 * index + 2)
            if fail is None:
                real = live.result
                live.result = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('publish fault'))
            pipe.step(2.0 * index + 2.1)
            if fail is None:
                live.result = real
            self.assert_conserved(pipe, offered)
        c = pipe.metrics_snapshot()['counts']
        self.assertEqual((c['windows_lost_to_detector'], c['result_errors']), (1, 1))

    def test_batcher_clock_error_keeps_the_window_waiting(self):
        # Review M1: submit fails after take(); the window must not vanish.
        pipe, live, detector, sub = make()
        live.start('c')
        pipe.offer('c', 'caller', 0, window(0.5), 0.0)
        pipe.batcher.set_live_calls_active(True, now=50.0)  # outside caller moves its clock on
        with self.assertRaises(ValueError):
            pipe.schedule(10.0)
        snap = self.assert_conserved(pipe, 1)
        self.assertEqual((snap['waiting'], snap['in_batcher']), (1, 0))
        self.assertEqual(pipe.schedule(50.0), 1, 'the kept window is scheduled once the clock agrees')
        self.assertEqual(pipe.step(50.1), 1)
        self.assert_conserved(pipe, 1)

    def test_odd_result_and_status_faults_are_contained(self):
        # Review L1/L2: a result without `applied` and a failing unmonitored publication.
        pipe, live, detector, sub = make(max_pending=2)
        live.start('c')
        live.result = lambda *a, **k: object()
        pipe.offer('c', 'caller', 0, window(0.5), 0.0)
        pipe.schedule(0.0)
        self.assertEqual(pipe.step(0.1), 0)
        self.assertEqual(pipe.counts['result_errors'], 1)
        live.status = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('hub down'))
        offered = 1
        for i in range(1, 40):  # never scheduled: reaches the stride cap
            self.assertIsNotNone(pipe.offer('c', 'caller', i, window(0.5), 2.0 * i))
            offered += 1
        self.assertEqual((pipe.counts['unmonitored_entered'], pipe.counts['status_publish_failures']), (1, 1))
        self.assert_conserved(pipe, offered)

    def test_results_not_applied_are_counted(self):
        pipe, live, detector, sub = make()
        live.start('c')
        real = live.result
        live.result = lambda *a, **k: types.SimpleNamespace(applied=False)
        pipe.offer('c', 'caller', 0, window(0.5), 0.0)
        pipe.schedule(0.0)
        self.assertEqual(pipe.step(0.1), 0)
        self.assertEqual((pipe.counts['result_not_applied'], pipe.counts['scored']), (1, 0))
        live.result = real
        self.assertEqual(self.assert_conserved(pipe, 1)['counts']['accepted'], 1)

    def test_gauges_follow_degraded_and_unmonitored_directions(self):
        pipe, live, detector, sub = make(max_pending=2)
        for call_id in ('a', 'b'):
            live.start(call_id)
        for i in range(40):  # 'a' never scheduled: reaches the stride cap
            pipe.offer('a', 'caller', i, window(0.5), 2.0 * i)
        pipe.offer('b', 'caller', 0, window(0.5), 80.0)
        snap = pipe.metrics_snapshot()
        self.assertEqual((snap['directions'], snap['degraded'], snap['unmonitored']), (2, 1, 1))
        self.assertEqual(snap['max_effective_stride_s'], 8.0)
        self.assertEqual(snap['counts']['unmonitored_entered'], 1)
        self.assertEqual(make()[0].metrics_snapshot()['max_effective_stride_s'], 0.0)


class InferenceTimingTests(unittest.TestCase):
    """Claude-authored, 14 Nov 2026: FR-O6 live detector-call histogram."""

    def make_timed(self, durations):
        """Timer returning (0.0, d) pairs, so each measured duration is exactly d
        (review: offsets like 1000 + d round below d and hid an off-by-one)."""
        from services.scorer.pipeline import LivePipeline
        reads = iter([value for d in durations for value in (0.0, d)])
        pipe, live, detector, sub = make()
        timed = LivePipeline(live, pipe.batcher, detector, pipe.config, timer=lambda: next(reads))
        return timed, live, detector

    def run_batches(self, pipe, live, detector, fails):
        live.start('c')
        for i, fail in enumerate(fails):
            detector.fail = fail
            pipe.offer('c', 'caller', i, window(0.5), 2.0 * i)
            pipe.schedule(2.0 * i)
            pipe.step(2.0 * i + 0.1)

    def test_buckets_are_cumulative_inclusive_and_split_by_outcome(self):
        from services.scorer.pipeline import INFERENCE_BUCKETS_S
        durations = [0.005, 0.0051, 0.3, 45.0, 0.05]
        pipe, live, detector = self.make_timed(durations)
        self.run_batches(pipe, live, detector, [None, None, None, None, 'raise'])
        hist = pipe.metrics_snapshot()['inference_seconds']
        self.assertEqual(hist['bounds'], list(INFERENCE_BUCKETS_S))
        ok, failed = hist['series']['ok'], hist['series']['failed']
        expected = [sum(d <= b for d in durations[:4]) for b in INFERENCE_BUCKETS_S]
        self.assertEqual(ok['buckets'], expected)
        self.assertEqual(ok['buckets'][0], 1, 'the 0.005 bound is inclusive')
        self.assertEqual((ok['count'], round(ok['sum'], 9)), (4, round(sum(durations[:4]), 9)))
        self.assertEqual((failed['count'], failed['buckets'][3]), (1, 1))  # 0.05 <= 0.05
        self.assertEqual(pipe.counts['timer_failures'], 0)

    def test_broken_or_backwards_timer_is_counted_and_never_stops_scoring(self):
        for timer in (lambda: (_ for _ in ()).throw(OSError('no clock')), lambda: float('nan'),
                      iter([5.0, 4.0]).__next__, lambda: True, lambda: 10 ** 400):
            pipe, live, detector, sub = make()
            pipe._timer = timer
            live.start('c')
            pipe.offer('c', 'caller', 0, window(0.5), 0.0)
            pipe.schedule(0.0)
            self.assertEqual(pipe.step(0.1), 1)
            self.assertEqual(pipe.counts['timer_failures'], 1)
            self.assertEqual(pipe.metrics_snapshot()['inference_seconds']['series']['ok']['count'], 0)
        with self.assertRaises(ValueError):
            type(pipe)(live, pipe.batcher, detector, pipe.config, timer=3)


class StatsBoxTests(unittest.TestCase):
    def test_publish_read_age_and_isolation(self):
        clock = Clock()
        stats = PipelineStats(clock)
        self.assertEqual(stats.read(), (None, None))
        pipe, live, _, _ = make()
        live.start('c')
        pipe.offer('c', 'caller', 0, window(0.5), 0.0)
        pipe.publish_metrics(stats)
        clock.now += 2.5
        snap, age = stats.read()
        self.assertEqual((snap['counts']['accepted'], age), (1, 2.5))
        snap['counts']['accepted'] = 99  # a reader's copy cannot change the box
        pipe.counts['accepted'] = 50  # nor can the owner after publishing
        self.assertEqual(stats.read()[0]['counts']['accepted'], 1)
        clock.now -= 10  # a clock oddity never gives a negative age
        self.assertEqual(stats.read()[1], 0.0)
        for bad in (None, {}, {'counts': 3}, []):
            with self.assertRaises(ValueError):
                stats.publish(bad)

    def test_publish_failure_is_counted_not_raised(self):
        pipe, _, _, _ = make()

        class Broken:
            def publish(self, snapshot):
                raise RuntimeError('reader gone')

        pipe.publish_metrics(Broken())
        self.assertEqual(pipe.counts['metrics_publish_failures'], 1)

    def test_concurrent_readers_see_whole_monotonic_snapshots(self):
        stats, errors, stop = PipelineStats(), [], threading.Event()

        def reader():
            last = -1
            while not stop.is_set():
                snap, _ = stats.read()
                if snap is None:
                    continue
                c = snap['counts']
                if c['accepted'] != c['scored'] or c['accepted'] < last:  # torn or reordered
                    errors.append(c)
                last = c['accepted']

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for n in range(3000):
            stats.publish({'counts': {'accepted': n, 'scored': n}})
        stop.set()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])


@unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
class ApiTests(unittest.TestCase):
    def client(self, stats):
        service = analysis.AnalysisService(lambda: (lambda samples: [0.0, 1.0]), model_id='fx')
        client = TestClient(create_app(service, pipeline_stats=stats))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def test_metrics_render_the_published_snapshot_without_call_ids(self):
        clock = Clock()
        stats = PipelineStats(clock)
        client = self.client(stats)
        samples, declared = parse_metrics(client.get('/metrics').text)
        self.assertEqual(samples['ava_live_pipeline_snapshot_valid'], 0.0)
        self.assertNotIn('ava_live_windows_offered_total', declared)
        pipe, live, detector, _ = make(max_pending=2)
        live.start('secret-call-x')
        for i in range(10):
            pipe.offer('secret-call-x', 'caller', i, window(0.7), 2.0 * i)
        pipe.schedule(20.0)
        pipe.step(20.1)
        pipe.publish_metrics(stats)
        clock.now += 1.5
        text = client.get('/metrics').text
        self.assertNotIn('secret-call', text)
        samples, declared = parse_metrics(text)
        snap = pipe.metrics_snapshot()
        c = snap['counts']
        for label, key in PIPELINE_INTAKE:
            self.assertEqual(samples[f'ava_live_windows_offered_total{{outcome="{label}"}}'], c[key], label)
        for label, key in PIPELINE_RESOLVED:
            self.assertEqual(samples[f'ava_live_windows_resolved_total{{outcome="{label}"}}'], c[key], label)
        resolved = sum(samples[f'ava_live_windows_resolved_total{{outcome="{label}"}}'] for label, _ in PIPELINE_RESOLVED)
        self.assertEqual(samples['ava_live_windows_offered_total{outcome="accepted"}'],
                         resolved + samples['ava_live_windows_waiting'] + samples['ava_live_windows_in_batcher'])
        self.assertGreater(samples['ava_live_windows_offered_total{outcome="stride_skipped"}'], 0)
        self.assertEqual(samples['ava_live_batches_total'], c['batches'])
        self.assertEqual((samples['ava_live_directions'], samples['ava_live_directions_degraded']), (1.0, 1.0))
        self.assertEqual(samples['ava_live_pipeline_snapshot_age_seconds'], 1.5)
        self.assertEqual(samples['ava_live_pipeline_snapshot_valid'], 1.0)
        self.assertEqual((declared['ava_live_windows_resolved_total'], declared['ava_live_windows_waiting'],
                          declared['ava_live_inference_seconds']), ('counter', 'gauge', 'histogram'))
        hist = snap['inference_seconds']['series']['ok']
        rendered = [v for k, v in samples.items() if k.startswith('ava_live_inference_seconds_bucket{outcome="ok"')]
        self.assertEqual(rendered, hist['buckets'] + [hist['count']])
        self.assertEqual(rendered, sorted(rendered))
        self.assertEqual(samples['ava_live_inference_seconds_count{outcome="ok"}'], c['batches'])
        self.assertGreater(c['batches'], 0)

    def test_malformed_snapshot_does_not_break_metrics(self):
        stats = PipelineStats()
        client = self.client(stats)
        pipe = make()[0]
        infinite = pipe.metrics_snapshot()
        infinite['counts']['scored'] = float('inf')
        bad_hist = []
        for mutate in (lambda h: h['bounds'].reverse(), lambda h: h['series'].pop('failed'),
                       lambda h: h['series']['ok']['buckets'].append(0),
                       lambda h: h['series']['ok'].update(buckets=[1] + [0] * (len(h['bounds']) - 1), count=1),
                       lambda h: h['series']['ok'].update(count=-1), lambda h: h['bounds'].__setitem__(0, 'x'),
                       lambda h: h['series']['ok'].update(buckets=[2] * len(h['bounds']), count=1),
                       lambda h: h['series']['ok'].update(sum=float('nan')),
                       lambda h: h['bounds'].__setitem__(0, True),
                       lambda h: h['bounds'].__setitem__(1, h['bounds'][0])):
            snapshot = pipe.metrics_snapshot()
            mutate(snapshot['inference_seconds'])
            bad_hist.append(snapshot)
        for bad in ({'counts': {}}, {'counts': {'accepted': -1}}, infinite, *bad_hist):
            stats.publish(bad)
            response = client.get('/metrics')
            self.assertEqual(response.status_code, 200)
            samples, declared = parse_metrics(response.text)
            self.assertEqual(samples['ava_live_pipeline_snapshot_valid'], 0.0)
            self.assertNotIn('ava_live_windows_offered_total', declared)
            self.assertIn('ava_jobs', declared)
        stats._clock = lambda: (_ for _ in ()).throw(RuntimeError('clock fault'))  # review L3
        response = client.get('/metrics')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(parse_metrics(response.text)[0]['ava_live_pipeline_snapshot_valid'], 0.0)

    def test_no_pipeline_families_without_stats(self):
        service = analysis.AnalysisService(lambda: (lambda samples: [0.0, 1.0]), model_id='fx')
        with TestClient(create_app(service)) as client:
            self.assertNotIn('ava_live_pipeline', client.get('/metrics').text)


if __name__ == '__main__':
    unittest.main()
