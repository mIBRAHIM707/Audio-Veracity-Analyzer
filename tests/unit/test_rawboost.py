"""RawBoost augmentation (backlog 9.2; PRD FR-M4 XLS-R + AASIST + RawBoost baseline,
architecture §4.2 training augmentation).

Claude-authored test design, 12 November 2026. Generated signals only. The scipy
reference functions are not installed, so filters are checked by their defining
properties and an independent FFT; this is not bit-parity with the reference code.
"""
import math
import unittest
from unittest import mock

import numpy as np

from models.augmentation import rawboost as rb


def tone(seconds=1.0, freq=440.0, amplitude=0.1, rate=16000):
    t = np.arange(int(seconds * rate)) / rate
    return amplitude * np.sin(2 * math.pi * freq * t)


def response_at(taps, hz, rate=16000):
    omega = 2 * math.pi * hz / rate
    return abs(np.sum(taps * np.exp(-1j * omega * np.arange(len(taps)))))


class FilterTests(unittest.TestCase):
    def test_bandstop_is_linear_phase_unit_dc_and_stops_its_band(self):
        taps = rb.firwin_bandstop(101, 2000, 3000)
        self.assertEqual(len(taps), 101)
        self.assertTrue(np.allclose(taps, taps[::-1]))
        self.assertAlmostEqual(response_at(taps, 0), 1.0, places=12)
        self.assertLess(response_at(taps, 2500), 0.01)
        self.assertAlmostEqual(response_at(taps, 6000), 1.0, delta=0.01)
        self.assertAlmostEqual(response_at(taps, 500), 1.0, delta=0.01)

    def test_bandstop_refuses_even_or_tiny_lengths_and_bad_cutoffs(self):
        for numtaps in (2, 100, 1, 11.0, True):
            with self.assertRaises(ValueError):
                rb.firwin_bandstop(numtaps, 100, 200)
        for f1, f2 in ((0, 100), (300, 200), (100, 8000), (-1, 100)):
            with self.assertRaises(ValueError):
                rb.firwin_bandstop(11, f1, f2)

    def test_max_response_matches_an_fft_on_the_freqz_grid(self):
        taps = np.random.default_rng(0).normal(size=37)
        expected = np.abs(np.fft.rfft(taps, 2 * rb.FREQZ_POINTS))[:rb.FREQZ_POINTS].max()
        self.assertAlmostEqual(rb.max_response(taps), expected, places=10)

    def test_notch_cascade_peak_gain_is_g_db_and_length_adds_up(self):
        rng = np.random.default_rng(1)
        taps = rb.notch_coefficients(rng, **dict(rb.NOTCH_DEFAULTS, min_g=-6, max_g=-6))
        self.assertAlmostEqual(rb.max_response(taps), 10 ** (-6 / 20), places=10)
        self.assertEqual(len(taps) % 2, 1)
        self.assertGreaterEqual(len(taps), 5 * 11 - 4)
        self.assertLessEqual(len(taps), 5 * 101 - 4)

    def test_fir_filter_keeps_length_and_reproduces_the_reference_trim(self):
        x = np.random.default_rng(2).normal(size=500)
        self.assertEqual(rb.filter_fir(x, rb.firwin_bandstop(31, 1000, 2000)).shape, x.shape)
        # Reference filterFIR trims (len+1)//2 samples: an identity filter advances by one.
        identity = rb.filter_fir(x, np.ones(1))
        self.assertTrue(np.array_equal(identity[:-1], x[1:]))
        self.assertEqual(identity[-1], 0.0)


