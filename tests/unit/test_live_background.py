"""Claude-authored tests (16 November 2026) for services/live/background.py, the pump
threads of the live service (backlog 5.1/5.2/8.1; PRD FR-I11, FR-A4; architecture
§2.4). Authored by Claude (Claude Code), not by GPT-6 Astra. Real threads and a real
EventHub with scripted pumps; time bounds are generous (CI machines are slow) and
asserted only as upper limits.
"""
from collections import deque
import threading
import time
import unittest

from services.events.fanout import EventHub
from services.live.background import PumpThread
from services.store.pump import StorePump


class QueuePump:
    """Moves up to `batch` items from a shared queue per call; returns the count."""

    def __init__(self, batch=3):
        self.items, self.moved, self.batch, self.lock = deque(), [], batch, threading.Lock()

    def __call__(self):
        with self.lock:
            taken = [self.items.popleft() for _ in range(min(self.batch, len(self.items)))]
        self.moved.extend(taken)
        return len(taken)


def wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


class PumpThreadTests(unittest.TestCase):
    def test_pumps_in_the_background_and_catches_up_without_waiting(self):
        pump = QueuePump(batch=3)
        thread = PumpThread('q', pump, interval_s=0.05)
        thread.start()
        self.addCleanup(thread.stop, 0)
        self.assertTrue(thread.alive)
        with pump.lock:
            pump.items.extend(range(300))
        # 100 back-to-back turns: far faster than 100 intervals (5 s) would allow.
        self.assertTrue(wait_until(lambda: len(pump.moved) == 300, timeout=2.0))
        self.assertEqual(pump.moved, list(range(300)))
        self.assertTrue(thread.stop(1.0))
        snap = thread.snapshot()
        self.assertEqual((snap['moved'], snap['faults'], snap['alive'], snap['drained'], snap['stuck']),
                         (300, 0, False, True, False))

    def test_stop_drains_what_was_published_before_it(self):
        pump = QueuePump(batch=2)
        thread = PumpThread('q', pump, interval_s=30)  # it would not wake on its own
        thread.start()
        self.assertTrue(wait_until(lambda: thread.stats['turns'] >= 1))
        with pump.lock:
            pump.items.extend(range(41))  # e.g. monitor_lost ends published while stopping
        started = time.monotonic()
        self.assertTrue(thread.stop(5.0))
        self.assertLess(time.monotonic() - started, 3.0)  # the stop request woke the wait
        self.assertEqual(pump.moved, list(range(41)))
        self.assertTrue(thread.stats['drained'])

    def test_faults_are_counted_and_the_loop_goes_on(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) % 2:
                raise ConnectionError('secret-host:5432 refused')
            return 0
        thread = PumpThread('flaky', flaky, interval_s=0.01)
        thread.start()
        self.assertTrue(wait_until(lambda: len(calls) >= 6))
        self.assertTrue(thread.stop(0.5))
        self.assertGreaterEqual(thread.stats['faults'], 3)
        self.assertEqual(thread.stats['last_fault'], 'ConnectionError')  # the type only, no message
        self.assertNotIn('secret-host', repr(thread.snapshot()))

    def test_a_stuck_pump_never_holds_the_stop_or_the_publisher(self):
        # A database that stopped answering: record() blocks inside the client call.
        release = threading.Event()
        self.addCleanup(release.set)
        hub = EventHub()

        class HungStore:
            def record(self, envelope):
                release.wait(30)
                return 'stored'
        store_pump = StorePump(hub.subscribe(buffer=64), HungStore())
        thread = PumpThread('store', store_pump.pump, interval_s=0.01)
        thread.start()
        hub.publish('call.started', 'c1', {})
        self.assertTrue(wait_until(lambda: store_pump.backlog == 1))  # now blocked in record()
        started = time.monotonic()
        for i in range(500):  # the scoring side keeps publishing; the hub never blocks
            hub.publish('call.started', f'c{i + 2}', {})
        self.assertLess(time.monotonic() - started, 1.0)
        started = time.monotonic()
        self.assertFalse(thread.stop(0.2, join_grace_s=0.2))
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(thread.stats['stuck'])
        self.assertTrue(thread._thread.daemon)  # cannot keep the process alive
        release.set()
        self.assertTrue(wait_until(lambda: not thread.alive))

    def test_a_pump_that_never_empties_is_cut_at_the_drain_deadline(self):
        thread = PumpThread('endless', lambda: 1, interval_s=0.01)
        thread.start()
        self.assertTrue(wait_until(lambda: thread.stats['turns'] > 10))
        started = time.monotonic()
        self.assertTrue(thread.stop(0.2))
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual((thread.stats['drained'], thread.stats['stuck']), (False, False))
        self.assertGreater(thread.stats['drain_turns'], 0)

    def test_a_forced_stop_shortens_an_earlier_longer_drain(self):
        # Re-review R2: request_stop(0) after request_stop(300) ends the drain now.
        thread = PumpThread('endless', lambda: 1, interval_s=0.01)
        thread.start()
        self.assertTrue(wait_until(lambda: thread.stats['turns'] > 3))
        thread.request_stop(300)
        self.assertFalse(thread.join(0.2))  # still draining toward the long deadline
        started = time.monotonic()
        thread.request_stop(0)
        self.assertTrue(thread.join(2.0))
        self.assertLess(time.monotonic() - started, 2.0)
        thread.request_stop(300)  # a later, longer request never lengthens it again
        self.assertFalse(thread.stats['drained'])

    def test_zero_drain_skips_the_final_drain(self):
        pump = QueuePump()
        idle = threading.Event()

        def counted():
            moved = pump()
            if not moved:
                idle.set()  # set after the turn returned: the loop is about to wait (review L2)
            return moved
        thread = PumpThread('q', counted, interval_s=30, pending=lambda: len(pump.items))
        thread.start()
        self.assertTrue(idle.wait(5))
        time.sleep(0.05)  # let the loop enter its 30 s wait
        with pump.lock:
            pump.items.extend(range(10))
        self.assertTrue(thread.stop(0))
        self.assertEqual((thread.stats['drain_turns'], thread.stats['drained'], thread.stats['left_pending']),
                         (0, False, 10))
        self.assertEqual(pump.moved, [])

    def test_drained_needs_nothing_pending_not_just_a_quiet_turn(self):
        # Review M2: a store in an outage moves nothing while its backlog waits.
        hub = EventHub()

        class DownStore:
            def record(self, envelope):
                from services.store.events_pg import StoreError
                raise StoreError('database down', retryable=True)
        subscription = hub.subscribe(buffer=64)
        store_pump = StorePump(subscription, DownStore())
        thread = PumpThread('store', store_pump.pump, interval_s=0.01,
                            pending=lambda: store_pump.backlog + subscription.buffered())
        thread.start()
        for i in range(3):
            hub.publish('call.started', f'c{i}', {})
        self.assertTrue(wait_until(lambda: store_pump.backlog == 3))
        self.assertTrue(thread.stop(0.5))
        self.assertEqual((thread.stats['drained'], thread.stats['left_pending']), (False, 3))

    def test_request_stop_lets_several_pumps_drain_in_parallel(self):
        # Review L1: one deadline for all pumps, not one per pump in turn.
        threads = [PumpThread(f'p{i}', lambda: 1, interval_s=0.01) for i in range(3)]
        for thread in threads:
            thread.start()
        self.assertTrue(wait_until(lambda: all(t.stats['turns'] > 3 for t in threads)))
        started = time.monotonic()
        for thread in threads:
            thread.request_stop(0.5)
        deadline = time.monotonic() + 1.5
        self.assertTrue(all(t.join(deadline - time.monotonic()) for t in threads))
        self.assertLess(time.monotonic() - started, 1.4)  # three sequential drains would need 1.5 s+
        self.assertTrue(all(t.stats['drained'] is False for t in threads))

    def test_non_integer_results_count_as_nothing_moved(self):
        results = deque([None, True, -3, 'x', 2.5, 4])
        thread = PumpThread('odd', lambda: results.popleft() if results else 0, interval_s=0.01)
        thread.start()
        self.assertTrue(wait_until(lambda: not results))
        self.assertTrue(thread.stop(0.5))
        self.assertEqual(thread.stats['moved'], 4)

    def test_validation_and_lifecycle(self):
        for args, kwargs in ((('', lambda: 0), {}), (('x', None), {}), (('x', lambda: 0), {'pending': 3}),
                             (('x', lambda: 0), {'interval_s': 0}),
                             (('x', lambda: 0), {'interval_s': True}), (('x', lambda: 0), {'interval_s': 61}),
                             (('x', lambda: 0), {'interval_s': float('nan')})):
            with self.assertRaises(ValueError, msg=(args, kwargs)):
                PumpThread(*args, **kwargs)
        thread = PumpThread('x', lambda: 0)
        self.assertTrue(thread.stop())  # never started: nothing to stop
        thread.start()
        with self.assertRaises(RuntimeError):
            thread.start()
        for bad in (-1, 301, True, 'soon'):
            with self.assertRaises(ValueError):
                thread.stop(bad)
        self.assertTrue(thread.stop(0.1))
        self.assertTrue(thread.stop(0.1))  # a second stop is harmless


if __name__ == '__main__':
    unittest.main()
