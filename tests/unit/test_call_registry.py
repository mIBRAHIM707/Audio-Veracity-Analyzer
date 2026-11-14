"""FR-A3 in-memory call registry (backlog 5.2 partial).

Claude-authored test design, 14 November 2026. Envelopes are built through the real
`services.events` contract and hub; timestamps are explicit seconds.
"""
import threading
import unittest

from services.calls.registry import CallRegistry, RegistryFeed
from services.events.contract import make_envelope
from services.events.fanout import EventHub


class Stream:
    """Builds valid FR-A4 envelopes with increasing seq."""

    def __init__(self):
        self.seq = 0

    def __call__(self, kind, call_id, ts, **data):
        self.seq += 1
        return make_envelope(kind, call_id, data, seq=self.seq, ts=ts)

    def score(self, call_id, ts, score, state='monitoring', degraded=False, **extra):
        return self('score.updated', call_id, ts, score=score, state=state, degraded=degraded,
                    effective_stride_s=1.0, **extra)

    def end(self, call_id, ts, final_state='monitoring', reason='bye', speech=10.0):
        return self('call.ended', call_id, ts, final_state=final_state, reason=reason,
                    speech_seconds=speech)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.reg, self.ev = CallRegistry(), Stream()

    def test_full_call_with_alert_raised_cleared_and_reraised(self):
        r, ev = self.reg, self.ev
        self.assertTrue(r.apply(ev('call.started', 'c1', 100.0)))
        r.apply(ev.score('c1', 101.0, 0.2))
        r.apply(ev.score('c1', 102.0, 0.8, state='suspicious'))
        r.apply(ev('alert.raised', 'c1', 103.0, alert_id='a1', direction='caller'))
        r.apply(ev.score('c1', 103.0, 0.9, state='alert'))
        r.apply(ev('alert.cleared', 'c1', 110.0, alert_id='a1'))
        r.apply(ev.score('c1', 110.0, 0.5, state='suspicious'))
        r.apply(ev('alert.raised', 'c1', 120.0, alert_id='a2'))
        [summary] = r.list_calls('active')
        self.assertEqual((summary['state'], summary['status'], summary['alerts_open'],
                          summary['alerts_total'], summary['last_score']), ('alert', 'active', 1, 2, 0.5))
        self.assertEqual(summary['score_trend'], [0.2, 0.8, 0.9, 0.5])
        r.apply(ev.end('c1', 130.0, final_state='alert', reason='timeout', speech=25.5))
        self.assertEqual(r.list_calls('active'), [])
        detail = r.get_call('c1')
        self.assertEqual((detail['status'], detail['final_state'], detail['end_reason'],
                          detail['speech_seconds'], detail['ended_ts']), ('ended', 'alert', 'timeout', 25.5, 130.0))
        alerts = {a['alert_id']: a for a in detail['alerts']}
        self.assertEqual((alerts['a1']['state'], alerts['a1']['raised_ts'], alerts['a1']['cleared_ts'],
                          alerts['a1']['direction']), ('cleared', 103.0, 110.0, 'caller'))
        self.assertEqual((alerts['a2']['state'], alerts['a2']['cleared_ts']), ('ended_open', None))
        self.assertEqual(r.stats()['applied'], 9)

    def test_events_after_end_duplicates_and_stale_seq_are_ignored_and_counted(self):
        r, ev = self.reg, self.ev
        r.apply(ev('call.started', 'c1', 1.0))
        first = ev.score('c1', 2.0, 0.3)
        second = ev.score('c1', 3.0, 0.4)
        self.assertTrue(r.apply(second))
        self.assertFalse(r.apply(first), 'lower seq than already applied is stale')
        self.assertFalse(r.apply(second), 'same envelope twice')
        r.apply(ev.end('c1', 4.0))
        self.assertFalse(r.apply(ev.score('c1', 5.0, 0.9, state='suspicious')))
        detail = r.get_call('c1')
        self.assertEqual([p['score'] for p in detail['timeline']['full_resolution']], [0.4])
        self.assertEqual(detail['state'], 'monitoring')
        stats = r.stats()
        self.assertEqual((stats['duplicate_or_stale'], stats['after_end']), (2, 1))

    def test_unknown_call_is_created_implicitly_and_a_late_start_is_merged(self):
        r, ev = self.reg, self.ev
        r.apply(ev.score('mid', 50.0, 0.1))
        r.apply(ev('call.started', 'mid', 40.0))
        detail = r.get_call('mid')
        self.assertTrue(detail['started_seen'])
        self.assertEqual(detail['started_ts'], 40.0)
        self.assertEqual(r.stats()['implicit_start'], 1)
        r.apply(ev.end('gone', 60.0))  # end of a call never seen
        self.assertEqual(r.get_call('gone')['status'], 'ended')
        self.assertFalse(r.get_call('gone')['started_seen'])

    def test_clear_of_unknown_alert_and_system_health_and_null_scores(self):
        r, ev = self.reg, self.ev
        r.apply(ev('alert.cleared', 'c', 5.0, alert_id='x'))
        self.assertFalse(r.apply(ev('system.health', None, 6.0, reason='capacity')))
        r.apply(ev.score('c', 7.0, None, state='unmonitored', degraded=True))
        detail = r.get_call('c')
        self.assertEqual(detail['alerts'], [{'alert_id': 'x', 'raised_ts': None, 'direction': None,
                                             'state': 'cleared', 'cleared_ts': 5.0}])
        self.assertEqual((detail['state'], detail['degraded'], detail['last_score']), ('unmonitored', True, None))
        self.assertEqual(detail['score_trend'], [None])
        stats = r.stats()
        self.assertEqual((stats['unknown_alert_cleared'], stats['ignored_system_health']), (1, 1))

    def test_queries_return_copies_and_validate_status(self):
        r, ev = self.reg, self.ev
        r.apply(ev.score('c', 1.0, 0.5))
        detail = r.get_call('c')
        detail['timeline']['full_resolution'][0]['score'] = 99
        detail['alerts'].append('junk')
        self.assertEqual(r.get_call('c')['timeline']['full_resolution'][0]['score'], 0.5)
        self.assertEqual(r.get_call('c')['alerts'], [])
        self.assertIsNone(r.get_call('nope'))
        with self.assertRaises(ValueError):
            r.list_calls('open')
        with self.assertRaises(TypeError):
            r.apply({'type': 'score.updated'})


