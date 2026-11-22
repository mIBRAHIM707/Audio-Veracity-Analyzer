"""Claude-authored tests (18 November 2026) for the jitter-buffer hold time (backlog
7.2; PRD FR-O6 "latency per stage"; architecture §3 latency budget "RTP arrival ->
frame available", 60 ms): services/tap/jitter.py `hold_*` counts, the receiver passing
its clock at the end of a call, and `ava_live_jitter_hold_seconds` on the live
`/metrics` (services/live/health.py).

Authored by Claude (Claude Code), not by GPT-6 Astra. Times are explicit (the buffer is
clock-explicit; the receiver gets a manual clock), so every expected hold is exact to
the microsecond the counts use.
"""
import struct
import unittest

from data.prepare import codecs as g711
from services.live.health import HealthServer
from services.tap.jitter import FRAME_S, HOLD_BUCKETS_MS, HOLD_KEYS, JitterBuffer
from services.tap.rtp_receiver import RtpSession
from tests.unit.test_live_health import ManualClock, make_service, samples


def frame(code):
    return g711.decode(bytes([code]) * 160, 'ulaw')


def holds(buffer):
    """{bucket key or total: count} of the hold stats, for compact assertions."""
    return {key: buffer.stats[key] for key in (*HOLD_KEYS, 'hold_frames', 'hold_us_sum', 'hold_unmeasured')}


def expected(holds_us, unmeasured=0):
    """The stats a list of exact holds (microseconds) must produce."""
    out = {key: sum(1 for h in holds_us if h <= ms * 1000) for ms, key in zip(HOLD_BUCKETS_MS, HOLD_KEYS)}
    out.update({'hold_frames': len(holds_us), 'hold_us_sum': sum(holds_us), 'hold_unmeasured': unmeasured})
    return out


class HoldCountTests(unittest.TestCase):
    def test_a_steady_stream_holds_each_frame_for_the_depth(self):
        # 50 frames, one every 20 ms, popped as each arrives: frame k is due 60 ms after it
        # arrived, so 47 frames come out of pop_due after exactly 60 ms; the last 3 are
        # flushed at the last arrival time, after 40, 20 and 0 ms.
        buffer = JitterBuffer(depth_s=0.06)
        delivered = []
        for k in range(50):
            now = 10.0 + k * FRAME_S
            buffer.push(k, frame(0x40), now)
            delivered += buffer.pop_due(now)
        self.assertEqual(len(delivered), 47)
        self.assertEqual(holds(buffer), expected([60000] * 47))
        delivered += buffer.flush(10.0 + 49 * FRAME_S)
        self.assertEqual(len(delivered), 50)
        self.assertEqual(holds(buffer), expected([60000] * 47 + [40000, 20000, 0]))
        self.assertEqual(buffer.stats['hold_le_20ms'], 2)  # 20 ms is inside its bucket (le)
        self.assertTrue(all(concealed == 0 for _, concealed in delivered))

    def test_bucket_bounds_are_inclusive_and_beyond_the_last_counts_only_in_the_total(self):
        cases = ((0.1, 100000), (0.100001, 100001), (5.0, 5000000), (7.25, 7250000))
        for wait, held_us in cases:
            buffer = JitterBuffer(depth_s=0.0)
            buffer.push(0, frame(0x40), 1.0)
            self.assertEqual(len(buffer.pop_due(1.0 + wait)), 1)
            self.assertEqual(holds(buffer), expected([held_us]), wait)
        self.assertEqual(buffer.stats['hold_le_5000ms'], 0)  # 7.25 s: only +Inf (hold_frames)

    def test_concealed_frames_are_not_timed(self):
        # Frames 0, 1, 3 arrive (2 lost); 2 is concealed when it is due and is not a hold.
        buffer = JitterBuffer(depth_s=0.06)
        for seq, at in ((0, 0.0), (1, 0.02), (3, 0.06)):
            buffer.push(seq, frame(0x40), at)
        out = buffer.pop_due(0.06 + 3 * FRAME_S)
        self.assertEqual([c for _, c in out], [0, 0, 160, 0])
        self.assertEqual(buffer.stats['concealed_frames'], 1)
        self.assertEqual(holds(buffer), expected([120000, 100000, 60000]))

    def test_handed_over_frames_are_timed_when_handed_out(self):
        # Frames 0-4 wait in the buffer when a sequence jump re-anchors it; they are handed
        # over by the next pop_due, so their hold ends there, not at the jump.
        buffer = JitterBuffer(depth_s=0.06, max_ahead=10)
        for k in range(5):
            buffer.push(k, frame(0x40), 1.0 + 0.001 * k)
        self.assertEqual(buffer.push(500, frame(0x41), 1.005), 'resync')
        out = buffer.pop_due(1.5)
        self.assertEqual(len(out), 6)  # the 5 handed over, then 500 (due at 1.065)
        self.assertEqual(holds(buffer), expected([500000 - 1000 * k for k in range(5)] + [495000]))

    def test_frames_without_a_usable_time_are_counted_unmeasured(self):
        buffer = JitterBuffer(depth_s=0.06)
        for k in range(3):
            buffer.push(k, frame(0x40), 2.0 + k * FRAME_S)
        self.assertEqual(len(buffer.flush()), 3)  # no clock given
        self.assertEqual(holds(buffer), expected([], unmeasured=3))
        buffer.push(3, frame(0x40), 5.0)
        self.assertEqual(len(buffer.flush(4.0)), 1)  # a clock that went back: not a negative hold
        self.assertEqual(holds(buffer), expected([], unmeasured=4))
        with self.assertRaises(ValueError):
            buffer.flush(float('nan'))

    def test_every_received_frame_handed_out_is_counted_once(self):
        # Seeded reordering, loss, duplicates and a jump; whatever comes out, the received
        # frames handed out equal hold_frames + hold_unmeasured, each counted once.
        import random
        rng = random.Random(18)
        buffer = JitterBuffer(depth_s=0.06, max_ahead=40)
        out, now, seq, arrival = [], 0.0, 0, 0.0
        for step in range(600):
            now += FRAME_S
            seq += 60 if step == 300 else 1
            if rng.random() < 0.05:
                continue  # lost
            arrival = max(arrival, now + rng.random() * 0.03)  # a monotonic receiver clock
            buffer.push(seq & 0xFFFF, frame(0x40), arrival)
            if rng.random() < 0.03:
                buffer.push(seq & 0xFFFF, frame(0x40), arrival)  # duplicate
            out += buffer.pop_due(arrival)
        out += buffer.flush(now + 1.0)
        received_out = sum(1 for _, concealed in out if concealed == 0)
        self.assertEqual(buffer.stats['hold_frames'] + buffer.stats['hold_unmeasured'], received_out)
        self.assertEqual(buffer.stats['hold_unmeasured'], 0)
        # Review L4: against what was received, not only what came out.
        self.assertEqual(buffer.stats['received'], buffer.stats['hold_frames'] + buffer.stats['hold_unmeasured']
                         + buffer.stats['dropped_pending_received'])
        self.assertGreater(buffer.stats['resyncs'], 0)
        cumulative = [buffer.stats[key] for key in HOLD_KEYS]
        self.assertEqual(cumulative, sorted(cumulative))
        self.assertLessEqual(cumulative[-1], buffer.stats['hold_frames'])


