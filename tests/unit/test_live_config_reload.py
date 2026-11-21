"""FR-A9 scoring-config file plus reload in the live process (backlog 7.3;
services/live/config_reload.py, its command-line, operations-log and /metrics wiring).

Claude-authored test design, 20 November 2026 (Claude Code; not GPT-6 Astra). A real
`ConfigStore` and a real file in a temporary folder; calls are scored by a real
`LiveCalls` through the service's `_CallConfigs` adapter, so "applies to calls started
after the reload" is checked on published alerts, not on a fake. Covers the baseline
read, applied/unchanged/rejected/unreadable outcomes reported once per file state, the
process-wide keys that need a restart, the model-card window, malformed documents, no
path in any record, the bounded record queue, the command line (refusal, usage errors,
a real `PumpThread` applying an edit during a run, log lines and the stopped record) and
the `/metrics` families.
"""
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from services.events.fanout import EventHub
from services.inference.capacity import CallGate
from services.live.config_reload import MAX_RECORDS, ConfigFileWatcher
from services.live.eventlog import EventLog
from services.live.service import _CallConfigs
from services.scorer.config import MAX_CONFIG_BYTES, ConfigStore, parse_config
from services.scorer.live import LiveCalls

WINDOW_S = 4.0
START = {'scoring': {'thresholds': {'alert_enter': 0.75}}}
LOWER = {'scoring': {'thresholds': {'suspicious_enter': 0.55, 'alert_enter': 0.65, 'alert_exit': 0.5,
                                    'suspicious_exit': 0.4}}}


