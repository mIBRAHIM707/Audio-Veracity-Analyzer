"""FR-O6 structured JSON operations log of the live service (backlog 7.2;
services/live/eventlog.py and its command-line wiring).

Claude-authored test design, 17 November 2026. A real EventHub and LiveCalls publish the
lifecycle events; the service's counters are a stand-in object whose numbers the tests
change between pumps. Covers the line format, the field allowlists (no caller number,
score or other data ever written), score updates never logged, counter deltas against
the construction-time baseline (merged once per second), detector failure and recovery
transitions, service lines exempt from the rate limit, the rate limit and hub overflow
reported as `log_dropped`, ASCII output, a failing stream, validation and the command
line.
"""
from datetime import datetime
import io
import json
from types import SimpleNamespace
import unittest

from services.events.fanout import MAX_BUFFER, EventHub
from services.inference.capacity import CallGate
from services.live.eventlog import FAILING_AFTER, EventLog
from services.scorer.live import LiveCalls
from tests.unit.test_scorer_pipeline import Configs


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def stand_in_service():
    return SimpleNamespace(counts={'connects': 0, 'disconnects': 0, 'loop_errors': 0, 'hook_errors': 0,
                                   'shutdown_errors': 0},
                           tap=SimpleNamespace(stats={'tap_failures': 0, 'bad_events': 0, 'sink_errors': 0,
                                                      'refused_capacity': 0}),
                           sink=SimpleNamespace(counts={'pump_errors': 0, 'errors': 0, 'recorder_errors': 0}),
                           pipeline=SimpleNamespace(counts={'result_errors': 0, 'unmonitored_entered': 0}),
                           detector_failure_run=0)


