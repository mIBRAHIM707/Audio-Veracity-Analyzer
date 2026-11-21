"""ONNX benchmark mechanics with fake sessions and a stepping clock (backlog 8.QA).

Claude-authored test cases, 14 November 2026. No onnxruntime: the session factory and
clock are injected, so the timings and statistics below are exact by construction.
"""
import unittest
from types import SimpleNamespace

from models.export import benchmark_onnx as bo

GOOD_INPUTS = [SimpleNamespace(name='waveform', shape=['batch', 64000], type='tensor(float)')]
GOOD_OUTPUTS = [SimpleNamespace(name='logits', shape=['batch', 2], type='tensor(float)'),
                SimpleNamespace(name='embedding', shape=['batch', 256], type='tensor(float)')]


class Clock:
    """Time advances by whatever the last fake operation took, applied at the next read,
    so a span read around one operation equals that operation's scripted duration."""

    def __init__(self):
        self.now, self.next_duration = 0.0, 0.0

    def __call__(self):
        self.now += self.next_duration
        self.next_duration = 0.0
        return self.now


class FakeSession:
    def __init__(self, clock, durations, calls, inputs=GOOD_INPUTS):
        self.clock, self.durations, self.calls, self.inputs = clock, durations, calls, inputs

    def get_inputs(self):
        return self.inputs

    def get_outputs(self):
        return GOOD_OUTPUTS

    def run(self, names, feeds):
        window = feeds['waveform']
        self.calls.append(window.shape)
        self.clock.next_duration = self.durations.pop(0)
        return None


def _factory(clock, durations_by_label, opened, inputs=GOOD_INPUTS):
    calls = []

    def open_session(path, *, threads, expected_sha256):
        opened.append((path, threads, expected_sha256))
        clock.next_duration = 0.5  # session creation, must not enter the call timings
        label = path.split('/')[-1]
        return FakeSession(clock, durations_by_label[label], calls, inputs)

    return open_session, calls


MODELS = {'fp32': {'path': 'x/fp32', 'sha256': 'a' * 64}, 'int8': {'path': 'x/int8', 'sha256': 'b' * 64}}