class Folder(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / 'secret-site-folder' / 'ava.json'
        self.path.parent.mkdir()
        self.write(START)
        self.fixed = parse_config(START, model_window_s=WINDOW_S)
        self.store = ConfigStore(self.fixed)
        self.watcher = ConfigFileWatcher(str(self.path), self.store, fixed=self.fixed, model_window_s=WINDOW_S)

    def write(self, document, *, raw=None):
        self.path.write_bytes(raw if raw is not None else json.dumps(document).encode())

    def poll(self):
        self.assertEqual(self.watcher.pump(), 0, 'the pump always waits its interval')
        return self.watcher.take_records()

    def assert_no_path(self, records):
        text = json.dumps(records)
        self.assertNotIn('secret-site-folder', text)
        self.assertNotIn(self._tmp.name.replace('\\', '\\\\'), text)


class WatcherTests(Folder):
    def test_the_start_up_settings_are_the_baseline(self):
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.poll(), [])
        report = self.watcher.report()
        self.assertEqual(report['config_reloads'], {'applied': 0, 'unchanged': 0, 'rejected': 0, 'unreadable': 0})
        self.assertEqual((report['config_version'], report['polls']), (1, 2))

    def test_a_valid_edit_applies_to_new_calls_only(self):
        self.poll()
        running = self.store.for_new_call()  # a call that started before the edit
        self.write(LOWER)
        records = self.poll()
        after = self.store.for_new_call()
        self.assertEqual(after.version, 2)
        self.assertEqual(after.config.thresholds.alert_enter, 0.65)
        self.assertEqual(running.config.thresholds.alert_enter, 0.75, 'a running call keeps its snapshot')
        self.assertEqual(records, [{'event': 'config_reloaded', 'level': 'info', 'version': 2,
                                    'fingerprint': after.fingerprint}])
        self.assertEqual(self.poll(), [], 'the same bytes again: nothing to do')
        self.assertEqual(self.watcher.report()['config_reloads']['applied'], 1)

    def test_a_change_before_the_first_poll_is_applied(self):
        self.write(LOWER)  # edited between the start-up read and the watcher's first read
        self.assertEqual([r['event'] for r in self.poll()], ['config_reloaded'])
        self.assertEqual(self.store.for_new_call().version, 2)

    def test_spacing_or_key_order_only_is_unchanged(self):
        self.poll()
        self.write(None, raw=b'{\n  "scoring" : {"thresholds": {"alert_enter": 0.75}}\n}\n')
        self.assertEqual(self.poll(), [{'event': 'config_unchanged', 'level': 'info', 'version': 1,
                                        'fingerprint': self.store.for_new_call().fingerprint}])
        self.assertEqual(self.store.for_new_call().version, 1)

    def test_an_invalid_edit_keeps_the_running_settings_until_fixed(self):
        self.poll()
        self.write({'scoring': {'thresholds': {'alert_enter': 0.5, 'alert_exit': 0.6}, 'ema_alpha': 2}})
        records = self.poll()
        self.assertEqual([(r['event'], r['level']) for r in records], [('config_rejected', 'warning')])
        problems = ' '.join(records[0]['problems'])
        self.assertIn('alert_exit < alert_enter', problems)
        self.assertIn('ema_alpha', problems, 'every failed rule is reported')
        self.assertEqual(self.store.for_new_call().version, 1)
        self.assertEqual(self.poll(), [], 'reported once, not on every poll')
        self.write(LOWER)
        self.assertEqual([r['event'] for r in self.poll()], ['config_reloaded'])
        self.assertEqual(self.watcher.report()['config_reloads'],
                         {'applied': 1, 'unchanged': 0, 'rejected': 1, 'unreadable': 0})

    def test_process_wide_keys_need_a_restart(self):
        self.poll()
        for section, key, value in (('audio', 'hop_s', 1.0), ('scoring', 'degraded_max_stride_factor', 2),
                                    ('scoring', 'degraded_recovery_s', 10.0)):
            with self.subTest(key=key):
                document = json.loads(json.dumps(LOWER))  # with a valid per-call change too
                document.setdefault(section, {})[key] = value
                self.write(document)
                records = self.poll()
                self.assertEqual(records[0]['event'], 'config_rejected')
                self.assertEqual(records[0]['problems'], [f'{section}.{key} applies to every call and needs a restart'])
                self.assertEqual(self.store.for_new_call().version, 1, 'nothing of the document is applied')
        self.write({**START, 'audio': {'hop_s': 2.0, 'window_s': 4.0}})  # the running values, spelled out
        self.assertEqual([r['event'] for r in self.poll()], ['config_unchanged'])

    def test_dropping_the_caller_direction_is_rejected(self):
        # Review M1: only the caller is tapped, so ["agent"] would score nothing.
        self.poll()
        self.write({'scoring': {'score_directions': ['agent']}})
        records = self.poll()
        self.assertEqual([r['event'] for r in records], ['config_rejected'])
        self.assertIn('must include caller', records[0]['problems'][0])
        self.assertEqual(self.store.for_new_call().version, 1)
        self.write({**START, 'scoring': {**START['scoring'], 'score_directions': ['caller', 'agent']}})
        self.assertEqual([r['event'] for r in self.poll()], ['config_reloaded'])

    def test_the_model_card_window_still_binds(self):
        self.poll()
        self.write({'audio': {'window_s': 3.0, 'hop_s': 2.0}})
        records = self.poll()
        self.assertEqual(records[0]['event'], 'config_rejected')
        self.assertIn('differs from the model card window 4.0', records[0]['problems'][0])

    def test_malformed_documents_are_rejected_without_the_path(self):
        self.poll()
        cases = {'not json': b'{"scoring": ', 'duplicate key': b'{"scoring": {}, "scoring": {}}',
                 'NaN': b'{"scoring": {"ema_alpha": NaN}}', 'not utf-8': b'{"scoring": "\xff"}',
                 'oversized': b' ' * (MAX_CONFIG_BYTES + 1), 'not an object': b'[1, 2]',
                 'unknown section': b'{"secrets": {}}'}
        seen = []
        for name, raw in cases.items():
            with self.subTest(name):
                self.write(None, raw=raw)
                records = self.poll()
                self.assertEqual([r['event'] for r in records], ['config_rejected'])
                seen += records
        self.assert_no_path(seen)
        self.assertEqual(self.store.for_new_call().version, 1)
        self.assertTrue(all(len(p) <= 200 for r in seen for p in r['problems']))

    def test_a_missing_file_is_reported_once_and_its_return_too(self):
        self.poll()
        self.path.unlink()
        records = self.poll()
        self.assertEqual(records, [{'event': 'config_unreadable', 'level': 'warning', 'error': 'FileNotFoundError',
                                    'detail': records[0]['detail']}])
        self.assert_no_path(records)
        self.assertEqual(self.poll(), [], 'the same failure is not repeated')
        self.write(START)  # restored with the running settings
        self.assertEqual([r['event'] for r in self.poll()], ['config_unchanged'])
        self.assertEqual(self.store.for_new_call().version, 1)

    def test_a_directory_in_place_of_the_file_is_unreadable(self):
        self.poll()
        self.path.unlink()
        self.path.mkdir()
        self.assertEqual([r['event'] for r in self.poll()], ['config_unreadable'])

    def test_undelivered_records_are_bounded(self):
        self.poll()
        for index in range(MAX_RECORDS + 5):
            self.write({'scoring': {'ema_alpha': 0.1 + index / 1000}})
            self.watcher.pump()
        records = self.watcher.take_records()
        self.assertEqual(len(records), MAX_RECORDS)
        self.assertEqual(records[-1]['version'], MAX_RECORDS + 6, 'the newest are kept')
        self.assertEqual(self.watcher.report()['records_dropped'], 5)

    def test_validation(self):
        for bad in ('', None):
            with self.assertRaises(ValueError):
                ConfigFileWatcher(bad, self.store, fixed=self.fixed, model_window_s=WINDOW_S)
        with self.assertRaises(ValueError):
            ConfigFileWatcher('x.json', self.store, fixed=self.fixed, model_window_s=WINDOW_S, reader=None)


