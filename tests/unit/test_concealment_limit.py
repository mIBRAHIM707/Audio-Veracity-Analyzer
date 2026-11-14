"""Repeat-then-silence packet concealment from architecture §2.2 (repeat the last 20 ms
frame, then zero-fill after 3 consecutive losses); backlog 3.3 fade-out item.

Claude-authored test cases, 10 November 2026. Generated codes only.
"""
import hashlib
import unittest

import numpy as np

from data.prepare.codecs import (CONCEALMENTS, FRAME_SAMPLES, REPEAT_LIMIT, _conceal, decode,
                                 derive_jitter, derive_loss, drop_late_packets, drop_packets, encode,
                                 telephone_roundtrip)

S = {'ulaw': 0xFF, 'alaw': 0xD5}


def frames(count, extra=0):
    body = np.repeat(np.arange(1, count + 1, dtype=np.uint8), FRAME_SAMPLES)
    return np.concatenate([body, np.full(extra, 0xAB, dtype=np.uint8)])


def per_packet(output):
    return [int(output[i]) for i in range(0, len(output), FRAME_SAMPLES)]


def longest_run(mask):
    best = run = 0
    for gone in mask:
        run = run + 1 if gone else 0
        best = max(best, run)
    return best


def by_hand(codes, mask, law):
    """Independent expectation: repeat the last received packet for the first three
    packets of a burst, silence afterwards or when nothing was received yet."""
    out, previous, run = bytearray(codes.tobytes()), None, 0
    for index, gone in enumerate(mask):
        start, end = index * FRAME_SAMPLES, min((index + 1) * FRAME_SAMPLES, len(codes))
        if not gone:
            previous, run = bytes(codes[start:end]), 0
            continue
        run += 1
        if previous is not None and run <= 3:
            out[start:end] = (previous * 2)[:end - start]
        else:
            out[start:end] = bytes([S[law]]) * (end - start)
    return bytes(out)