class AlgorithmTests(unittest.TestCase):
    def test_impulsive_noise_touches_at_most_p_percent_within_g_sd(self):
        x = tone()
        for seed in range(5):
            y = rb.impulsive_signal_dependent(x, np.random.default_rng(seed), p=10, g_sd=2)
            changed = np.flatnonzero(y != x)
            self.assertLessEqual(len(changed), int(0.10 * len(x)))
            self.assertTrue(np.all(np.abs(y - x) <= 2 * np.abs(x) + 1e-15))
        self.assertTrue(np.array_equal(rb.impulsive_signal_dependent(x, np.random.default_rng(0), p=0, g_sd=2), x))

    def test_stationary_noise_is_added_at_10_to_40_db_snr(self):
        x = tone(amplitude=0.3)
        snrs = []
        for seed in range(8):
            y = rb.stationary_signal_independent(x, np.random.default_rng(seed), 10, 40, **rb.NOTCH_DEFAULTS)
            snr = 20 * math.log10(np.linalg.norm(x) / np.linalg.norm(y - x))
            self.assertGreaterEqual(snr, 10 - 1e-9)
            self.assertLessEqual(snr, 40 + 1e-9)
            snrs.append(snr)
        self.assertGreater(max(snrs) - min(snrs), 1.0)

    def test_convolutive_output_is_centred_bounded_and_same_length(self):
        x = tone() + tone(freq=3000, amplitude=0.05)
        y = rb.linear_nonlinear_convolutive(x, np.random.default_rng(3), **rb.ALGORITHM_DEFAULTS['lnl'])
        self.assertEqual(y.shape, x.shape)
        self.assertAlmostEqual(float(y.mean()), 0.0, places=12)
        self.assertLessEqual(np.abs(y).max(), 1.0)
        self.assertFalse(np.allclose(y, x))

    def test_higher_powers_get_the_biased_gain_range_like_the_reference(self):
        gains = []
        original = rb.notch_coefficients

        def spy(rng, **kwargs):
            gains.append((kwargs['min_g'], kwargs['max_g']))
            return original(rng, **kwargs)

        with mock.patch.object(rb, 'notch_coefficients', spy):
            rb.linear_nonlinear_convolutive(tone(), np.random.default_rng(0), **rb.ALGORITHM_DEFAULTS['lnl'])
        self.assertEqual(gains, [(0, 0)] + [(-5, -20)] * 4)
        draws = [rb._uniform(np.random.default_rng(s), -5, -20) for s in range(50)]
        self.assertTrue(all(-20 <= d <= -5 for d in draws))