class CallScopeTests(Folder):
    """FR-I4 through the real LiveCalls: a call started before the reload keeps the old
    thresholds, a call started after it uses the new ones."""

    def test_only_calls_started_after_the_reload_use_it(self):
        hub = EventHub()
        feed = hub.subscribe()
        live = LiveCalls(_CallConfigs(self.store), hub, CallGate(4))
        self.poll()
        live.start('before', from_number='1', to='700', tap_format='ulaw')
        self.write(LOWER)
        self.poll()
        live.start('after', from_number='2', to='700', tap_format='ulaw')
        for index in range(6):  # EMA settles near 0.7: above the new alert level, below the old one
            for call in ('before', 'after'):
                live.result(call, 'caller', index, 0.7, effective_stride_s=2.0)
        envelopes, _ = feed.drain()
        raised = {e.call_id for e in envelopes if e.type == 'alert.raised'}
        self.assertEqual(raised, {'after'})

    def test_a_running_call_keeps_its_minimum_evidence(self):
        # Review L4: a second per-call key through the real LiveCalls.
        hub = EventHub()
        feed = hub.subscribe()
        live = LiveCalls(_CallConfigs(self.store), hub, CallGate(4))
        self.poll()
        live.start('before', from_number='1', to='700', tap_format='ulaw')  # min_windows 2
        self.write({**START, 'scoring': {**START['scoring'], 'min_windows': 5}})
        self.assertEqual([r['event'] for r in self.poll()], ['config_reloaded'])
        live.start('after', from_number='2', to='700', tap_format='ulaw')
        for index in range(3):
            for call in ('before', 'after'):
                live.result(call, 'caller', index, 0.95, effective_stride_s=2.0)
        envelopes, _ = feed.drain()
        raised = {e.call_id for e in envelopes if e.type == 'alert.raised'}
        self.assertEqual(raised, {'before'}, '3 windows: enough for the old minimum of 2, not the new 5')


class EventLogTests(Folder):
    def test_reload_outcomes_are_service_lines(self):
        stream = io.StringIO()
        from tests.unit.test_live_eventlog import stand_in_service
        log = EventLog(EventHub(), stand_in_service(), stream, config_watcher=self.watcher, max_lines_per_s=1)
        self.poll()
        self.write({'scoring': {'ema_alpha': 7}})
        self.watcher.pump()
        self.write(LOWER)
        self.watcher.pump()
        log.pump()
        lines = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([(l['event'], l['level']) for l in lines],
                         [('config_rejected', 'warning'), ('config_reloaded', 'info')],
                         'both written although the rate limit is 1 line/s')
        self.assertEqual(lines[1]['version'], 2)
        self.assertIn('ema_alpha', lines[0]['problems'][0])
        self.assertTrue(stream.getvalue().isascii())
        self.assertEqual(log.pump(), 0, 'each record once')


