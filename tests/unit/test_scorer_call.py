"""Call-level scoring: direction selection, agent-direction semantics and the FR-I2
window quality gate (backlog 4.3; PRD FR-I8, FR-I2, FR-U1; architecture §2.2, §2.4).

Claude-authored test cases, 10 November 2026. Pure logic with literal expectations.
"""
import itertools
import unittest

from services.scorer.call import CallScorer
from services.scorer.config import ScoringConfig, parse_config
from services.scorer.session import SessionClosed

BOTH = ScoringConfig(ema_alpha=1.0, score_directions=('caller', 'agent'))
CALLER = ScoringConfig(ema_alpha=1.0)


def scorer(config=BOTH, call_id='call-1'):
    ids = (f'alert-{n}' for n in itertools.count(1))
    return CallScorer(config, call_id=call_id, new_alert_id=lambda: next(ids))


def call_states(events):
    return [(e.data['state'], e.data['driving_direction']) for e in events if e.type == 'call.state']


class DirectionSelectionTests(unittest.TestCase):
    def test_default_scores_caller_only(self):
        c = scorer(CALLER)
        c.human_agent_answered()
        self.assertEqual(c.add_window('agent', 0.9), [])
        self.assertEqual(c.ignored['direction_not_scored'], 1)
        self.assertEqual(list(c.sessions), ['caller'])
        self.assertEqual((c.state, c.driving_direction), ('monitoring', None))  # no driver yet

    def test_agent_scored_only_after_human_answer(self):
        c = scorer()
        for _ in range(3):  # IVR prompts
            self.assertEqual(c.add_window('agent', 0.95), [])
        self.assertEqual(c.ignored['before_human_answer'], 3)
        self.assertNotIn('agent', c.sessions)
        c.human_agent_answered()
        c.human_agent_answered()  # repeated signal is harmless
        events = c.add_window('agent', 0.95)
        self.assertEqual(events[0].window, 1)  # IVR windows are not evidence
        self.assertTrue(events[0].data['provisional'])
        self.assertEqual(c.sessions['agent'].windows, 1)

    def test_more_severe_direction_drives_with_caller_on_ties(self):
        c = scorer()
        c.human_agent_answered()
        timeline = []
        for direction, p in (('caller', 0.1), ('agent', 0.1), ('caller', 0.1), ('agent', 0.7),
                             ('agent', 0.8), ('agent', 0.8), ('caller', 0.7), ('caller', 0.8),
                             ('caller', 0.8), ('agent', 0.1), ('caller', 0.5), ('caller', 0.1)):
            timeline += call_states(c.add_window(direction, p))
        # Worked by hand (alpha 1, min_windows 2, default thresholds):
        self.assertEqual(timeline, [
            ('suspicious', 'agent'),    # agent window 2 at 0.7; caller still monitoring
            ('alert', 'agent'),         # agent window 4, second 0.8 in a row
            ('alert', 'caller'),        # caller window 5 also alert: tie goes to the caller
            ('suspicious', 'caller'),   # agent cleared earlier; caller 0.5 < 0.55: tie again
            ('suspicious', 'agent'),    # caller 0.1 < 0.40 -> monitoring; agent drives
        ])
        self.assertEqual((c.sessions['caller'].state, c.sessions['agent'].state), ('monitoring', 'suspicious'))
        self.assertEqual((c.state, c.driving_direction), ('suspicious', 'agent'))

    def test_agent_only_configuration(self):
        c = scorer(ScoringConfig(score_directions=('agent',)))
        self.assertEqual((c.state, c.driving_direction), ('monitoring', None))
        self.assertEqual(c.add_window('caller', 0.9), [])
        with self.assertRaisesRegex(ValueError, 'reason'):  # validated even with no sessions
            c.end('hangup', speech_seconds={})
        self.assertIsNone(c.ended)
        summary = c.end('bye', speech_seconds={})[-1].data
        self.assertEqual((summary['verdict'], summary['driving_direction']), ('undetermined', None))
        c = scorer(ScoringConfig(score_directions=('agent',)))
        c.human_agent_answered()
        self.assertEqual([e.type for e in c.add_window('agent', 0.1)], ['score.updated'])  # no call.state

    def test_call_state_event_names_the_window_direction(self):
        c = scorer()
        c.human_agent_answered()
        c.add_window('agent', 0.7)
        event = [e for e in c.add_window('agent', 0.7) if e.type == 'call.state'][0]
        self.assertEqual((event.data['window_direction'], event.data['driving_direction']), ('agent', 'agent'))


