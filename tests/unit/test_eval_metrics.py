"""Known-answer tests for evaluation metrics (backlog 9.3 partial; PRD FR-M5, FR-M6,
FR-M10).

Claude-authored test cases, 10 November 2026. Every expected value below was worked
out by hand from the listed scores; these test arithmetic, not any model.
"""
import math
import unittest

import numpy as np

from models.evaluation.metrics import (bootstrap_ci, dialect_far, dialect_reportable, ece, eer, ema_first,
                                       error_rates, evidence_curve, far_at_frr, fit_temperature,
                                       min_dcf, reliability_bins, subset_report)

# Three genuine and four synthetic scores; thresholds .1 .2 .4 .5 .6 .7 .8 inf give
# FAR 1, 2/3, 1/3, 1/3, 1/3, 0, 0, 0 and FRR 0, 0, 0, 1/4, 1/2, 1/2, 3/4, 1.
GENUINE = [0.1, 0.2, 0.6]
SYNTHETIC = [0.4, 0.5, 0.7, 0.8]
COSTS = dict(c_miss=1, c_fa=10, pi_spoof=0.05)  # ASVspoof 5 Track 1 values (see test_eval_asvspoof5_costs)


class CurveTests(unittest.TestCase):
    def test_error_rates_by_hand(self):
        thresholds, far, frr = error_rates(GENUINE, SYNTHETIC)
        np.testing.assert_allclose(thresholds, [0.1, 0.2, 0.4, 0.5, 0.6, 0.7, 0.8, np.inf])
        np.testing.assert_allclose(far, [1, 2 / 3, 1 / 3, 1 / 3, 1 / 3, 0, 0, 0])
        np.testing.assert_allclose(frr, [0, 0, 0, 0.25, 0.5, 0.5, 0.75, 1])

    def test_eer_known_answers(self):
        self.assertAlmostEqual(eer(GENUINE, SYNTHETIC), 1 / 3)       # interpolated crossing
        self.assertAlmostEqual(eer([0.1, 0.3], [0.2, 0.4]), 0.5)      # exact tie at 0.3
        self.assertEqual(eer([0.1, 0.2], [0.8, 0.9]), 0.0)            # perfect separation
        self.assertAlmostEqual(eer([0.9, 0.8], [0.1, 0.2]), 1.0)      # perfectly inverted
        self.assertAlmostEqual(eer([0.5] * 4, [0.5] * 4), 0.5)        # no information

    def test_far_at_frr(self):
        self.assertEqual(far_at_frr(GENUINE, SYNTHETIC, 0.25), (1 / 3, 0.5))
        self.assertEqual(far_at_frr(GENUINE, SYNTHETIC, 0.05), (1 / 3, 0.4))  # FR-M5 point
        self.assertEqual(far_at_frr(GENUINE, SYNTHETIC, 0.5), (0.0, 0.7))
        with self.assertRaises(ValueError):
            far_at_frr(GENUINE, SYNTHETIC, 1.5)

    def test_min_dcf_known_answers(self):
        # DCF = 0.95 FAR + 0.5 FRR, minimum 0.25 at threshold 0.7, normalized by 0.5.
        self.assertAlmostEqual(min_dcf(GENUINE, SYNTHETIC, **COSTS), 0.5)
        self.assertEqual(min_dcf([0.1, 0.2], [0.8, 0.9], **COSTS), 0.0)
        self.assertAlmostEqual(min_dcf([0.9, 0.8], [0.1, 0.2], **COSTS), 1.0)  # never worse than default
        for bad in (dict(COSTS, c_miss=0), dict(COSTS, c_fa=-1), dict(COSTS, pi_spoof=0),
                    dict(COSTS, pi_spoof=1), dict(COSTS, c_fa=True), dict(COSTS, c_miss=float('nan'))):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    min_dcf(GENUINE, SYNTHETIC, **bad)
        with self.assertRaises(TypeError):
            min_dcf(GENUINE, SYNTHETIC)  # costs must be given explicitly

    def test_insufficient_or_malformed_scores_refused(self):
        for genuine, synthetic in (([], [0.5]), ([0.5], []), ([float('nan')], [0.5]),
                                   ([[0.1, 0.2]], [0.5]), ([0.5], [float('inf')])):
            with self.subTest(genuine=genuine, synthetic=synthetic):
                with self.assertRaises(ValueError):
                    eer(genuine, synthetic)