class EventLogTests(unittest.TestCase):
    def setUp(self):
        self.hub = EventHub(clock=lambda: 1763366400.25)  # hub lines carry the event's own time
        self.live = LiveCalls(Configs(), self.hub, CallGate(8), new_alert_id=iter(f'alert-{i}' for i in range(9)).__next__)
        self.service = stand_in_service()
        self.stream = io.StringIO()
        self.mono = Clock()
        self.log = EventLog(self.hub, self.service, self.stream, wall=lambda: 1763366499.0, clock=self.mono)

    def tick(self):
        """One pump a second later (counters are compared at most once per second)."""
        self.mono.t += 1.0
        return self.log.pump()

    def lines(self):
        text, self.stream = self.stream.getvalue(), io.StringIO()
        self.log.stream = self.stream
        return [json.loads(line) for line in text.splitlines()]

    def test_lifecycle_events_with_allowlisted_fields_only(self):
        self.live.start('c1', from_number='03001234567', to='700', tap_format='ulaw')
        for index in range(3):
            self.live.result('c1', 'caller', index, 0.9, effective_stride_s=2.0)
        self.hub.publish('system.health', 'c1', {'reason': 'media_quality', 'media_state': 'degraded',
                                                 'secret_extra': '03001234567'})
        self.live.end('c1', 'bye', speech_seconds={'caller': 6.0})
        self.log.pump()
        text = self.stream.getvalue()
        lines = self.lines()
        events = [line['event'] for line in lines]
        self.assertEqual(events[0], 'call_started')
        self.assertIn('alert_raised', events)
        self.assertEqual(events[-1], 'call_ended')
        self.assertNotIn('score_updated', events, 'per-window scores are never logged')
        for secret in ('03001234567', 'secret_extra', '"score"', '"ema"', 'speech_seconds'):
            self.assertNotIn(secret, text)
        started = lines[0]
        self.assertEqual(started, {'ts': '2025-11-17T08:00:00.250Z', 'level': 'info', 'event': 'call_started',
                                   'call_id': 'c1'}, 'the event time, not the write time (review L4)')
        alert = next(line for line in lines if line['event'] == 'alert_raised')
        self.assertEqual((alert['level'], alert['alert_id'], alert['direction']), ('warning', 'alert-0', 'caller'))
        health = next(line for line in lines if line['event'] == 'system_health')
        self.assertEqual(set(health), {'ts', 'level', 'event', 'call_id', 'reason', 'media_state'})
        ended = lines[-1]
        self.assertEqual(set(ended), {'ts', 'level', 'event', 'call_id', 'reason', 'final_state'})
        self.assertEqual(ended['reason'], 'bye')
        self.assertEqual(datetime.fromisoformat(started['ts'].replace('Z', '+00:00')).timestamp(), 1763366400.25)

    def test_counter_deltas_after_a_silent_baseline(self):
        self.service.counts['connects'] = 4  # history before the log was built: not replayed
        self.log.close()
        self.log = EventLog(self.hub, self.service, self.stream, clock=self.mono)
        self.log.pump()
        self.assertEqual(self.lines(), [])
        self.service.counts['connects'] = 5
        self.service.counts['disconnects'] = 2
        self.service.tap.stats['refused_capacity'] = 3
        self.service.sink.counts['recorder_errors'] = 1
        self.tick()
        got = {line['event']: (line['level'], line['count']) for line in self.lines()}
        self.assertEqual(got, {'ari_connected': ('info', 1), 'ari_disconnected': ('warning', 2),
                               'call_refused_capacity': ('warning', 3), 'retention_error': ('error', 1)})
        self.tick()
        self.assertEqual(self.lines(), [], 'nothing new, nothing written')

    def test_detector_failure_run_is_logged_once_each_way(self):
        self.log.pump()
        for run in (1, FAILING_AFTER - 1):
            self.service.detector_failure_run = run
            self.tick()
        self.assertEqual(self.lines(), [])
        for run in (FAILING_AFTER, FAILING_AFTER + 5):
            self.service.detector_failure_run = run
            self.tick()
        self.assertEqual([(l['event'], l['failed_batches_in_a_row']) for l in self.lines()],
                         [('detector_failing', FAILING_AFTER)])
        self.service.detector_failure_run = 0
        self.tick()
        self.tick()
        self.assertEqual([l['event'] for l in self.lines()], ['detector_recovered'])

    def test_a_rising_counter_is_merged_not_flooded(self):
        # Re-review N1: a fault on every turn (back-to-back pumps) must stay bounded.
        log = EventLog(self.hub, self.service, self.stream, max_lines_per_s=5, clock=self.mono)
        for _ in range(100):
            self.service.tap.stats['bad_events'] += 1
            log.pump()  # same instant: the PumpThread's catch-up turns
        self.mono.t += 1.0
        log.pump()
        counts = [json.loads(line)['count'] for line in self.stream.getvalue().splitlines()]
        self.assertEqual(counts, [1, 99], 'one line at the first turn, the rest merged a second later')

    def test_rate_limit_drops_and_reports_lost_lines(self):
        log = EventLog(self.hub, self.service, self.stream, max_lines_per_s=5, clock=self.mono)
        for i in range(12):
            self.hub.publish('call.started', f'c{i}', {})
        self.assertEqual(log.pump(), 5)
        self.mono.t += 1.0  # a second later: budget for 5 more, the first being the report
        self.assertEqual(log.pump(), 1)
        lines = [json.loads(line) for line in self.stream.getvalue().splitlines()]
        self.assertEqual([l['event'] for l in lines], ['call_started'] * 5 + ['log_dropped'])
        self.assertEqual(lines[-1]['count'], 7)
        self.assertEqual(log.counts['dropped'], 7)

    def test_the_first_connect_after_construction_is_logged(self):
        # Review L2: the baseline is taken when the log is built, before the service runs.
        self.service.counts['connects'] = 1
        self.log.pump()
        self.assertEqual([(l['event'], l['count']) for l in self.lines()], [('ari_connected', 1)])

    def test_service_lines_survive_a_burst_of_call_events(self):
        # Review M1: an ARI drop with many calls ending must not cost the drop or the
        # detector failure lines, whatever the rate limit.
        log = EventLog(self.hub, self.service, self.stream, max_lines_per_s=5, clock=self.mono)
        for i in range(60):
            self.hub.publish('call.started', f'c{i}', {})
        self.service.counts['disconnects'] = 1
        self.service.detector_failure_run = FAILING_AFTER
        log.pump()
        events = [json.loads(line)['event'] for line in self.stream.getvalue().splitlines()]
        self.assertEqual(events[:2], ['ari_disconnected', 'detector_failing'])
        self.assertEqual(events.count('call_started'), 3, 'the rest of the budget')
        self.assertEqual(log.counts['dropped'], 57)

    def test_lines_are_ascii_json_whatever_the_ids(self):
        # Review L1: valid JSON on any console encoding, no raw control characters.
        self.hub.publish('call.started', 'cé\u009b[31m', {})
        self.log.pump()
        text = self.stream.getvalue()
        self.assertTrue(text.isascii())
        self.assertEqual(json.loads(text)['call_id'], 'cé\u009b[31m')

    def test_hub_overflow_is_reported(self):
        for i in range(MAX_BUFFER + 3):
            self.hub.publish('score.updated', 'c1', {'score': 0.1, 'state': 'monitoring', 'degraded': False,
                                                     'effective_stride_s': 2.0})
        self.log.pump()
        lines = self.lines()
        self.assertEqual([(l['event'], l['count']) for l in lines], [('log_dropped', 3)])
        self.assertEqual(self.log.counts['hub_dropped'], 3, 'review L5: exported as the pump hub_dropped')

    def test_a_broken_stream_is_counted_never_raised(self):
        class Broken:
            def write(self, text):
                raise OSError('pipe closed')

            def flush(self):
                pass
        log = EventLog(self.hub, self.service, Broken())
        self.hub.publish('call.started', 'c1', {})
        self.assertEqual(log.pump(), 0)
        self.assertEqual(log.counts['write_failures'], 1)
        missing = EventLog(self.hub, SimpleNamespace(counts={}), io.StringIO())
        self.assertEqual(missing.pump(), 0)
        self.assertEqual(missing.counts['read_failures'], 2, 'the baseline at construction and the turn')

    def test_unsafe_values_are_cut_or_dropped(self):
        self.hub.publish('alert.raised', 'c1', {'alert_id': 'a' * 500, 'direction': {'nested': 'x'}})
        self.log.pump()
        line = self.lines()[0]
        self.assertEqual((len(line['alert_id']), line['direction']), (128, None))

    def test_validation_and_close(self):
        for bad in (0, 10_001, True, 2.5):
            with self.assertRaises(ValueError):
                EventLog(self.hub, self.service, self.stream, max_lines_per_s=bad)
        count = self.hub.subscriber_count
        self.log.close()
        self.assertEqual(self.hub.subscriber_count, count - 1)


