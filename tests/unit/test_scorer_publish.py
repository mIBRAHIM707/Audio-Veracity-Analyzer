"""Scorer -> FR-A4 event mapping and publishing (backlog 5.2 partial).

Claude-authored test design, 14 November 2026. Real `OrderedCallScorer`, real
`EventHub` and contract, real `CallRegistry`; `ema_alpha=1.0` so EMA equals the
window score and the hand-worked timelines below are exact (defaults: min 2 windows,
suspicious 0.60/0.40, alert 0.75/0.55, alert after 2 consecutive windows).
"""
import itertools
import unittest

from services.calls.registry import CallRegistry, RegistryFeed
from services.events.fanout import EventHub
from services.scorer.config import ScoringConfig
from services.scorer.ordered import OrderedCallScorer
from services.scorer.publish import CallEventPublisher, map_end_events, map_window_events
from services.scorer.session import Event

CALLER = ScoringConfig(ema_alpha=1.0)
BOTH = ScoringConfig(ema_alpha=1.0, score_directions=('caller', 'agent'))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        self.t += 1.0
        return self.t


def make(config=CALLER, call_id='c1'):
    ids = (f'alert-{i}' for i in itertools.count(1))
    hub = EventHub(clock=Clock())
    sub = hub.subscribe(buffer=4096)
    scorer = OrderedCallScorer.create(config, call_id=call_id, new_alert_id=lambda: next(ids))
    return hub, sub, scorer, CallEventPublisher(hub, call_id)


def drain(sub):
    envelopes, dropped = sub.drain()
    assert dropped == 0
    return envelopes


class WindowMappingTests(unittest.TestCase):
    def test_caller_timeline_publishes_scores_alert_raise_and_clear(self):
        hub, sub, scorer, pub = make()
        pub.started(source='fixture')
        timeline = []
        for idx, score in enumerate((0.9, 0.9, 0.9, 0.1, 0.1)):
            result = scorer.add_result('caller', idx, score)
            pub.window(result['events'], degraded=idx == 4, effective_stride_s=1.0 if idx < 4 else 2.0)
            timeline.append([(e.type, e.data.get('state'), e.data.get('alert_id')) for e in drain(sub)])
        self.assertEqual(timeline[0][0], ('call.started', None, None))
        self.assertEqual(timeline[0][1:], [('score.updated', 'monitoring', None)])
        self.assertEqual(timeline[1], [('score.updated', 'suspicious', None)])
        self.assertEqual(timeline[2], [('score.updated', 'alert', None), ('alert.raised', None, 'alert-1')])
        self.assertEqual(timeline[3], [('score.updated', 'suspicious', None), ('alert.cleared', None, 'alert-1')])
        self.assertEqual(timeline[4], [('score.updated', 'monitoring', None)])
        self.assertEqual(pub.failures, {})
        self.assertEqual(pub.published, 8)

    def test_score_updated_fields_follow_the_window_and_the_caller_supplied_stride(self):
        hub, sub, scorer, pub = make()
        pub.window(scorer.add_result('caller', 0, 0.3)['events'], degraded=True, effective_stride_s=4.0)
        [envelope] = drain(sub)
        self.assertEqual(envelope.data, {
            'score': 0.3, 'ema': 0.3, 'provisional': True, 'direction': 'caller',
            'direction_state': 'monitoring', 'direction_alert_id': None, 'window': 1, 'scored': True,
            'state': 'monitoring', 'driving_direction': None, 'quality': 'ok', 'degraded': True,
            'effective_stride_s': 4.0})
        self.assertEqual(envelope.call_id, 'c1')

    def test_agent_direction_drives_the_call_state_on_caller_updates_too(self):
        hub, sub, scorer, pub = make(BOTH)
        scorer.human_agent_answered()
        for idx in range(2):
            pub.window(scorer.add_result('agent', idx, 0.7)['events'], effective_stride_s=1.0)
        pub.window(scorer.add_result('caller', 0, 0.1)['events'], effective_stride_s=1.0)
        last = drain(sub)[-1]
        self.assertEqual((last.data['direction'], last.data['direction_state'], last.data['state'],
                          last.data['driving_direction']), ('caller', 'monitoring', 'suspicious', 'agent'))

    def test_quality_drop_is_published_as_an_unscored_update_and_sticks(self):
        hub, sub, scorer, pub = make()
        pub.window(scorer.add_result('caller', 0, 0.2, concealed_fraction=0.5)['events'], effective_stride_s=1.0)
        [gated] = drain(sub)
        self.assertEqual((gated.type, gated.data['score'], gated.data['scored'], gated.data['quality'],
                          gated.data['concealed_fraction']), ('score.updated', None, False, 'low', 0.5))
        pub.window(scorer.add_result('caller', 1, 0.2, concealed_fraction=0.5)['events'], effective_stride_s=1.0)
        self.assertEqual(drain(sub), [], 'later gated windows publish nothing (scorer emits nothing)')
        pub.window(scorer.add_result('caller', 2, 0.2)['events'], effective_stride_s=1.0)
        [scored] = drain(sub)
        self.assertEqual((scored.data['scored'], scored.data['quality']), (True, 'low'))

    def test_rejected_results_and_internal_events_publish_nothing(self):
        hub, sub, scorer, pub = make()
        scorer.add_result('caller', 5, 0.2)
        duplicate = scorer.add_result('caller', 5, 0.2)
        self.assertEqual(pub.window(duplicate['events'], effective_stride_s=1.0), [])
        internal = [Event('state.changed', 1, {'call_id': 'c1'})]
        published, state, unknown = map_window_events(internal, call_state=pub.call_state, degraded=False,
                                                      effective_stride_s=1.0)
        self.assertEqual((published, state, unknown), ([], pub.call_state, []))
        self.assertEqual(drain(sub), [])

    def test_alert_events_carry_direction_ema_and_window(self):
        hub, sub, scorer, pub = make()
        for idx, score in enumerate((0.9, 0.9, 0.8, 0.2)):
            pub.window(scorer.add_result('caller', idx, score)['events'], effective_stride_s=1.0)
        alerts = [e for e in drain(sub) if e.type.startswith('alert.')]
        self.assertEqual([(e.type, e.data) for e in alerts], [
            ('alert.raised', {'alert_id': 'alert-1', 'direction': 'caller', 'ema': 0.8, 'window': 3}),
            ('alert.cleared', {'alert_id': 'alert-1', 'direction': 'caller', 'ema': 0.2, 'window': 4})])

    def test_agent_alert_while_caller_suspicious_drives_the_call(self):
        hub, sub, scorer, pub = make(BOTH)
        scorer.human_agent_answered()
        for idx in range(2):
            pub.window(scorer.add_result('caller', idx, 0.65)['events'], effective_stride_s=1.0)
        for idx in range(3):
            pub.window(scorer.add_result('agent', idx, 0.95)['events'], effective_stride_s=1.0)
        events = drain(sub)
        raised = [e for e in events if e.type == 'alert.raised']
        self.assertEqual([(e.data['direction'], e.data['alert_id']) for e in raised], [('agent', 'alert-1')])
        last_score = [e for e in events if e.type == 'score.updated'][-1]
        self.assertEqual((last_score.data['state'], last_score.data['driving_direction']), ('alert', 'agent'))
        self.assertEqual(pub.call_state, {'state': 'alert', 'driving_direction': 'agent', 'quality': 'ok'})