class BootstrapTests(unittest.TestCase):
    def test_reproducible_and_brackets_estimate(self):
        rng = np.random.default_rng(1)
        g, s = rng.normal(0.3, 0.1, 400), rng.normal(0.6, 0.1, 400)
        first = bootstrap_ci(eer, g, s, seed=7, n_resamples=200)
        self.assertEqual(first, bootstrap_ci(eer, g, s, seed=7, n_resamples=200))
        self.assertNotEqual(first, bootstrap_ci(eer, g, s, seed=8, n_resamples=200))
        low, high = first
        self.assertLess(low, eer(g, s))
        self.assertGreater(high, eer(g, s))
        self.assertLess(high - low, 0.1)

    def test_perfect_separation_interval_is_zero(self):
        self.assertEqual(bootstrap_ci(eer, [0.1, 0.2, 0.3], [0.7, 0.8], seed=0, n_resamples=100), (0.0, 0.0))

    def test_arguments_validated(self):
        for kw in (dict(seed=-1), dict(seed=1.0), dict(seed=0, n_resamples=10),
                   dict(seed=0, confidence=1.0)):
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):
                    bootstrap_ci(eer, GENUINE, SYNTHETIC, **kw)


class CalibrationTests(unittest.TestCase):
    def test_ece_known_answers(self):
        self.assertAlmostEqual(ece([0.25] * 4, [1, 0, 0, 0]), 0.0)
        self.assertAlmostEqual(ece([0.9] * 3, [0, 0, 0]), 0.9)
        self.assertAlmostEqual(ece([0.1, 0.1, 0.9, 0.9], [0, 0, 1, 1]), 0.1)
        self.assertAlmostEqual(ece([1.0, 0.0], [1, 0]), 0.0)  # p = 1 falls in the last bin

    def test_ece_validation(self):
        for p, y in (([0.5], [2]), ([0.5, 0.5], [1]), ([1.5], [1]), ([0.5], ['1'])):
            with self.subTest(p=p, y=y):
                with self.assertRaises(ValueError):
                    ece(p, y)

    def test_temperature_recovers_known_scale(self):
        rng = np.random.default_rng(3)
        z = rng.normal(0, 3, 20000)
        y = (rng.random(z.size) < 1 / (1 + np.exp(-z / 2.0))).astype(float)  # true T = 2
        self.assertAlmostEqual(fit_temperature(z, y), 2.0, delta=0.15)
        calibrated = 1 / (1 + np.exp(-z / fit_temperature(z, y)))
        self.assertLess(ece(calibrated, y), ece(1 / (1 + np.exp(-z)), y))  # scaling helps

    def test_temperature_validation(self):
        with self.assertRaises(ValueError):
            fit_temperature([0.1, 0.2], [1])
        with self.assertRaises(ValueError):
            fit_temperature([0.1, -0.1], [1, 0], low=2.0, high=1.0)

    def test_temperature_refuses_degenerate_fits(self):
        z = np.linspace(-4, 4, 200)
        cases = {'single_class': (z, np.ones(200)),
                 'inverted': (z, (z < 0).astype(float)),        # sign cannot be fixed by T
                 'all_zero_logits': (np.zeros(200), (z > 0).astype(float)),
                 'string_labels': (z[:2], np.array(['1', '0'])),
                 'bool_labels': (z[:2], np.array([True, False]))}
        for name, (logits, labels) in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    fit_temperature(logits, labels)

    def test_numpy_integers_accepted(self):
        self.assertTrue(dialect_reportable(np.int64(10), np.int64(500)))
        self.assertEqual(len(bootstrap_ci(eer, GENUINE, SYNTHETIC, seed=np.int64(1), n_resamples=np.int32(100))), 2)
        self.assertAlmostEqual(ece([0.25] * 4, [1, 0, 0, 0], n_bins=np.int64(10)), 0.0)
        self.assertAlmostEqual(min_dcf(GENUINE, SYNTHETIC, c_miss=np.float64(1), c_fa=np.int64(10),
                                       pi_spoof=0.05), 0.5)
        for bad in (1.0, True):
            with self.subTest(target=bad):
                with self.assertRaises(ValueError):
                    far_at_frr(GENUINE, SYNTHETIC, bad)


