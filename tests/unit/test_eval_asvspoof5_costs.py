"""ASVspoof 5 Track 1 minDCF costs (backlog 9.3; PRD FR-M5).

Claude-authored test cases, 14 November 2026. The constants were read from the
ASVspoof 5 Evaluation Plan v0.6 (Table 1) on that date; expected values were worked
out by hand with the plan's form DCF = beta * P_miss + P_fa, beta = 1.9, where P_miss
is bona fide rejected (AVA's FAR) and P_fa is spoof accepted (AVA's FRR).
"""
import unittest

from models.evaluation import metrics, report


class Asvspoof5CostTests(unittest.TestCase):
    def test_values_match_the_evaluation_plan(self):
        self.assertEqual(metrics.asvspoof5_track1_costs(), {'c_miss': 1, 'c_fa': 10, 'pi_spoof': 0.05})
        source = metrics.ASVSPOOF5_TRACK1_SOURCE
        for fragment in ('v0.6', 'Table 1', 'Track 1', 'C_miss=1', 'C_fa=10', 'pi_spf=0.05',
                         'asvspoof.org', '2026-11-14'):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, source)

    def test_each_call_returns_a_new_dict(self):
        first = metrics.asvspoof5_track1_costs()
        first['c_fa'] = 1
        self.assertEqual(metrics.asvspoof5_track1_costs()['c_fa'], 10)

    def test_beta_form_known_answer(self):
        # genuine .1 .4 .6, synthetic .3 .7 .9; thresholds .1 .3 .4 .6 .7 .9 inf give
        # FAR 1, 2/3, 2/3, 1/3, 0, 0, 0 and FRR 0, 0, 1/3, 1/3, 1/3, 2/3, 1;
        # 1.9 FAR + FRR = 1.9, 1.267, 1.6, 0.967, 0.333, 0.667, 1 -> minimum 1/3 at 0.7.
        costs = metrics.asvspoof5_track1_costs()
        self.assertAlmostEqual(metrics.min_dcf([0.1, 0.4, 0.6], [0.3, 0.7, 0.9], **costs), 1 / 3)
        # The plan's beta and the module's normalizer agree for these costs.
        beta = costs['c_miss'] / costs['c_fa'] * (1 - costs['pi_spoof']) / costs['pi_spoof']
        self.assertAlmostEqual(beta, 1.9)
        _, far, frr = metrics.error_rates([0.1, 0.4, 0.6], [0.3, 0.7, 0.9])
        self.assertAlmostEqual(min(beta * a + b for a, b in zip(far, frr)), 1 / 3)

    def test_bounds_with_plan_costs(self):
        costs = metrics.asvspoof5_track1_costs()
        # Accept-everything costs P_fa = 1, the cheaper trivial system; minDCF never exceeds it.
        self.assertAlmostEqual(metrics.min_dcf([0.9, 0.8], [0.1, 0.2], **costs), 1.0)
        self.assertEqual(metrics.min_dcf([0.1, 0.2], [0.8, 0.9], **costs), 0.0)

    def test_report_records_costs_and_source(self):
        rows = [{'label': label, 'logit': z, 'temperature': 1.0, 'p_synthetic': report._sigmoid(z),
                 'codec': 'g711_ulaw', 'provenance': {'language': 'ur'}}
                # Logits chosen so p_synthetic orders like genuine .1 .4 .6, synthetic .3 .7 .9
                # (the known-answer case above): minDCF 1/3 only with the plan's costs.
                for label, z in [('genuine', -2.2), ('genuine', -0.4), ('genuine', 0.4),
                                 ('synthetic', -0.8), ('synthetic', 0.8), ('synthetic', 2.2)]]
        built = report.build_report(rows, costs=metrics.asvspoof5_track1_costs(),
                                    costs_status=metrics.ASVSPOOF5_TRACK1_SOURCE,
                                    temperature_status='uncalibrated', model_id='test-model',
                                    windows_summary={'report_sha256': '0' * 64},
                                    min_clips_per_class=1, n_resamples=100)
        self.assertEqual(built['costs'], {'c_miss': 1, 'c_fa': 10, 'pi_spoof': 0.05})
        self.assertEqual(built['costs_status'], metrics.ASVSPOOF5_TRACK1_SOURCE)
        self.assertAlmostEqual(built['subsets'][0]['min_dcf'], 1 / 3)
        # Other costs give a different, hand-worked value, so the report really used the
        # costs it was given: c_miss=1, c_fa=10, pi=0.5 -> FAR + 10 FRR, minimum 2/3 at .3.
        other = report.build_report(rows, costs={'c_miss': 1, 'c_fa': 10, 'pi_spoof': 0.5},
                                    costs_status='other costs', temperature_status='uncalibrated',
                                    model_id='test-model', windows_summary={'report_sha256': '0' * 64},
                                    min_clips_per_class=1, n_resamples=100)
        self.assertAlmostEqual(other['subsets'][0]['min_dcf'], 2 / 3)


if __name__ == '__main__':
    unittest.main()
