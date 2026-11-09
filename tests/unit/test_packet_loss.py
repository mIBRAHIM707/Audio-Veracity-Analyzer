"""Seeded G.711 packet loss and concealment (backlog 3.3 partial; PRD FR-D6).

Claude-authored test cases, 9 November 2026. Infrastructure tests on generated codes.
"""
import hashlib
import unittest

import numpy as np

from data.prepare.codecs import (FRAME_SAMPLES, MAX_LOSS_RATE, LAWS, decode, drop_packets, encode,
                                 telephone_roundtrip)

SILENCE = {'ulaw': 0xFF, 'alaw': 0xD5}


def frames(count, extra=0):
    """Every packet carries a distinct code value so concealment sources are identifiable."""
    body = np.repeat(np.arange(1, count + 1, dtype=np.uint8), FRAME_SAMPLES)
    return np.concatenate([body, np.full(extra, 0xAB, dtype=np.uint8)]).tobytes()


def lost_mask(record_seed, rate, count):
    return np.random.default_rng(record_seed).random(count) < rate


class DropPacketsTests(unittest.TestCase):
    def test_zero_loss_is_identity_with_complete_record(self):
        encoded = frames(50, extra=37)
        for law in LAWS:
            with self.subTest(law=law):
                output, record = drop_packets(encoded, law, 0.0, 1)
                self.assertEqual(output, encoded)
                self.assertEqual(record['packets'], 51)  # trailing 37-sample packet counts
                self.assertEqual(record['lost_packets'], 0)
                self.assertEqual(record['lost_indices_sha256'], hashlib.sha256(b'').hexdigest())

    def test_same_seed_reproduces_and_different_seed_differs(self):
        encoded = frames(200)
        first = drop_packets(encoded, 'ulaw', 0.05, 7)
        self.assertEqual(first, drop_packets(encoded, 'ulaw', 0.05, 7))
        others = {drop_packets(encoded, 'ulaw', 0.05, seed)[1]['lost_indices_sha256'] for seed in range(8, 13)}
        self.assertNotIn(first[1]['lost_indices_sha256'], others)

    def test_silence_concealment_replaces_exactly_the_lost_packets(self):
        encoded = frames(300)
        for law in LAWS:
            with self.subTest(law=law):
                output, record = drop_packets(encoded, law, 0.05, 3, concealment='silence')
                mask = lost_mask(3, 0.05, 300)
                self.assertEqual(record['lost_packets'], int(mask.sum()))
                self.assertGreater(record['lost_packets'], 0)
                got = np.frombuffer(output, dtype=np.uint8).reshape(300, FRAME_SAMPLES)
                original = np.frombuffer(encoded, dtype=np.uint8).reshape(300, FRAME_SAMPLES)
                np.testing.assert_array_equal(got[~mask], original[~mask])
                self.assertTrue(np.all(got[mask] == SILENCE[law]))
                # Silence code decodes to digital zero (mu-law 0xFF) or the smallest A-law level.
                self.assertLessEqual(int(np.max(np.abs(np.frombuffer(decode(bytes([SILENCE[law]]), law), '<i2')))), 8)

    def test_repeat_concealment_copies_last_received_packet_through_bursts(self):
        encoded = frames(400, extra=50)
        output, record = drop_packets(encoded, 'alaw', 0.05, 11, concealment='repeat')
        count = record['packets']
        mask = lost_mask(11, 0.05, count)
        self.assertTrue(np.any(mask[1:] & mask[:-1]), 'fixture seed must contain a burst')
        codes = np.frombuffer(output, dtype=np.uint8)
        last_received = None
        for index in range(count):
            packet = codes[index * FRAME_SAMPLES:(index + 1) * FRAME_SAMPLES]
            source = np.frombuffer(encoded, dtype=np.uint8)[index * FRAME_SAMPLES:(index + 1) * FRAME_SAMPLES]
            if not mask[index]:
                np.testing.assert_array_equal(packet, source)
                last_received = source
            elif last_received is None:
                self.assertTrue(np.all(packet == SILENCE['alaw']))
            else:
                np.testing.assert_array_equal(packet, np.resize(last_received, len(packet)))
        self.assertEqual(len(output), len(encoded))

    def test_loss_rate_close_to_requested_over_many_packets(self):
        _, record = drop_packets(frames(250) * 40, 'ulaw', 0.05, 5)  # 10,000 packets
        self.assertAlmostEqual(record['lost_packets'] / record['packets'], 0.05, delta=0.01)

    def test_first_packet_lost_with_repeat_falls_back_to_silence(self):
        seed = next(s for s in range(10_000) if lost_mask(s, 0.05, 3)[0])
        output, _ = drop_packets(frames(3), 'ulaw', 0.05, seed, concealment='repeat')
        self.assertTrue(all(code == SILENCE['ulaw'] for code in output[:FRAME_SAMPLES]))

    def test_loss_pattern_is_pinned_across_library_changes(self):
        # Golden values (numpy 9 Nov 2026): a changed RNG stream must fail here, because
        # recorded per-clip chains would otherwise stop reproducing silently.
        _, record = drop_packets(frames(250) * 4, 'ulaw', 0.05, 7)
        self.assertEqual(record['packets'], 1000)
        self.assertEqual(record['lost_packets'], 52)
        self.assertEqual(record['lost_indices_sha256'],
                         'b6995c8cc0c870a0c7fbc228ac6409278f83fe004afdc6f7b695f63bbcb105e6')
        self.assertEqual(np.flatnonzero(lost_mask(7, 0.05, 1000))[:6].tolist(), [6, 23, 24, 32, 37, 90])
        self.assertIn('default_rng', record['rng'])

    def test_lost_short_final_packet_repeats_truncated_previous_packet(self):
        count = 41
        seed = next(s for s in range(100_000)
                    if (m := lost_mask(s, 0.05, count))[-1] and not m[-2])
        encoded = frames(40, extra=50)
        output, record = drop_packets(encoded, 'alaw', 0.05, seed, concealment='repeat')
        self.assertEqual(record['packets'], count)
        self.assertEqual(output[-50:], encoded[-FRAME_SAMPLES - 50:-50][:50])

    def test_invalid_arguments_are_rejected(self):
        good = frames(2)
        cases = [
            ((good, 'ulaw', -0.01, 1), {}), ((good, 'ulaw', MAX_LOSS_RATE + 1e-9, 1), {}),
            ((good, 'ulaw', True, 1), {}), ((good, 'ulaw', '0.01', 1), {}),
            ((good, 'ulaw', float('nan'), 1), {}),
            ((good, 'ulaw', 0.01, None), {}), ((good, 'ulaw', 0.01, -1), {}), ((good, 'ulaw', 0.01, 1.0), {}),
            ((good, 'ulaw', 0.01, 1), {'concealment': 'interpolate'}),
            ((b'', 'ulaw', 0.01, 1), {}), ((bytearray(good), 'ulaw', 0.01, 1), {}),
            ((good, 'g729', 0.01, 1), {}),
        ]
        for args, kwargs in cases:
            with self.subTest(args=args[1:], kwargs=kwargs):
                with self.assertRaises(ValueError):
                    drop_packets(*args, **kwargs)