class EndMappingTests(unittest.TestCase):
    def test_call_level_end_sums_speech_and_keeps_open_alerts(self):
        hub, sub, scorer, pub = make(BOTH)
        scorer.human_agent_answered()
        for idx in range(3):
            pub.window(scorer.add_result('caller', idx, 0.9)['events'], effective_stride_s=1.0)
        pub.window(scorer.add_result('agent', 0, 0.1)['events'], effective_stride_s=1.0)
        drain(sub)
        pub.ended(scorer.end('bye', speech_seconds={'caller': 7.5, 'agent': 2.0}))
        [ended] = drain(sub)
        self.assertEqual(ended.type, 'call.ended')
        self.assertEqual(ended.data, {
            'final_state': 'alert', 'reason': 'bye', 'speech_seconds': 9.5,
            'speech_seconds_by_direction': {'caller': 7.5, 'agent': 2.0},
            'windows_by_direction': {'caller': 3, 'agent': 1}, 'verdict': 'alert',
            'driving_direction': 'caller', 'quality': 'ok', 'ended_open_alert_ids': {'caller': 'alert-1'}})

    def test_short_call_ends_undetermined(self):
        hub, sub, scorer, pub = make()
        pub.window(scorer.add_result('caller', 0, 0.1)['events'], effective_stride_s=1.0)
        pub.ended(scorer.end('timeout', speech_seconds={'caller': 1.0}))
        ended = drain(sub)[-1]
        self.assertEqual((ended.data['final_state'], ended.data['verdict'], ended.data['reason']),
                         ('monitoring', 'undetermined', 'timeout'))

    def test_malformed_end_events_are_refused_by_the_pure_mapper(self):
        with self.assertRaises(ValueError):
            map_end_events([])
        with self.assertRaises(ValueError):
            map_end_events([Event('call.summary', 0, {}), Event('score.updated', 0, {})])


