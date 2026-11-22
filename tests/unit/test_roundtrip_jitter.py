"""Per-clip jitter plans in the G.711 telephone roundtrip, combined with packet loss
into one lost-or-late mask (backlog 3.3 partial; PRD FR-D6 loss and jitter, FR-D13
chain recorded per clip).

Claude-authored test cases, 10 November 2026. Infrastructure tests on generated tones;
they say nothing about detector accuracy.
"""
import hashlib
import unittest

import numpy as np

from data.prepare.codecs import (FRAME_SAMPLES, decode, derive_jitter, derive_loss, drop_late_packets,
                                 drop_packets, encode, telephone_roundtrip)

SILENCE = {'ulaw': 0xFF, 'alaw': 0xD5}
# Regression pin recorded 10 Nov 2026 with NumPy's default_rng (PCG64): 1500 packets,
# mean delay 20 ms, 60 ms buffer, seed 12345 -> 63 late (4.2 %; expected exp(-3) = 4.98 %).
PINNED_LATE = (63, '21466f7627029d280659b0fa6613ebcc1bb0a14389b27bd25ec490ce08c6b976')


def tone(seconds, rate=16000, freq=440, amplitude=9000):
    t = np.arange(int(rate * seconds)) / rate
    return np.rint(amplitude * np.sin(2 * np.pi * freq * t)).astype('<i2').tobytes()


def loss_mask(plan, count):
    return np.random.default_rng(plan['seed']).random(count) < plan['loss_rate']


def late_mask(plan, count):
    delays = np.random.default_rng(plan['seed']).exponential(1.0, count) * plan['jitter_ms']
    return delays > plan['buffer_ms']


def conceal_by_hand(codes, missing, law, concealment):
    """Independent per-packet expectation: a missing packet repeats the last packet that
    arrived (truncated for a short final packet) or becomes the silence code."""
    out, previous = bytearray(codes), None
    for index, gone in enumerate(missing):
        start, end = index * FRAME_SAMPLES, min((index + 1) * FRAME_SAMPLES, len(codes))
        if not gone:
            previous = bytes(codes[start:end])
        elif concealment == 'repeat' and previous is not None:
            out[start:end] = (previous * 2)[:end - start]
        else:
            out[start:end] = bytes([SILENCE[law]]) * (end - start)
    return bytes(out)


def find_overlapping_clip(count, master_seed=3):
    """First clip ID whose plans give lost-and-late packets and, in both orders, a
    packet missing for one reason directly followed by one missing only for the other.
    Only that adjacency separates one union concealment from two sequential ones."""
    for i in range(5000):
        clip = f'fictional/clip-{i:04d}'
        lp, jp = derive_loss(master_seed, clip), derive_jitter(master_seed, clip)
        lost, late = loss_mask(lp, count), late_mask(jp, count)
        late_then_lost = late[:-1] & ~lost[:-1] & lost[1:] & ~late[1:]
        lost_then_late = lost[:-1] & ~late[:-1] & late[1:] & ~lost[1:]
        if (lost & late).any() and late_then_lost.any() and lost_then_late.any():
            return lp, jp, lost, late
    raise AssertionError('no clip with overlapping and adjacent loss and jitter in 5000 IDs')


class RoundtripJitterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Seeded noise, not a tone: a tone with a whole number of periods per 20 ms packet
        # makes every packet identical, so repeat concealment would be invisible.
        cls.source = np.random.default_rng(0).integers(-8000, 8000, 16000 * 30).astype('<i2').tobytes()
        cls.clean = {law: telephone_roundtrip(cls.source, law) for law in ('ulaw', 'alaw')}
        cls.count = 1500  # packets at 8 kHz

    def test_fixture_packets_all_differ(self):
        codes = np.frombuffer(encode(self.clean['ulaw'][0], 'ulaw'), dtype=np.uint8).reshape(-1, FRAME_SAMPLES)
        self.assertEqual(len(np.unique(codes, axis=0)), self.count)

    def test_no_impairment_keeps_none_records(self):
        decoded, record = self.clean['ulaw']
        self.assertIsNone(record['jitter'])
        self.assertIsNone(record['packet_loss'])
        self.assertIsNone(record['packet_concealment'])
        self.assertEqual(record['sample_count'], 8000 * 30)

    def test_jitter_plan_alone_matches_drop_late_packets_and_is_recorded(self):
        for i in range(200):
            plan = derive_jitter(11, f'j-{i}')
            if late_mask(plan, self.count).sum() >= 3:
                break
        else:
            self.fail('no jitter plan with late packets')
        clean, _ = self.clean['alaw']
        decoded, record = telephone_roundtrip(self.source, 'alaw', jitter_plan=plan)
        expected, late_record = drop_late_packets(encode(clean, 'alaw'), 'alaw', plan['jitter_ms'],
                                                  plan['buffer_ms'], plan['seed'])
        self.assertEqual(decoded, decode(expected, 'alaw'))
        jitter = record['jitter']
        for key in ('derivation', 'master_seed', 'clip_id', 'jitter_ms', 'buffer_ms', 'seed'):
            self.assertEqual(jitter[key], plan[key])
        for key in ('late_packets', 'late_indices_sha256', 'packets', 'model', 'concealment'):
            self.assertEqual(jitter[key], late_record[key])
        self.assertIsNone(record['packet_loss'])
        summary = record['packet_concealment']
        self.assertEqual(summary['concealed_packets'], jitter['late_packets'])
        self.assertEqual(summary['concealed_indices_sha256'], jitter['late_indices_sha256'])
        self.assertIsNone(summary['lost_and_late_packets'])  # not applicable: no loss requested
        self.assertEqual(summary['concealed_fraction'], jitter['late_packets'] / self.count)
        self.assertEqual(jitter['applied_via'], 'packet_concealment')
        rebuilt = derive_jitter(jitter['master_seed'], jitter['clip_id'])  # from the record alone
        self.assertEqual(telephone_roundtrip(self.source, 'alaw', jitter_plan=rebuilt,
                                             concealment=jitter['concealment'])[0], decoded)

    def test_loss_only_summary_matches_loss_record(self):
        plan = derive_loss(5, 'fictional/loss-only')
        clean, _ = self.clean['ulaw']
        decoded, record = telephone_roundtrip(self.source, 'ulaw', loss_plan=plan)
        expected, _ = drop_packets(encode(clean, 'ulaw'), 'ulaw', plan['loss_rate'], plan['seed'])
        self.assertEqual(decoded, decode(expected, 'ulaw'))
        self.assertIsNone(record['jitter'])
        summary = record['packet_concealment']
        self.assertEqual(summary['concealed_packets'], record['packet_loss']['lost_packets'])
        self.assertEqual(summary['concealed_indices_sha256'], record['packet_loss']['lost_indices_sha256'])
        self.assertIsNone(summary['lost_and_late_packets'])

    def test_combined_union_differs_from_sequential_concealment(self):
        """Concealing loss and then jitter (or the reverse) repeats an already concealed
        packet next to an adjacent gap; the union repeats only packets that arrived."""
        loss_plan, jitter_plan, _, _ = find_overlapping_clip(self.count)
        codes = encode(self.clean['ulaw'][0], 'ulaw')
        lr, ls = loss_plan['loss_rate'], loss_plan['seed']
        jm, jb, js = jitter_plan['jitter_ms'], jitter_plan['buffer_ms'], jitter_plan['seed']
        loss_first = drop_late_packets(drop_packets(codes, 'ulaw', lr, ls)[0], 'ulaw', jm, jb, js)[0]
        jitter_first = drop_packets(drop_late_packets(codes, 'ulaw', jm, jb, js)[0], 'ulaw', lr, ls)[0]
        union, _ = telephone_roundtrip(self.source, 'ulaw', loss_plan=loss_plan, jitter_plan=jitter_plan)
        self.assertNotEqual(hashlib.sha256(union).hexdigest(),
                            hashlib.sha256(decode(loss_first, 'ulaw')).hexdigest())
        self.assertNotEqual(hashlib.sha256(union).hexdigest(),
                            hashlib.sha256(decode(jitter_first, 'ulaw')).hexdigest())

    def test_combined_chain_rebuilds_from_record_alone(self):
        loss_plan, jitter_plan, _, _ = find_overlapping_clip(self.count)
        decoded, record = telephone_roundtrip(self.source, 'alaw', loss_plan=loss_plan,
                                              jitter_plan=jitter_plan, concealment='silence')
        loss, jitter = record['packet_loss'], record['jitter']
        again, again_record = telephone_roundtrip(
            self.source, 'alaw', loss_plan=derive_loss(loss['master_seed'], loss['clip_id']),
            jitter_plan=derive_jitter(jitter['master_seed'], jitter['clip_id']),
            concealment=record['packet_concealment']['concealment'])
        self.assertEqual(again, decoded)
        self.assertEqual(again_record, record)

    def test_pinned_late_pattern(self):
        """Golden values so a NumPy or derivation change cannot pass unnoticed."""
        _, record = drop_late_packets(bytes(FRAME_SAMPLES * 1500), 'ulaw', 20, 60, 12345)
        self.assertEqual((record['late_packets'], record['late_indices_sha256']), PINNED_LATE)

    def test_combined_mask_is_the_union_concealed_once(self):
        loss_plan, jitter_plan, lost, late = find_overlapping_clip(self.count)
        missing = lost | late
        for law in ('ulaw', 'alaw'):
            codes = encode(self.clean[law][0], law)
            for concealment in ('repeat', 'silence'):
                with self.subTest(law=law, concealment=concealment):
                    decoded, record = telephone_roundtrip(self.source, law, loss_plan=loss_plan,
                                                          jitter_plan=jitter_plan, concealment=concealment)
                    expected = conceal_by_hand(codes, missing, law, concealment)
                    self.assertEqual(decoded, decode(expected, law))
                    summary = record['packet_concealment']
                    self.assertEqual(summary['packets'], self.count)
                    self.assertEqual(summary['concealed_packets'], int(missing.sum()))
                    self.assertEqual(summary['concealed_fraction'], int(missing.sum()) / self.count)
                    self.assertEqual(summary['lost_and_late_packets'], int((lost & late).sum()))
                    self.assertEqual(summary['concealed_packets'],
                                     record['packet_loss']['lost_packets'] + record['jitter']['late_packets']
                                     - summary['lost_and_late_packets'])
                    self.assertEqual(summary['concealed_indices_sha256'], hashlib.sha256(
                        np.flatnonzero(missing).astype('<i4').tobytes()).hexdigest())
                    self.assertEqual((summary['mask'], summary['concealment']), ('lost_or_late', concealment))
                    self.assertEqual(record['packet_loss']['lost_packets'], int(lost.sum()))
                    self.assertEqual(record['jitter']['late_packets'], int(late.sum()))

    def test_combined_differs_from_each_impairment_alone(self):
        loss_plan, jitter_plan, _, _ = find_overlapping_clip(self.count)
        def digest(**plans):
            return telephone_roundtrip(self.source, 'ulaw', **plans)[1]['decoded_pcm_sha256']
        both = digest(loss_plan=loss_plan, jitter_plan=jitter_plan)
        self.assertNotEqual(both, digest(loss_plan=loss_plan))
        self.assertNotEqual(both, digest(jitter_plan=jitter_plan))
        self.assertNotEqual(both, self.clean['ulaw'][1]['decoded_pcm_sha256'])

    def test_short_final_packet_in_combined_mask(self):
        source = tone(1, rate=8000)[:2 * (FRAME_SAMPLES * 9 + 37)]  # 9 full packets + 37 samples
        count = 10
        for i in range(20000):
            clip = f'short-{i}'
            lp, jp = derive_loss(1, clip), derive_jitter(1, clip)
            lost, late = loss_mask(lp, count), late_mask(jp, count)
            if late[-1] and not lost[-1] and not (lost | late)[-2]:
                break
        else:
            self.fail('no plan pair with a late short final packet')
        decoded, record = telephone_roundtrip(source, 'ulaw', rate=8000, loss_plan=lp, jitter_plan=jp)
        codes = encode(source, 'ulaw')
        self.assertEqual(decoded, decode(conceal_by_hand(codes, lost | late, 'ulaw', 'repeat'), 'ulaw'))
        self.assertEqual(record['packet_concealment']['packets'], count)
        self.assertEqual(len(decoded), len(source))

    def test_raw_loss_arguments_combine_with_jitter_plan(self):
        _, jitter_plan, _, late = find_overlapping_clip(self.count)
        lost = np.random.default_rng(21).random(self.count) < 0.05
        codes = encode(self.clean['alaw'][0], 'alaw')
        decoded, record = telephone_roundtrip(self.source, 'alaw', loss_rate=0.05, seed=21,
                                              jitter_plan=jitter_plan, concealment='silence')
        self.assertEqual(decoded, decode(conceal_by_hand(codes, lost | late, 'alaw', 'silence'), 'alaw'))
        self.assertNotIn('derivation', record['packet_loss'])
        self.assertEqual(record['jitter']['derivation'], 'ava-jitter-v1')

    def test_jitter_plan_tampering_and_mismatch_refused(self):
        silence = np.zeros(1600, dtype='<i2').tobytes()
        plan = derive_jitter(1, 'a')
        bad_plans = ({**plan, 'jitter_ms': 20}, {**plan, 'buffer_ms': 400}, {**plan, 'seed': 1},
                     {**plan, 'master_seed': 2}, {**plan, 'master_seed': -1},
                     {k: v for k, v in plan.items() if k != 'clip_id'}, {**plan, 'extra': 1},
                     derive_loss(1, 'a'), [plan], 'plan')
        for bad in bad_plans:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    telephone_roundtrip(silence, 'ulaw', jitter_plan=bad)
        with self.assertRaisesRegex(ValueError, 'different clips'):
            telephone_roundtrip(silence, 'ulaw', loss_plan=derive_loss(1, 'b'), jitter_plan=plan)
        with self.assertRaisesRegex(ValueError, 'master seeds'):
            telephone_roundtrip(silence, 'ulaw', loss_plan=derive_loss(2, 'a'), jitter_plan=plan)
        for loss_rate, seed in ((0.2, 1), (0.01, None), (0.01, -1)):
            with self.subTest(loss_rate=loss_rate, seed=seed):
                with self.assertRaisesRegex(ValueError, 'loss_rate|seed'):
                    telephone_roundtrip(b'\x00', 'ulaw', loss_rate=loss_rate, seed=seed,
                                        jitter_plan=plan)  # refused before the odd-length input
        with self.assertRaisesRegex(ValueError, 'derive_loss'):  # a jitter plan is not a loss plan
            telephone_roundtrip(silence, 'ulaw', loss_plan=plan)
        with self.assertRaises(ValueError):
            telephone_roundtrip(silence, 'ulaw', jitter_plan=plan, concealment='fade')

    def test_combined_chain_is_reproducible(self):
        loss_plan, jitter_plan, _, _ = find_overlapping_clip(self.count)
        first = telephone_roundtrip(self.source, 'alaw', loss_plan=loss_plan, jitter_plan=jitter_plan)
        self.assertEqual(first, telephone_roundtrip(self.source, 'alaw', loss_plan=loss_plan,
                                                    jitter_plan=jitter_plan))


if __name__ == '__main__':
    unittest.main()