class BenchmarkOnnxTests(unittest.TestCase):
    def test_exact_statistics_and_rtf(self):
        clock, opened = Clock(), []
        # Per model: 1 warm-up (untimed) then 3 timed calls.
        durations = {'fp32': [9.0, 1.0, 3.0, 2.0], 'int8': [9.0, 0.4, 0.2, 0.6]}
        open_session, calls = _factory(clock, durations, opened)
        report = bo.benchmark(MODELS, threads=3, warmup=1, repeats=3, clock=clock, open_session=open_session)
        rows = {row['label']: row for row in report['rows']}
        self.assertEqual(rows['fp32']['timings_seconds'], [1.0, 3.0, 2.0])
        self.assertEqual(rows['fp32']['median_seconds_per_call'], 2.0)
        self.assertEqual(rows['fp32']['max_seconds_per_call'], 3.0)
        self.assertAlmostEqual(rows['fp32']['median_rtf'], 2.0 / 4.0)
        self.assertAlmostEqual(rows['int8']['median_seconds_per_call'], 0.4)
        self.assertEqual(rows['int8']['session_open_seconds'], [0.5])
        self.assertIn('p95_status', rows['fp32'])
        self.assertNotIn('p95_seconds_per_call', rows['fp32'])
        self.assertEqual(sorted(opened), [('x/fp32', 3, 'a' * 64), ('x/int8', 3, 'b' * 64)])
        self.assertEqual(calls, [(1, 64000)] * 8)
        self.assertEqual(list(rows), ['fp32', 'int8'])  # rows keep the given order

    def test_p95_and_batch_throughput(self):
        clock, opened = Clock(), []
        durations = {'fp32': [float(i) for i in range(1, 21)]}
        open_session, _ = _factory(clock, durations, opened)
        report = bo.benchmark({'fp32': MODELS['fp32']}, batch=2, warmup=0, repeats=20, clock=clock,
                              open_session=open_session)
        row = report['rows'][0]
        self.assertAlmostEqual(row['p95_seconds_per_call'], 19.05)  # 1..20, linear interpolation
        self.assertNotIn('median_rtf', row)  # RTF only at batch 1
        self.assertAlmostEqual(row['median_throughput_seconds_per_audio_second'], 10.5 / 8.0)

    def test_rounds_interleave_with_a_seeded_order(self):
        clock, opened = Clock(), []
        durations = {'fp32': [1.0] * 6, 'int8': [1.0] * 6}
        open_session, _ = _factory(clock, durations, opened)
        report = bo.benchmark(MODELS, warmup=0, repeats=2, rounds=3, seed=5, clock=clock, open_session=open_session)
        self.assertEqual(len(report['round_orders']), 3)
        for order in report['round_orders']:
            self.assertEqual(sorted(order), ['fp32', 'int8'])
        self.assertEqual([path for path, _, _ in opened],
                         ['x/' + label for order in report['round_orders'] for label in order])
        self.assertEqual(report['rows'][0]['round_medians_seconds'], [1.0, 1.0, 1.0])

    def test_contract_violation_and_bad_clock(self):
        bad_inputs = [SimpleNamespace(name='audio', shape=['batch', 64000], type='tensor(float)')]
        open_session, _ = _factory(Clock(), {'fp32': [1.0] * 4}, [], inputs=bad_inputs)
        with self.assertRaisesRegex(ValueError, 'I/O contract'):
            bo.benchmark({'fp32': MODELS['fp32']}, warmup=0, repeats=2, clock=Clock(), open_session=open_session)
        clock = Clock()
        open_session, _ = _factory(clock, {'fp32': [1.0, 0.0]}, [])
        with self.assertRaisesRegex(RuntimeError, 'non-positive'):
            bo.benchmark({'fp32': MODELS['fp32']}, warmup=0, repeats=2, clock=clock, open_session=open_session)

    def test_prd_targets_and_percentile_pinned(self):
        # Copies of models/ava/benchmark_cpu.py (which needs torch); pinned to PRD §6.1.
        self.assertEqual(bo.PRD_TARGETS['p95_latency_seconds'], 1.5)
        self.assertEqual(bo.PRD_TARGETS['rtf'], 0.375)
        self.assertIn('INT8 ONNX, 4 threads, batch 1', bo.PRD_TARGETS['conditions'])
        self.assertEqual(bo.MIN_TIMINGS_FOR_P95, 20)
        self.assertEqual(bo._percentile([4.0, 1.0, 3.0, 2.0], 0.5), 2.5)
        self.assertEqual(bo._percentile([1.0], 0.95), 1.0)

    def test_environment_and_limitations(self):
        clock = Clock()
        open_session, _ = _factory(clock, {'fp32': [1.0, 1.0]}, [])
        report = bo.benchmark({'fp32': MODELS['fp32']}, warmup=0, repeats=2, clock=clock, open_session=open_session)
        self.assertIsInstance(report['environment']['logical_cpus'], int)
        self.assertIn('not measured', report['limitations'])

    def test_argument_refusals(self):
        for models in ({}, [], {'': MODELS['fp32']}, {'m': {'path': 'p'}}, {'m': {'path': 'p', 'sha256': 's', 'x': 1}}):
            with self.subTest(models=models):
                with self.assertRaises(ValueError):
                    bo.benchmark(models, open_session=None)
        for kwargs in ({'threads': 0}, {'batch': 0}, {'batch': 65}, {'warmup': -1}, {'repeats': 1},
                       {'rounds': 0}, {'seed': -1}, {'threads': True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    bo.benchmark(MODELS, open_session=None, **kwargs)


if __name__ == '__main__':
    unittest.main()