class EvidenceCurveTests(unittest.TestCase):
    def test_ema_first_by_hand(self):
        self.assertEqual(ema_first([0.5], 1, 0.4), 0.5)
        self.assertAlmostEqual(ema_first([1.0, 0.0], 2, 0.4), 0.6)            # 0.4*0 + 0.6*1
        self.assertAlmostEqual(ema_first([1.0, 0.0, 0.5, 0.9], 3, 0.4), 0.56)  # 4th ignored

    def test_curve_counts_and_values(self):
        # Genuine clips start low then drift; synthetic clips start ambiguous then rise.
        genuine = [[0.6, 0.1, 0.1], [0.2, 0.2], [0.1]]
        synthetic = [[0.5, 0.9, 0.9], [0.4, 0.9]]
        rows = evidence_curve(genuine, synthetic, lengths=(6, 1, 2, 3, 2), cohort='available',
                              min_clips_per_class=1, codec='g711_ulaw', n_resamples=100)
        self.assertEqual([(r['windows'], r['genuine_clips'], r['synthetic_clips'], r['status']) for r in rows],
                         [(1, 3, 2, 'ok'), (2, 2, 2, 'ok'), (3, 1, 1, 'ok'), (6, 0, 0, 'insufficient data')])
        self.assertEqual({(r['cohort'], r['codec']) for r in rows}, {('available', 'g711_ulaw')})
        # n=1: genuine 0.6, 0.2, 0.1 vs synthetic 0.5, 0.4 -> FAR at FRR 5 % uses
        # threshold 0.4 (FRR 0): genuine >= 0.4 is 1 of 3.
        self.assertAlmostEqual(rows[0]['far_at_frr'], 1 / 3)
        # n=2: genuine EMA 0.4*0.1+0.6*0.6 = 0.40, 0.20; synthetic 0.66, 0.60 -> separated.
        self.assertEqual((rows[1]['eer'], rows[1]['far_at_frr']), (0.0, 0.0))
        self.assertEqual(rows[1]['eer_ci'], (0.0, 0.0))
        self.assertNotIn('eer', rows[3])

    def test_fixed_cohort_uses_the_same_clips_at_every_length(self):
        # Short clips are easy, long clips hard: 'available' mixes them, 'fixed' does not.
        genuine = [[0.1], [0.1], [0.5, 0.5, 0.5]]
        synthetic = [[0.9], [0.9], [0.45, 0.45, 0.45]]
        available = evidence_curve(genuine, synthetic, lengths=(1, 3), cohort='available',
                                   min_clips_per_class=1, n_resamples=100)
        fixed = evidence_curve(genuine, synthetic, lengths=(1, 3), min_clips_per_class=1, n_resamples=100)
        self.assertEqual([r['genuine_clips'] for r in available], [3, 1])
        self.assertEqual([r['genuine_clips'] for r in fixed], [1, 1])
        self.assertAlmostEqual(available[0]['eer'], 1 / 3)   # n=1 over all clips: by hand
        self.assertEqual(fixed[0]['eer'], 1.0)               # n=1 over the long pair only (inverted)

    def test_min_clips_rule_default_is_ten(self):
        rows = evidence_curve([[0.1]] * 9, [[0.9]] * 12, lengths=(1,), n_resamples=100)
        self.assertEqual((rows[0]['status'], rows[0]['genuine_clips']), ('insufficient data', 9))
        rows = evidence_curve([[0.1]] * 10, [[0.9]] * 10, lengths=(1,), n_resamples=100)
        self.assertEqual(rows[0]['status'], 'ok')

    def test_ema_matches_live_scorer(self):
        from services.scorer.config import ScoringConfig
        from services.scorer.session import DirectionSession
        scores = [0.9, 0.1, 0.7, 0.3, 0.95, 0.05]
        session = DirectionSession(ScoringConfig(), call_id='parity')
        live = [session.add_window(p)[0].data['ema'] for p in scores]
        self.assertEqual(live, [ema_first(scores, n, 0.4) for n in range(1, 7)])

    def test_ema_first_validation(self):
        for args in (([0.5], 0, 0.4), ([0.5], 2, 0.4), ([], 1, 0.4), ([0.5], 1, 0), ([0.5], 1, True)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    ema_first(*args)

    def test_curve_validation(self):
        for kw in (dict(alpha=0), dict(alpha=1.5), dict(alpha=True), dict(lengths=()), dict(lengths=(0,)),
                   dict(lengths=(1.0,)), dict(cohort='all'), dict(min_clips_per_class=0)):
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):
                    evidence_curve([[0.1]], [[0.9]], **kw)
        with self.assertRaises(ValueError):
            evidence_curve([[]], [[0.9]])   # an empty clip is malformed
        with self.assertRaises(ValueError):
            evidence_curve([[float('nan')]], [[0.9]])
        for bad in ([[1.5]], [[-0.2]]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    evidence_curve(bad, [[0.9]])


def records():
    """Urdu: GENUINE/SYNTHETIC scores (EER 1/3); Punjabi: separated; plus codec field."""
    rows = [dict(label='genuine', score=p, language='urd', codec='g711') for p in GENUINE]
    rows += [dict(label='synthetic', score=p, language='urd', codec='gsm') for p in SYNTHETIC]
    rows += [dict(label='genuine', score=0.1, language='pnb', codec='g711'),
             dict(label='synthetic', score=0.9, language='pnb')]  # no codec on this one
    return rows


class SubsetReportTests(unittest.TestCase):
    def test_rows_counts_and_values(self):
        rows = subset_report(records(), ['language', 'codec'], costs=COSTS, min_clips_per_class=1,
                             n_resamples=100)
        summary = [(r['field'], r['value'], r['genuine'], r['synthetic'], r['missing'], r['status'])
                   for r in rows]
        self.assertEqual(summary, [
            (None, None, 4, 5, 0, 'ok'),
            ('language', 'pnb', 1, 1, 0, 'ok'),
            ('language', 'urd', 3, 4, 0, 'ok'),
            ('codec', 'g711', 4, 0, 1, 'insufficient data'),
            ('codec', 'gsm', 0, 4, 1, 'insufficient data')])
        urdu = rows[2]
        self.assertAlmostEqual(urdu['eer'], 1 / 3)
        self.assertEqual((urdu['far_at_frr'], urdu['threshold']), (1 / 3, 0.4))
        self.assertAlmostEqual(urdu['min_dcf'], 0.5)
        self.assertEqual((rows[1]['eer'], rows[1]['min_dcf']), (0.0, 0.0))
        self.assertNotIn('eer', rows[3])
        self.assertEqual(len(urdu['min_dcf_ci']), 2)  # FR-M5: interval on every metric
        self.assertEqual((rows[3]['missing_genuine'], rows[3]['missing_synthetic']), (0, 1))
        again = subset_report(records(), ['language', 'codec'], costs=COSTS, min_clips_per_class=1,
                              n_resamples=100)
        self.assertEqual(again, rows)  # seeded intervals reproduce

    def test_field_with_no_values_is_reported(self):
        rows = subset_report(records(), ['code_switched'], costs=COSTS, min_clips_per_class=1,
                             n_resamples=100)
        self.assertEqual((rows[1]['field'], rows[1]['status'], rows[1]['missing']),
                         ('code_switched', 'no values', 9))

    def test_minimum_clips_default(self):
        rows = subset_report(records(), ['language'], costs=COSTS, n_resamples=100)
        self.assertEqual({r['status'] for r in rows}, {'insufficient data'})  # nothing reaches 10

    def test_validation(self):
        bad_records = ([dict(label='real', score=0.5)], [dict(label='genuine', score=1.5)],
                       [dict(label='genuine', score=True)], [dict(label='genuine')], ['row'])
        for bad in bad_records:
            with self.subTest(records=bad):
                with self.assertRaises(ValueError):
                    subset_report(bad, [], costs=COSTS)
        for kw in (dict(costs={}), dict(costs=dict(COSTS, pi_spoof=2)), dict(costs=[1, 10, 0.05]),
                   dict(costs=dict(COSTS, extra=1)), dict(costs=COSTS, seed=-1),
                   dict(costs=COSTS, n_resamples=5)):
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):  # refused even though every row is insufficient
                    subset_report(records(), ['language'], **kw)
        for fields in ('language', ['language', 'language'], ['score'], ['label'], ['']):
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError):
                    subset_report(records(), fields, costs=COSTS)
        for value in (['urd'], True, 1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    subset_report([dict(label='genuine', score=0.1, language=value)], ['language'], costs=COSTS)


class ReliabilityTests(unittest.TestCase):
    def test_bins_by_hand(self):
        bins = reliability_bins([0.05, 0.15, 0.95, 1.0], [0, 1, 1, 1], n_bins=10)
        self.assertEqual([(b['low'], b['count'], b['fraction_synthetic']) for b in bins],
                         [(0.0, 1, 0.0), (0.1, 1, 1.0), (0.9, 2, 1.0)])
        self.assertAlmostEqual(bins[2]['mean_p'], 0.975)
        self.assertEqual([b['high'] for b in bins], [0.1, 0.2, 1.0])
        with self.assertRaises(ValueError):
            reliability_bins([0.5], [3])

    def test_bins_agree_with_ece(self):
        rng = np.random.default_rng(5)
        p = rng.random(500)
        y = (rng.random(500) < p ** 2).astype(int)
        bins = reliability_bins(p, y)  # default 15 bins, as ece
        weighted = sum(b['count'] * abs(b['fraction_synthetic'] - b['mean_p']) for b in bins) / 500
        self.assertAlmostEqual(weighted, ece(p, y))
        self.assertEqual(sum(b['count'] for b in bins), 500)


class DialectRuleTests(unittest.TestCase):
    def test_dialect_far_at_fixed_threshold(self):
        # Pothwari: 10 speakers x 50 clips, 1 clip in 10 at 0.9 (flagged), others 0.1.
        pothwari = [dict(dialect='pothwari', speaker=f's{k}', score=0.9 if n % 10 == 0 else 0.1)
                    for k in range(10) for n in range(50)]
        # Saraiki: only 9 speakers -> insufficient even with 900 clips.
        saraiki = [dict(dialect='saraiki', speaker=f't{k}', score=0.1) for k in range(9) for _ in range(100)]
        listed = ['saraiki', 'pothwari', 'hindko']
        rows = dialect_far(pothwari + saraiki, dialects=listed, threshold=0.5, n_resamples=200)
        self.assertEqual([(r['dialect'], r['speakers'], r['genuine_clips'], r['status']) for r in rows],
                         [('saraiki', 9, 900, 'insufficient data'), ('pothwari', 10, 500, 'ok'),
                          ('hindko', 0, 0, 'insufficient data')])  # listed order; no-data dialect kept
        self.assertEqual(rows[1]['far'], 0.1)              # 50 of 500 at or above 0.5
        low, high = rows[1]['far_ci']
        self.assertTrue(low < 0.1 < high)
        self.assertFalse(rows[1]['ci_degenerate'])
        self.assertNotIn('far', rows[0])
        at_score = dialect_far(pothwari, dialects=['pothwari'], threshold=0.9, n_resamples=200)[0]['far']
        self.assertEqual(at_score, 0.1)                     # score equal to threshold is flagged
        clean = dialect_far([dict(r, score=0.1) for r in pothwari], dialects=['pothwari'],
                            threshold=0.5, n_resamples=200)[0]
        self.assertEqual((clean['far'], clean['far_ci'], clean['ci_degenerate']), (0.0, (0.0, 0.0), True))
        short = pothwari[:499]
        self.assertEqual(dialect_far(short, dialects=['pothwari'], threshold=0.5)[0]['status'],
                         'insufficient data')

    def test_dialect_far_validation(self):
        good = dict(dialect='majhi', speaker='a', score=0.2)
        for bad in ([dict(good, dialect='')], [dict(good, speaker=None)], [dict(good, score=2)], ['x'],
                    [dict(good, score=float('nan'))], [dict(good, label='synthetic')],
                    [dict(good, dialect='Majhi')], [dict(good, dialect=5)]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    dialect_far(bad, dialects=['majhi'], threshold=0.5)
        for kw in (dict(threshold=1.5), dict(threshold=True), dict(threshold=0.5, seed=-1),
                   dict(threshold=0.5, n_resamples=10), dict(threshold=0.5, confidence='0.95'),
                   dict(threshold=0.5, confidence=True), dict(threshold=0.5, dialects=[]),
                   dict(threshold=0.5, dialects=['majhi', 'majhi'])):
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):
                    dialect_far([good], **{'dialects': ['majhi'], **kw})
        for edge in (0, 1):
            dialect_far([good], dialects=['majhi'], threshold=edge)  # boundaries accepted
        dialect_far([dict(good, label='genuine')], dialects=['majhi'], threshold=0.5)
        with self.assertRaises(TypeError):
            dialect_far([good], dialects=['majhi'])  # the deployed threshold must be given

    def test_fr_m10_boundaries(self):
        self.assertTrue(dialect_reportable(10, 500))
        self.assertFalse(dialect_reportable(9, 5000))
        self.assertFalse(dialect_reportable(50, 499))
        for bad in ((-1, 500), (10, 500.0), (True, 500)):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    dialect_reportable(*bad)


if __name__ == '__main__':
    unittest.main()
