"""Claude-authored tests (15 November 2026) for services/tap/jitter.py (backlog 6.3;
PRD FR-I2; architecture §2.2 step 1). Authored by Claude (Claude Code), not by GPT-6
Astra. Pure: explicit arrival times, no sockets.
"""
import unittest

import numpy as np

from data.prepare import codecs as g711
from services.tap.jitter import FRAME_SAMPLES, SILENCE, JitterBuffer, LossMonitor


def frame(value):
    return np.full(FRAME_SAMPLES, value, dtype='<i2').tobytes()


def values(played):
    return [int(np.frombuffer(pcm, dtype='<i2')[0]) for pcm, _ in played]


class JitterBufferTests(unittest.TestCase):
    def test_in_order_with_jitter_inside_the_depth_is_exact(self):
        buffer = JitterBuffer()
        rng = np.random.default_rng(1)
        played = []
        for k in range(100):
            arrival = 10.0 + 0.02 * k + float(rng.uniform(0, 0.055))  # jitter < 60 ms depth
            buffer.push(k, frame(k + 1), arrival)
            played += buffer.pop_due(arrival)
        played += buffer.flush()
        self.assertEqual(values(played), list(range(1, 101)))
        self.assertEqual(sum(c for _, c in played), 0)
        self.assertEqual((buffer.stats['late'], buffer.stats['concealed_frames']), (0, 0))

    def test_playout_timing_starts_after_the_depth(self):
        buffer = JitterBuffer(depth_s=0.06)
        buffer.push(500, frame(1), 1.0)
        self.assertEqual(buffer.pop_due(1.059), [])
        self.assertEqual(values(buffer.pop_due(1.06)), [1])
        buffer.push(501, frame(2), 1.07)
        buffer.push(502, frame(3), 1.075)
        self.assertEqual(values(buffer.pop_due(1.1)), [2, 3])  # due at 1.08 and 1.10
        self.assertEqual(buffer.pop_due(1.1), [])

    def test_reordering_inside_the_depth_is_repaired(self):
        buffer = JitterBuffer()
        for seq, arrival in ((0, 0.0), (2, 0.04), (1, 0.05), (4, 0.08), (3, 0.081)):
            buffer.push(seq, frame(seq + 1), arrival)
        played = buffer.pop_due(0.14)  # frame 4 is due at 0.06 + 4 x 0.02
        self.assertEqual(values(played), [1, 2, 3, 4, 5])
        self.assertEqual(values(buffer.pop_due(0.2)), [5, 5, 5])  # nothing more sent: concealed
        self.assertEqual(buffer.stats['reordered'], 2)
        self.assertEqual(sum(c for _, c in played), 0)

    def test_loss_concealment_matches_the_training_rule(self):
        # Parity with data/prepare/codecs.py repeat_then_silence on mu-law: decoding the
        # concealed codes equals what the buffer plays from decoded frames.
        rng = np.random.default_rng(2)
        codes = rng.integers(0, 256, 40 * FRAME_SAMPLES).astype(np.uint8)
        lost = np.zeros(40, dtype=bool)
        lost[[3, 7, 8, 9, 10, 11, 20, 39]] = True  # single, a run of 5, single, last
        expected = g711.decode(g711._conceal(codes, lost, 'ulaw', 'repeat_then_silence').tobytes(), 'ulaw')
        buffer = JitterBuffer()
        for k in range(40):
            if not lost[k]:
                buffer.push(k, g711.decode(codes[k * FRAME_SAMPLES:(k + 1) * FRAME_SAMPLES].tobytes(), 'ulaw'),
                            0.02 * k)
        played = buffer.pop_due(0.02 * 39 + 0.06)
        self.assertEqual(b''.join(pcm for pcm, _ in played), expected)
        self.assertEqual([c > 0 for _, c in played], list(lost))
        self.assertEqual((buffer.stats['repeated_frames'], buffer.stats['silence_frames']), (6, 2))

    def test_late_duplicate_wrap_and_bound(self):
        buffer = JitterBuffer(max_ahead=50)
        buffer.push(65534, frame(1), 0.0)
        self.assertEqual(buffer.push(65535, frame(2), 0.02), 'accepted')
        self.assertEqual(buffer.push(0, frame(3), 0.04), 'accepted')  # wraps
        self.assertEqual(buffer.push(0, frame(3), 0.041), 'duplicate')
        # Due by 0.2 s: 8 frames; after the 3 received ones, 3 repeats then silence.
        self.assertEqual(values(buffer.pop_due(0.2)), [1, 2, 3, 3, 3, 3, 0, 0])
        self.assertEqual(buffer.push(1, frame(4), 0.2), 'late')  # its frame was already played
        self.assertEqual(buffer.stats['resyncs'], 0)

    def run_stream(self, buffer, packets, until):
        """Push (sequence, value, arrival) packets in arrival order, popping as time
        goes, and return every played frame up to `until`."""
        played = []
        for seq, value, arrival in packets:
            played += buffer.pop_due(arrival)
            buffer.push(seq & 0xFFFF, frame(value), arrival)
        t = packets[-1][2]
        while t < until:
            t += 0.01
            played += buffer.pop_due(t)
        return played

    def test_sender_pause_with_contiguous_sequence_recovers(self):
        # Re-review H-B1: 2 s without packets, then the sender continues seq+1.
        buffer = JitterBuffer()
        packets = [(k, k + 1, 0.02 * k) for k in range(50)]
        packets += [(50 + k, 1000 + k, 3.0 + 0.02 * k) for k in range(100)]
        played = self.run_stream(buffer, packets, 3.0 + 0.02 * 100 + 0.2)
        resumed = values(played)
        self.assertEqual(buffer.stats['resyncs'], 1)
        self.assertEqual(buffer.stats['late'], 0)
        tail = resumed[resumed.index(1000):]
        self.assertEqual(tail[:100], list(range(1000, 1100)), 'every packet after the pause is played')
        self.assertEqual(values(played)[:50], list(range(1, 51)))

    def test_sequence_jumps_forward_and_back_recover(self):
        buffer = JitterBuffer()
        packets = [(k, 1, 0.02 * k) for k in range(20)]
        packets += [(5000 + k, 2, 0.4 + 0.02 * k) for k in range(20)]  # jump forward
        packets += [(100 + k, 3, 0.8 + 0.02 * k) for k in range(20)]  # jump back (sequence reset)
        played = self.run_stream(buffer, packets, 1.3)
        received = [int(np.frombuffer(pcm, dtype='<i2')[0]) for pcm, c in played if c == 0]
        self.assertEqual(buffer.stats['resyncs'], 2)
        self.assertEqual(received, [1] * 20 + [2] * 20 + [3] * 20, 'nothing received is lost at a resync')

    def test_one_very_late_packet_is_late_not_a_resync(self):
        buffer = JitterBuffer()
        for k in range(10):
            buffer.push(k, frame(1), 0.02 * k)
        buffer.pop_due(0.4)
        self.assertEqual(buffer.push(12, frame(9), 0.4), 'late')  # 100 ms past its slot
        self.assertEqual(buffer.push(21, frame(2), 0.41), 'accepted')
        self.assertEqual(buffer.stats['resyncs'], 0)

    def test_clock_drift_of_100_ppm_either_way_over_20_minutes(self):
        for ppm in (100, -100):
            buffer = JitterBuffer()
            rate = 1 + ppm * 1e-6
            frames = 60_000  # 20 minutes
            played = concealed = 0
            for k in range(frames):
                arrival = 0.02 * k * rate
                for _, c in buffer.pop_due(arrival):
                    played += 1
                    concealed += c > 0
                buffer.push(k & 0xFFFF, frame(1), arrival)
            self.assertEqual(buffer.stats['late'], 0, ppm)
            self.assertEqual(buffer.stats['resyncs'], 0, ppm)
            self.assertEqual(concealed, 0, ppm)
            self.assertGreater(buffer.stats['drift_adjustments'], 0, ppm)
            self.assertLessEqual(buffer.buffered, 10, 'latency stays bounded')

    def test_reordering_before_playout_starts_is_kept(self):
        # Re-review M-B1: seq 101 arrives first, seq 100 one millisecond later.
        buffer = JitterBuffer()
        self.assertEqual(buffer.push(101, frame(2), 0.000), 'accepted')
        self.assertEqual(buffer.push(100, frame(1), 0.001), 'accepted')
        self.assertEqual(values(buffer.pop_due(0.1)), [1, 2, 2])  # 100 due at 0.06, 101 at 0.08

    def test_pop_due_is_bounded_and_times_are_checked(self):
        buffer = JitterBuffer()
        buffer.push(0, frame(1), 0.0)
        self.assertEqual(len(buffer.pop_due(3600.0, limit=500)), 500)
        self.assertEqual(len(buffer.pop_due(3600.0, limit=500)), 500)
        for bad in (None, float('nan'), float('inf'), True, '1'):
            with self.assertRaises(ValueError):
                buffer.pop_due(bad)
            with self.assertRaises(ValueError):
                buffer.push(1, frame(1), bad)

    def test_flush_plays_up_to_the_highest_frame(self):
        buffer = JitterBuffer()
        buffer.push(10, frame(1), 0.0)
        buffer.push(13, frame(4), 0.001)
        played = buffer.flush()
        self.assertEqual(values(played), [1, 1, 1, 4])
        self.assertEqual([c for _, c in played], [0, 160, 160, 0])
        self.assertEqual(buffer.flush(), [])

    def test_refusals(self):
        for kwargs in ({'depth_s': -0.1}, {'depth_s': 2}, {'repeat_limit': -1}, {'max_ahead': 0}):
            with self.assertRaises(ValueError):
                JitterBuffer(**kwargs)
        buffer = JitterBuffer()
        for sequence, pcm in ((-1, frame(0)), (65536, frame(0)), (1, b'\x00' * 10), (1, 'pcm')):
            with self.assertRaises(ValueError):
                buffer.push(sequence, pcm, 0.0)
        self.assertEqual(SILENCE, frame(0))


class LossMonitorTests(unittest.TestCase):
    def test_one_report_per_episode_and_only_on_a_full_window(self):
        monitor = LossMonitor(window_frames=100, threshold=0.05)
        events = [monitor.add(True) for _ in range(10)]  # 100 % concealed, window not full
        self.assertEqual(events, [None] * 10)
        events = [monitor.add(False) for _ in range(90)]
        self.assertEqual(events[-1], 'degraded')  # 10 % over a full window
        self.assertEqual([e for e in events if e], ['degraded'])
        events = [monitor.add(False) for _ in range(5)]
        self.assertEqual([e for e in events if e], ['recovered'])  # 5 of 100 = threshold, not above
        self.assertAlmostEqual(monitor.fraction, 0.05)
        with self.assertRaises(ValueError):
            LossMonitor(window_frames=0)
        with self.assertRaises(ValueError):
            LossMonitor(threshold=1.0)


if __name__ == '__main__':
    unittest.main()