class CommandLineTests(unittest.TestCase):
    def harness(self, hub):
        from tests.unit import test_live_cli
        harness = test_live_cli.Harness()
        made = harness.service

        def service(*args, **kwargs):
            fake = made(*args, **kwargs)
            fake.hub, fake.detector_failure_run = hub, 0
            fake.tap.stats.update(tap_failures=0, bad_events=0, sink_errors=0, refused_capacity=0)
            fake.pipeline.counts.update(result_errors=0, unmonitored_entered=0)
            return fake
        harness.service = service

        def on_run(stop):
            hub.publish('call.started', 'c1', {'from_number': '03001234567'})
            hub.publish('call.ended', 'c1', {'final_state': 'monitoring', 'reason': 'bye', 'speech_seconds': 3.0})
            stop.set()
        harness.on_run = on_run
        return harness

    def test_default_writes_json_lines_between_start_and_stop(self):
        from tests.unit.test_live_cli import BASE, EXIT_OK, run
        code, lines, text = run(BASE, self.harness(EventHub()))
        self.assertEqual(code, EXIT_OK, text)
        events = [line['event'] for line in lines]
        self.assertEqual(events[0], 'starting')
        self.assertEqual(events[-1], 'stopped')
        self.assertEqual(events[1:-1], ['call_started', 'call_ended'], 'drained at stop, in order')
        self.assertNotIn('03001234567', text)
        self.assertEqual(lines[-1]['eventlog']['counts']['lines'], 2)

    def test_off_writes_no_event_lines(self):
        from tests.unit.test_live_cli import BASE, EXIT_OK, run
        code, lines, text = run(BASE + ['--event-log', 'off'], self.harness(EventHub()))
        self.assertEqual(code, EXIT_OK, text)
        self.assertEqual([line['event'] for line in lines], ['starting', 'stopped'])


if __name__ == '__main__':
    unittest.main()
