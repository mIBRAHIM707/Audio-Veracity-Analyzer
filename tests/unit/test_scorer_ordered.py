"""Serialized, in-order scorer boundary (backlog 5 boundary check from phase-4 QA M1
and L3; PRD FR-I5, FR-I7).

Claude-authored test cases, 10 November 2026.
"""
import itertools
import threading
import unittest

from services.scorer.config import ScoringConfig
from services.scorer.ordered import OrderedCallScorer
from services.scorer.session import END_REASONS, SessionClosed

BOTH = ScoringConfig(ema_alpha=1.0, score_directions=('caller', 'agent'))
REASON = sorted(END_REASONS)[0]


def ordered(config=BOTH):
    ids = (f'alert-{n}' for n in itertools.count(1))
    return OrderedCallScorer.create(config, call_id='call-1', new_alert_id=lambda: next(ids))


class OrderedCallScorerTests(unittest.TestCase):
    def test_in_order_results_apply_and_gaps_are_allowed(self):
        o = ordered()
        for n, idx in enumerate((0, 1, 4, 9), start=1):                 # 2-3, 5-8 skipped by stride
            result = o.add_result('caller', idx, 0.2)
            self.assertEqual((result['applied'], result['reason']), (True, None))
            self.assertEqual(result['events'][0].window, n)              # session counts scored windows
        self.assertEqual(o._scorer.sessions['caller'].windows, 4)
        self.assertEqual(o.last_applied('caller'), 9)

    def test_duplicate_and_stale_results_are_rejected_without_touching_the_scorer(self):
        o = ordered()
        o.add_result('caller', 0, 0.2)
        o.add_result('caller', 5, 0.9)
        before = (o._scorer.sessions['caller'].windows, o._scorer.sessions['caller'].ema)
        self.assertEqual(o.add_result('caller', 5, 0.1), {'applied': False, 'reason': 'duplicate', 'events': []})
        self.assertEqual(o.add_result('caller', 3, 0.1), {'applied': False, 'reason': 'stale', 'events': []})
        self.assertEqual(o.add_result('caller', 0, 0.1)['reason'], 'stale')
        self.assertEqual((o._scorer.sessions['caller'].windows, o._scorer.sessions['caller'].ema), before)
        self.assertEqual(o.rejected, {'duplicate': 1, 'stale': 2})
        self.assertTrue(o.add_result('caller', 6, 0.1)['applied'])

    def test_directions_have_independent_indexes(self):
        o = ordered()
        o.human_agent_answered()
        o.add_result('caller', 7, 0.2)
        self.assertTrue(o.add_result('agent', 0, 0.2)['applied'])
        self.assertEqual(o.add_result('agent', 0, 0.2)['reason'], 'duplicate')
        self.assertEqual((o.last_applied('caller'), o.last_applied('agent')), (7, 0))

    def test_ignored_window_still_consumes_its_index(self):
        o = ordered()
        self.assertEqual(o.add_result('agent', 3, 0.9),                  # before the human answers
                         {'applied': True, 'reason': None, 'events': []})
        self.assertEqual(o._scorer.ignored['before_human_answer'], 1)
        self.assertEqual(o.add_result('agent', 3, 0.9)['reason'], 'duplicate')

    def test_invalid_input_and_scorer_errors_change_nothing(self):
        o = ordered()
        o.add_result('caller', 2, 0.2)
        before = o.snapshot()
        for args in (('robot', 3, 0.2), ('caller', -1, 0.2), ('caller', True, 0.2),
                     ('caller', 3.0, 0.2), ('caller', None, 0.2), ('caller', 3, 1.5),
                     ('caller', 3, float('nan'))):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    o.add_result(*args)
                self.assertEqual(o.snapshot(), before)
        with self.assertRaises(ValueError):
            o.add_result('caller', 3, 0.2, concealed_fraction=2.0)
        self.assertEqual(o.snapshot(), before)
        self.assertTrue(o.add_result('caller', 3, 0.2)['applied'])     # same index retried

    def test_returned_events_are_copies(self):
        o = ordered()
        o.add_result('caller', 0, 0.2)['events'][0].data['call_id'] = 'tampered'
        events = o.end(REASON, speech_seconds={'caller': 1.0})
        events[-1].data['verdict'] = 'tampered'
        events[0].data['direction'] = 'tampered'
        self.assertNotEqual(o._scorer.ended.data['verdict'], 'tampered')
        self.assertEqual(o._scorer.ended.data['call_id'], 'call-1')
        self.assertEqual(o.snapshot()['ended'], True)

    def test_closed_call_refuses_results_and_keeps_the_index(self):
        o = ordered()
        o.add_result('caller', 0, 0.2)
        o.end(REASON, speech_seconds={'caller': 1.0})
        with self.assertRaises(SessionClosed):
            o.add_result('caller', 1, 0.2)
        self.assertEqual(o.last_applied('caller'), 0)

    def test_second_worker_waits_for_the_first_and_then_sees_a_duplicate(self):
        # Deterministic: worker A is held inside the scorer; worker B, with the same
        # index, must block on the lock rather than apply it a second time.
        o = ordered(ScoringConfig(ema_alpha=1.0))
        original, calls = o._scorer.add_window, []
        started, release = threading.Event(), threading.Event()

        def held(direction, p, **kw):
            calls.append(direction)
            started.set()
            self.assertTrue(release.wait(5))
            return original(direction, p, **kw)

        o._scorer.add_window = held
        results = {}
        a = threading.Thread(target=lambda: results.__setitem__('a', o.add_result('caller', 5, 0.3)))
        b = threading.Thread(target=lambda: results.__setitem__('b', o.add_result('caller', 5, 0.3)))
        a.start()
        self.assertTrue(started.wait(5))
        b.start()
        b.join(0.2)
        self.assertTrue(b.is_alive())                                    # blocked on the call lock
        release.set()
        a.join(5)
        b.join(5)
        self.assertEqual((results['a']['applied'], results['b']['reason']), (True, 'duplicate'))
        self.assertEqual(calls, ['caller'])
        self.assertEqual(o.last_applied('caller'), 5)

    def test_ignored_agent_window_then_lower_index_after_answer_is_stale(self):
        o = ordered()
        o.add_result('agent', 2, 0.9)                                    # IVR, ignored
        o.human_agent_answered()
        self.assertEqual(o.add_result('agent', 1, 0.9)['reason'], 'stale')
        self.assertTrue(o.add_result('agent', 3, 0.9)['applied'])

    def test_alert_id_failure_keeps_the_index_for_a_retry(self):
        fail = [True]

        def new_id():
            if fail[0]:
                raise RuntimeError('id service down')
            return 'alert-1'

        o = OrderedCallScorer.create(ScoringConfig(ema_alpha=1.0), call_id='c', new_alert_id=new_id)
        failed_at = None
        for idx in range(50):                                            # high scores until an alert is due
            try:
                o.add_result('caller', idx, 0.99)
            except RuntimeError:
                failed_at = idx
                break
        self.assertIsNotNone(failed_at)
        self.assertGreater(failed_at, 0)
        self.assertEqual(o.last_applied('caller'), failed_at - 1)
        idx = failed_at
        fail[0] = False
        events = o.add_result('caller', idx, 0.99)['events']
        self.assertIn('alert.raised', [e.type for e in events])

    def test_end_with_bad_input_changes_nothing_and_ended_call_refuses_everything(self):
        o = ordered()
        o.add_result('caller', 0, 0.2)
        before = o.snapshot()
        with self.assertRaises(ValueError):
            o.end(REASON, speech_seconds={'agent': 1.0})
        self.assertEqual(o.snapshot(), before)
        o.end(REASON, speech_seconds={'caller': 1.0})
        for call in (lambda: o.add_result('caller', 0, 0.2), lambda: o.human_agent_answered(),
                     lambda: o.end(REASON, speech_seconds={'caller': 1.0})):
            with self.assertRaises(SessionClosed):
                call()
        self.assertEqual(o.rejected, {'duplicate': 0, 'stale': 0})

    def test_constructor_needs_a_call_scorer(self):
        with self.assertRaises(TypeError):
            OrderedCallScorer(BOTH)


if __name__ == '__main__':
    unittest.main()
