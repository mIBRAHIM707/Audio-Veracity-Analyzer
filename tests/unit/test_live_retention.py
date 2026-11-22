"""FR-O5 alert snippets in the live service (backlog 5.3; services/live/retention.py,
the sink/service hooks, the command-line rules and the live /metrics families).

Claude-authored test design, 17 November 2026. Real EventHub, LiveCalls, CallGate and
SnippetStore on temporary folders outside the repository, with synthetic int16 ramps.
Store-logic cases use a deliberately insecure reversible test sealer; one case uses
the real AES-256-GCM sealer and skips without the pinned cryptography package. No
telephony; the tap is represented by the sink's own hooks.
"""
import argparse
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np

from services.events.fanout import MAX_BUFFER, EventHub
from services.inference.capacity import CallGate
from services.live import __main__ as cli
from services.live.retention import SnippetRecorder
from services.retention.snippets import RetentionPolicy, SnippetStore, open_snippet
from services.scorer.live import LiveCalls
from tests.unit.test_scorer_pipeline import Configs

RATE = 8000


class FlipSealer:
    """Reversible and NOT secure (test stand-in); checks the associated data on open."""
    algorithm = 'TEST-FLIP-NOT-SECURE'

    def __init__(self, fail=False):
        self.fail = fail

    def seal(self, plaintext, associated):
        if self.fail:
            raise RuntimeError('key unavailable')
        return bytes(associated[-8:]) + bytes(b ^ 0xA5 for b in plaintext)

    def open(self, sealed, associated):
        if sealed[:8] != bytes(associated[-8:]):
            raise ValueError('wrong alert')
        return bytes(b ^ 0xA5 for b in sealed[8:])


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def ramp(n, start=0):
    return (np.arange(start, start + n) % 30000).astype(np.int16)


class Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / 'snippets'
        self.wall = Clock(1_763_000_000.0)
        self.mono = Clock()
        self.hub = EventHub(clock=lambda: 1.0)
        self.live = LiveCalls(Configs(), self.hub, CallGate(8), new_alert_id=iter(f'alert-{i}' for i in range(99)).__next__)

    def store(self, sealer=None):
        return SnippetStore(self.root, sealer or FlipSealer(), clock=self.wall, _insecure_test_sealer=True)

    def recorder(self, enabled=True, store=None, **options):
        return SnippetRecorder(store or self.store(), RetentionPolicy(enabled), self.hub, self.live,
                               clock=self.mono, wall_clock=self.wall, **options)

    NEW_CALL = '2025-11-13T02:13:21.500+0000'  # 1763000001.5: just after the policy (wall 1763000000)

    def start(self, recorder, call_id, created=NEW_CALL):
        self.assertTrue(self.live.start(call_id))
        recorder.started(call_id, created)

    def raise_alert(self, call_id):
        """Three windows at 0.9 raise one alert with the test config (ema_alpha 1)."""
        for index in range(3):
            self.live.result(call_id, 'caller', index, 0.9, effective_stride_s=2.0)

    def files(self):
        return sorted(p.name for p in self.root.iterdir()) if self.root.exists() else []