class MetricsTests(Folder):
    def test_live_metrics_export_the_reload_counts(self):
        from services.live.background import PumpThread
        from services.live.health import HealthServer
        from tests.unit.test_api_readiness_metrics import parse_metrics
        self.poll()
        self.write({'scoring': {'ema_alpha': 7}})
        self.watcher.pump()
        self.write(LOWER)
        self.watcher.pump()
        server = HealthServer(object(), pumps=[{'thread': PumpThread('config', self.watcher.pump),
                                                'resource': None, 'report': self.watcher.report}])
        self.addCleanup(server.close)
        text = '\n'.join(server._pump_lines())
        self.assertNotIn('secret-site-folder', text)
        samples, declared = parse_metrics(text)
        self.assertEqual((declared['ava_live_config_reloads_total'], declared['ava_live_config_version']),
                         ('counter', 'gauge'))
        self.assertEqual({outcome: samples[f'ava_live_config_reloads_total{{outcome="{outcome}"}}']
                          for outcome in ('applied', 'unchanged', 'rejected', 'unreadable')},
                         {'applied': 1, 'unchanged': 0, 'rejected': 1, 'unreadable': 0})
        self.assertEqual(samples['ava_live_config_version'], 2)


class CommandLineTests(Folder):
    def harness(self):
        from tests.unit import test_live_cli
        harness = test_live_cli.Harness()
        plain_load, plain_service = harness.load_detector, harness.service

        def load_detector(*args, **kwargs):
            detector, card, config = plain_load(*args, **kwargs)
            document = harness.loaded[-1][2]
            return detector, {**card, 'window_s': WINDOW_S}, parse_config(document, model_window_s=WINDOW_S)

        def service(*args, **kwargs):
            made = plain_service(*args, **kwargs)
            made.configs = ConfigStore(made.config)
            made.hub, made.detector_failure_run = EventHub(), 0  # so the operations log runs
            return made
        harness.load_detector, harness.service = load_detector, service
        return harness

    def test_watch_needs_a_scoring_config(self):
        from tests.unit.test_live_cli import BASE, EXIT_REFUSED, run
        code, lines, _ = run(BASE + ['--scoring-config-watch-s', '5'], self.harness())
        self.assertEqual(code, EXIT_REFUSED)
        self.assertEqual(lines, [{'event': 'refused', 'reason': '--scoring-config-watch-s needs --scoring-config'}])

    def test_watch_interval_bounds_are_usage_errors(self):
        from tests.unit.test_live_cli import BASE, run
        for bad in ('0.5', '61', 'nan', 'x'):
            with self.subTest(bad), self.assertRaises(SystemExit) as raised, \
                    mock.patch('sys.stderr', io.StringIO()):
                run(BASE + ['--scoring-config', str(self.path), '--scoring-config-watch-s', bad], self.harness())
            self.assertEqual(raised.exception.code, 2)

    def test_off_by_default(self):
        from tests.unit.test_live_cli import BASE, EXIT_OK, run
        code, lines, text = run(BASE + ['--scoring-config', str(self.path)], self.harness())
        self.assertEqual(code, EXIT_OK, text)
        self.assertIsNone(lines[0]['scoring_config_watch_s'])
        self.assertNotIn('config', lines[-1])

    def test_an_edit_during_the_run_reaches_new_calls(self):
        from tests.unit.test_live_cli import BASE, EXIT_OK, run
        harness = self.harness()
        applied = threading.Event()

        def on_run(stop):
            service = harness.services[0]
            self.write(LOWER)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and service.configs.for_new_call().version < 2:
                time.sleep(0.05)
            if service.configs.for_new_call().version == 2:
                applied.set()
            stop.set()
        harness.on_run = on_run
        code, lines, text = run(BASE + ['--scoring-config', str(self.path), '--scoring-config-watch-s', '1'], harness)
        self.assertEqual(code, EXIT_OK, text)
        self.assertTrue(applied.is_set(), 'the pump thread applied the edit within 10 s')
        self.assertEqual(lines[0]['scoring_config_watch_s'], 1.0)
        events = [line['event'] for line in lines]
        self.assertEqual(events[0], 'starting')
        self.assertEqual(events[-1], 'stopped')
        self.assertEqual(events.count('config_reloaded'), 1)
        stopped = lines[-1]['config']
        self.assertEqual((stopped['config_version'], stopped['config_reloads']['applied'], stopped['alive']),
                         (2, 1, False))
        self.assertEqual(harness.services[0].configs.for_new_call().config.thresholds.alert_enter, 0.65)
        self.assertNotIn('secret-site-folder', text, 'no record prints the path')


if __name__ == '__main__':
    unittest.main()
