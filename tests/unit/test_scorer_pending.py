"""Bounded per-call pending-window queue (backlog 8.2; PRD FR-I10).

Claude-authored test cases, 10 November 2026. Timelines are worked by hand with the
default configuration: hop 2 s, stride cap 4x, recovery 30 s.
"""
import unittest

from services.scorer.config import ScoringConfig
from services.scorer.degraded import StrideController
from services.scorer.pending import PendingWindows

DEFAULT = ScoringConfig()


def _queue(max_pending=2):
    return PendingWindows(StrideController(DEFAULT), max_pending=max_pending)


class PendingWindowsTests(unittest.TestCase):
    def test_inference_keeping_up_never_degrades(self):
        q = _queue()
        for i in range(10):
            result = q.offer(2.0 * i, i, f'w{i}')
            self.assertEqual(result, {'accepted': True, 'dropped_index': None, 'changes': []})
            self.assertEqual(q.take(), (i, f'w{i}'))
        self.assertEqual(q.controller.status(), {'degraded': False, 'effective_stride_s': 2.0, 'unmonitored': False})
        self.assertEqual(q.counts, {'offered': 10, 'accepted': 10, 'skipped': 0, 'dropped': 0, 'taken': 10})

    def test_stalled_inference_grows_stride_drops_oldest_and_goes_unmonitored(self):
        self._stall_to_unmonitored()

    def _stall_to_unmonitored(self):
        q = _queue(max_pending=2)
        expected = [
            # index, accepted, dropped_index, changes
            (0, True, None, []),
            (1, True, None, [('effective_stride_s', 2.0, 4.0)]),
            (2, False, None, []),                      # 2 - 1 < factor 2
            (3, True, 0, [('effective_stride_s', 4.0, 6.0)]),
            (4, False, None, []),
            (5, False, None, []),
            (6, True, 1, [('effective_stride_s', 6.0, 8.0)]),
            (7, False, None, []), (8, False, None, []), (9, False, None, []),
            (10, True, 3, [('unmonitored', False, True)]),
        ]
        for index, accepted, dropped, changes in expected:
            with self.subTest(index=index):
                result = q.offer(2.0 * index, index, index)
                self.assertEqual(result, {'accepted': accepted, 'dropped_index': dropped, 'changes': changes})
                self.assertLessEqual(len(q), 2)
        self.assertEqual(q.counts, {'offered': 11, 'accepted': 5, 'skipped': 6, 'dropped': 3, 'taken': 0})
        self.assertEqual([q.take(), q.take(), q.take()], [(6, 6), (10, 10), None])
        self.assertEqual(q.counts['taken'], 2)                           # empty take not counted
        return q

    def test_recovery_after_thirty_seconds_under_budget_returns_to_one_hop(self):
        q = self._stall_to_unmonitored()
        # Times and indexes are independent here: the queue spaces by index, the
        # controller's recovery clock by time.
        self.assertEqual(q.offer(28.0, 14, 'a')['changes'], [])        # empty queue: clock starts at 28
        q.take()
        self.assertFalse(q.offer(30.0, 15, 'b')['accepted'])             # still capped stride 4
        self.assertEqual(q.offer(36.0, 18, 'c')['changes'], [])
        q.take()
        self.assertEqual(q.offer(57.999, 22, 'd')['changes'], [])        # 29.999 s: not yet
        q.take()
        self.assertEqual(q.offer(58.0, 26, 'e')['changes'],
                         [('effective_stride_s', 8.0, 2.0), ('unmonitored', True, False)])
        q.take()
        self.assertTrue(q.offer(60.0, 27, 'f')['accepted'])              # one hop again

    def test_waiting_window_at_acceptance_is_behind_but_in_flight_is_not(self):
        q = _queue(max_pending=3)
        q.offer(0.0, 0, 'a')
        q.take()                                                         # in flight
        self.assertEqual(q.offer(2.0, 1, 'b')['changes'], [])
        self.assertEqual(q.offer(4.0, 2, 'c')['changes'], [('effective_stride_s', 2.0, 4.0)])

    def test_single_slot_queue_replaces_the_waiting_window(self):
        q = _queue(max_pending=1)
        q.offer(0.0, 0, 'a')
        result = q.offer(2.0, 1, 'b')
        self.assertEqual((result['accepted'], result['dropped_index']), (True, 0))
        self.assertEqual((len(q), q.take(), q.take()), (1, (1, 'b'), None))

    def test_memory_stays_bounded_under_a_long_stall(self):
        q = _queue(max_pending=3)
        for i in range(1000):
            q.offer(2.0 * i, i, bytes(10))
            self.assertLessEqual(len(q), 3)
        c = q.counts
        self.assertEqual(c['offered'], 1000)
        self.assertEqual(c['accepted'] + c['skipped'], 1000)
        self.assertEqual(c['accepted'] - c['dropped'], len(q))
        self.assertTrue(q.controller.unmonitored)

    def test_invalid_indexes_change_nothing(self):
        q = _queue()
        q.offer(0.0, 5, 'a')
        before = (len(q), dict(q.counts), q.controller.status())
        for bad in (5, 4, -1, True, 6.0, '6', None):
            with self.subTest(index=bad):
                with self.assertRaises(ValueError):
                    q.offer(1.0, bad, 'x')
                self.assertEqual((len(q), q.counts, q.controller.status()), before)
        self.assertTrue(q.offer(2.0, 6, 'b')['accepted'])

    def test_controller_error_changes_nothing(self):
        q = _queue()
        q.offer(10.0, 0, 'a')
        before = (len(q), dict(q.counts), q.controller.status())
        for now in (5.0, float('nan'), 'soon'):
            with self.subTest(now=now):
                with self.assertRaises(ValueError):
                    q.offer(now, 1, 'b')
                self.assertEqual((len(q), q.counts, q.controller.status()), before)
        self.assertTrue(q.offer(11.0, 1, 'b')['accepted'])               # index 1 still usable

    def test_controller_error_on_a_full_queue_drops_nothing(self):
        q = _queue(max_pending=1)
        q.offer(10.0, 0, 'a')
        with self.assertRaises(ValueError):
            q.offer(5.0, 1, 'b')
        self.assertEqual(q.counts['dropped'], 0)
        self.assertEqual(q.take(), (0, 'a'))

    def test_bad_or_backwards_clock_refused_on_a_skipped_window(self):
        q = _queue(max_pending=2)
        q.offer(0.0, 0, 'a')
        q.offer(2.0, 1, 'b')                                             # behind: stride 2 hops
        before = (len(q), dict(q.counts), q.controller.status())
        for now in ('soon', -5.0, 1.0, float('inf')):
            with self.subTest(now=now):
                with self.assertRaises(ValueError):
                    q.offer(now, 2, 'c')                                 # index 2 would be skipped
                self.assertEqual((len(q), q.counts, q.controller.status()), before)
        self.assertFalse(q.offer(4.0, 2, 'c')['accepted'])
        with self.assertRaises(ValueError):
            q.offer(3.0, 3, 'd')                                         # behind the skipped window's time

    def test_first_index_above_zero_with_a_wide_stride(self):
        q = _queue(max_pending=2)
        q.offer(0.0, 7, 'a')
        q.offer(2.0, 8, 'b')                                             # behind: factor 2
        self.assertFalse(q.offer(4.0, 9, 'c')['accepted'])
        self.assertTrue(q.offer(6.0, 10, 'd')['accepted'])

    def test_constructor_refuses_bad_arguments(self):
        with self.assertRaises(TypeError):
            PendingWindows(DEFAULT, max_pending=2)
        for bad in (0, -1, True, 1.5, None):
            with self.subTest(max_pending=bad):
                with self.assertRaises(ValueError):
                    PendingWindows(StrideController(DEFAULT), max_pending=bad)


if __name__ == '__main__':
    unittest.main()