class TimelineTests(unittest.TestCase):
    def test_points_older_than_30_minutes_of_the_newest_are_bucketed(self):
        reg, ev = CallRegistry(), Stream()
        # 1 s points from t=1000 to t=3000 (2001 points).
        for t in range(1000, 3001):
            reg.apply(ev.score('c', float(t), (t % 10) / 10, direction='caller'))
        detail = reg.get_call('c')
        full = detail['timeline']['full_resolution']
        self.assertEqual(full[0]['ts'], 1200.0, 'horizon is newest (3000) - 1800 s, inclusive')
        self.assertEqual(len(full), 1801)
        buckets = detail['timeline']['downsampled']
        self.assertEqual(len(buckets), 20)  # 1000..1199 in 10 s buckets
        self.assertEqual(sum(b['points'] for b in buckets), 200)
        first = buckets[0]
        self.assertEqual((first['start_ts'], first['end_ts'], first['direction']), (1000.0, 1010.0, 'caller'))
        self.assertAlmostEqual(first['score_mean'], 0.45)
        self.assertEqual(first['score_max'], 0.9)
        self.assertEqual([b['start_ts'] for b in buckets], sorted(b['start_ts'] for b in buckets))

    def test_bucket_aggregates_severity_degraded_nulls_and_directions(self):
        reg, ev = CallRegistry(full_window_s=100, bucket_s=10), Stream()
        reg.apply(ev.score('c', 0.0, 0.2, direction='caller'))
        reg.apply(ev.score('c', 3.0, 0.6, state='alert', direction='caller'))
        reg.apply(ev.score('c', 5.0, None, state='unmonitored', degraded=True, direction='caller'))
        reg.apply(ev.score('c', 4.0, 0.9, state='suspicious', direction='agent'))
        reg.apply(ev.score('c', 500.0, 0.1, direction='caller'))
        buckets = reg.get_call('c')['timeline']['downsampled']
        self.assertEqual([b['direction'] for b in buckets], ['agent', 'caller'])
        agent, caller = buckets
        self.assertEqual((agent['points'], agent['score_mean'], agent['state']), (1, 0.9, 'suspicious'))
        self.assertEqual((caller['points'], caller['scored_points'], caller['state'], caller['degraded']),
                         (3, 2, 'alert', True))
        self.assertAlmostEqual(caller['score_mean'], 0.4)
        self.assertEqual(caller['score_max'], 0.6)

    def test_clock_stepping_back_does_not_pull_old_points_back_to_full_resolution(self):
        reg, ev = CallRegistry(full_window_s=100, bucket_s=10), Stream()
        reg.apply(ev.score('c', 0.0, 0.1))
        reg.apply(ev.score('c', 200.0, 0.2))
        reg.apply(ev.score('c', 50.0, 0.3))  # wall clock stepped back: older than newest - 100
        detail = reg.get_call('c')
        self.assertEqual([p['score'] for p in detail['timeline']['full_resolution']], [0.2])
        self.assertEqual(sorted(b['start_ts'] for b in detail['timeline']['downsampled']), [0.0, 50.0])

    def test_point_and_bucket_caps_bound_memory(self):
        reg, ev = CallRegistry(full_window_s=1000, bucket_s=1, max_points=5, max_buckets=3), Stream()
        for t in range(10):
            reg.apply(ev.score('c', float(t), 0.5))
        timeline = reg.get_call('c')['timeline']
        self.assertEqual([p['ts'] for p in timeline['full_resolution']], [5.0, 6.0, 7.0, 8.0, 9.0])
        self.assertEqual([b['start_ts'] for b in timeline['downsampled']], [2.0, 3.0, 4.0])
        self.assertEqual(timeline['dropped_buckets'], 2)