class RecorderTests(Harness):
    def test_off_buffers_nothing_subscribes_nothing_and_purges(self):
        on = self.recorder(True)
        self.start(on, 'old')
        on.audio('old', ramp(800).tobytes())
        self.raise_alert('old')
        on.pump_events()
        on.write_pending()
        self.assertEqual(len(self.files()), 1)
        subscribers = self.hub.subscriber_count
        off = SnippetRecorder(SnippetStore.for_cleanup(self.root, clock=self.wall), RetentionPolicy(False), self.hub,
                              self.live, clock=self.mono)
        self.assertEqual(self.hub.subscriber_count, subscribers, 'off: no hub subscription')
        self.assertTrue(self.live.start('new'))
        off.started('new')
        off.audio('new', ramp(800).tobytes())
        self.raise_alert('new')
        self.assertEqual(off.pump_events(), 0)
        self.assertEqual(off.write_pending(), 0)
        self.assertEqual(self.files(), [], 'FR-O5: switching off purges in one cleanup run')
        report = off.report()
        self.assertEqual((report['mode'], report['snippets']['cleanup_runs'], report['snippets']['cleanup_deleted'],
                          report['snippets']['calls_buffered']), ('purge', 1, 1, 0))

    def test_an_alert_stores_the_buffered_caller_audio_of_its_call(self):
        recorder = self.recorder()
        self.start(recorder, 'c1')
        sent = ramp(2 * RATE)
        for begin in range(0, len(sent), 160):  # 20 ms chunks as the tap delivers them
            recorder.audio('c1', sent[begin:begin + 160].tobytes())
        self.raise_alert('c1')
        self.assertEqual(recorder.pump_events(), 1)
        self.assertEqual(self.files(), [], 'nothing is written on the loop thread')
        self.assertEqual(recorder.write_pending(), 1)
        snippet = open_snippet(self.root, 'alert-0', FlipSealer())
        self.assertEqual((snippet['call_id'], snippet['direction'], snippet['sample_rate']), ('c1', 'caller', RATE))
        np.testing.assert_array_equal(snippet['pcm'], sent)
        counts = recorder.report()['snippets']
        self.assertEqual((counts['alerts_seen'], counts['captured'], counts['written']), (1, 1, 1))

    def test_the_snippet_is_the_30_s_ending_at_the_alert(self):
        recorder = self.recorder()
        self.start(recorder, 'c1')
        sent = ramp(35 * RATE)
        for begin in range(0, len(sent), 4000):
            recorder.audio('c1', sent[begin:begin + 4000].tobytes())
        self.raise_alert('c1')
        recorder.pump_events()
        recorder.write_pending()
        pcm = open_snippet(self.root, 'alert-0', FlipSealer())['pcm']
        np.testing.assert_array_equal(pcm, sent[-30 * RATE:])

    def test_no_audio_cases_are_counted_not_stored(self):
        recorder = self.recorder()
        self.assertTrue(self.live.start('unbuffered'))  # admitted by LiveCalls, never started on the recorder
        self.raise_alert('unbuffered')
        self.start(recorder, 'silent')  # started, but no audio yet
        self.raise_alert('silent')
        self.start(recorder, 'c3')
        recorder.audio('c3', ramp(800).tobytes())
        self.hub.publish('alert.raised', 'c3', {'alert_id': 'agent-1', 'direction': 'agent'})  # agent: not tapped
        self.hub.publish('alert.raised', 'c3', {'alert_id': 'ok-1', 'direction': 'caller'})
        self.assertEqual(recorder.pump_events(), 1)
        counts = recorder.report()['snippets']
        self.assertEqual((counts['alerts_seen'], counts['no_audio'], counts['captured']), (4, 3, 1))

    def test_rings_are_released_after_the_end_but_tail_alerts_still_get_audio(self):
        recorder = self.recorder()
        self.start(recorder, 'c1')
        recorder.audio('c1', ramp(1600).tobytes())
        self.raise_alert('c1')
        self.live.end('c1', 'bye', speech_seconds={'caller': 6.0})  # ends in the same loop turn
        self.assertEqual(recorder.pump_events(), 1, 'the alert published before the end is captured')
        self.assertEqual(recorder.report()['snippets']['released'], 1)
        recorder.audio('c1', ramp(800).tobytes())  # late audio for the ended call is ignored
        self.assertEqual(recorder.report()['snippets']['audio_errors'], 0)
        self.start(recorder, 'c2')
        recorder.close()
        self.assertEqual(recorder.pump_events(), 0, 'closed: nothing held, nothing pumped')

    def test_a_retap_replaces_the_earlier_ring(self):
        recorder = self.recorder()
        self.start(recorder, 'c1')
        recorder.audio('c1', np.full(800, 7, np.int16).tobytes())
        recorder.started('c1', self.NEW_CALL)  # re-tapped: the old ring is not this session's audio
        recorder.audio('c1', ramp(800).tobytes())
        self.raise_alert('c1')
        recorder.pump_events()
        recorder.write_pending()
        np.testing.assert_array_equal(open_snippet(self.root, 'alert-0', FlipSealer())['pcm'], ramp(800))

    def test_calls_started_before_the_policy_or_without_a_start_time_are_not_buffered(self):
        # Review M2: a restart that turns retention on re-taps calls already in progress
        # (TapApp.resume); FR-O5 applies the change only to calls started after it.
        recorder = self.recorder()
        cases = (('before', '2025-11-13T02:13:19.000+0000'), ('at', '2025-11-13T02:13:20.000+0000'),
                 ('no fraction', '2025-11-13T02:13:25+0000'), ('missing', ''), ('garbage', 'yesterday'),
                 ('none', None))
        for call_id, created in cases:
            self.start(recorder, call_id, created)
            recorder.audio(call_id, ramp(800).tobytes())
            self.raise_alert(call_id)
        self.assertEqual(recorder.pump_events(), 2, 'only the calls created at or after the policy')
        counts = recorder.report()['snippets']
        self.assertEqual((counts['calls_buffered'], counts['started_before_policy'], counts['start_time_unknown']),
                         (2, 1, 3))
        self.assertEqual(counts['no_audio'], 4)

    def test_the_queue_is_bounded(self):
        recorder = self.recorder(max_queued=2)
        for call_id in ('a', 'b', 'c'):
            self.start(recorder, call_id)
            recorder.audio(call_id, ramp(800).tobytes())
            self.raise_alert(call_id)
        self.assertEqual(recorder.pump_events(), 2)
        self.assertEqual((recorder.pending(), recorder.report()['snippets']['queue_full']), (2, 1))
        self.assertEqual(recorder.write_pending(), 2)
        self.assertEqual(recorder.pending(), 0)

    def test_write_and_cleanup_faults_are_counted_never_raised(self):
        recorder = self.recorder(store=self.store(FlipSealer(fail=True)))
        self.start(recorder, 'c1')
        recorder.audio('c1', ramp(800).tobytes())
        self.raise_alert('c1')
        recorder.pump_events()
        self.assertEqual(recorder.write_pending(), 1)
        report = recorder.report()
        self.assertEqual((report['snippets']['write_failed'], report['store_failures']), (1, 1))

        class Broken:
            failures = {}
            cleanup_only = False

            def put(self, *args, **kwargs):
                raise OSError('disk gone')

            def cleanup(self, policy):
                raise OSError('disk gone')
        broken = self.recorder(store=Broken())
        self.start(broken, 'c2')
        broken.audio('c2', ramp(800).tobytes())
        self.raise_alert('c2')
        broken.pump_events()
        self.assertEqual(broken.write_pending(), 1)
        self.assertEqual((broken.report()['snippets']['cleanup_errors'], broken.report()['snippets']['write_failed']),
                         (1, 1), 'review L6: a raising store is counted too')

    def test_cleanup_runs_at_most_once_per_interval(self):
        recorder = self.recorder(cleanup_interval_s=600)
        recorder.write_pending()
        self.mono.t += 599
        recorder.write_pending()
        self.assertEqual(recorder.report()['snippets']['cleanup_runs'], 1)
        self.mono.t += 1
        recorder.write_pending()
        self.assertEqual(recorder.report()['snippets']['cleanup_runs'], 2)

    def test_ttl_cleanup_keeps_young_snippets_and_deletes_old_ones(self):
        recorder = SnippetRecorder(self.store(), RetentionPolicy(True, ttl_s=3600), self.hub, self.live,
                                   clock=self.mono, wall_clock=self.wall)
        self.start(recorder, 'c1')
        recorder.audio('c1', ramp(800).tobytes())
        self.raise_alert('c1')
        recorder.pump_events()
        recorder.write_pending()
        self.assertEqual(len(self.files()), 1)
        self.wall.t += 3600
        self.mono.t += 600
        recorder.write_pending()
        self.assertEqual(self.files(), [])
        self.assertEqual(recorder.report()['snippets']['cleanup_deleted'], 1)

    def test_cleanup_never_touches_files_it_did_not_create(self):
        # Review M3: a mistyped --retention-dir must not lose foreign files, in purge or TTL mode.
        import os
        import time as wall_time
        self.root.mkdir(parents=True)
        foreign = ['notes.tmp', 'report.snip', 'x.snip.tmp', 'snip-.tmp', 'a' * 63 + '.snip', 'keep.txt']
        for name in foreign:
            (self.root / name).write_bytes(b'not ours')
            old = wall_time.time() - 3600
            os.utime(self.root / name, (old, old))
        (self.root / ('b' * 64 + '.snip')).mkdir()  # a folder with a snippet-like name
        own_temp = self.root / 'snip-abc_123.tmp'
        own_temp.write_bytes(b'')
        os.utime(own_temp, (wall_time.time() - 3600,) * 2)
        corrupt = self.root / ('c' * 64 + '.snip')
        corrupt.write_bytes(b'garbage')  # our name, unreadable: deleted (fail closed)
        for policy in (RetentionPolicy(True, ttl_s=3600), RetentionPolicy(False)):
            self.store().cleanup(policy)
        self.assertEqual(sorted(self.files()), sorted(foreign + ['b' * 64 + '.snip']))

    def test_hub_overflow_is_counted(self):
        recorder = self.recorder()
        self.start(recorder, 'c1')
        for _ in range(MAX_BUFFER + 5):
            self.hub.publish('system.health', None, {'reason': 'capacity'})
        recorder.pump_events()
        self.assertEqual(recorder.report()['snippets']['hub_dropped'], 6)  # call.started was published first

    def test_validation(self):
        for options in ({'max_queued': 0}, {'max_queued': 257}, {'max_queued': True}, {'cleanup_interval_s': 0.5},
                        {'cleanup_interval_s': 86401}, {'cleanup_interval_s': float('nan')}):
            with self.subTest(options), self.assertRaises(ValueError):
                self.recorder(**options)
        with self.assertRaises(ValueError):
            SnippetRecorder(SnippetStore.for_cleanup(self.root, clock=self.wall), RetentionPolicy(True), self.hub,
                            self.live)
        cleanup_only = SnippetStore.for_cleanup(self.root, clock=self.wall)
        self.assertFalse(cleanup_only.put('a', call_id='c', direction='caller', pcm=ramp(10), sample_rate=RATE))
        self.assertEqual(cleanup_only.failures, {'cleanup_only': 1})

    def test_real_aes_gcm_round_trip(self):
        try:
            from services.retention.snippets import AesGcmSealer
            sealer = AesGcmSealer(bytes(range(32)))
        except RuntimeError:
            self.skipTest('requirements-retention.txt not installed')
        recorder = SnippetRecorder(SnippetStore(self.root, sealer, clock=self.wall), RetentionPolicy(True),
                                   self.hub, self.live, clock=self.mono, wall_clock=self.wall)
        self.start(recorder, 'c1')
        recorder.audio('c1', ramp(1600).tobytes())
        self.raise_alert('c1')
        recorder.pump_events()
        recorder.write_pending()
        raw = (self.root / self.files()[0]).read_bytes()
        self.assertNotIn(ramp(1600).tobytes()[:64], raw, 'audio is not stored in the clear')
        np.testing.assert_array_equal(open_snippet(self.root, 'alert-0', sealer)['pcm'], ramp(1600))