class RepeatThenSilenceTests(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(REPEAT_LIMIT, 3)
        self.assertIn('repeat_then_silence', CONCEALMENTS)

    def test_long_burst_repeats_three_then_silence(self):
        mask = np.array([0, 1, 1, 1, 1, 1, 0, 1], dtype=bool)
        codes = frames(8)
        self.assertEqual(per_packet(_conceal(codes, mask, 'ulaw', 'repeat_then_silence')),
                         [1, 1, 1, 1, S['ulaw'], S['ulaw'], 7, 7])
        self.assertEqual(per_packet(_conceal(codes, mask, 'ulaw', 'repeat')), [1, 1, 1, 1, 1, 1, 7, 7])
        self.assertEqual(per_packet(_conceal(codes, mask, 'alaw', 'repeat_then_silence'))[4], S['alaw'])

    def test_run_resets_after_a_received_packet(self):
        mask = np.array([0, 1, 1, 1, 0, 1, 1, 1, 1], dtype=bool)
        self.assertEqual(per_packet(_conceal(frames(9), mask, 'ulaw', 'repeat_then_silence')),
                         [1, 1, 1, 1, 5, 5, 5, 5, S['ulaw']])

    def test_leading_loss_is_silence(self):
        mask = np.array([1, 1, 0, 1], dtype=bool)
        self.assertEqual(per_packet(_conceal(frames(4), mask, 'ulaw', 'repeat_then_silence')),
                         [S['ulaw'], S['ulaw'], 3, 3])

    def test_short_final_packet_within_limit_is_truncated_repeat(self):
        codes = frames(3, extra=37)
        mask = np.array([0, 1, 1, 1], dtype=bool)
        output = _conceal(codes, mask, 'ulaw', 'repeat_then_silence')
        self.assertEqual(output.tobytes(), by_hand(codes, mask, 'ulaw'))
        self.assertTrue((output[3 * FRAME_SAMPLES:] == 1).all())
        mask = np.array([0, 1, 1, 1, 1], dtype=bool)  # fourth lost packet is the short one
        output = _conceal(frames(4, extra=37), mask, 'ulaw', 'repeat_then_silence')
        self.assertTrue((output[4 * FRAME_SAMPLES:] == S['ulaw']).all())

    def check_mode(self, output, record, codes, mask, law, plain_repeat_output):
        """Exact output, recorded rule, and proof the fixture separates the modes."""
        self.assertGreater(longest_run(mask), REPEAT_LIMIT)  # precondition: a long burst exists
        self.assertEqual(output, by_hand(codes, mask, law))
        self.assertNotEqual(output, plain_repeat_output)
        self.assertEqual((record['concealment'], record['repeat_limit']), ('repeat_then_silence', 3))

    def test_mode_through_drop_packets(self):
        count = 2000
        codes = np.resize(np.arange(1, 251, dtype=np.uint8), count * FRAME_SAMPLES)  # values < 255
        seed = next(s for s in range(5000)
                    if longest_run(np.random.default_rng(s).random(count) < 0.05) > REPEAT_LIMIT)
        lost = np.random.default_rng(seed).random(count) < 0.05
        output, record = drop_packets(codes.tobytes(), 'ulaw', 0.05, seed, 'repeat_then_silence')
        self.check_mode(output, record, codes, lost, 'ulaw',
                        drop_packets(codes.tobytes(), 'ulaw', 0.05, seed, 'repeat')[0])
        self.assertIsNone(drop_packets(codes.tobytes(), 'ulaw', 0.05, seed, 'repeat')[1]['repeat_limit'])

    def test_mode_through_drop_late_packets(self):
        count = 2000
        codes = np.resize(np.arange(1, 251, dtype=np.uint8), count * FRAME_SAMPLES)

        def late(s):
            return np.random.default_rng(s).exponential(1.0, count) * 20 > 60
        seed = next(s for s in range(5000) if longest_run(late(s)) > REPEAT_LIMIT)
        output, record = drop_late_packets(codes.tobytes(), 'alaw', 20, 60, seed, 'repeat_then_silence')
        self.check_mode(output, record, codes, late(seed), 'alaw',
                        drop_late_packets(codes.tobytes(), 'alaw', 20, 60, seed, 'repeat')[0])

    def test_mode_through_roundtrip(self):
        source = np.random.default_rng(1).integers(-8000, 8000, 16000 * 30).astype('<i2').tobytes()
        clean = np.frombuffer(encode(telephone_roundtrip(source, 'ulaw')[0], 'ulaw'), dtype=np.uint8)
        count = len(clean) // FRAME_SAMPLES
        for i in range(3000):
            loss_plan, jitter_plan = derive_loss(9, f'fictional/{i}'), derive_jitter(9, f'fictional/{i}')
            mask = ((np.random.default_rng(loss_plan['seed']).random(count) < loss_plan['loss_rate'])
                    | (np.random.default_rng(jitter_plan['seed']).exponential(1.0, count)
                       * jitter_plan['jitter_ms'] > jitter_plan['buffer_ms']))
            if longest_run(mask) > REPEAT_LIMIT:
                break
        else:
            self.fail('no clip with a burst longer than the repeat limit')
        decoded, record = telephone_roundtrip(source, 'ulaw', loss_plan=loss_plan, jitter_plan=jitter_plan,
                                              concealment='repeat_then_silence')
        summary = record['packet_concealment']
        self.assertEqual(summary['concealed_indices_sha256'], hashlib.sha256(
            np.flatnonzero(mask).astype('<i4').tobytes()).hexdigest())  # rebuilt mask is the real one
        plain = telephone_roundtrip(source, 'ulaw', loss_plan=loss_plan, jitter_plan=jitter_plan,
                                    concealment='repeat')[0]
        self.assertGreater(longest_run(mask), REPEAT_LIMIT)
        self.assertEqual(decoded, decode(by_hand(clean, mask, 'ulaw'), 'ulaw'))
        self.assertNotEqual(hashlib.sha256(decoded).digest(), hashlib.sha256(plain).digest())
        for part in (summary, record['packet_loss'], record['jitter']):
            self.assertEqual((part['concealment'], part['repeat_limit']), ('repeat_then_silence', 3))

    def test_unknown_mode_still_refused(self):
        with self.assertRaises(ValueError):
            drop_packets(frames(2).tobytes(), 'ulaw', 0.01, 1, 'repeat_then_fade')


if __name__ == '__main__':
    unittest.main()
