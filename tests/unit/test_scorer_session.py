"""Per-direction scoring session: EMA, minimum evidence, hysteresis and undetermined
outcomes (backlog 4.2; PRD FR-I5, FR-I7; architecture §2.4, ADR-11).

Claude-authored test cases, 10 November 2026. Pure state-machine logic with literal
expected values; no model, audio or network.
"""
import fractions
import itertools
import random
import unittest

from services.scorer.config import ConfigStore, ScoringConfig, Thresholds
from services.scorer.session import (ALERT_CONSECUTIVE_WINDOWS, RECENT_SCORES, DirectionSession,
                                     SessionClosed)

DIRECT = ScoringConfig(ema_alpha=1.0)  # EMA equals the window score: drives the FSM exactly


def session(config=DIRECT, **kw):
    ids = (f'alert-{n}' for n in itertools.count(1))
    return DirectionSession(config, call_id=kw.pop('call_id', 'call-1'),
                            new_alert_id=lambda: next(ids), **kw)


def feed(s, scores):
    """Feed scores; return (states after each window, every non-score event as tuples)."""
    states, events = [], []
    for score in scores:
        for event in s.add_window(score):
            if event.type == 'score.updated':
                states.append(event.data['state'])
            else:
                events.append((event.window, event.type,
                               event.data.get('to') or event.data.get('alert_id')))
    return states, events


def reference_states(scores, config):
    """Independent restatement of architecture §2.4 (one transition per window, the
    alert streak counted from min_windows on). Returns the state after each window."""
    t, ema, streak, state, out = config.thresholds, None, 0, 'monitoring', []
    for n, p in enumerate(scores, 1):
        ema = p if n == 1 else config.ema_alpha * p + (1 - config.ema_alpha) * ema
        if n >= config.min_windows:
            streak = streak + 1 if ema >= t.alert_enter else 0
            rules = {
                'monitoring': [(ema >= t.suspicious_enter, 'suspicious')],
                'suspicious': [(streak >= 2, 'alert'), (ema < t.suspicious_exit, 'monitoring')],
                'alert': [(ema < t.alert_exit, 'suspicious')],
            }[state]
            state = next((target for condition, target in rules if condition), state)
        out.append(state)
    return out


class EmaAndEvidenceTests(unittest.TestCase):
    def test_ema_values_with_default_alpha(self):
        s = session(ScoringConfig())
        emas = [s.add_window(p)[0].data['ema'] for p in (1.0, 0.0, 0.5)]
        self.assertAlmostEqual(emas[0], 1.0)   # first window seeds the EMA
        self.assertAlmostEqual(emas[1], 0.6)   # 0.4*0 + 0.6*1
        self.assertAlmostEqual(emas[2], 0.56)  # 0.4*0.5 + 0.6*0.6
        self.assertEqual((s.max_score, list(s.recent)), (1.0, [1.0, 0.0, 0.5]))

    def test_first_window_is_provisional_and_changes_no_state(self):
        s = session()
        first = s.add_window(1.0)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].type, 'score.updated')
        self.assertTrue(first[0].data['provisional'])
        self.assertEqual(first[0].data['state'], 'monitoring')
        second = s.add_window(1.0)
        self.assertFalse(second[0].data['provisional'])
        self.assertEqual([e.type for e in second], ['score.updated', 'state.changed'])
        self.assertEqual(second[0].data['state'], 'suspicious')  # state after this window

    def test_min_windows_from_config(self):
        s = session(ScoringConfig(ema_alpha=1.0, min_windows=4))
        states, events = feed(s, [1.0] * 6)
        self.assertEqual(states, ['monitoring', 'monitoring', 'monitoring', 'suspicious', 'alert', 'alert'])
        self.assertEqual(events, [(4, 'state.changed', 'suspicious'), (5, 'state.changed', 'alert'),
                                  (5, 'alert.raised', 'alert-1')])

    def test_fewer_than_min_windows_is_undetermined_never_genuine(self):
        for scores in ([], [0.0], [1.0]):
            with self.subTest(scores=scores):
                s = session()
                feed(s, scores)
                ended = s.end('bye', speech_seconds=len(scores) * 2.0).data
                self.assertEqual(ended['verdict'], 'undetermined')
                self.assertEqual((ended['windows'], ended['final_state']), (len(scores), 'monitoring'))
                self.assertIsNone(ended['ended_open_alert_id'])
        s = session()
        feed(s, [0.0, 0.0])
        self.assertEqual(s.end('bye', speech_seconds=6).data['verdict'], 'monitoring')

    def test_recent_ring_is_bounded(self):
        s = session()
        feed(s, [i / 100 for i in range(RECENT_SCORES + 5)])
        self.assertEqual(len(s.recent), RECENT_SCORES)
        self.assertEqual(s.recent[0], 0.05)
        self.assertEqual(s.max_score, (RECENT_SCORES + 4) / 100)