class QualityGateTests(unittest.TestCase):
    def test_threshold_is_strictly_more_than_ten_percent(self):
        c = scorer(CALLER)
        self.assertEqual(c.add_window('caller', 0.2, concealed_fraction=0.10)[0].type, 'score.updated')
        self.assertEqual(c.quality, 'ok')
        events = c.add_window('caller', 0.2, concealed_fraction=0.1000001)
        self.assertEqual([e.type for e in events], ['call.quality'])
        self.assertEqual(events[0].data['quality'], 'low')
        self.assertEqual((c.quality, c.sessions['caller'].windows, c.dropped_low_quality['caller']),
                         ('low', 1, 1))

    def test_dropped_windows_are_not_evidence_and_quality_stays_low(self):
        c = scorer(CALLER)
        c.add_window('caller', 0.9)
        self.assertEqual(c.add_window('caller', 0.9, concealed_fraction=1.0)[0].type, 'call.quality')
        self.assertEqual(c.add_window('caller', 0.9, concealed_fraction=0.5), [])  # one event only
        self.assertEqual(c.sessions['caller'].windows, 1)
        self.assertEqual(c.sessions['caller'].state, 'monitoring')
        events = c.add_window('caller', 0.9)
        self.assertEqual([e for e in events if e.type == 'call.state'][0].data['quality'], 'low')
        summary = c.end('bye', speech_seconds={'caller': 6})[-1].data
        self.assertEqual((summary['quality'], summary['dropped_low_quality']['caller']), ('low', 2))

    def test_configured_gate_is_used(self):
        config = parse_config({'audio': {'max_concealed_fraction': 0.2}, 'scoring': {'ema_alpha': 1}},
                              model_window_s=4.0)
        self.assertEqual(config.max_concealed_fraction, 0.2)
        self.assertEqual(parse_config({}, model_window_s=4.0).max_concealed_fraction, 0.10)
        c = scorer(config)
        self.assertEqual(c.add_window('caller', 0.5, concealed_fraction=0.15)[0].type, 'score.updated')
        self.assertEqual(c.add_window('caller', 0.5, concealed_fraction=0.21)[0].type, 'call.quality')


