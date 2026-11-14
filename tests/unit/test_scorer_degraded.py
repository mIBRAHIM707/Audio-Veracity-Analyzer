"""Degraded-mode stride control (backlog 8.2 partial; PRD FR-I10, FR-I6; architecture
§2.4, §7.3).

Claude-authored test cases, 10 November 2026. Pure logic with an explicit clock; not
a measurement of real inference load.
"""
import unittest

from services.scorer.config import ScoringConfig
from services.scorer.degraded import StrideController

DEFAULT = ScoringConfig()  # hop 2 s, cap 4x, recovery 30 s


class StrideControllerTests(unittest.TestCase):
    def test_starts_normal(self):
        c = StrideController(DEFAULT)
        self.assertEqual(c.status(), {'degraded': False, 'effective_stride_s': 2.0, 'unmonitored': False})
        self.assertEqual(c.observe(0, behind=False), [])

    def test_stride_grows_one_hop_per_behind_report_up_to_the_cap(self):
        c = StrideController(DEFAULT)
        strides = []
        for t in range(4):
            c.observe(t, behind=True)
            strides.append((c.effective_stride_s, c.unmonitored))
        self.assertEqual(strides, [(4.0, False), (6.0, False), (8.0, False), (8.0, True)])
        self.assertTrue(c.degraded)
        self.assertEqual(c.observe(5, behind=True), [])  # stays capped and unmonitored

    def test_change_reports(self):
        c = StrideController(DEFAULT)
        self.assertEqual(c.observe(0, behind=True), [('effective_stride_s', 2.0, 4.0)])
        c.observe(1, behind=True)
        c.observe(2, behind=True)
        self.assertEqual(c.observe(3, behind=True), [('unmonitored', False, True)])
        c.observe(4, behind=False)
        self.assertEqual(c.observe(34, behind=False),
                         [('effective_stride_s', 8.0, 2.0), ('unmonitored', True, False)])

    def test_recovery_needs_thirty_seconds_continuously_under_budget(self):
        c = StrideController(DEFAULT)
        c.observe(0, behind=True)
        c.observe(10, behind=False)            # recovery clock starts at 10
        c.observe(39.999, behind=False)
        self.assertTrue(c.degraded)            # 29.999 s under budget is not enough
        c.observe(40, behind=False)
        self.assertEqual(c.status(), {'degraded': False, 'effective_stride_s': 2.0, 'unmonitored': False})

    def test_behind_report_restarts_recovery(self):
        c = StrideController(DEFAULT)
        c.observe(0, behind=True)
        c.observe(1, behind=False)
        c.observe(25, behind=True)             # stride 6 s, clock reset
        c.observe(26, behind=False)
        c.observe(50, behind=False)
        self.assertEqual(c.effective_stride_s, 6.0)
        c.observe(56, behind=False)
        self.assertFalse(c.degraded)

    def test_under_budget_while_normal_does_not_arm_recovery(self):
        c = StrideController(DEFAULT)
        c.observe(0, behind=False)
        c.observe(100, behind=True)            # degraded only now
        c.observe(101, behind=False)
        c.observe(130, behind=False)           # 29 s after the first under-budget report
        self.assertTrue(c.degraded)
        c.observe(131, behind=False)
        self.assertFalse(c.degraded)

    def test_configured_cap_and_recovery(self):
        config = ScoringConfig(hop_s=1.0, degraded_max_stride_factor=1, degraded_recovery_s=5.0)
        c = StrideController(config)
        self.assertEqual(c.observe(0, behind=True), [('unmonitored', False, True)])  # cap 1x: straight to unmonitored
        self.assertEqual(c.effective_stride_s, 1.0)
        c.observe(1, behind=False)
        c.observe(6, behind=False)
        self.assertFalse(c.unmonitored)

    def test_invalid_input_changes_nothing(self):
        c = StrideController(DEFAULT)
        c.observe(10, behind=True)
        before = c.status()
        for now, behind in ((9, False), (float('nan'), True), (float('inf'), True), ('11', True),
                            (True, True), (10 ** 400, True), (11, 1), (11, None)):
            with self.subTest(now=now, behind=behind):
                with self.assertRaises(ValueError):
                    c.observe(now, behind=behind)
                self.assertEqual(c.status(), before)
        # Behavioural proof that neither the clock nor the recovery start moved:
        c.observe(10, behind=False)            # equal timestamp accepted; recovery starts at 10
        c.observe(39.999, behind=False)
        self.assertTrue(c.degraded)
        c.observe(40, behind=False)
        self.assertFalse(c.degraded)
        with self.assertRaises(TypeError):
            StrideController({'hop_s': 2})

    def test_unvalidated_config_values_refused(self):
        for config in (ScoringConfig(degraded_max_stride_factor=0), ScoringConfig(degraded_max_stride_factor=2.0),
                       ScoringConfig(degraded_recovery_s=-1), ScoringConfig(degraded_recovery_s=0),
                       ScoringConfig(hop_s=0)):
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    StrideController(config)

    def test_behind_while_unmonitored_restarts_recovery(self):
        c = StrideController(DEFAULT)
        for t in range(4):
            c.observe(t, behind=True)
        c.observe(10, behind=False)
        c.observe(30, behind=True)             # still unmonitored, clock reset
        c.observe(32, behind=False)
        c.observe(61, behind=False)
        self.assertTrue(c.unmonitored)
        c.observe(62, behind=False)
        self.assertFalse(c.unmonitored)

    def test_recovery_from_unmonitored_at_capped_stride_spacing(self):
        c = StrideController(DEFAULT)
        for t in range(4):
            c.observe(t, behind=True)
        times = [8.0 * k for k in range(1, 6)]  # one report per capped 8 s stride
        states = []
        for t in times:
            c.observe(t, behind=False)
            states.append(c.unmonitored)
        self.assertEqual(states, [True, True, True, True, False])  # 8 -> 40 is 32 s >= 30 s

    def test_sparse_reports_recover_on_one_late_report(self):
        c = StrideController(DEFAULT)
        c.observe(0, behind=True)
        c.observe(10, behind=False)
        c.observe(1000, behind=False)           # nothing observed in between
        self.assertFalse(c.degraded)

    def test_fractional_hop_stride_is_rounded(self):
        c = StrideController(ScoringConfig(window_s=0.4, hop_s=0.1))
        c.observe(0, behind=True)
        c.observe(1, behind=True)
        self.assertEqual(c.effective_stride_s, 0.3)


if __name__ == '__main__':
    unittest.main()
