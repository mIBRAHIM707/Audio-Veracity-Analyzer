"""Claude-authored tests (18 November 2026) for in-call RTP/jitter counts and the
degraded-call gauge of the tap (backlog 7.2; PRD FR-O6 "Prometheus metrics", FR-I2
sustained loss; services/tap/tap_app.py `receiver_live`, `calls_degraded`). Authored by
Claude (Claude Code), not by GPT-6 Astra.

Before 18 Nov the live /metrics RTP and jitter families counted a call only when it
ended, so loss on a long call stayed invisible while it ran. These cases pin the
refresh period, the sums, that the counter never goes down when a call ends, the
degraded gauge, faults and the /metrics rendering. Fake ARI, packets fed straight into
each call's session with a manual clock (the helpers of test_tap).
"""
import unittest

from services.tap.tap_app import RECEIVER_REFRESH_S, TapApp
from tests.unit import test_tap as _tap  # module, not its TestCase names: not collected twice
from tests.unit.test_tap import FakeAri, Sink, answered, rtp


class LiveCountsTests(unittest.TestCase):
    SOURCE = _tap.TapAppTests.SOURCE
    feed = _tap.TapAppTests.feed  # the same packet helper as test_tap (plain function)

    def make(self, **options):
        self.ari, self.sink, self.now = FakeAri(None), Sink(), [0.0]
        app = TapApp(self.ari, self.sink, receiver_host='host.docker.internal', bind_host='127.0.0.1',
                     clock=lambda: self.now[0], **options)
        self.addCleanup(app.close)
        return app

    def destroy(self, app, call_id):
        app.handle({'type': 'ChannelDestroyed', 'channel': {'id': call_id}})

    def test_live_counts_appear_while_the_call_runs_and_follow_the_refresh_period(self):
        self.assertEqual(RECEIVER_REFRESH_S, 1.0)
        app = self.make(jitter_depth_s=0.06)
        app.handle(answered('c1'))
        # 1.5 s of packets with a hole every 10th; polls at each arrival (feed helper).
        self.feed(app, 'c1', [(k, 0x41, 0.02 * k) for k in range(75) if k % 10 != 5])
        self.assertEqual(app.receiver_totals, {}, 'ended-only totals are unchanged while the call runs')
        session = app.calls['c1'].session
        # The last refresh was at t = 1.0 (first poll at 0.0, then every 1 s): the
        # packets read by then, not the ones after it.
        live = app.receiver_live
        self.assertGreater(live['packets'], 0)
        self.assertLess(live['packets'], session.receiver_stats()['packets'])
        self.now[0] = 2.0
        app.poll(0.0)
        self.assertIsNot(app.receiver_live, live, 'replaced, never changed in place')
        self.assertEqual(app.receiver_live, {k: v for k, v in session.receiver_stats().items()})
        self.assertEqual(app.receiver_live['packets'], 68)
        self.now[0] = 2.5  # inside the period: no new read
        held = app.receiver_live
        app.poll(0.0)
        self.assertIs(app.receiver_live, held)

    def test_live_counts_sum_open_and_ended_calls_and_never_decrease_at_an_end(self):
        app = self.make(jitter_depth_s=0.06)
        for call_id in ('c2', 'c3'):
            app.handle(answered(call_id))
        self.feed(app, 'c2', [(k, 0x42, 0.02 * k) for k in range(40)])
        self.feed(app, 'c3', [(k, 0x43, 1.0 + 0.02 * k) for k in range(40)])
        self.now[0] = 3.0
        app.poll(0.0)
        before = dict(app.receiver_live)
        first = app.calls['c2'].session.receiver_stats()
        second = app.calls['c3'].session.receiver_stats()
        self.assertEqual(before, {k: first.get(k, 0) + second.get(k, 0) for k in set(first) | set(second)})
        # c2 receives more after the last refresh, then ends: its final counts replace
        # its refreshed ones in the same step, so no kind goes down.
        app.calls['c2'].session._accept(rtp(40, b'\x42' * 160), self.SOURCE)
        self.destroy(app, 'c2')
        after = app.receiver_live
        for key, value in before.items():
            self.assertGreaterEqual(after[key], value, key)
        self.assertEqual(after['packets'], before['packets'] + 1)
        self.assertEqual(app.receiver_totals, self.sink.receiver_stats['c2'])
        self.destroy(app, 'c3')
        final = {k: self.sink.receiver_stats['c2'].get(k, 0) + self.sink.receiver_stats['c3'].get(k, 0)
                 for k in set(self.sink.receiver_stats['c2']) | set(self.sink.receiver_stats['c3'])}
        self.assertEqual((app.receiver_live, app.receiver_totals), (final, final), 'no open calls: equal')
        self.destroy(app, 'c3')  # already ended: nothing counted twice
        self.assertEqual(app.receiver_live, final)

    def test_live_counts_of_a_lost_and_retapped_call_restart_from_its_new_session(self):
        app = self.make(jitter_depth_s=0.06)
        app.handle(answered('c4'))
        self.feed(app, 'c4', [(k, 0x44, 0.02 * k) for k in range(60)])
        self.now[0] = 1.3
        app.poll(0.0)
        self.destroy(app, app.calls['c4'].media_id)  # monitor_lost: may be tapped again
        lost = self.sink.receiver_stats['c4']['packets']
        app.handle(answered('c4'))
        self.feed(app, 'c4', [(k, 0x45, 2.0 + 0.02 * k) for k in range(10)])
        self.now[0] = 3.5
        app.poll(0.0)
        self.assertEqual(app.receiver_live['packets'], lost + 10)

    def test_live_degraded_gauge_counts_open_calls_in_sustained_loss(self):
        app = self.make(jitter_depth_s=0.06)
        app.handle(answered('c5'))
        app.handle(answered('c6'))
        self.assertEqual(app.calls_degraded, 0)
        # c5: 12 s with every 10th packet lost (10 % > 5 %), as in test_tap's degraded case.
        self.feed(app, 'c5', [(k, 0x46, 0.02 * k) for k in range(600) if k % 10 != 5])
        self.feed(app, 'c6', [(k, 0x47, 12.0 + 0.02 * k) for k in range(5)])  # c6 clean, keeps c5 polled
        self.now[0] = 13.5
        app.poll(0.0)
        self.assertEqual(self.sink.events.count(('quality', 'c5', 'degraded')), 1)
        self.assertEqual(app.calls_degraded, 1)
        self.destroy(app, 'c5')
        self.assertEqual(app.calls_degraded, 0, 'an ended call no longer counts, at once')

    def test_live_an_unreadable_session_keeps_its_previous_counts(self):
        app = self.make(jitter_depth_s=0.06)
        app.handle(answered('c7'))
        self.feed(app, 'c7', [(k, 0x48, 0.02 * k) for k in range(60)])
        self.now[0] = 1.5
        app.poll(0.0)
        held = app.receiver_live['packets']
        session = app.calls['c7'].session
        original = session.receiver_stats

        def broken():
            raise TypeError('stats unavailable')
        session.receiver_stats = broken
        self.now[0] = 3.0
        app.poll(0.0)
        self.assertEqual(app.receiver_live['packets'], held)
        before = dict(app.receiver_live)
        session.receiver_stats = lambda: {'packets': -5, 'gaps': 1.5, 'jitter_played': True, 9: 1, 'missing': 2,
                                          'jitter_received': 1}
        self.now[0] = 4.5
        app.poll(0.0)
        # Review L1 (18 Nov): malformed, missing or lower values keep the earlier ones, so
        # no exported counter goes down; a valid higher value is taken.
        self.assertEqual(app.receiver_live, {**before, 'missing': max(2, before['missing'])})
        self.assertEqual(app.receiver_live['packets'], held)
        session.receiver_stats = original

    def test_live_an_unreadable_session_at_its_end_keeps_its_last_read_counts(self):
        # Review L2 (18 Nov): the end still releases the tap and counts what was read.
        app = self.make(jitter_depth_s=0.06)
        app.handle(answered('c9'))
        self.feed(app, 'c9', [(k, 0x4A, 0.02 * k) for k in range(60)])
        self.now[0] = 1.5
        app.poll(0.0)
        held = dict(app.receiver_live)
        session = app.calls['c9'].session

        def broken():
            raise TypeError('stats unavailable')
        session.receiver_stats = broken
        self.destroy(app, 'c9')
        self.assertEqual((app.receiver_live, app.receiver_totals), (held, held))
        self.assertTrue(session.closed, 'the session is still released')
        self.assertEqual(self.sink.events[-1], ('ended', 'c9', 'bye'))

    def test_live_counts_never_decrease_across_an_ssrc_change(self):
        app = self.make(jitter_depth_s=0.06)
        app.handle(answered('c10'))
        packets = [(k, 0x4B, 0.02 * k) for k in range(60)]
        packets += [(30000 + k, 0x4C, 1.2 + 0.02 * k, 99) for k in range(120)]  # new SSRC: buffer restarts
        seen = []
        session = app.calls['c10'].session
        for seq, code, at, *ssrc in packets:
            self.now[0] = at
            app.poll(0.0)
            seen.append(dict(app.receiver_live))
            session._accept(rtp(seq, bytes([code]) * 160, ssrc=ssrc[0] if ssrc else 7), self.SOURCE)
        self.now[0] = 4.0
        app.poll(0.0)
        self.destroy(app, 'c10')
        seen.append(dict(app.receiver_live))
        self.assertEqual((seen[-1]['ssrc_changes'], seen[-1]['jitter_restarts']), (1, 1))
        for earlier, later in zip(seen, seen[1:]):
            for key, value in earlier.items():
                self.assertGreaterEqual(later.get(key, 0), value, key)

    def test_live_a_clock_step_back_refreshes_rather_than_freezing(self):
        app = self.make(jitter_depth_s=0.06)
        app.handle(answered('c8'))
        self.feed(app, 'c8', [(k, 0x49, 10.0 + 0.02 * k) for k in range(30)])
        self.now[0] = 2.0  # monotonic clock stepped back (never expected; must not stop the refresh)
        app.poll(0.0)
        self.assertEqual(app.receiver_live['packets'], app.calls['c8'].session.receiver_stats()['packets'])