class LimitTests(unittest.TestCase):
    def test_active_limit_refuses_new_calls_and_ended_limit_forgets_oldest(self):
        reg, ev = CallRegistry(max_active=2, max_ended=2), Stream()
        for name in ('a', 'b'):
            reg.apply(ev('call.started', name, 1.0))
        self.assertFalse(reg.apply(ev('call.started', 'c', 1.0)))
        self.assertIsNone(reg.get_call('c'))
        for name in ('a', 'b'):
            reg.apply(ev.end(name, 2.0))
        reg.apply(ev('call.started', 'd', 3.0))
        reg.apply(ev.end('d', 4.0))
        self.assertEqual([c['call_id'] for c in reg.list_calls('ended')], ['d', 'b'])
        self.assertIsNone(reg.get_call('a'))
        stats = reg.stats()
        self.assertEqual((stats['refused_active_limit'], stats['forgotten_ended'], stats['ended']), (1, 1, 2))

    def test_invalid_limits_are_refused(self):
        for kwargs in ({'bucket_s': 0}, {'full_window_s': -1}, {'max_points': 1.5}, {'max_active': True},
                       {'max_ended': 0}, {'bucket_s': float('nan')}, {'max_alerts': 0},
                       {'idle_timeout_s': float('inf')}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                CallRegistry(**kwargs)


class RobustnessTests(unittest.TestCase):
    """Added after the 14 Nov independent review (lost call.ended, unbounded alerts,
    handler faults)."""

    def test_lost_call_ended_expires_by_event_time_and_frees_the_active_slot(self):
        reg, ev = CallRegistry(max_active=1, idle_timeout_s=60), Stream()
        reg.apply(ev('call.started', 'lost', 0.0))
        reg.apply(ev('alert.raised', 'lost', 10.0, alert_id='a'))
        # call.ended for 'lost' never arrives; another call starts 61 s of event time later.
        self.assertFalse(reg.apply(ev('call.started', 'next', 70.0 - 9.5)), 'not idle yet: slot still held')
        self.assertTrue(reg.apply(ev('call.started', 'next', 71.0)))
        lost = reg.get_call('lost')
        self.assertEqual((lost['status'], lost['end_reason'], lost['final_state'], lost['ended_ts']),
                         ('ended', 'idle_expired', 'alert', 10.0))
        self.assertEqual(lost['alerts'][0]['state'], 'ended_open')
        self.assertEqual(reg.stats()['idle_expired'], 1)

    def test_without_slot_pressure_an_idle_call_stays_active_and_is_flagged_stale(self):
        reg, ev = CallRegistry(idle_timeout_s=60), Stream()
        reg.apply(ev('call.started', 'quiet', 0.0))
        reg.apply(ev('alert.raised', 'quiet', 1.0, alert_id='a'))
        reg.apply(ev('call.started', 'busy', 100.0))
        quiet = reg.get_call('quiet')
        self.assertEqual((quiet['status'], quiet['stale'], quiet['alerts'][0]['state']), ('active', True, 'open'))
        self.assertFalse(reg.get_call('busy')['stale'])
        self.assertEqual([c['stale'] for c in reg.list_calls('active')], [True, False])
        reg.apply(ev.score('quiet', 101.0, 0.9, state='alert'))
        self.assertFalse(reg.get_call('quiet')['stale'])
        self.assertEqual(reg.stats()['idle_expired'], 0)

    def test_an_idle_expired_call_resumes_on_a_new_event_but_a_real_end_is_final(self):
        reg, ev = CallRegistry(idle_timeout_s=60, max_active=1), Stream()
        reg.apply(ev('call.started', 'quiet', 0.0))
        reg.apply(ev('alert.raised', 'quiet', 1.0, alert_id='a'))
        reg.apply(ev('call.started', 'other', 100.0))  # needs the slot: 'quiet' is expired
        self.assertEqual(reg.get_call('quiet')['status'], 'ended')
        self.assertFalse(reg.apply(ev.score('quiet', 120.0, 0.9)), "'other' is not idle: no slot")
        self.assertEqual(reg.get_call('quiet')['end_reason'], 'idle_expired')
        self.assertTrue(reg.apply(ev.score('quiet', 170.0, 0.9, state='alert')))
        quiet = reg.get_call('quiet')
        self.assertEqual((quiet['status'], quiet['end_reason'], quiet['alerts'][0]['state']),
                         ('active', None, 'open'))
        self.assertEqual(reg.get_call('other')['end_reason'], 'idle_expired')
        stats = reg.stats()
        self.assertEqual((stats['revived'], stats['idle_expired']), (1, 2))
        reg.apply(ev.end('quiet', 172.0, final_state='alert'))
        self.assertFalse(reg.apply(ev.score('quiet', 173.0, 0.1)))
        self.assertEqual(reg.get_call('quiet')['end_reason'], 'bye')

    def test_reviving_with_a_full_ended_list_keeps_the_call(self):
        reg, ev = CallRegistry(idle_timeout_s=10, max_active=1, max_ended=1), Stream()
        reg.apply(ev('call.started', 'a', 0.0))
        reg.apply(ev('call.started', 'b', 20.0))  # expires a; ended = [a]
        reg.apply(ev.score('a', 40.0, 0.5))      # expires b (ended full), revives a
        self.assertEqual(reg.get_call('a')['status'], 'active')
        self.assertEqual(reg.get_call('b')['end_reason'], 'idle_expired')
        self.assertEqual(reg.stats()['apply_errors'], 0)

    def test_alerts_per_call_are_capped_and_long_directions_are_not_stored(self):
        reg, ev = CallRegistry(max_alerts=2), Stream()
        for i in range(4):
            reg.apply(ev('alert.raised', 'c', float(i), alert_id=f'a{i}', direction='x' * 17))
        reg.apply(ev('alert.cleared', 'c', 9.0, alert_id='never-raised'))
        reg.apply(ev.score('c', 10.0, 0.5, state='alert', direction='y' * 17))
        detail = reg.get_call('c')
        self.assertEqual([a['alert_id'] for a in detail['alerts']], ['a0', 'a1'])
        self.assertEqual((detail['alerts_dropped'], detail['alerts'][0]['direction']), (3, None))
        self.assertIsNone(detail['timeline']['full_resolution'][0]['direction'])

    def test_a_failing_handler_is_counted_and_later_events_still_apply(self):
        reg, ev = CallRegistry(), Stream()
        original = reg._add_point

        def broken(call, point):
            raise RuntimeError('fault')

        reg._add_point = broken
        self.assertFalse(reg.apply(ev.score('c', 1.0, 0.5)))
        reg._add_point = original
        self.assertTrue(reg.apply(ev.score('c', 2.0, 0.6)))
        self.assertEqual(reg.stats()['apply_errors'], 1)
        self.assertEqual(reg.get_call('c')['score_trend'], [0.6])


class FeedTests(unittest.TestCase):
    def test_feed_applies_hub_events_in_order_and_counts_losses(self):
        hub, reg = EventHub(clock=lambda: 10.0), CallRegistry()
        feed = RegistryFeed(reg, hub, buffer=3)
        hub.publish('call.started', 'c', {})
        hub.publish('score.updated', 'c', {'score': 0.1, 'state': 'monitoring', 'degraded': False,
                                           'effective_stride_s': 1.0})
        self.assertEqual(feed.pump(), 2)
        for i in range(5):
            hub.publish('score.updated', 'c', {'score': i / 10, 'state': 'monitoring', 'degraded': False,
                                               'effective_stride_s': 1.0})
        self.assertEqual(feed.pump(), 3)
        self.assertEqual(reg.stats()['lost_events'], 2)
        self.assertEqual(reg.get_call('c')['score_trend'], [0.1, 0.2, 0.3, 0.4])
        feed.close()
        self.assertEqual(hub.subscriber_count, 0)

    def test_concurrent_pumps_never_apply_out_of_order(self):
        hub, reg = EventHub(clock=lambda: 10.0), CallRegistry()
        feed = RegistryFeed(reg, hub, buffer=4096)
        stop = threading.Event()

        def pump():
            while not stop.is_set():
                feed.pump()

        threads = [threading.Thread(target=pump) for _ in range(4)]
        for thread in threads:
            thread.start()
        try:
            for i in range(2000):
                hub.publish('score.updated', 'c', {'score': 0.5, 'state': 'monitoring', 'degraded': False,
                                                   'effective_stride_s': 1.0})
        finally:
            stop.set()
            for thread in threads:
                thread.join()
        feed.pump()
        stats = reg.stats()
        self.assertEqual((stats['applied'], stats['duplicate_or_stale'], stats['lost_events']), (2000, 0, 0))


if __name__ == '__main__':
    unittest.main()