class HandOverTests(unittest.TestCase):
    """Review L4 (18 Nov): SSRC restart, split hand-outs and the 60 s hand-over bound."""

    def test_an_ssrc_restart_hands_over_frames_timed_at_hand_out(self):
        buffer = JitterBuffer(depth_s=0.06)
        for k in range(3):
            buffer.push(100 + k, frame(0x40), 3.0 + 0.01 * k)
        buffer.restart(3.03)
        buffer.push(9000, frame(0x41), 3.04)  # unrelated sequence numbers
        out = buffer.pop_due(3.2)
        self.assertEqual(len(out), 4)
        self.assertEqual(holds(buffer), expected([200000, 190000, 180000, 160000]))

    def test_a_pending_list_split_by_limit_is_timed_at_each_later_pop(self):
        buffer = JitterBuffer(depth_s=0.06, max_ahead=10)
        for k in range(6):
            buffer.push(k, frame(0x40), 1.0)
        buffer.push(500, frame(0x41), 1.0)  # resync: 0-5 handed over
        self.assertEqual(len(buffer.pop_due(1.1, limit=4)), 4)
        self.assertEqual(holds(buffer), expected([100000] * 4))
        self.assertEqual(len(buffer.pop_due(1.3, limit=4)), 3)  # 4 and 5, then 500 (due 1.06)
        self.assertEqual(holds(buffer), expected([100000] * 4 + [300000] * 3))

    def test_received_frames_dropped_at_the_hand_over_bound_are_counted_not_timed(self):
        from services.tap.jitter import MAX_PENDING_FRAMES
        buffer = JitterBuffer(depth_s=0.06, max_ahead=4000)
        for k in range(MAX_PENDING_FRAMES + 100):
            buffer.push(k, frame(0x40), 1.0)
        self.assertEqual(buffer.push(MAX_PENDING_FRAMES + 4200, frame(0x41), 1.0), 'resync')
        self.assertEqual((buffer.stats['dropped_pending'], buffer.stats['dropped_pending_received']), (100, 100))
        out = []
        while True:
            chunk = buffer.pop_due(2.0)
            if not chunk:
                break
            out += chunk
        self.assertEqual(len(out), MAX_PENDING_FRAMES + 1)
        self.assertEqual(buffer.stats['received'], buffer.stats['hold_frames'] + buffer.stats['dropped_pending_received'])

    def test_an_unrepresentable_hold_is_unmeasured_and_loses_no_frame(self):
        # Review L2: each time is finite, their difference in microseconds is not.
        buffer = JitterBuffer(depth_s=0.0)
        buffer.push(0, frame(0x40), -1e308)
        self.assertEqual(len(buffer.flush(1e308)), 1)
        self.assertEqual(holds(buffer), expected([], unmeasured=1))


