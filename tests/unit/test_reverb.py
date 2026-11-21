"""Claude-authored tests (16 November 2026): room impulse response convolution
(backlog 3.4; PRD FR-D7 RIRs, FR-D13 lineage). Authored by Claude (Claude Code), not by
GPT-6 Astra. Expected values come from hand-built RIRs and an independent direct-form
convolution (numpy.convolve), never from pinned digests of FFT output (it may differ in
the last bits across platforms before int16 rounding).
"""
import hashlib
import unittest

import numpy as np

from data.prepare import reverb
from data.prepare.reverb import apply_planned, apply_rir, derive_reverb, rir_profile


def pcm(values):
    return np.asarray(values, dtype='<i2').tobytes()


def samples(raw):
    return np.frombuffer(raw, dtype='<i2').astype(np.int64)


def speech(seconds=1.0, seed=7, amplitude=4000):
    rng = np.random.default_rng(seed)
    return pcm(np.rint(rng.normal(0, amplitude, int(seconds * reverb.RATE))).clip(-32768, 32767))


def decaying_rir(seed=3, length=4000, pre=0, rt=0.15):
    """Exponentially decaying noise tail behind a unit direct path at index `pre`."""
    rng = np.random.default_rng(seed)
    t = np.arange(length) / reverb.RATE
    tail = rng.normal(0, 0.25, length) * np.exp(-6.9 * t / rt)
    h = np.concatenate([rng.normal(0, 0.01, pre), [1.0], tail[1:]])
    return pcm(np.rint(h / np.max(np.abs(h)) * 20000))