class StateMachineTests(unittest.TestCase):
    def test_earliest_alert_is_window_three_through_suspicious(self):
        self.assertEqual(ALERT_CONSECUTIVE_WINDOWS, 2)
        states, events = feed(session(), [1.0] * 4)
        self.assertEqual(states, ['monitoring', 'suspicious', 'alert', 'alert'])
        self.assertEqual(events, [(2, 'state.changed', 'suspicious'), (3, 'state.changed', 'alert'),
                                  (3, 'alert.raised', 'alert-1')])

    def test_threshold_equalities(self):
        t = Thresholds()
        # EMA equal to an enter threshold enters; equal to an exit threshold does not exit.
        states, _ = feed(session(), [0, t.suspicious_enter])
        self.assertEqual(states[-1], 'suspicious')
        states, _ = feed(session(), [0, t.suspicious_enter, t.suspicious_exit])
        self.assertEqual(states[-1], 'suspicious')
        states, _ = feed(session(), [0, 0.6, t.alert_enter, t.alert_enter])
        self.assertEqual(states[-1], 'alert')
        states, _ = feed(session(), [0, 0.6, 0.8, 0.8, t.alert_exit])
        self.assertEqual(states[-1], 'alert')
        states, _ = feed(session(), [0, 0.6, 0.8, 0.8, 0.5499999])
        self.assertEqual(states[-1], 'suspicious')
        states, _ = feed(session(), [0, 0.6, 0.3999999])
        self.assertEqual(states[-1], 'monitoring')

    def test_hysteresis_band_holds_state(self):
        # suspicious holds between the exit (0.40) and alert-enter levels
        states, events = feed(session(), [0, 0.6, 0.45, 0.59, 0.41])
        self.assertEqual(states[1:], ['suspicious'] * 4)
        # alert holds between alert_exit (0.55) and below alert_enter
        states, _ = feed(session(), [0, 0.8, 0.8, 0.6, 0.56, 0.7])
        self.assertEqual(states[2:], ['alert'] * 4)

    def test_streak_must_be_consecutive(self):
        states, events = feed(session(), [0, 0.6, 0.8, 0.7, 0.8, 0.8])
        self.assertEqual(states, ['monitoring', 'suspicious', 'suspicious', 'suspicious', 'suspicious', 'alert'])
        self.assertEqual(events[-1], (6, 'alert.raised', 'alert-1'))

    def test_clear_and_realert_get_new_alert_id(self):
        s = session()
        states, events = feed(s, [0, 0.8, 0.8, 0.5, 0.8, 0.8, 0.1, 0.1])
        self.assertEqual(states, ['monitoring', 'suspicious', 'alert', 'suspicious', 'suspicious', 'alert',
                                  'suspicious', 'monitoring'])
        self.assertEqual([e for e in events if e[1] != 'state.changed'],
                         [(3, 'alert.raised', 'alert-1'), (4, 'alert.cleared', 'alert-1'),
                          (6, 'alert.raised', 'alert-2'), (7, 'alert.cleared', 'alert-2')])
        self.assertIsNone(s.alert_id)

    def test_one_transition_per_window(self):
        states, _ = feed(session(), [0, 0.8, 0.8, 0.0])
        self.assertEqual(states[-1], 'suspicious')  # alert -> suspicious, not straight to monitoring

    def test_random_scores_match_reference_model_and_invariants(self):
        """Compares every window with `reference_states`, a separate table-driven
        restatement of architecture §2.4, and checks event pairing invariants."""
        rng = random.Random(20261110)
        alerts_seen = 0
        for trial in range(300):
            config = ScoringConfig(ema_alpha=rng.choice([0.2, 0.4, 1.0]), min_windows=rng.randint(1, 4))
            s = session(config)
            # Bursts of high scores so low-alpha trials also reach alert.
            scores = [rng.choice([rng.random(), rng.uniform(0.7, 1.0), rng.uniform(0.0, 0.3)])
                      for _ in range(rng.randint(0, 60))]
            expected = reference_states(scores, config)
            previous, open_alert, raised = 'monitoring', None, []
            for index, score in enumerate(scores, 1):
                events = s.add_window(score)
                if index < config.min_windows:
                    self.assertEqual([e.type for e in events], ['score.updated'])
                for event in events[1:]:
                    if event.type == 'state.changed':
                        self.assertEqual(event.data['from'], previous)
                        self.assertNotEqual({previous, event.data['to']}, {'monitoring', 'alert'})
                        previous = event.data['to']
                    elif event.type == 'alert.raised':
                        self.assertIsNone(open_alert)
                        open_alert = event.data['alert_id']
                        raised.append(open_alert)
                    elif event.type == 'alert.cleared':
                        self.assertEqual(event.data['alert_id'], open_alert)
                        open_alert = None
                    else:
                        self.fail(f'unexpected event {event.type}')
                self.assertEqual(events[0].data['state'], expected[index - 1], (trial, index))
                self.assertEqual(events[0].data['state'], previous)
                self.assertEqual((s.state == 'alert'), open_alert is not None)
            alerts_seen += len(raised)
            self.assertEqual(len(raised), len(set(raised)))
            ended = s.end('timeout', speech_seconds=0).data
            self.assertEqual(ended['ended_open_alert_id'], open_alert)
        self.assertGreater(alerts_seen, 50)  # the trials do exercise alerting

    def test_unordered_thresholds_still_pass_through_suspicious(self):
        # Not forbidden by FR-I4: alert_enter below suspicious_enter.
        config = ScoringConfig(ema_alpha=1.0, thresholds=Thresholds(0.9, 0.75, 0.55, 0.40))
        states, _ = feed(session(config), [0.8, 0.8, 0.8, 0.95, 0.8])
        self.assertEqual(states, ['monitoring', 'monitoring', 'monitoring', 'suspicious', 'alert'])


