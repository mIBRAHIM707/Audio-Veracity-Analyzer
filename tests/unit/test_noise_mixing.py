"""Claude-authored test cases, 9 November 2026: additive noise at SNR (PRD FR-D7).

Generated fixtures only; no audio files. Infrastructure evidence, not model accuracy.
"""
import hashlib
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'data' / 'prepare'))
import noise as noise_mod  # noqa: E402


def pcm(values):
    return np.asarray(values, dtype='<i2').tobytes()


def tone(n, amp=8000, freq=440, rate=16000):
    return pcm(np.rint(amp * np.sin(2 * np.pi * freq * np.arange(n) / rate)))


def white(n, seed, amp=3000):
    return pcm(np.rint(np.random.default_rng(seed).normal(0, amp, n)).clip(-32768, 32767))


def measured_snr(speech, mixed):
    clean = np.frombuffer(speech, '<i2').astype(np.float64)
    added = np.frombuffer(mixed, '<i2').astype(np.float64) - clean
    return 10 * np.log10(np.mean(clean ** 2) / np.mean(added ** 2))


class MixAtSnrTests(unittest.TestCase):
    def test_measured_snr_matches_target_across_range(self):
        speech, noise = tone(32000), white(48000, 1)
        for target in (0, 0.5, 5, 10, 17.3, 20):
            mixed, record = noise_mod.mix_at_snr(speech, noise, target, seed=7)
            self.assertEqual(len(mixed), len(speech))
            self.assertAlmostEqual(measured_snr(speech, mixed), target, delta=0.02)
            self.assertEqual(record['clipped_samples'], 0)
            self.assertEqual(record['snr_db'], float(target))
            self.assertAlmostEqual(record['achieved_snr_db'], measured_snr(speech, mixed),
                                   places=9)

    def test_gain_uses_excerpt_power_not_whole_noise_power(self):
        speech = tone(4000)
        loud = np.rint(np.random.default_rng(8).normal(0, 8000, 20000))
        quiet = np.rint(np.random.default_rng(9).normal(0, 500, 20000))
        noise = pcm(np.concatenate([loud, quiet]))
        seed = next((s for s in range(200)
                     if np.random.default_rng(s).integers(40000) in range(20000, 36000)), None)
        self.assertIsNotNone(seed, 'no seed puts the excerpt inside the quiet half')
        mixed, record = noise_mod.mix_at_snr(speech, noise, 10, seed)
        self.assertGreaterEqual(record['noise_offset'], 20000)
        self.assertAlmostEqual(measured_snr(speech, mixed), 10, delta=0.02)
        whole = np.frombuffer(noise, '<i2').astype(np.float64)
        clean = np.frombuffer(speech, '<i2').astype(np.float64)
        excerpt = whole[record['noise_offset']:record['noise_offset'] + 4000]
        whole_gain = np.sqrt(np.mean(clean ** 2) / (np.mean(whole ** 2) * 10))
        wrong_snr = 10 * np.log10(np.mean(clean ** 2) / np.mean((whole_gain * excerpt) ** 2))
        self.assertGreater(abs(wrong_snr - 10), 5)

    def test_deterministic_for_seed_and_offsets_vary_between_seeds(self):
        speech, noise = tone(16000), white(40000, 2)
        first = noise_mod.mix_at_snr(speech, noise, 10, seed=3)
        self.assertEqual(first, noise_mod.mix_at_snr(speech, noise, 10, seed=3))
        offsets = {noise_mod.mix_at_snr(speech, noise, 10, seed=s)[1]['noise_offset']
                   for s in range(20)}
        self.assertGreater(len(offsets), 15)
        self.assertTrue(all(0 <= o < 40000 for o in offsets))

    def test_excerpt_is_taken_at_recorded_offset_with_wraparound(self):
        speech = tone(1000)
        noise_values = np.arange(-150, 150) * 100  # 300 distinct samples, shorter than speech
        mixed, record = noise_mod.mix_at_snr(speech, pcm(noise_values), 10, seed=11)
        self.assertTrue(record['noise_wrapped'])
        offset = record['noise_offset']
        expected_excerpt = np.array([noise_values[(offset + i) % 300] for i in range(1000)],
                                    dtype=np.float64)
        self.assertEqual(expected_excerpt[300 - offset], noise_values[0])
        clean = np.frombuffer(speech, '<i2').astype(np.float64)
        gain = np.sqrt(np.mean(clean ** 2) / (np.mean(expected_excerpt ** 2) * 10))
        self.assertAlmostEqual(record['noise_gain'], gain, places=12)
        expected = np.rint(clean + gain * expected_excerpt).astype('<i2')
        self.assertEqual(mixed, expected.tobytes())
        self.assertEqual(record['noise_repeats'], -(-(offset + 1000) // 300))

    def test_unwrapped_when_noise_covers_clip_from_offset(self):
        speech, noise = tone(100), white(100000, 4)
        seeds = [s for s in range(50)
                 if noise_mod.mix_at_snr(speech, noise, 5, s)[1]['noise_offset'] <= 99900]
        self.assertTrue(seeds, 'no seed gives an unwrapped excerpt')
        _, record = noise_mod.mix_at_snr(speech, noise, 5, seeds[0])
        self.assertFalse(record['noise_wrapped'])
        self.assertEqual(record['noise_repeats'], 1)

    def test_excerpt_ending_on_last_noise_sample_is_not_wrapped(self):
        speech = tone(1000)
        noise = white(1100, 12)  # excerpt from offset 100 ends exactly at the end
        seed = next((s for s in range(20000)
                     if np.random.default_rng(s).integers(1100) == 100), None)
        self.assertIsNotNone(seed, 'no seed gives offset 100')
        _, record = noise_mod.mix_at_snr(speech, noise, 10, seed=seed)
        self.assertEqual(record['noise_offset'], 100)
        self.assertEqual(record['noise_repeats'], 1)
        self.assertFalse(record['noise_wrapped'])

    def test_inputs_are_not_modified_and_record_has_lineage(self):
        speech, noise = tone(8000), white(9000, 5)
        speech_copy, noise_copy = bytes(speech), bytes(noise)
        mixed, record = noise_mod.mix_at_snr(speech, noise, 12, seed=1)
        self.assertEqual((speech, noise), (speech_copy, noise_copy))
        self.assertEqual(record['speech_sha256'], hashlib.sha256(speech).hexdigest())
        self.assertEqual(record['mixed_pcm_sha256'], hashlib.sha256(mixed).hexdigest())
        self.assertEqual(record['quantization'], 'round_half_even_clip_pcm16')
        self.assertEqual(record['noise_sha256'], hashlib.sha256(noise).hexdigest())
        self.assertEqual(record['noise_samples'], 9000)
        self.assertEqual(record['samples'], 8000)
        self.assertEqual(record['implementation'], noise_mod.NOISE_IMPLEMENTATION)
        self.assertEqual(record['snr_basis'], 'whole_clip_mean_square')
        self.assertEqual(record['numpy_version'], np.__version__)

    def test_saturation_is_clipped_and_counted(self):
        speech = pcm(np.full(4000, 30000))
        noise = pcm(np.tile([20000, -20000], 2000))
        mixed, record = noise_mod.mix_at_snr(speech, noise, 0, seed=0)
        out = np.frombuffer(mixed, '<i2')
        self.assertEqual(int(out.max()), 32767)
        self.assertEqual(record['clipped_samples'], 2000)
        self.assertEqual(int(np.count_nonzero(out == 32767)), 2000)
        # Clipping leaves added samples of +2767 / -30000, so the achieved SNR is not 0 dB.
        expected = 10 * np.log10(30000 ** 2 / ((2767 ** 2 + 30000 ** 2) / 2))
        self.assertAlmostEqual(record['achieved_snr_db'], expected, places=9)
        self.assertGreater(record['achieved_snr_db'], 2.9)

    def test_negative_saturation_and_exact_limits(self):
        speech = pcm(np.full(4000, -30000))
        noise = pcm(np.tile([20000, -20000], 2000))
        mixed, record = noise_mod.mix_at_snr(speech, noise, 0, seed=0)
        out = np.frombuffer(mixed, '<i2')
        self.assertEqual(int(out.min()), -32768)
        self.assertEqual(record['clipped_samples'], 2000)
        # Mixes landing exactly on -32768 and 32767 are in range and not counted.
        # Same multiset of squares, so the 0 dB gain is exactly 1 and sums are exact.
        edge_speech = pcm(np.tile([16383, -16384, 16384, -16383], 25))
        edge_noise = pcm(np.tile([16384, -16384, -16383, 16383], 25))
        aligned = next((s for s in range(100)
                        if np.random.default_rng(s).integers(100) % 4 == 0), None)
        self.assertIsNotNone(aligned, 'no seed keeps the excerpt aligned')
        mixed, record = noise_mod.mix_at_snr(edge_speech, edge_noise, 0, seed=aligned)
        out = np.frombuffer(mixed, '<i2')
        self.assertEqual(record['noise_offset'] % 4, 0)
        self.assertEqual(record['noise_gain'], 1.0)
        self.assertEqual(record['clipped_samples'], 0)
        self.assertEqual(int(out.max()), 32767)
        self.assertEqual(int(out.min()), -32768)

    def test_rejects_invalid_inputs(self):
        speech, noise = tone(1000), white(1000, 6)
        bad = [
            (bytearray(speech), noise, 10, 0), (speech, b'', 10, 0), (b'\x00', noise, 10, 0),
            (speech, noise, -0.01, 0), (speech, noise, 20.01, 0), (speech, noise, float('nan'), 0),
            (speech, noise, True, 0), (speech, noise, '10', 0), (speech, noise, 10, -1),
            (speech, noise, 10, 1.0), (speech, noise, 10, None), (speech, noise, 10, False),
            (pcm(np.zeros(1000)), noise, 10, 0), (speech, pcm(np.zeros(1000)), 10, 0),
            (speech, noise[:-1], 10, 0), (memoryview(speech), noise, 10, 0),
            (speech, noise, float('inf'), 0), (speech, noise, np.float32(10), 0),
        ]
        for index, args in enumerate(bad):
            with self.subTest(case=index):
                with self.assertRaises(ValueError):
                    noise_mod.mix_at_snr(*args)

    def test_silent_excerpt_inside_nonsilent_noise_is_refused(self):
        speech = tone(100)
        noise = pcm(np.concatenate([np.zeros(100000), np.full(10, 500)]))
        # Offsets come from the documented rng; pick seeds whose excerpt lies in the zeros.
        seeds = [s for s in range(30) if np.random.default_rng(s).integers(100010) < 99800]
        self.assertTrue(seeds, 'no seed puts the excerpt inside the zeros')
        with self.assertRaises(ValueError):
            noise_mod.mix_at_snr(speech, noise, 10, seeds[0])


class DeriveNoiseTests(unittest.TestCase):
    """Claude-authored, 9 November 2026: per-clip SNR/seed plans (review M2)."""

    def test_pinned_value_and_independent_digest(self):
        plan = noise_mod.derive_noise(2026, 'csalt/spk01/clip0001.wav')
        self.assertEqual(plan, {'derivation': 'ava-noise-snr-v1', 'master_seed': 2026,
                                'clip_id': 'csalt/spk01/clip0001.wav',
                                'snr_db': 12.47682, 'seed': 3317115706357268130})
        digest = hashlib.sha256(b'ava-noise-snr-v1\x002026\x00csalt/spk01/clip0001.wav').digest()
        self.assertEqual(plan['seed'], int.from_bytes(digest[8:16], 'big'))
        self.assertEqual(plan['snr_db'],
                         round(20 * int.from_bytes(digest[:8], 'big') / 2 ** 64, 6))

    def test_deterministic_bounded_and_spread_over_clips(self):
        plans = [noise_mod.derive_noise(7, f'clip-{i}') for i in range(2000)]
        self.assertEqual(plans[5], noise_mod.derive_noise(7, 'clip-5'))
        snrs = np.array([p['snr_db'] for p in plans])
        self.assertTrue(((snrs >= 0) & (snrs <= 20)).all())
        counts = np.histogram(snrs, bins=4, range=(0, 20))[0]
        self.assertTrue((counts > 400).all(), counts)  # ~500 per 5 dB bin
        self.assertEqual(len({p['seed'] for p in plans}), 2000)
        self.assertNotEqual(noise_mod.derive_noise(8, 'clip-5'), plans[5])

    def test_independent_of_packet_loss_derivation(self):
        # data/prepare/codecs.py shares its name with the stdlib module, so load it by path.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'prepare_codecs', Path(__file__).resolve().parents[2] / 'data' / 'prepare' / 'codecs.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertNotEqual(module.LOSS_DERIVATION, noise_mod.NOISE_DERIVATION)
        loss = [module.derive_loss(7, f'clip-{i}') for i in range(2000)]
        noise = [noise_mod.derive_noise(7, f'clip-{i}') for i in range(2000)]
        self.assertTrue(all(a['seed'] != b['seed'] for a, b in zip(loss, noise)))
        r = np.corrcoef([p['loss_rate'] for p in loss], [p['snr_db'] for p in noise])[0, 1]
        self.assertLess(abs(r), 0.1)

    def test_clip_id_is_exact_string(self):
        self.assertNotEqual(noise_mod.derive_noise(1, 'a.wav'), noise_mod.derive_noise(1, 'A.wav'))
        self.assertNotEqual(noise_mod.derive_noise(1, 'a.wav'), noise_mod.derive_noise(1, 'a.wav '))

    def test_rejects_invalid_derivation_inputs(self):
        for index, (master, clip) in enumerate([(-1, 'x'), (1.0, 'x'), (True, 'x'), (None, 'x'),
                                                (1, ''), (1, None), (1, b'x')]):
            with self.subTest(case=index), self.assertRaises(ValueError):
                noise_mod.derive_noise(master, clip)

    def test_mix_planned_matches_direct_mix_and_records_derivation(self):
        speech, noise = tone(8000), white(20000, 13)
        plan = noise_mod.derive_noise(42, 'clip-a')
        mixed, record = noise_mod.mix_planned(speech, noise, plan)
        direct, direct_record = noise_mod.mix_at_snr(speech, noise, plan['snr_db'], plan['seed'])
        self.assertEqual(mixed, direct)
        self.assertEqual(record, {**direct_record, 'derivation': 'ava-noise-snr-v1',
                                  'master_seed': 42, 'clip_id': 'clip-a'})
        rebuilt = noise_mod.derive_noise(record['master_seed'], record['clip_id'])
        self.assertEqual((rebuilt['snr_db'], rebuilt['seed']), (record['snr_db'], record['seed']))
        self.assertAlmostEqual(measured_snr(speech, mixed), plan['snr_db'], delta=0.02)

    def test_mix_planned_refuses_altered_or_malformed_plans(self):
        speech, noise = tone(1000), white(2000, 14)
        plan = noise_mod.derive_noise(42, 'clip-a')
        altered = [{**plan, 'snr_db': 3.0}, {**plan, 'seed': plan['seed'] + 1},
                   {**plan, 'clip_id': 'clip-b'}, {**plan, 'derivation': 'other'},
                   {**plan, 'master_seed': 43}, {**plan, 'master_seed': -1},
                   {**plan, 'extra': 1}, {k: v for k, v in plan.items() if k != 'seed'},
                   {}, None, list(plan.items())]
        for index, bad in enumerate(altered):
            with self.subTest(case=index), \
                    self.assertRaisesRegex(ValueError, 'unchanged from derive_noise'):
                noise_mod.mix_planned(speech, noise, bad)


if __name__ == '__main__':
    unittest.main()
