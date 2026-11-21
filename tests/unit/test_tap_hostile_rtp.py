"""Claude-authored tests (16 November 2026): malformed and hostile RTP at the passive
tap (backlog 6.3 "malformed RTP cases"; PRD FR-I1, FR-I2, FR-I10 "rather than queue
unboundedly"; architecture §2.1 RTP receiver, §2.2 step 1 jitter buffer).

Authored by Claude (Claude Code), not by GPT-6 Astra. Seeded fuzzing of the RTP parser;
the jitter buffer's output bound under sequence-jumping streams (the hand-over budget
added 16 Nov after a measured ~124x amplification); RTCP, DTMF and garbage on a real
UDP receiver socket on 127.0.0.1. Pure apart from loopback UDP.
"""
import socket
import struct
import time
import unittest

import numpy as np

from services.tap.jitter import FRAME_S, HANDOVER_BURST_FRAMES, MAX_PENDING_FRAMES, JitterBuffer
from services.tap.rtp_receiver import MAX_DATAGRAM, RtpError, RtpSession, parse_rtp
from tests.unit.test_tap import rtp

FRAME = bytes(320)


def frame(value):
    return np.full(160, value, dtype='<i2').tobytes()


class ParserFuzzTests(unittest.TestCase):
    def check(self, datagram):
        try:
            packet = parse_rtp(datagram)
        except RtpError:
            return None
        self.assertLessEqual(len(packet['payload']), len(datagram) - 12)
        self.assertTrue(0 <= packet['payload_type'] < 128 and 0 <= packet['sequence'] < 65536)
        self.assertIn(packet['payload'], bytes(datagram))
        return packet

    def test_random_datagrams_only_parse_or_raise_rtp_error(self):
        rng = np.random.default_rng(1116)
        parsed = 0
        for _ in range(4000):
            size = int(rng.integers(0, MAX_DATAGRAM + 40))
            datagram = rng.integers(0, 256, size, dtype=np.uint8).tobytes()
            if size and rng.random() < 0.5:  # bias towards version 2 so header paths run
                datagram = bytes([0x80 | (datagram[0] & 0x3F)]) + datagram[1:]
            parsed += self.check(datagram) is not None
        self.assertGreater(parsed, 100, 'the fuzzer reached the accepting paths')

    def test_mutated_valid_packets_only_parse_or_raise_rtp_error(self):
        rng = np.random.default_rng(16)
        bases = [rtp(5), rtp(6, b'abc', pt=8, csrc=3, extension=b'\x00' * 8, padding=4), rtp(7, b'\x01' * 960)]
        for _ in range(4000):
            data = bytearray(bases[int(rng.integers(len(bases)))])
            for _ in range(int(rng.integers(1, 4))):
                choice = rng.random()
                if choice < 0.6 and data:
                    data[int(rng.integers(len(data)))] = int(rng.integers(256))
                elif choice < 0.8:
                    del data[int(rng.integers(len(data) + 1)):]
                else:
                    data += rng.integers(0, 256, int(rng.integers(1, 64)), dtype=np.uint8).tobytes()
            self.check(bytes(data))