class SinkHookTests(unittest.TestCase):
    class Live:
        def __init__(self, admit):
            self.admit, self.started = admit, []

        def start(self, call_id, **data):
            self.started.append(call_id)
            return self.admit

    class Feed:
        def audio(self, *args, **kwargs):
            pass

    class Recorder:
        def __init__(self, fail=False):
            self.fail, self.calls = fail, []

        def started(self, call_id, created=None):
            self.calls.append(('started', call_id, created))
            if self.fail:
                raise RuntimeError('boom')

        def audio(self, call_id, pcm):
            self.calls.append(('audio', call_id, len(pcm)))
            if self.fail:
                raise RuntimeError('boom')

    def sink(self, admit, recorder):
        from services.tap.scoring_sink import TapScoringSink
        return TapScoringSink(self.Live(admit), self.Feed(), object(), lambda: 0.0, recorder=recorder)

    def call(self, call_id):
        from types import SimpleNamespace
        return SimpleNamespace(call_id=call_id, caller='1000', dialled='700', codec='ulaw',
                               started='2026-11-17T08:00:00.000+0000')

    def test_only_admitted_calls_and_their_raw_audio_reach_the_recorder(self):
        recorder = self.Recorder()
        sink = self.sink(True, recorder)
        sink.started(self.call('c1'))
        sink.audio('c1', ramp(160).tobytes(), [(160, 0)])
        sink.audio('other', ramp(160).tobytes(), [(160, 0)])
        self.assertEqual(recorder.calls, [('started', 'c1', '2026-11-17T08:00:00.000+0000'), ('audio', 'c1', 320)],
                         'the PBX creation time is passed on (review M2)')
        refused = self.Recorder()
        sink = self.sink(False, refused)
        sink.started(self.call('c2'))
        sink.audio('c2', ramp(160).tobytes(), [(160, 0)])
        self.assertEqual(refused.calls, [], 'a call refused for capacity is not buffered')

    def test_recorder_faults_are_counted_and_scoring_goes_on(self):
        sink = self.sink(True, self.Recorder(fail=True))
        sink.started(self.call('c1'))
        sink.audio('c1', ramp(160).tobytes(), [(160, 0)])
        self.assertEqual((sink.counts['recorder_errors'], sink.counts['started'], sink.counts['audio_chunks']),
                         (2, 1, 1))