def rtp(seq):
    return struct.pack('!BBHII', 0x80, 0, seq, seq * 160, 7) + b'\x40' * 160


class ReceiverTests(unittest.TestCase):
    def test_the_end_of_call_flush_is_timed_with_the_receiver_clock(self):
        now = [100.0]
        session = RtpSession(host='127.0.0.1', clock=lambda: now[0], jitter=JitterBuffer(depth_s=0.06))
        self.addCleanup(session.close)
        for k in range(10):
            now[0] = 100.0 + k * FRAME_S
            session._accept(rtp(k), ('127.0.0.1', 40000))
            session.take_frames()
        now[0] += 0.005
        self.assertEqual(len(session.take_frames(final=True)), 3)
        stats = session.receiver_stats()
        self.assertEqual((stats['jitter_hold_frames'], stats['jitter_hold_unmeasured']), (10, 0))
        self.assertEqual(stats['jitter_hold_us_sum'], 7 * 60000 + 45000 + 25000 + 5000)


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.service = make_service(ManualClock())
        self.health = HealthServer(self.service, port=0)
        self.addCleanup(self.health.stop)

    def test_hold_histogram_is_rendered_from_ended_and_open_call_totals(self):
        values = samples(self.health.metrics())
        self.assertEqual((values['ava_live_jitter_hold_seconds_count'], values['ava_live_jitter_hold_seconds_sum'],
                          values['ava_live_jitter_hold_seconds_bucket{le="+Inf"}']), (0, 0, 0))
        # Two ended calls, as the tap adds them (receiver stats carry the `jitter_` prefix).
        first = JitterBuffer(depth_s=0.06)
        second = JitterBuffer(depth_s=0.0)
        for k in range(5):
            first.push(k, frame(0x40), k * FRAME_S)
            first.pop_due(k * FRAME_S)
        first.flush(4 * FRAME_S)  # 2 x 60 ms, then 40, 20, 0 ms
        second.push(0, frame(0x40), 0.0)
        second.pop_due(6.0)  # 6 s: beyond the last bucket
        for buffer in (first, second):
            self.service.tap._add_receiver_totals({f'jitter_{k}': v for k, v in buffer.stats.items()})
        text = self.health.metrics()
        values = samples(text)
        bucket = {ms: values[f'ava_live_jitter_hold_seconds_bucket{{le="{ms / 1000!r}"}}'] for ms in HOLD_BUCKETS_MS}
        self.assertEqual((bucket[20], bucket[40], bucket[60], bucket[5000]), (2, 3, 5, 5))
        self.assertEqual(values['ava_live_jitter_hold_seconds_bucket{le="+Inf"}'], 6)
        self.assertEqual(values['ava_live_jitter_hold_seconds_count'], 6)
        self.assertAlmostEqual(values['ava_live_jitter_hold_seconds_sum'], 0.18 + 6.0, places=9)
        self.assertEqual(values['ava_live_jitter_hold_unmeasured_total'], 0)
        self.assertIn('# TYPE ava_live_jitter_hold_seconds histogram', text)
        self.assertNotIn('kind="hold_', text)  # not mixed into the jitter_buffer kinds
        self.assertEqual(values['ava_live_metrics_render_errors_total'], 0)

    def test_inconsistent_counts_drop_only_the_hold_families(self):
        for bad in ({'jitter_hold_le_20ms': 5, 'jitter_hold_le_40ms': 4, 'jitter_hold_frames': 9},
                    {'jitter_hold_le_5000ms': 3, 'jitter_hold_frames': 2},
                    {'jitter_hold_frames': 1, 'jitter_hold_us_sum': float('nan')},
                    {'jitter_hold_us_sum': 5}):  # a sum with no frames (review nit 5)
            self.service.tap.receiver_live = {key: bad.get(key, 0) for key in bad} | {'packets': 4}
            before = samples(self.health.metrics())['ava_live_metrics_render_errors_total']
            values = samples(self.health.metrics())
            self.assertFalse([name for name in values if name.startswith('ava_live_jitter_hold')], bad)
            self.assertEqual(values['ava_live_rtp_receiver_total{kind="packets"}'], 4)
            self.assertEqual(values['ava_live_metrics_render_errors_total'], before + 1)


if __name__ == '__main__':
    unittest.main()