class JitterOutputBoundTests(unittest.TestCase):
    """Whatever the sequence numbers, output is bounded by what was sent plus time:
    pop_due plays frames due by now (one drift step per packet at most), received
    frames are played once, and concealment in hand-overs spends a real-time budget.
    Bound used: 2 x (packets + elapsed frames) + the burst (+ a few frames of depth)."""

    def drive(self, sequences, arrivals, restarts=()):
        buffer = JitterBuffer()
        out = []
        for i, (seq, now) in enumerate(zip(sequences, arrivals)):
            if i in restarts:
                buffer.restart(now)
            buffer.push(seq, FRAME, now)
            out += buffer.pop_due(now)
            self.assertLessEqual(buffer.buffered, buffer.max_ahead + 1)
            self.assertLessEqual(len(buffer._pending_out), MAX_PENDING_FRAMES)
        out += buffer.flush()
        for pcm, concealed in out:
            self.assertEqual((len(pcm), concealed in (0, 160)), (320, True))
        return buffer, out

    def bound(self, arrivals):
        elapsed_frames = (arrivals[-1] - arrivals[0]) / FRAME_S + 1
        return 2 * (len(arrivals) + elapsed_frames) + HANDOVER_BURST_FRAMES + 5

    def test_alternating_forward_jumps_cannot_amplify_output(self):
        steps = 500
        arrivals = [i * FRAME_S for i in range(steps)]
        for jumps in ([249, 250], [249], [240, 260, 255], [-60, 249]):
            sequences, seq = [], 1000
            for i in range(steps):
                seq = (seq + jumps[i % len(jumps)]) & 0xFFFF
                sequences.append(seq)
            buffer, out = self.drive(sequences, arrivals)
            self.assertLessEqual(len(out), self.bound(arrivals), jumps)
            self.assertGreater(buffer.stats['dropped_handover'], 0, jumps)

    def test_seeded_random_hostile_streams_stay_bounded(self):
        rng = np.random.default_rng(20261116)
        for trial in range(40):
            steps = int(rng.integers(50, 400))
            gaps = rng.choice([0.0, 0.001, FRAME_S, 0.2, 1.5], size=steps, p=[0.2, 0.2, 0.4, 0.15, 0.05])
            arrivals = list(np.cumsum(gaps) + 5.0)
            sequences = [int(s) for s in rng.integers(0, 65536, steps)]
            for i in range(steps):  # mix in runs and small jumps, which reach the hole paths
                if i and rng.random() < 0.5:
                    sequences[i] = (sequences[i - 1] + int(rng.choice([1, 2, 5, 100, 249, 3000]))) & 0xFFFF
            restarts = {int(i) for i in rng.integers(0, steps, 3)}
            buffer, out = self.drive(sequences, arrivals, restarts)
            self.assertLessEqual(len(out), self.bound(arrivals), f'trial {trial}')

    def test_a_normal_resync_hands_over_everything_buffered(self):
        buffer = JitterBuffer()
        for k in range(10):  # 10 frames buffered with a hole at 5, nothing due yet
            if k != 5:
                buffer.push(100 + k, frame(k + 1), 1.0 + 0.001 * k)
        buffer.push(100 + 9 + 400, frame(99), 1.02)  # sequence jump: resync hands over 100..109
        handed = buffer.pop_due(1.02)
        self.assertEqual([int(np.frombuffer(p, '<i2')[0]) for p, _ in handed], [1, 2, 3, 4, 5, 5, 7, 8, 9, 10])
        self.assertEqual([c for _, c in handed], [0, 0, 0, 0, 0, 160, 0, 0, 0, 0])
        self.assertEqual(buffer.stats['dropped_handover'], 0)

    def test_concealment_spends_the_budget_holes_beyond_it_are_skipped_and_it_refills(self):
        buffer = JitterBuffer()
        buffer.push(0, frame(1), 0.0)
        buffer.push(200, frame(2), 0.0)  # 199-frame hole before it, all inside max_ahead
        buffer.push(1000, frame(3), 0.0)  # jump: hands over 0..200; 50 of the 199 holes concealed
        handed = buffer.pop_due(0.0)
        self.assertEqual((len(handed), sum(1 for _, c in handed if c), buffer.stats['dropped_handover']),
                         (2 + HANDOVER_BURST_FRAMES, HANDOVER_BURST_FRAMES, 199 - 50))
        self.assertEqual((handed[0][1], handed[-1][1]), (0, 0), 'both received frames played')
        buffer.push(1150, frame(4), 0.0)  # 149-frame hole, no time passed: no concealment left
        buffer.push(3000, frame(5), 0.0)
        self.assertEqual([c for _, c in buffer.pop_due(0.0)], [0, 0], 'received frames still played')
        self.assertEqual(buffer.stats['dropped_handover'], 149 + 149)
        buffer.push(3010, frame(6), 0.2)  # 0.2 s later: 10 frames of concealment
        buffer.push(5000, frame(7), 0.2)
        self.assertEqual(len(buffer.pop_due(0.2)), 2 + 9)  # 3000, nine holes, 3010
        buffer.restart(10.0)  # long quiet: refilled to the cap, not beyond; frame 5000 costs nothing
        self.assertEqual((buffer._handover_budget, len(buffer.pop_due(10.0))), (HANDOVER_BURST_FRAMES, 1))
        for bad in (float('nan'), 'now', True, None):
            with self.assertRaises(ValueError):
                buffer.restart(bad)

    def test_a_backlog_after_a_loop_stall_is_never_dropped(self):
        # Review H1 (16 Nov): the tap polls a backlog at one clock value and then flushes,
        # restarts or resyncs; every received frame must come out, none concealed.
        def contiguous(buffer, count, start=0, arrival=1.0):
            for k in range(count):
                buffer.push((start + k) & 0xFFFF, frame(k + 1), arrival)
        for ending in ('flush', 'restart', 'resync'):
            buffer = JitterBuffer()
            contiguous(buffer, 100)  # a 2 s backlog, nothing popped in between
            if ending == 'flush':
                out = buffer.flush()
            elif ending == 'restart':
                buffer.restart(1.0)
                out = buffer.pop_due(1.0)
            else:
                buffer.push(5000, frame(0), 1.0)  # a sequence reset inside the backlog
                out = buffer.pop_due(1.0)
            self.assertEqual(([int(np.frombuffer(p, '<i2')[0]) for p, _ in out], buffer.stats['dropped_handover']),
                             (list(range(1, 101)), 0), ending)
            self.assertTrue(all(c == 0 for _, c in out), ending)
        buffer = JitterBuffer()
        contiguous(buffer, 300)  # a 6 s stall: the stream outruns max_ahead and resyncs itself
        out = buffer.pop_due(1.0) + buffer.flush()
        self.assertEqual((len(out), sum(c for _, c in out), buffer.stats['dropped_handover']), (300, 0, 0))

    def test_a_fast_sender_is_bounded_by_what_it_sends(self):
        # Documents the pop_due side of the bound (drift moves playout one frame per
        # packet); it does not exercise the hand-over budget, which the jump tests guard.
        buffer = JitterBuffer()
        arrivals, out = [], []
        for k in range(1000):  # a packet every 1 ms, sequence step 2 (every other frame a hole)
            now = 1.0 + 0.001 * k
            arrivals.append(now)
            buffer.push((2 * k) & 0xFFFF, FRAME, now)
            out += buffer.pop_due(now)
        out += buffer.flush()
        self.assertLessEqual(len(out), self.bound(arrivals))
        self.assertEqual(sum(1 for _, c in out if not c), 1000, 'every received frame played once')

    def test_time_going_backwards_does_not_refill(self):
        buffer = JitterBuffer()
        buffer.push(0, FRAME, 100.0)
        buffer.push(2, FRAME, 100.0)
        buffer.push(300, FRAME, 100.0)  # jump: 0..2 handed over, the hole at 1 concealed
        buffer.pop_due(100.0)
        spent = buffer._handover_budget
        self.assertEqual(spent, HANDOVER_BURST_FRAMES - 1)
        buffer.pop_due(50.0)
        self.assertEqual(buffer._handover_budget, spent)
        buffer.pop_due(100.02)  # one frame of time after the newest time seen, not after 50
        self.assertAlmostEqual(buffer._handover_budget, spent + 1, places=6)


class HostileUdpTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.session = RtpSession(payload_type=0, host='127.0.0.1', clock=lambda: self.now[0],
                                  jitter=JitterBuffer())
        self.addCleanup(self.session.close)
        self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sender.bind(('127.0.0.1', 0))
        self.addCleanup(self.sender.close)

    def send(self, *packets, sender=None):
        for packet in packets:
            (sender or self.sender).sendto(packet, ('127.0.0.1', self.session.port))

    def poll_until(self, predicate, seconds=5):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            self.session.poll(0.05)
        return predicate()

    def test_rtcp_dtmf_and_garbage_neither_play_nor_lock_the_source(self):
        rtcp_sr = struct.pack('!BBHI', 0x80, 200, 6, 7) + bytes(20)  # sender report, PT 200
        rtcp_rr = struct.pack('!BBHI', 0x81, 201, 7, 7) + bytes(24)
        dtmf = rtp(1, b'\x01\x0a\x00\xa0', pt=101)  # RFC 4733 event
        stranger = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        stranger.bind(('127.0.0.1', 0))
        self.addCleanup(stranger.close)
        self.send(rtcp_sr, rtcp_rr, dtmf, b'', b'\x80' * 11, sender=stranger)
        self.assertTrue(self.poll_until(lambda: self.session.stats['other_payload'] + self.session.stats['malformed']
                                        >= 4))
        self.assertIsNone(self.session.source, 'nothing but a valid G.711 packet locks the source')
        self.send(rtp(10))
        self.assertTrue(self.poll_until(lambda: self.session.stats['packets'] == 1))
        self.assertEqual(self.session.source, self.sender.getsockname())
        self.assertEqual(self.session.stats['other_payload'], 3)  # SR, RR, DTMF

    def test_a_jumping_sender_through_the_receiver_stays_near_real_time(self):
        seq, out, steps = 0, 0, 200
        for i in range(steps):
            self.now[0] = 100.0 + i * FRAME_S
            seq = (seq + (249 if i % 2 else 250)) & 0xFFFF
            ssrc = 7 if i % 50 else 7 + i  # an SSRC change now and then (restart path)
            self.send(rtp(seq, ssrc=ssrc))
            handled = i + 1
            self.assertTrue(self.poll_until(lambda: self.session.stats['packets']
                                            + self.session.stats['late_or_duplicate'] >= handled))
            out += len(self.session.take_frames())
        out += len(self.session.take_frames(final=True))
        self.assertLessEqual(out, 2 * (steps + steps) + HANDOVER_BURST_FRAMES + 5)  # packets + elapsed frames
        self.assertGreater(self.session.receiver_stats()['jitter_dropped_handover'], 0)


if __name__ == '__main__':
    unittest.main()