class CommandLineTests(unittest.TestCase):
    BASE = ['--card', 'c.json', '--onnx', 'm.onnx', '--vad-model', 'v.onnx', '--ari-url', 'http://127.0.0.1:8088',
            '--ari-user', 'ava', '--receiver-host', '127.0.0.1']

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def parse(self, *extra):
        return cli.build_parser().parse_args(self.BASE + list(extra))

    def key(self, data):
        """A key file the loader accepts on Linux too (no group/other access)."""
        path = self.tmp / 'k.bin'
        path.write_bytes(data)
        os.chmod(path, 0o600)
        return path

    def test_default_is_off_with_nothing_to_do(self):
        args = self.parse()
        # 22 Nov: an absent --retention parses as None and means off.
        self.assertEqual((args.retention, args.retention_ttl_days), (None, 7.0))
        self.assertIsNone(cli._retention(args))

    def test_refusals_never_show_the_key_or_path(self):
        key = self.key(b'S' * 31)
        for extra, words in ((['--retention', 'on'], 'needs --retention-dir and --retention-key-file'),
                             (['--retention', 'on', '--retention-dir', str(self.tmp / 's')], 'needs')):
            with self.subTest(extra):
                message = cli._retention(self.parse(*extra))
                self.assertIsInstance(message, str)
                self.assertIn(words, message)
        try:
            import cryptography  # noqa: F401
        except ImportError:  # the key file is checked first; a valid one then needs the package
            key = self.key(bytes(range(32)))
            message = cli._retention(self.parse('--retention', 'on', '--retention-dir', str(self.tmp / 's'),
                                                '--retention-key-file', str(key)))
            self.assertIn('cryptography', message)
            return
        message = cli._retention(self.parse('--retention', 'on', '--retention-dir', str(self.tmp / 's'),
                                            '--retention-key-file', str(key)))
        self.assertIn('32 raw bytes', message)
        self.assertNotIn(str(key), message)
        inside = Path(cli.__file__).resolve().parents[2] / 'snippets-test-never-created'
        key = self.key(bytes(range(32)))
        message = cli._retention(self.parse('--retention', 'on', '--retention-dir', str(inside),
                                            '--retention-key-file', str(key)))
        self.assertIn('outside the repository', message)
        self.assertFalse(inside.exists())

    def test_off_with_a_folder_purges_and_on_with_a_key_stores(self):
        store, policy = cli._retention(self.parse('--retention-dir', str(self.tmp / 's')))
        self.assertTrue(store.cleanup_only)
        self.assertFalse(policy.enabled)
        try:
            import cryptography  # noqa: F401
        except ImportError:
            return
        key = self.key(bytes(range(32)))
        store, policy = cli._retention(self.parse('--retention', 'on', '--retention-dir', str(self.tmp / 's'),
                                                  '--retention-key-file', str(key), '--retention-ttl-days', '2'))
        self.assertFalse(store.cleanup_only)
        self.assertEqual((policy.enabled, policy.ttl_s), (True, 2 * 86400.0))

    def test_off_with_a_key_file_never_opens_it(self):
        """Claude-authored (22 Nov 2026, backlog 11.1): Compose and k3s pass the same
        flags with retention on or off, so an explicit `off` accepts a key file and never
        reads it: a missing, short or directory "key" changes nothing, without a folder it
        is off, with one it purges. Without --retention a key file is refused (review L1:
        `on` was probably meant, and the start would purge the folder silently)."""
        broken = self.key(b'S' * 5)
        for key in (self.tmp / 'missing.key', broken, self.tmp):
            with self.subTest(key=key.name):
                self.assertIsNone(cli._retention(self.parse('--retention', 'off', '--retention-key-file', str(key))))
                for implicit in (['--retention-key-file', str(key)],
                                 ['--retention-dir', str(self.tmp / 's'), '--retention-key-file', str(key)]):
                    message = cli._retention(self.parse(*implicit))
                    self.assertEqual(message, '--retention-key-file needs an explicit --retention on (or off)')
                self.assertFalse((self.tmp / 's').exists(), 'a refused start purges nothing')
                with mock.patch('services.retention.snippets.AesGcmSealer.from_key_file',
                                side_effect=AssertionError('the key was opened')):
                    store, policy = cli._retention(self.parse('--retention', 'off', '--retention-dir',
                                                              str(self.tmp / 's'), '--retention-key-file', str(key)))
                self.assertTrue(store.cleanup_only)
                self.assertFalse(policy.enabled)

    def test_ttl_days_bounds(self):
        self.assertEqual(self.parse('--retention-ttl-days', '365').retention_ttl_days, 365.0)
        for bad in ('0', '-1', '366', 'nan', 'inf', 'week'):
            with self.subTest(bad), self.assertRaises(SystemExit), mock.patch('sys.stderr', io.StringIO()):
                self.parse('--retention-ttl-days', bad)
        self.assertIsInstance(cli._ttl_days('1.5'), float)
        with self.assertRaises(argparse.ArgumentTypeError):
            cli._ttl_days('400')