class RoundtripLossTests(unittest.TestCase):
    def test_roundtrip_records_loss_and_matches_explicit_chain(self):
        t = np.arange(16000 * 3) / 16000
        source = np.rint(9000 * np.sin(2 * np.pi * 440 * t)).astype('<i2').tobytes()
        clean, clean_record = telephone_roundtrip(source, 'ulaw')
        self.assertIsNone(clean_record['packet_loss'])
        lossy, record = telephone_roundtrip(source, 'ulaw', loss_rate=0.05, seed=21, concealment='silence')
        self.assertEqual(record['packet_loss']['seed'], 21)
        self.assertEqual(record['packet_loss']['packets'], 150)
        self.assertEqual(record['encoded_sha256'], clean_record['encoded_sha256'])  # loss after encode
        # Re-encoding decoded G.711 levels returns the original codes (see the codec tests).
        self.assertEqual(hashlib.sha256(encode(clean, 'ulaw')).hexdigest(), record['encoded_sha256'])
        expected, _ = drop_packets(encode(clean, 'ulaw'), 'ulaw', 0.05, 21, concealment='silence')
        self.assertEqual(lossy, decode(expected, 'ulaw'))
        self.assertEqual(len(lossy), len(clean))
        self.assertGreater(record['packet_loss']['lost_packets'], 0)  # seed 21 drops packets
        self.assertNotEqual(lossy, clean)

    def test_roundtrip_rejects_loss_without_seed_and_unknown_concealment(self):
        silence = np.zeros(1600, dtype='<i2').tobytes()
        with self.assertRaises(ValueError):
            telephone_roundtrip(silence, 'alaw', loss_rate=0.01)
        with self.assertRaises(ValueError):  # validated even when no loss is requested
            telephone_roundtrip(silence, 'alaw', concealment='interpolate')

    def test_seed_without_loss_records_zero_lost_packets(self):
        silence = np.zeros(1600, dtype='<i2').tobytes()
        decoded, record = telephone_roundtrip(silence, 'ulaw', seed=4)
        self.assertEqual(record['packet_loss']['lost_packets'], 0)
        self.assertEqual(decoded, telephone_roundtrip(silence, 'ulaw')[0])


if __name__ == '__main__':
    unittest.main()
