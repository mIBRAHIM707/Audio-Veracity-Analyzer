"""Capacity refusal and live-first micro-batching (backlog 8.1 partial; PRD FR-I9;
architecture §2.2, §2.3, §7.3).

Claude-authored test cases, 10 November 2026. Pure scheduling logic with an explicit
clock; not a measurement of CPU capacity or latency (backlog 8.QA).
"""
import threading
import unittest

from services.inference.capacity import (CallGate, FileBacklogFull, LiveBacklogFull,
                                         MicroBatcher)


class CallGateTests(unittest.TestCase):
    def test_refuses_above_max_with_health_payload_and_never_raises(self):
        gate = CallGate()
        for n in range(8):
            self.assertEqual(gate.admit(f'c{n}'), (True, None))
        admitted, health = gate.admit('c8')
        self.assertFalse(admitted)
        self.assertEqual(health, {'reason': 'capacity', 'call_id': 'c8', 'active_calls': 8, 'max_calls': 8})
        self.assertEqual((gate.active, gate.refused), (8, 1))

    def test_readmit_release_and_slot_reuse(self):
        gate = CallGate(max_calls=1)
        self.assertEqual(gate.admit('a'), (True, None))
        self.assertEqual(gate.admit('a'), (True, None))  # same call again is not a new session
        self.assertFalse(gate.admit('b')[0])
        self.assertTrue(gate.release('a'))
        self.assertFalse(gate.release('a'))
        self.assertFalse(gate.release('never'))
        self.assertEqual(gate.admit('b'), (True, None))

    def test_concurrent_admission_never_exceeds_max(self):
        gate, results, start = CallGate(max_calls=8), [], threading.Barrier(50)

        def admit(n):
            start.wait()
            results.append(gate.admit(f'c{n}')[0])

        threads = [threading.Thread(target=admit, args=(n,)) for n in range(50)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual((results.count(True), gate.active, gate.refused), (8, 8, 42))

    def test_invalid_arguments(self):
        for bad in (0, -1, 8.0, True, None):
            with self.subTest(max_calls=bad):
                with self.assertRaises(ValueError):
                    CallGate(max_calls=bad)
        for bad in ('', None, 5):
            with self.subTest(call_id=bad):
                with self.assertRaises(ValueError):
                    CallGate().admit(bad)


def batcher(**kw):
    kw.setdefault('capacity_windows_per_s', 8.0)
    return MicroBatcher(**kw)


class MicroBatcherTests(unittest.TestCase):
    def test_live_batch_due_when_full_or_after_collection_interval(self):
        b = batcher()
        for n in range(3):
            b.submit(f'l{n}', kind='live', now=0.0)
        self.assertIsNone(b.next_batch(0.05))
        self.assertEqual(b.next_batch(0.1), ('live', ['l0', 'l1', 'l2']))  # 100 ms waited
        for n in range(6):
            b.submit(f'm{n}', kind='live', now=1.0)
        self.assertEqual(b.next_batch(1.0), ('live', ['m0', 'm1', 'm2', 'm3']))  # full: no wait
        self.assertIsNone(b.next_batch(1.05))
        self.assertEqual(b.next_batch(1.1), ('live', ['m4', 'm5']))

    def test_live_first_and_file_never_while_live_waits(self):
        b = batcher()
        for n in range(8):
            b.submit(f'f{n}', kind='file', now=0.0)
        b.submit('l0', kind='live', now=0.0)
        self.assertIsNone(b.next_batch(0.05))  # file is due (8 waiting) but a live window waits
        self.assertEqual(b.next_batch(0.1), ('live', ['l0']))
        self.assertEqual(b.next_batch(0.1), ('file', [f'f{n}' for n in range(8)]))

    def test_file_unlimited_without_live_calls(self):
        b = batcher()
        for n in range(20):
            b.submit(n, kind='file', now=0.0)
        batches = [b.next_batch(0.0), b.next_batch(0.0)]
        self.assertIsNone(b.next_batch(0.05))      # 4 left: not full, not yet waited 100 ms
        batches.append(b.next_batch(0.1))
        self.assertEqual([len(items) for _, items in batches], [8, 8, 4])

    def test_token_bucket_limits_file_share_and_does_not_starve_in_silence(self):
        b = batcher(capacity_windows_per_s=8.0)  # 25 % -> 2 file windows per second
        b.set_live_calls_active(True, now=0.0)
        for n in range(500):
            b.submit(n, kind='file', now=0.0)
        self.assertIsNone(b.next_batch(0.0))  # bucket starts empty
        self.assertEqual(b.next_batch(0.5), ('file', [0]))  # 0.5 s x 2/s = 1 token
        dispatched, max_batch = 1, 1
        t = 0.5
        while t < 60.0:  # a silent live call: no live windows at all for a minute
            t = round(t + 0.1, 10)
            batch = b.next_batch(t)
            if batch:
                dispatched += len(batch[1])
                max_batch = max(max_batch, len(batch[1]))
        self.assertEqual(dispatched, 120)  # 0.25 x 8 windows/s x 60 s
        self.assertEqual(max_batch, 1)  # while live: single-window file batches only

    def test_bucket_is_capped_and_live_end_lifts_limit(self):
        b = batcher(capacity_windows_per_s=8.0)
        b.set_live_calls_active(True, now=0.0)
        for n in range(30):
            b.submit(n, kind='file', now=0.0)
        self.assertEqual(len(b.next_batch(100.0)[1]), 1)   # bucket capped at max_file_while_live
        self.assertIsNone(b.next_batch(100.0))             # no burst after a long idle stretch
        b.set_live_calls_active(False, now=100.0)
        self.assertEqual(len(b.next_batch(100.0)[1]), 8)   # no live call: no limit
        b.set_live_calls_active(True, now=100.0)
        self.assertIsNone(b.next_batch(100.0))             # restarts empty
        with self.assertRaises(AttributeError):
            b.live_calls_active = False                    # read-only; use the setter

    def test_zero_batch_share_holds_file_work_while_live(self):
        b = batcher(batch_share=0.0)
        b.set_live_calls_active(True, now=0.0)
        b.submit('f', kind='file', now=0.0)
        self.assertEqual([b.next_batch(t) for t in (0.1, 10.0, 1000.0)], [None, None, None])

    def test_file_held_while_live_waits_even_before_live_is_due(self):
        b = batcher()
        for n in range(8):
            b.submit(f'f{n}', kind='file', now=0.0)
        b.submit('l', kind='live', now=0.0)
        self.assertIsNone(b.next_batch(0.0))   # live not yet due, file due, file still held

    def test_file_backlog_never_blocks_live(self):
        b = batcher(max_pending_live=2, max_pending_file=3)
        for n in range(3):
            b.submit(n, kind='file', now=0.0)
        with self.assertRaises(FileBacklogFull):
            b.submit(3, kind='file', now=0.0)
        b.submit('l0', kind='live', now=0.0)   # file queue full: live still accepted
        b.submit('l1', kind='live', now=0.0)
        with self.assertRaises(LiveBacklogFull):
            b.submit('l2', kind='live', now=0.0)
        self.assertEqual(b.pending(), {'live': 2, 'file': 3})
        with self.assertRaises(ValueError):    # clock checked before the queue limit
            b.submit('x', kind='live', now=-1.0)

    def test_invalid_input_changes_nothing(self):
        b = batcher()
        b.submit('a', kind='live', now=5.0)
        for kwargs in (dict(kind='batch', now=6.0), dict(kind='live', now=float('nan')),
                       dict(kind='live', now=4.0), dict(kind='live', now='6'), dict(kind='live', now=True)):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    b.submit('x', **kwargs)
                self.assertEqual(b.pending(), {'live': 1, 'file': 0})
        with self.assertRaises(ValueError):
            b.next_batch(4.9)  # clock went backwards
        with self.assertRaises(ValueError):
            b.set_live_calls_active(1, now=6.0)
        self.assertEqual(b.next_batch(5.1), ('live', ['a']))

    def test_constructor_validation(self):
        for kw in (dict(capacity_windows_per_s=0), dict(capacity_windows_per_s=-1),
                   dict(capacity_windows_per_s=float('inf')), dict(collect_s=-0.1),
                   dict(max_live=0), dict(max_file=2.0), dict(batch_share=1.5), dict(batch_share=-0.1),
                   dict(max_pending_live=0), dict(max_pending_file=1.0), dict(max_file_while_live=0)):
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):
                    batcher(**kw)
        with self.assertRaises(TypeError):
            MicroBatcher()  # capacity must be given (measured), no default


if __name__ == '__main__':
    unittest.main()
