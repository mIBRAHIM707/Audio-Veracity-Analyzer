"""CPU benchmark harness mechanics (backlog 8.QA; PRD §6.1 definitions).

Claude-authored test design, 12 November 2026. Tiny random models and a fake clock;
these check the arithmetic, labelling and refusals, not the machine's speed.
"""
import contextlib
import io
import json
import unittest

import torch

from models.ava import benchmark_cpu as bench
from tests.model.test_ava_sls import TINY


class FakeClock:
    """Returns successive start/stop readings so each timed call lasts the next duration."""

    def __init__(self, durations):
        self.durations, self.now, self.started = list(durations), 0.0, False

    def __call__(self):
        if self.started:
            self.now += self.durations.pop(0)
        self.started = not self.started
        return self.now


class BenchmarkTests(unittest.TestCase):
    def test_rtf_follows_the_prd_definition_at_batch_one(self):
        durations = [1.0, 2.0, 3.0, 4.0, 10.0]
        row = bench.benchmark([1], repeats=5, warmup=0, base=TINY, clock=FakeClock(durations))['rows'][0]
        self.assertEqual(row['median_seconds_per_call'], 3.0)
        self.assertEqual(row['median_rtf'], 3.0 / 4.0)  # 4 s of audio at batch 1
        self.assertEqual((row['max_seconds_per_call'], row['timings_seconds']), (10.0, durations))
        self.assertNotIn('p95_seconds_per_call', row)
        self.assertNotIn('p95_rtf', row)
        self.assertIn('fewer than 20', row['p95_status'])

    def test_p95_only_with_twenty_timings_across_rounds(self):
        durations = [float(i) for i in range(1, 21)]
        report = bench.benchmark([1], repeats=10, rounds=2, warmup=0, base=TINY, clock=FakeClock(durations))
        row = report['rows'][0]
        self.assertEqual(row['timings'], 20)
        self.assertAlmostEqual(row['p95_seconds_per_call'], 19.0 + 0.05 * 1.0)
        self.assertAlmostEqual(row['p95_rtf'], row['p95_seconds_per_call'] / 4.0)
        self.assertEqual(row['round_medians_seconds'], [5.5, 15.5])

    def test_batch_above_one_is_labelled_throughput_not_rtf(self):
        row = bench.benchmark([1], repeats=2, warmup=0, batch=2, base=TINY, clock=FakeClock([2.0, 2.0]))['rows'][0]
        self.assertNotIn('median_rtf', row)
        self.assertEqual(row['median_throughput_seconds_per_audio_second'], 2.0 / 8.0)

    def test_rounds_interleave_cuts_in_seeded_orders(self):
        report = bench.benchmark([1, 2, 3], repeats=2, warmup=0, rounds=4, base=TINY)
        orders = report['round_orders']
        self.assertEqual(len(orders), 4)
        self.assertTrue(all(sorted(order) == [1, 2, 3] for order in orders))
        self.assertGreater(len({tuple(order) for order in orders}), 1)
        again = bench.benchmark([1, 2, 3], repeats=2, warmup=0, rounds=4, base=TINY)['round_orders']
        self.assertEqual(orders, again)
        for row in report['rows']:
            self.assertEqual((row['timings'], len(row['round_medians_seconds'])), (8, 4))

    def test_rows_memory_labels_and_restored_global_state(self):
        previous = torch.get_num_threads()
        torch.manual_seed(123)
        expected_next = torch.rand(1)
        torch.manual_seed(123)
        report = bench.benchmark([1, 3], threads=1, repeats=2, warmup=1, base=TINY)
        self.assertTrue(torch.equal(torch.rand(1), expected_next), 'global RNG state is restored')
        self.assertEqual(torch.get_num_threads(), previous)
        self.assertLess(report['rows'][0]['parameters'], report['rows'][1]['parameters'])
        for row in report['rows']:
            self.assertGreater(row['median_seconds_per_call'], 0)
            self.assertGreater(row['rss_before_build_bytes'], 0)
            self.assertGreaterEqual(row['sampled_peak_rss_bytes'], row['rss_before_build_bytes'] // 2)
        self.assertIn('random weights', report['settings']['runtime'])
        self.assertIn('no PRD target is met or missed', report['limitations'])
        self.assertIn('measured_at_utc', report['environment'])
        json.dumps(report, allow_nan=False)

    def test_refusals_restore_thread_count(self):
        previous = torch.get_num_threads()
        for kwargs in ({'threads': 0}, {'batch': 0}, {'repeats': 1}, {'warmup': -1}, {'seed': -1},
                       {'threads': True}, {'rounds': 0}):
            with self.assertRaises(ValueError):
                bench.benchmark([1], base=TINY, **kwargs)
        for cuts in ([], [0], [1, 1], (2.0,), 3):
            with self.assertRaises(ValueError):
                bench.benchmark(cuts, base=TINY)
        with self.assertRaises(RuntimeError):
            bench.benchmark([1], repeats=2, warmup=0, base=TINY, clock=FakeClock([0.0, 0.0]))
        self.assertEqual(torch.get_num_threads(), previous)

    def test_cli_reports_failures_without_traceback(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(bench.main(['--repeats', '1']), 1)
        self.assertIn('CPU benchmark failed', err.getvalue())


if __name__ == '__main__':
    unittest.main()