class RirProfileTests(unittest.TestCase):
    def test_unit_impulse_returns_the_input_unchanged(self):
        clip = speech()
        out, record = apply_rir(clip, pcm([16384]))
        self.assertEqual(out, clip)
        # The gain undoes the RIR's raw int16 scale.
        self.assertEqual((record['direct_index'], record['clipped_samples']), (0, 0))
        self.assertAlmostEqual(record['gain'] * 16384, 1.0, places=12)
        self.assertIsNone(record['drr_db'])  # no energy after the direct path
        self.assertEqual(record['tail_energy_fraction'], 0.0)

    def test_leading_samples_are_dropped_so_the_copy_is_not_delayed(self):
        clip = speech(0.5)
        out, record = apply_rir(clip, pcm([0] * 37 + [-12000]))  # negative direct path
        # Not delayed, and the direct path is made positive (no inverted copies).
        self.assertEqual(out, clip)
        self.assertEqual((record['direct_index'], record['direct_sign']), (37, -1))
        self.assertEqual(record['pre_direct_energy_fraction'], 0.0)
        self.assertEqual(record['aligned_rir_samples'], 1)

    def test_known_answer_against_direct_convolution(self):
        clip = speech(0.75)
        rir = decaying_rir(pre=25)
        out, record = apply_rir(clip, rir)
        h = samples(rir).astype(np.float64)
        direct = int(np.argmax(np.abs(h)))
        self.assertEqual(direct, 25)
        x = samples(clip).astype(np.float64)
        full = np.convolve(x, h[direct:])
        wet = full[:len(x)]
        gain = np.sqrt(np.mean(x ** 2) / np.mean(wet ** 2))
        expected = np.clip(np.rint(wet * gain), -32768, 32767)
        self.assertLessEqual(int(np.max(np.abs(samples(out) - expected))), 1)
        self.assertAlmostEqual(record['gain'], gain, delta=1e-9 * gain)
        self.assertAlmostEqual(record['tail_energy_fraction'], np.sum(full[len(x):] ** 2) / np.sum(full ** 2),
                               places=9)
        self.assertEqual(len(out), len(clip))
        # Level preserved: mean square within int16 rounding of the input's.
        self.assertAlmostEqual(np.mean(samples(out) ** 2.0) / np.mean(x ** 2), 1.0, delta=1e-3)
        self.assertEqual(record['rir_sha256'], hashlib.sha256(rir).hexdigest())
        self.assertEqual(record['reverberant_pcm_sha256'], hashlib.sha256(out).hexdigest())
        self.assertEqual(record['speech_sha256'], hashlib.sha256(clip).hexdigest())
        self.assertEqual(record['rir_samples'], len(rir) // 2)

    def test_reverberation_changes_the_signal(self):
        clip = speech(2.0)
        rir = decaying_rir()
        out, _ = apply_rir(clip, rir)
        a, b = samples(clip).astype(float), samples(out).astype(float)
        correlation = float(np.corrcoef(a, b)[0, 1])
        # For white input the correlation with the dry signal is h[direct] / ||h||.
        h = samples(rir).astype(float)
        expected = abs(h[0]) / np.sqrt(np.sum(h ** 2))
        self.assertLess(expected, 0.5)  # this RIR is reverberant, not a near copy
        self.assertAlmostEqual(correlation, expected, delta=0.03)

    def test_drr_and_pre_direct_energy_known_answers(self):
        # direct 1000 at index 2; 99 before it (below 1/10 of the peak); 100 inside +/-40
        # samples; 50 x 4 late.
        h = [99, 0, 1000] + [0] * 9 + [100] + [0] * 40 + [50, 50, 50, 50]
        aligned, profile = rir_profile(pcm(h))
        near, late = 99 ** 2 + 1000 ** 2 + 100 ** 2, 4 * 50 ** 2
        self.assertEqual((profile['direct_index'], profile['peak_index'], profile['direct_sign']), (2, 2, 1))
        self.assertAlmostEqual(profile['drr_db'], 10 * np.log10(near / late), places=9)
        self.assertAlmostEqual(profile['pre_direct_energy_fraction'], 99 ** 2 / (near + late), places=12)
        self.assertEqual(len(aligned), len(h) - 2)
        self.assertEqual(profile['direct_half_width_samples'], 40)

    def test_direct_path_is_the_first_arrival_not_a_stronger_reflection(self):
        # First arrival 3000 at index 5, a reflection of 10000 at index 60 (speakerphone-like).
        h = [0] * 5 + [3000] + [0] * 54 + [-10000] + [400] * 20
        aligned, profile = rir_profile(pcm(h))
        self.assertEqual((profile['direct_index'], profile['peak_index'], profile['direct_sign']), (5, 60, 1))
        self.assertEqual(aligned[0], 3000.0)
        # Exactly 1/10 of the peak counts as an arrival; just below does not.
        self.assertEqual(rir_profile(pcm([1000, 0, 10000]))[1]['direct_index'], 0)
        self.assertEqual(rir_profile(pcm([999, 0, 10000]))[1]['direct_index'], 2)
        # A sinc-like pulse: the negative side-lobe is the first arrival, but the sign is
        # the main lobe's (review L7).
        _, lobe = rir_profile(pcm([0, -1500, 10000, -1500, 300]))
        self.assertEqual((lobe['direct_index'], lobe['direct_sign']), (1, 1))
        # The copy is not delayed: an impulse train shifted by the direct index lines up.
        clip = speech(0.5)
        out, _ = apply_rir(clip, pcm([0] * 7 + [-5000]))
        self.assertEqual(out, clip)

    def test_first_of_equal_peaks_is_the_direct_path(self):
        _, profile = rir_profile(pcm([0, 500, -500, 500]))
        self.assertEqual(profile['direct_index'], 1)

    def test_clipping_is_counted_not_refused(self):
        alternating = pcm([32767, -32767] * 400)
        out, record = apply_rir(alternating, pcm([1000, 1000]))
        # x[n] + x[n-1] is zero except the first sample, so the level rule drives it to clip.
        self.assertGreater(record['clipped_samples'], 0)
        self.assertEqual(len(out), len(alternating))

    def test_refusals(self):
        clip = speech(0.2)
        cases = [
            (b'', pcm([1])), (b'\x01', pcm([1])), ('text', pcm([1])),
            (pcm([0] * 100), pcm([1])),  # silent speech
            (clip, pcm([0, 0, 0])),  # silent RIR
            (clip, b'\x01\x02\x03'),  # odd length
            (clip, pcm([1] * (reverb.MAX_RIR_SAMPLES + 1))),
            (clip, pcm([0] * (reverb.MAX_DIRECT_INDEX + 1) + [1000])),
        ]
        for speech_pcm, rir in cases:
            with self.assertRaises(ValueError):
                apply_rir(speech_pcm, rir)
        # The boundaries themselves are accepted.
        apply_rir(clip, pcm([0] * reverb.MAX_DIRECT_INDEX + [1000]))
        apply_rir(clip, pcm([1000] + [1] * (reverb.MAX_RIR_SAMPLES - 1)))


class DeriveReverbTests(unittest.TestCase):
    def test_deterministic_in_range_and_spread(self):
        plans = [derive_reverb(2026, f'clip-{i}', 4) for i in range(400)]
        self.assertEqual(plans, [derive_reverb(2026, f'clip-{i}', 4) for i in range(400)])
        counts = np.bincount([p['index'] for p in plans], minlength=4)
        self.assertEqual(len(counts), 4)
        self.assertTrue(all(60 <= c <= 140 for c in counts), counts)  # about 100 each
        self.assertNotEqual([p['index'] for p in plans],
                            [derive_reverb(2027, f'clip-{i}', 4)['index'] for i in range(400)])
        self.assertEqual(derive_reverb(1, 'c', 1)['index'], 0)
        self.assertEqual(set(derive_reverb(1, 'c', 3)), {'derivation', 'master_seed', 'clip_id', 'pool_size', 'index'})

    def test_validation(self):
        for args in [(-1, 'c', 2), (True, 'c', 2), (1.0, 'c', 2), (1, '', 2), (1, None, 2),
                     (1, 'c', 0), (1, 'c', reverb.MAX_POOL + 1), (1, 'c', True), (1, 'c', 2.0)]:
            with self.assertRaises(ValueError, msg=args):
                derive_reverb(*args)

    def test_apply_planned_uses_the_drawn_rir_and_refuses_tampering(self):
        clip = speech(0.3)
        pool = [pcm([16000]), decaying_rir(seed=5), decaying_rir(seed=6, pre=10)]
        plan = derive_reverb(9, 'clip-x', 3)
        out, record = apply_planned(clip, pool, plan)
        direct, _ = apply_rir(clip, pool[plan['index']])
        self.assertEqual(out, direct)
        self.assertEqual((record['pool_index'], record['pool_size'], record['derivation']),
                         (plan['index'], 3, reverb.REVERB_DERIVATION))
        bad_plans = [dict(plan, index=(plan['index'] + 1) % 3), dict(plan, pool_size=2), {}, None,
                     dict(plan, extra=1)]
        for bad in bad_plans:
            with self.assertRaises(ValueError):
                apply_planned(clip, pool, bad)
        for bad_pool in (pool[:2], pool + [pcm([1])], 'x'):
            with self.assertRaises(ValueError):
                apply_planned(clip, bad_pool, plan)


if __name__ == '__main__':
    unittest.main()