class FailureContainmentTests(unittest.TestCase):
    def test_hub_and_contract_failures_are_counted_never_raised_and_state_still_tracks(self):
        def broken_clock():
            raise OSError('clock')

        hub = EventHub(clock=broken_clock)
        scorer = OrderedCallScorer.create(CALLER, call_id='c', new_alert_id=lambda: 'a')
        pub = CallEventPublisher(hub, 'c')
        for idx in range(2):
            self.assertEqual(pub.window(scorer.add_result('caller', idx, 0.9)['events'], effective_stride_s=1.0), [])
        self.assertEqual(pub.call_state['state'], 'suspicious', 'state follows the scorer even when publishing fails')
        self.assertEqual(pub.failures, {'EventHubError': 2})
        good = EventHub()
        pub2 = CallEventPublisher(good, 'c')
        self.assertEqual(pub2.status('suspended', degraded=False, effective_stride_s=1.0), [])
        self.assertEqual(pub2.status('alert', degraded=False, effective_stride_s=1.0), [],
                         'alert states come only from the scorer')
        # The contract refuses a non-positive stride (only reachable through a caller bug).
        self.assertEqual(pub2.status('unmonitored', degraded=True, effective_stride_s=0.0), [])
        self.assertEqual(pub2.window([Event('mystery', 1, {'call_id': 'c'})], effective_stride_s=1.0), [])
        self.assertEqual(pub2.window([Event('score.updated', 1, {'call_id': 'other'})], effective_stride_s=1.0), [])
        self.assertEqual(pub2.failures, {'mapping:ValueError': 3, 'EventContractError': 1, 'unknown_event': 1})
        with self.assertRaises(ValueError):
            CallEventPublisher(good, '')

    def test_a_mapping_failure_does_not_lose_the_call_state(self):
        """Review M1: an alert window published with a bad stride must still move the
        tracked state, so the next window does not publish a stale `suspicious`."""
        hub, sub, scorer, pub = make()
        for idx in range(2):
            pub.window(scorer.add_result('caller', idx, 0.9)['events'], effective_stride_s=1.0)
        self.assertEqual(pub.window(scorer.add_result('caller', 2, 0.9)['events'], effective_stride_s=None), [])
        self.assertEqual(pub.call_state['state'], 'alert')
        pub.window(scorer.add_result('caller', 3, 0.9)['events'], effective_stride_s=1.0)
        last = drain(sub)[-1]
        self.assertEqual((last.type, last.data['state']), ('score.updated', 'alert'))
        self.assertEqual(pub.failures, {'mapping:TypeError': 1})

    def test_unknown_events_are_skipped_but_the_rest_of_the_window_is_published(self):
        hub, sub, scorer, pub = make()
        events = scorer.add_result('caller', 0, 0.4)['events'] + [Event('future.type', 1, {'call_id': 'c1'})]
        [env] = pub.window(events, effective_stride_s=1.0)
        self.assertEqual(env.type, 'score.updated')
        self.assertEqual(pub.failures, {'unknown_event': 1})

    def test_end_with_agent_configured_but_never_answered(self):
        hub, sub, scorer, pub = make(BOTH)
        pub.window(scorer.add_result('agent', 0, 0.9)['events'], effective_stride_s=1.0)  # IVR: ignored
        self.assertEqual(scorer.scored_directions(), ('caller',))
        [ended] = pub.ended(scorer.end('bye', speech_seconds={'caller': 4.0}))
        self.assertEqual((ended.data['speech_seconds'], ended.data['windows_by_direction'], ended.data['verdict']),
                         (4.0, {'caller': 0}, 'undetermined'))

    def test_status_publishes_unscored_state(self):
        hub, sub, scorer, pub = make()
        [env] = pub.status('unmonitored', degraded=True, effective_stride_s=4.0, reason='stride_cap')
        self.assertEqual((env.data['state'], env.data['score'], env.data['scored'], env.data['reason']),
                         ('unmonitored', None, False, 'stride_cap'))


class EndToEndTests(unittest.TestCase):
    def test_scorer_to_registry_through_the_hub(self):
        hub, sub, scorer, pub = make()
        registry = CallRegistry()
        feed = RegistryFeed(registry, hub)
        pub.started()
        for idx, score in enumerate((0.9, 0.9, 0.9, 0.1, 0.9, 0.9)):
            pub.window(scorer.add_result('caller', idx, score)['events'], effective_stride_s=1.0)
        pub.ended(scorer.end('bye', speech_seconds={'caller': 12.0}))
        feed.pump()
        detail = registry.get_call('c1')
        self.assertEqual((detail['status'], detail['final_state'], detail['speech_seconds'], detail['started_seen']),
                         ('ended', 'alert', 12.0, True))
        self.assertEqual([(a['alert_id'], a['state']) for a in detail['alerts']],
                         [('alert-1', 'cleared'), ('alert-2', 'ended_open')])
        self.assertEqual([p['score'] for p in detail['timeline']['full_resolution']], [0.9, 0.9, 0.9, 0.1, 0.9, 0.9])
        self.assertEqual(registry.stats()['applied'], 11)


if __name__ == '__main__':
    unittest.main()