class CliWiringTests(unittest.TestCase):
    """Review M4: the production command line builds the recorder, runs its pump thread
    (purge or write) and drains queued snippets on stop. Fake ARI/model/service from
    test_live_cli; the recorder, hub, LiveCalls and store are real."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.snippets = self.tmp / 'snippets'

    def harness(self, raise_alert):
        from tests.unit import test_live_cli
        harness = test_live_cli.Harness()
        made = harness.service
        state = {}

        def service(*args, **kwargs):
            factory = kwargs.pop('recorder_factory', None)
            fake = made(*args, **kwargs)
            hub = EventHub()
            live = LiveCalls(Configs(), hub, CallGate(8), new_alert_id=lambda: 'alert-cli')
            fake.recorder = factory(hub, live) if factory else None
            state.update(service=fake, live=live)
            return fake
        harness.service = service

        def on_run(stop):
            recorder, live = state['service'].recorder, state['live']
            if raise_alert and recorder is not None:
                live.start('c1')
                recorder.started('c1', '2999-01-01T00:00:00.000+0000')  # created after the policy
                recorder.audio('c1', ramp(1600).tobytes())
                for index in range(3):
                    live.result('c1', 'caller', index, 0.9, effective_stride_s=2.0)
                recorder.pump_events()  # what LiveService does each loop turn
            stop.set()
        harness.on_run = on_run
        return harness

    def test_on_writes_the_queued_snippet_at_stop_and_prints_no_path(self):
        try:
            from services.retention.snippets import AesGcmSealer
            AesGcmSealer(bytes(32))
        except RuntimeError:
            self.skipTest('requirements-retention.txt not installed')
        from tests.unit.test_live_cli import BASE, EXIT_OK, run
        key = self.tmp / 'k.bin'
        key.write_bytes(bytes(range(32)))
        os.chmod(key, 0o600)
        code, lines, text = run(BASE + ['--retention', 'on', '--retention-dir', str(self.snippets),
                                        '--retention-key-file', str(key)], self.harness(True))
        self.assertEqual(code, EXIT_OK, text)
        start = next(line for line in lines if line['event'] == 'starting')
        self.assertEqual(start['retention'], {'mode': 'on', 'ttl_days': 7.0})
        stopped = next(line for line in lines if line['event'] == 'stopped')
        self.assertEqual((stopped['retention']['snippets']['written'], stopped['retention']['queued']), (1, 0))
        self.assertTrue(stopped['retention']['drained'])
        self.assertEqual(len(list(self.snippets.iterdir())), 1)
        for secret in (str(self.snippets), str(key), self.tmp.name):
            self.assertNotIn(secret, text)

    def test_off_with_a_folder_purges_on_start(self):
        from services.retention.snippets import _alert_key
        from tests.unit.test_live_cli import BASE, EXIT_OK, run
        self.snippets.mkdir()
        own = self.snippets / (_alert_key('earlier') + '.snip')
        own.write_bytes(b'AVASNIP1 from an earlier run')
        (self.snippets / 'notes.txt').write_text('keep')
        code, lines, text = run(BASE + ['--retention-dir', str(self.snippets)], self.harness(False))
        self.assertEqual(code, EXIT_OK, text)
        self.assertEqual(next(l for l in lines if l['event'] == 'starting')['retention'],
                         {'mode': 'purge', 'ttl_days': 7.0})
        stopped = next(line for line in lines if line['event'] == 'stopped')
        self.assertEqual(stopped['retention']['snippets']['cleanup_deleted'], 1)
        self.assertEqual(sorted(p.name for p in self.snippets.iterdir()), ['notes.txt'])

    def test_default_has_no_recorder_and_says_off(self):
        from tests.unit.test_live_cli import BASE, EXIT_OK, run
        code, lines, text = run(BASE, self.harness(False))
        self.assertEqual(code, EXIT_OK, text)
        self.assertEqual(next(l for l in lines if l['event'] == 'starting')['retention'], 'off')
        self.assertNotIn('retention', next(l for l in lines if l['event'] == 'stopped'))


class MetricsTests(Harness):
    def test_live_metrics_export_the_recorder_counts(self):
        from services.live.background import PumpThread
        from services.live.health import HealthServer
        from tests.unit.test_api_readiness_metrics import parse_metrics
        recorder = self.recorder()
        self.start(recorder, 'secret-call')
        recorder.audio('secret-call', ramp(800).tobytes())
        self.raise_alert('secret-call')
        recorder.pump_events()
        server = HealthServer(object(), pumps=[{'thread': PumpThread('retention', recorder.write_pending),
                                                'resource': None, 'report': recorder.report}])
        self.addCleanup(server.close)
        text = '\n'.join(server._pump_lines())
        self.assertNotIn('secret-call', text)
        self.assertNotIn('alert-0', text)
        samples, declared = parse_metrics(text)
        self.assertEqual((declared['ava_live_snippets_total'], declared['ava_live_snippets_queued']),
                         ('counter', 'gauge'))
        self.assertEqual(samples['ava_live_snippets_total{outcome="captured"}'], 1)
        self.assertEqual(samples['ava_live_snippets_queued'], 1)
        self.assertEqual(samples['ava_live_pump_alive{pump="retention"}'], 0)


if __name__ == '__main__':
    unittest.main()