class EndAndFailureTests(unittest.TestCase):
    def test_summary_with_open_alert_per_direction(self):
        c = scorer()
        c.human_agent_answered()
        for p in (0.8, 0.8, 0.8):
            c.add_window('agent', p)
        c.add_window('caller', 0.2)
        ended = c.end('monitor_lost', speech_seconds={'caller': 2.0, 'agent': 6.0})
        self.assertEqual([e.type for e in ended], ['call.ended', 'call.ended', 'call.summary'])
        summary = ended[-1].data
        self.assertEqual(summary['final_state'], 'alert')
        self.assertEqual(summary['driving_direction'], 'agent')
        self.assertEqual(summary['verdict'], 'alert')  # agent has enough evidence
        self.assertEqual(summary['ended_open_alert_ids'], {'agent': 'alert-1'})
        self.assertEqual(ended[0].data['verdict'], 'undetermined')  # caller had one window
        self.assertEqual(summary['ignored'], {'direction_not_scored': 0, 'before_human_answer': 0})

    def test_clean_agent_does_not_make_short_caller_genuine(self):
        c = scorer()
        c.human_agent_answered()
        c.add_window('caller', 0.1)
        for _ in range(3):
            c.add_window('agent', 0.1)
        summary = c.end('bye', speech_seconds={'caller': 2, 'agent': 6})[-1].data
        self.assertEqual((summary['final_state'], summary['verdict']), ('monitoring', 'undetermined'))
        c = scorer()
        c.human_agent_answered()
        for _ in range(2):
            c.add_window('caller', 0.1)
            c.add_window('agent', 0.1)
        self.assertEqual(c.end('bye', speech_seconds={'caller': 4, 'agent': 4})[-1].data['verdict'],
                         'monitoring')

    def test_quality_gate_applies_only_to_windows_that_would_be_scored(self):
        c = scorer()
        c.add_window('agent', 0.5, concealed_fraction=0.9)  # before the human answer
        self.assertEqual((c.quality, c.ignored['before_human_answer']), ('ok', 1))
        c = scorer(CALLER)
        c.add_window('agent', 0.5, concealed_fraction=0.9)  # direction not scored
        self.assertEqual(c.quality, 'ok')

    def test_failing_alert_id_generator_leaves_call_unchanged(self):
        def broken():
            raise RuntimeError('id service down')
        c = CallScorer(CALLER, call_id='c', new_alert_id=broken)
        c.add_window('caller', 0.8)
        c.add_window('caller', 0.8)
        before = (c.state, c.driving_direction, c.sessions['caller'].windows)
        with self.assertRaises(RuntimeError):
            c.add_window('caller', 0.8)
        self.assertEqual((c.state, c.driving_direction, c.sessions['caller'].windows), before)

    def test_all_directions_short_is_undetermined(self):
        c = scorer()
        c.human_agent_answered()
        c.add_window('caller', 0.0)
        c.add_window('agent', 0.0)
        self.assertEqual(c.end('bye', speech_seconds={'caller': 3, 'agent': 3})[-1].data['verdict'],
                         'undetermined')

    def test_invalid_window_input_changes_nothing(self):
        c = scorer()
        c.human_agent_answered()
        c.add_window('caller', 0.5)
        before = (c.sessions['caller'].windows, dict(c.ignored), dict(c.dropped_low_quality), c.quality)
        for direction, p, concealed in (('supervisor', 0.5, 0), ('caller', 1.5, 0), ('caller', 0.5, -0.1),
                                        ('caller', 0.5, float('nan')), ('caller', 0.5, True),
                                        ('agent', None, 0), ('caller', 0.5, 1.01)):
            with self.subTest(direction=direction, p=p, concealed=concealed):
                with self.assertRaises(ValueError):
                    c.add_window(direction, p, concealed_fraction=concealed)
                self.assertEqual((c.sessions['caller'].windows, dict(c.ignored),
                                  dict(c.dropped_low_quality), c.quality), before)

    def test_invalid_end_ends_nothing(self):
        for speech in ({'caller': 1}, {'caller': 1, 'agent': -1}, {'caller': 1, 'agent': 1, 'x': 1},
                       [1, 1], {'caller': 1, 'agent': float('nan')}):
            with self.subTest(speech=speech):
                c = scorer()
                c.human_agent_answered()
                with self.assertRaises(ValueError):
                    c.end('bye', speech_seconds=speech)
                self.assertIsNone(c.ended)
                self.assertIsNone(c.sessions['caller'].ended)
                self.assertIsNone(c.sessions['agent'].ended)
        c = scorer(CALLER)
        with self.assertRaises(ValueError):
            c.end('hangup', speech_seconds={'caller': 1})
        self.assertIsNone(c.sessions['caller'].ended)

    def test_ended_call_refuses_more_work(self):
        c = scorer(CALLER)
        c.end('timeout', speech_seconds={'caller': 0})
        for action in (lambda: c.add_window('caller', 0.5), c.human_agent_answered,
                       lambda: c.end('bye', speech_seconds={'caller': 0})):
            with self.assertRaises(SessionClosed):
                action()

    def test_construction_validated(self):
        with self.assertRaises(TypeError):
            CallScorer({'scoring': {}}, call_id='c')
        for call_id in ('', None, 5):
            with self.subTest(call_id=call_id):
                with self.assertRaises(ValueError):
                    CallScorer(ScoringConfig(score_directions=('agent',)), call_id=call_id)


if __name__ == '__main__':
    unittest.main()