class RawBoostTests(unittest.TestCase):
    def test_every_mode_runs_and_keeps_length_dtype_and_finiteness(self):
        x = tone().astype(np.float32)
        for mode in sorted(rb.MODES):
            with self.subTest(mode=mode):
                y, record = rb.rawboost(x, mode, 2026, 'clip-a')
                self.assertEqual((y.shape, y.dtype), (x.shape, np.float32))
                self.assertTrue(np.isfinite(y).all())
                self.assertFalse(np.array_equal(y, x))
                self.assertEqual((record['mode'], record['master_seed'], record['clip_id']), (mode, 2026, 'clip-a'))
        self.assertEqual(sorted(rb.rawboost(x, 8, 1, 'c')[1]['params']), ['isd', 'lnl'])
        self.assertEqual(sorted(rb.rawboost(x, 3, 1, 'c')[1]['params']), ['ssi'])

    def test_draws_are_per_clip_reproducible_and_order_independent(self):
        x = tone()
        first = rb.rawboost(x, 4, 7, 'clip-a')[0]
        rb.rawboost(x, 4, 7, 'clip-b')
        self.assertTrue(np.array_equal(first, rb.rawboost(x, 4, 7, 'clip-a')[0]))
        self.assertFalse(np.array_equal(first, rb.rawboost(x, 4, 7, 'clip-b')[0]))
        self.assertFalse(np.array_equal(first, rb.rawboost(x, 4, 8, 'clip-a')[0]))

    def test_every_mode_is_the_reference_composition(self):
        functions = {'lnl': rb.linear_nonlinear_convolutive, 'isd': rb.impulsive_signal_dependent,
                     'ssi': rb.stationary_signal_independent}
        series = {1: ['lnl'], 2: ['isd'], 3: ['ssi'], 4: ['lnl', 'isd', 'ssi'], 5: ['lnl', 'isd'],
                  6: ['lnl', 'ssi'], 7: ['isd', 'ssi']}
        x = tone(amplitude=0.9) + tone(freq=2500, amplitude=0.05)
        for mode, names in series.items():
            with self.subTest(mode=mode):
                rng = rb.generator(5, 'clip', mode)
                expected = x
                for name in names:
                    expected = functions[name](expected, rng, **rb.ALGORITHM_DEFAULTS[name])
                self.assertTrue(np.array_equal(rb.rawboost(x, mode, 5, 'clip')[0], expected))
        rng = rb.generator(5, 'clip', 8)
        both = functions['lnl'](x, rng, **rb.ALGORITHM_DEFAULTS['lnl']) + \
            functions['isd'](x, rng, **rb.ALGORITHM_DEFAULTS['isd'])
        expected = both / np.abs(both).max() if np.abs(both).max() > 1 else both
        self.assertGreater(np.abs(both).max(), 1)
        self.assertTrue(np.array_equal(rb.rawboost(x, 8, 5, 'clip')[0], expected))

    def test_peak_normalization_only_when_needed(self):
        loud, quiet = tone(amplitude=0.95), tone(amplitude=0.01)

        def lnl_identity(x):
            # Identity notch filters: output is the mean-removed sum of x^1..x^5, advanced
            # one sample by the reference trim; normalized only when its peak exceeds 1.
            with mock.patch.object(rb, 'notch_coefficients', lambda rng, **kw: np.ones(1)):
                return rb.linear_nonlinear_convolutive(x, np.random.default_rng(0), **rb.ALGORITHM_DEFAULTS['lnl'])

        for x, should_scale in ((loud, True), (quiet, False)):
            raw = sum(np.append(x[1:] ** k, 0.0) for k in range(1, 6))
            raw = raw - raw.mean()
            expected = raw / np.abs(raw).max() if should_scale else raw
            self.assertEqual(np.abs(raw).max() > 1, should_scale)
            self.assertTrue(np.allclose(lnl_identity(x), expected, atol=1e-12))
        for seed in range(3):
            isd = rb.impulsive_signal_dependent(loud, np.random.default_rng(seed), p=10, g_sd=2)
            self.assertAlmostEqual(float(np.abs(isd).max()), 1.0, places=12)
        self.assertAlmostEqual(float(np.abs(rb.rawboost(loud, 8, 1, 'c')[0]).max()), 1.0, places=12)

    def test_record_identifies_the_environment(self):
        record = rb.rawboost(tone(), 1, 1, 'c')[1]
        self.assertEqual((record['sample_rate'], record['bit_generator']), (16000, 'PCG64'))
        self.assertEqual(record['numpy_version'], np.__version__)

    def test_parameter_overrides_apply_and_are_recorded(self):
        x = tone()
        y, record = rb.rawboost(x, 2, 1, 'c', params={'isd': {'p': 0}})
        self.assertTrue(np.array_equal(y, x))
        self.assertEqual(record['params']['isd'], {'p': 0, 'g_sd': 2})
        self.assertEqual(rb.ALGORITHM_DEFAULTS['isd'], {'p': 10, 'g_sd': 2})

    def test_refusals(self):
        x = tone()
        for mode in (0, 9, True, 1.0):
            with self.assertRaises(ValueError):
                rb.rawboost(x, mode, 1, 'c')
        bad_inputs = (np.array([], float), np.zeros((2, 10)), np.arange(10), np.array([0.0, np.nan]), [0.1, 0.2])
        for bad in bad_inputs[:-1]:
            with self.assertRaises(ValueError):
                rb.rawboost(bad, 1, 1, 'c')
        self.assertEqual(rb.rawboost(bad_inputs[-1] * 50, 2, 1, 'c')[0].shape, (100,))
        with self.assertRaisesRegex(ValueError, r'\[-1, 1\]'):
            rb.rawboost(x * 20, 1, 1, 'c')
        with self.assertRaises(ValueError) as caught:
            rb.rawboost(x, 4, 1, 'c', params={'lnl': {'n_f': 0, 'min_coeff': 2}, 'isd': {'p': 101},
                                               'ssi': {'snr_min': 50, 'snr_max': 40}})
        for text in ('lnl.n_f', 'lnl.min_coeff', 'isd: need', 'ssi: need'):
            self.assertIn(text, str(caught.exception))
        for params in ({'isd': {'q': 1}}, {'rir': {}}, {'isd': 3}, {'isd': {'g_sd': -1}},
                       {'isd': {'p': float('nan')}}, {'lnl': {'n_bands': 2.0}}, {'ssi': {'min_f': 0}},
                       {'ssi': {'max_f': 9000}}, {'ssi': {'min_bw': 500, 'max_bw': 100}},
                       {'lnl': {'min_coeff': 50, 'max_coeff': 20}}, {'lnl': {'min_g': True}}):
            with self.assertRaises(ValueError):
                rb.rawboost(x, 2, 1, 'c', params=params)
        for seed, clip in ((-1, 'c'), (True, 'c'), (1, ''), (1, 3)):
            with self.assertRaises(ValueError):
                rb.rawboost(x, 1, seed, clip)


if __name__ == '__main__':
    unittest.main()