class EndAndFailureTests(unittest.TestCase):
    def test_ending_in_alert_is_ended_open_without_cleared(self):
        s = session()
        _, events = feed(s, [0, 0.8, 0.8])
        ended = s.end('monitor_lost', speech_seconds=7.5)
        self.assertEqual(ended.type, 'call.ended')
        self.assertEqual(ended.data['final_state'], 'alert')
        self.assertEqual(ended.data['verdict'], 'alert')
        self.assertEqual(ended.data['ended_open_alert_id'], 'alert-1')
        self.assertEqual(ended.data['reason'], 'monitor_lost')
        self.assertEqual(ended.data['speech_seconds'], 7.5)
        self.assertNotIn('alert.cleared', [e[1] for e in events])

    def test_ending_in_suspicious_after_clear_has_no_open_alert(self):
        s = session()
        feed(s, [0, 0.8, 0.8, 0.5])
        ended = s.end('bye', speech_seconds=8).data
        self.assertEqual((ended['final_state'], ended['ended_open_alert_id']), ('suspicious', None))

    def snapshot(self, s):
        return (s.windows, s.ema, s.state, list(s.recent), s.alert_streak, s.max_score, s.alert_id)

    def test_invalid_scores_change_nothing(self):
        s = session()
        feed(s, [0.0, 0.8, 0.8])  # in alert, so alert_id is set
        before = self.snapshot(s)
        self.assertEqual(before[2], 'alert')
        for value in (float('nan'), float('inf'), -0.01, 1.01, True, '0.5', None, 0.5j, 10 ** 400):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):  # ValueError for every bad score, by design
                    s.add_window(value)
                self.assertEqual(self.snapshot(s), before)
        s.add_window(0)
        s.add_window(1)  # integer boundaries accepted

    def test_non_float_real_scores_accepted(self):
        s = session()
        event = s.add_window(fractions.Fraction(3, 4))[0]  # a numbers.Real that is not a float
        self.assertEqual((event.data['score'], type(event.data['ema'])), (0.75, float))

    def test_failing_alert_id_generator_changes_nothing(self):
        def broken():
            raise RuntimeError('id service down')
        for generator, error in ((broken, RuntimeError), (lambda: '', ValueError), (lambda: 7, ValueError)):
            with self.subTest(generator=generator):
                s = DirectionSession(DIRECT, call_id='c', new_alert_id=generator)
                feed(s, [0, 0.8])
                before = self.snapshot(s)
                with self.assertRaises(error):
                    s.add_window(0.8)  # would raise the alert
                self.assertEqual(self.snapshot(s), before)

    def test_zero_window_end_has_no_scores(self):
        ended = session().end('bye', speech_seconds=0).data
        self.assertEqual((ended['ema'], ended['max_score'], ended['windows']), (None, None, 0))

    def test_state_machine_with_default_alpha(self):
        # alpha 0.4: three 0.9 scores keep the EMA at 0.9; then zeros give 0.54, 0.324, 0.1944.
        states, events = feed(session(ScoringConfig()), [0.9, 0.9, 0.9, 0.0, 0.0, 0.0])
        self.assertEqual(states, ['monitoring', 'suspicious', 'alert', 'suspicious', 'monitoring', 'monitoring'])
        self.assertEqual([e for e in events if e[1] != 'state.changed'],
                         [(3, 'alert.raised', 'alert-1'), (4, 'alert.cleared', 'alert-1')])

    def test_closed_session_refuses_more_work(self):
        s = session()
        s.end('bye', speech_seconds=0)
        with self.assertRaises(SessionClosed):
            s.add_window(0.5)
        with self.assertRaises(SessionClosed):
            s.end('bye', speech_seconds=0)

    def test_end_arguments_validated(self):
        for reason, seconds in (('hangup', 1), ('bye', -1), ('bye', float('nan')), ('bye', True), ('bye', '3')):
            with self.subTest(reason=reason, seconds=seconds):
                s = session()
                with self.assertRaises(ValueError):
                    s.end(reason, speech_seconds=seconds)
                self.assertIsNone(s.ended)  # a refused end leaves the session open

    def test_construction_validated(self):
        with self.assertRaises(TypeError):
            DirectionSession({'ema_alpha': 0.4}, call_id='c')
        with self.assertRaises(ValueError):
            DirectionSession(DIRECT, call_id='')
        with self.assertRaises(ValueError):  # agent direction is not scored by default (FR-I8)
            DirectionSession(DIRECT, call_id='c', direction='agent')
        DirectionSession(ScoringConfig(score_directions=('caller', 'agent')), call_id='c', direction='agent')

    def test_default_alert_ids_are_unique(self):
        ids = set()
        for n in range(20):
            s = DirectionSession(DIRECT, call_id=f'c{n}')
            feed(s, [0, 0.8, 0.8])
            ids.add(s.alert_id)
        self.assertEqual(len(ids), 20)

    def test_session_keeps_config_snapshot_across_reload(self):
        store = ConfigStore(DIRECT)
        s = DirectionSession(store.for_new_call().config, call_id='c')
        store.reload({'scoring': {'ema_alpha': 1.0, 'thresholds': {'suspicious_enter': 0.95}}},
                     model_window_s=4.0)
        states, _ = feed(s, [0, 0.7])
        self.assertEqual(states[-1], 'suspicious')  # old 0.60 threshold still applies


if __name__ == '__main__':
    unittest.main()
