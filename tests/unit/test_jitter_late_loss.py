"""Seeded jitter as late-packet loss behind a fixed playout buffer (backlog 3.3 partial;
PRD FR-D6 jitter, FR-I2 jitter buffer).

Claude-authored test cases, 9 November 2026. Infrastructure tests on generated codes.
"""
import hashlib
import math
import unittest

import numpy as np

from data.prepare.codecs import (FRAME_SAMPLES, LAWS, MAX_BUFFER_MS, MAX_JITTER_MS,
                                 drop_late_packets, drop_packets)

SILENCE = {'ulaw': 0xFF, 'alaw': 0xD5}


def frames(count, extra=0):
    body = np.repeat(np.arange(1, count + 1, dtype=np.uint8), FRAME_SAMPLES)
    return np.concatenate([body, np.full(extra, 0xAB, dtype=np.uint8)]).tobytes()


def packet_values(output):
    codes = np.frombuffer(output, dtype=np.uint8)
    return [int(codes[i]) for i in range(0, len(codes), FRAME_SAMPLES)]


class DropLatePacketsTests(unittest.TestCase):
    def test_zero_jitter_is_identity(self):
        encoded = frames(40, extra=11)
        for law in LAWS:
            with self.subTest(law=law):
                output, record = drop_late_packets(encoded, law, 0, 60, 3)
                self.assertEqual(output, encoded)
                self.assertEqual(record['packets'], 41)
                self.assertEqual(record['late_packets'], 0)
                self.assertEqual(record['late_indices_sha256'], hashlib.sha256(b'').hexdigest())

    def test_late_mask_matches_independent_exponential_draw_and_concealment(self):
        count, jitter, buffer, seed = 200, 20.0, 60.0, 11  # packet IDs must fit in uint8
        encoded = frames(count)
        expected_late = np.random.default_rng(seed).exponential(1.0, count) * jitter > buffer
        self.assertTrue(expected_late.any() and not expected_late.all())
        for concealment in ('repeat', 'silence'):
            with self.subTest(concealment=concealment):
                output, record = drop_late_packets(encoded, 'alaw', jitter, buffer, seed, concealment)
                values, previous = packet_values(output), None
                for index in range(count):
                    if not expected_late[index]:
                        self.assertEqual(values[index], index + 1)
                        previous = index + 1
                    elif concealment == 'repeat' and previous is not None:
                        self.assertEqual(values[index], previous)
                    else:
                        self.assertEqual(values[index], SILENCE['alaw'])
                self.assertEqual(record['late_packets'], int(expected_late.sum()))
                self.assertEqual(record['late_indices_sha256'], hashlib.sha256(
                    np.flatnonzero(expected_late).astype('<i4').tobytes()).hexdigest())

    def test_late_fraction_tracks_exponential_tail(self):
        count, jitter, buffer = 20000, 30.0, 90.0
        _, record = drop_late_packets(frames(1) * count, 'ulaw', jitter, buffer, 5)
        expected = math.exp(-buffer / jitter)  # P(delay > buffer) for exponential delay
        self.assertAlmostEqual(record['late_packets'] / count, expected, delta=0.01)

    def test_larger_buffer_never_adds_late_packets(self):
        encoded = frames(500)
        late = [drop_late_packets(encoded, 'ulaw', 20, b, 9)[1]['late_packets'] for b in (60, 80, 120, 400)]
        self.assertEqual(late, sorted(late, reverse=True))
        self.assertGreater(late[0], late[-1])

    def test_reproducible_and_record_complete(self):
        encoded = frames(200)
        first = drop_late_packets(encoded, 'ulaw', 20, 60, 7)
        self.assertGreater(first[1]['late_packets'], 0)
        self.assertEqual(first, drop_late_packets(encoded, 'ulaw', 20, 60, 7))
        self.assertNotEqual(first[0], drop_late_packets(encoded, 'ulaw', 20, 60, 8)[0])
        record = first[1]
        for key in ('jitter_ms', 'buffer_ms', 'seed', 'model', 'rng', 'numpy_version',
                    'packet_ms', 'concealment'):
            self.assertIn(key, record)
        self.assertEqual((record['jitter_ms'], record['buffer_ms'], record['seed']), (20.0, 60.0, 7))
        self.assertAlmostEqual(record['expected_late_fraction'], math.exp(-3))
        self.assertIn('not RFC 3550', record['jitter_definition'])

    def test_settings_beyond_frd6_loss_bound_refused(self):
        for jitter, buffer in ((20, 59), (100, 20), (1, 0), (50, 0)):
            with self.subTest(jitter=jitter, buffer=buffer):
                with self.assertRaisesRegex(ValueError, 'loss bound'):
                    drop_late_packets(frames(5), 'ulaw', jitter, buffer, 1)
        drop_late_packets(frames(5), 'ulaw', 20, 60, 1)  # exp(-3) < 0.05 is accepted

    def test_shared_concealment_keeps_packet_loss_behaviour(self):
        """Full expected output, built independently, for drop_packets after the refactor."""
        count = 120
        encoded = frames(count, extra=5)
        codes = np.frombuffer(encoded, dtype=np.uint8)
        for seed in range(40):
            lost = np.random.default_rng(seed).random(count + 1) < 0.05
            if lost[0] and lost[-1]:
                break
        else:
            seed = 7
            lost = np.random.default_rng(seed).random(count + 1) < 0.05
        for concealment in ('repeat', 'silence'):
            with self.subTest(concealment=concealment, seed=seed):
                output, record = drop_packets(encoded, 'ulaw', 0.05, seed, concealment)
                expected, previous = bytearray(encoded), None
                for index in range(count + 1):
                    start, end = index * FRAME_SAMPLES, min((index + 1) * FRAME_SAMPLES, len(codes))
                    if not lost[index]:
                        previous = codes[start:end]
                    elif concealment == 'repeat' and previous is not None:
                        expected[start:end] = bytes(np.resize(previous, end - start))
                    else:
                        expected[start:end] = bytes([SILENCE['ulaw']]) * (end - start)
                self.assertEqual(output, bytes(expected))
                self.assertEqual(record['lost_packets'], int(lost.sum()))

    def test_invalid_arguments_refused(self):
        encoded = frames(10)
        cases = [
            ('jitter_negative', dict(jitter_ms=-1)), ('jitter_over', dict(jitter_ms=MAX_JITTER_MS + 1)),
            ('jitter_nan', dict(jitter_ms=float('nan'))), ('jitter_bool', dict(jitter_ms=True)),
            ('buffer_negative', dict(buffer_ms=-0.5)), ('buffer_over', dict(buffer_ms=MAX_BUFFER_MS + 1)),
            ('buffer_inf', dict(buffer_ms=float('inf'))), ('buffer_str', dict(buffer_ms='60')),
            ('seed_none', dict(seed=None)), ('seed_negative', dict(seed=-1)), ('seed_float', dict(seed=1.0)),
            ('concealment', dict(concealment='fade')), ('law', dict(law='gsm')),
        ]
        for name, change in cases:
            with self.subTest(name):
                kwargs = dict(encoded=encoded, law='ulaw', jitter_ms=20, buffer_ms=60, seed=1)
                kwargs.update(change)
                with self.assertRaises(ValueError):
                    drop_late_packets(**kwargs)


if __name__ == '__main__':
    unittest.main()