class LiveCountsMetricsTests(unittest.TestCase):
    def test_metrics_render_in_call_counts_and_the_degraded_gauge(self):
        from tests.unit.test_live_health import ManualClock, make_service, samples
        from services.live.health import HealthServer
        service = make_service(ManualClock())
        health = HealthServer(service, port=0)
        self.addCleanup(health.stop)
        values = samples(health.metrics())
        self.assertEqual((values['ava_live_rtp_receiver_total{kind="packets"}'],
                          values['ava_live_rtp_calls_degraded']), (0, 0))
        service.tap.receiver_live = {'packets': 300, 'jitter_concealed_frames': 12}
        service.tap.calls_degraded = 2
        values = samples(health.metrics())
        self.assertEqual((values['ava_live_rtp_receiver_total{kind="packets"}'],
                          values['ava_live_jitter_buffer_total{kind="concealed_frames"}'],
                          values['ava_live_rtp_calls_degraded']), (300, 12, 2))
        service.tap.calls_degraded = float('nan')  # corrupted: the receiver section alone is dropped
        values = samples(health.metrics())
        self.assertNotIn('ava_live_rtp_calls_degraded', values)
        self.assertIn('ava_live_tap_events_total{kind="tapped"}', values)
        self.assertEqual(values['ava_live_metrics_render_errors_total'], 1)


if __name__ == '__main__':
    unittest.main()
