"""Claude-authored tests (15 November 2026) for services/live/__main__.py, the live
service command line (backlog 8.1; PRD FR-O4 start-up refusal, FR-T2, FR-A8). Authored
by Claude (Claude Code), not by GPT-6 Astra. Injected factories: no model, no Asterisk
(one real UDP bind check, one real subprocess).
"""
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from services.live.__main__ import EXIT_INTERRUPTED, EXIT_OK, EXIT_REFUSED, _check_bind, main
from services.live.service import StartupError
from services.scorer.config import ScoringConfig
from services.tap.ari import AriError

SECRET = 'pw-7f3a-do-not-print'
ROOT = Path(__file__).resolve().parents[2]
BASE = ['--card', 'card.json', '--onnx', 'ava.onnx', '--vad-model', 'vad.onnx', '--ari-url', 'http://127.0.0.1:8088',
        '--ari-user', 'ava', '--receiver-host', '10.0.0.5']


class FakeService:
    def __init__(self, ari, detector, config, vad_factory, **options):
        self.ari, self.detector, self.config, self.vad_factory, self.options = ari, detector, config, vad_factory, options
        self.counts = {'connects': 1}
        self.tap = SimpleNamespace(stats={'tapped': 0})
        self.sink = SimpleNamespace(counts={'ended': 0})
        self.pipeline = SimpleNamespace(counts={'scored': 0})
        self.on_run = None

    def run(self, stop):
        if self.on_run:
            self.on_run(stop)
        assert stop.is_set(), 'the fake runs only until stopped'


class FakeAri:
    def __init__(self, error=None):
        self.error = error

    def list_bridges(self):
        if self.error:
            raise self.error
        return []


class Harness:
    def __init__(self, *, load_error=None, vad_error=None, ari_error=None, bind_error=None):
        self.loaded, self.vads, self.services, self.aris, self.binds = [], [], [], [], []
        self.load_error, self.vad_error, self.ari_error, self.bind_error = load_error, vad_error, ari_error, bind_error
        self.on_run = None

    def load_detector(self, card, onnx, document, *, threads, per_window):
        self.loaded.append((card, onnx, document, threads, per_window))
        if self.load_error:
            raise self.load_error
        return 'detector', {'version': 'ava-test', 'onnx': {'sha256': 'ab' * 32}}, ScoringConfig()

    def vad(self, path):
        if self.vad_error:
            raise self.vad_error
        self.vads.append(path)
        return object()

    def ari(self, url, user, password, app):
        self.aris.append((url, user, password, app))
        return FakeAri(self.ari_error)

    def bind_check(self, host):
        self.binds.append(host)
        if self.bind_error:
            raise self.bind_error

    def service(self, *args, **kwargs):
        made = FakeService(*args, **kwargs)
        made.on_run = self.on_run
        self.services.append(made)
        return made

    def factories(self):
        return {'load_detector': self.load_detector, 'vad': self.vad, 'ari': self.ari, 'service': self.service,
                'bind_check': self.bind_check}


def run(argv, harness, environ=None):
    err = io.StringIO()
    stop = threading.Event()
    if harness.on_run is None:
        stop.set()
    code = main(argv, environ={'AVA_ARI_PASSWORD': SECRET} if environ is None else environ,
                factories=harness.factories(), stop_event=stop, stderr=err)
    return code, [json.loads(line) for line in err.getvalue().splitlines()], err.getvalue()


class CliTests(unittest.TestCase):
    def test_starts_wires_the_service_and_never_prints_the_password(self):
        harness = Harness()
        code, lines, text = run(BASE + ['--tap-extension', '700', '--tap-extension', '701', '--max-calls', '4',
                                        '--threads', '2', '--per-window', '--capacity-windows-per-s', '2.5'], harness)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(harness.loaded, [('card.json', 'ava.onnx', {}, 2, True)])
        self.assertEqual(harness.aris, [('http://127.0.0.1:8088', 'ava', SECRET, 'ava-tap')])
        self.assertEqual(harness.binds, ['0.0.0.0'])
        service = harness.services[0]
        self.assertEqual(service.options, {'receiver_host': '10.0.0.5', 'bind_host': '0.0.0.0', 'max_calls': 4,
                                           'capacity_windows_per_s': 2.5, 'tap_extensions': {'700', '701'}})
        self.assertEqual(harness.vads, ['vad.onnx'])  # checked once at start-up
        service.vad_factory()
        service.vad_factory()
        self.assertEqual(harness.vads, ['vad.onnx'] * 3)  # then one VAD per call direction
        self.assertEqual([line['event'] for line in lines], ['starting', 'stopped'])
        self.assertEqual((lines[0]['model_version'], lines[0]['tap_extensions'], lines[0]['ari_reachable']),
                         ('ava-test', ['700', '701'], True))
        self.assertEqual(len(lines[0]['config_fingerprint']), 64)
        self.assertNotIn(SECRET, text)
        self.assertEqual((lines[1]['service'], lines[1]['forced']), ({'connects': 1}, False))

    def test_every_refusal_exits_2_with_one_line_and_no_traceback(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bad_json = Path(temp.name) / 'bad.json'
        bad_json.write_text('{"audio": {"window_s": 4.0, "window_s": 3.0}}', encoding='utf-8')
        not_object = Path(temp.name) / 'list.json'
        not_object.write_text('[1, 2]', encoding='utf-8')
        cases = {
            'no password': (BASE, Harness(), {}),
            'empty password': (BASE, Harness(), {'AVA_ARI_PASSWORD': ''}),
            'other variable': (BASE + ['--ari-password-env', 'AVA_OTHER'], Harness(), None),
            'bind host not local': (BASE, Harness(bind_error=OSError(10049, 'address not valid')), None),
            'duplicate keys': (BASE + ['--scoring-config', str(bad_json)], Harness(), None),
            'not an object': (BASE + ['--scoring-config', str(not_object)], Harness(), None),
            'missing config': (BASE + ['--scoring-config', str(Path(temp.name) / 'absent.json')], Harness(), None),
            'model refused': (BASE, Harness(load_error=StartupError('refusing to start: hash differs')), None),
            'vad hash': (BASE, Harness(vad_error=ValueError('Silero model SHA-256 x does not match')), None),
            'vad runtime': (BASE, Harness(vad_error=type('InvalidProtobuf', (Exception,), {})('bad model')), None),
            'ari 401': (BASE, Harness(ari_error=AriError('ARI GET bridges failed with HTTP 401', 401)), None),
            'ari 403': (BASE, Harness(ari_error=AriError('ARI GET bridges failed with HTTP 403', 403)), None),
        }
        for label, (argv, harness, environ) in cases.items():
            with self.subTest(label):
                code, lines, text = run(argv, harness, environ)
                self.assertEqual(code, EXIT_REFUSED)
                self.assertEqual([line['event'] for line in lines], ['refused'])
                self.assertNotIn('Traceback', text)
                self.assertNotIn(SECRET, text)
                self.assertEqual(harness.services, [])  # nothing tapped
        self.assertIn('hash differs', run(BASE, Harness(load_error=StartupError('refusing to start: hash differs')))
                      [1][0]['reason'])

    def test_unreachable_asterisk_still_starts_and_says_so(self):
        harness = Harness(ari_error=AriError('ARI GET bridges failed: URLError'))
        code, lines, _ = run(BASE, harness)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual((lines[0]['event'], lines[0]['ari_reachable']), ('starting', False))

    def test_real_ari_client_refuses_a_password_in_the_url(self):
        harness = Harness()
        harness.ari = __import__('services.tap.ari', fromlist=['AriClient']).AriClient
        argv = [a if a != 'http://127.0.0.1:8088' else 'http://ava:pw-in-url@127.0.0.1:8088' for a in BASE]
        code, lines, text = run(argv, harness)
        self.assertEqual((code, [line['event'] for line in lines]), (EXIT_REFUSED, ['refused']))
        self.assertNotIn('pw-in-url', text)

    def test_scoring_document_reaches_the_start_up_gate(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / 'ava.json'
        path.write_text('{"audio": {"window_s": 4.0, "hop_s": 1.0}}', encoding='utf-8')
        harness = Harness()
        self.assertEqual(run(BASE + ['--scoring-config', str(path)], harness)[0], EXIT_OK)
        self.assertEqual(harness.loaded[0][2], {'audio': {'window_s': 4.0, 'hop_s': 1.0}})

    def test_bad_options_are_usage_errors(self):
        def receiver(value):
            return [a if a != '10.0.0.5' else value for a in BASE]
        for argv in ([*BASE, '--max-calls', '0'], [*BASE, '--max-calls', '65'], [*BASE, '--threads', '0'],
                     [*BASE, '--threads', '17'], [*BASE, '--threads', 'two'],
                     [*BASE, '--capacity-windows-per-s', '0'], [*BASE, '--capacity-windows-per-s', 'nan'],
                     [*BASE, '--capacity-windows-per-s', 'inf'], [*BASE, '--capacity-windows-per-s', '-1'],
                     receiver('0.0.0.0'), receiver('224.0.0.1'), receiver('255.255.255.255'), receiver('::1'),
                     receiver(''), receiver('bad host'), receiver('-x.example'), receiver('1.2.3'),
                     [*BASE, '--bind-host', 'localhost'], [*BASE, '--bind-host', '::'],
                     [*BASE, '--tap-extension', ''], [*BASE, '--tap-extension', '7 00'],
                     [*BASE, '--tap-extension', 'x' * 33], [*BASE, '--ari-password-env', ''],
                     [*BASE, '--ari-password-env', 'A-B']):
            with self.subTest(argv[-2:]), self.assertRaises(SystemExit) as raised:
                with mock.patch('sys.stderr', io.StringIO()):
                    main(argv, environ={'AVA_ARI_PASSWORD': SECRET}, factories=Harness().factories())
            self.assertEqual(raised.exception.code, 2)
        for good in ('host.docker.internal', 'asterisk-1', '192.168.1.20'):
            with self.subTest(good):
                self.assertEqual(run(receiver(good), Harness())[0], EXIT_OK)

    def test_real_bind_check(self):
        _check_bind('127.0.0.1')
        with self.assertRaises(OSError):
            _check_bind('192.0.2.1')  # TEST-NET-1: not an address of this machine

    def test_ctrl_c_during_start_up_exits_130_without_a_traceback(self):
        harness = Harness(load_error=KeyboardInterrupt())
        code, lines, text = run(BASE, harness)
        self.assertEqual((code, [line['event'] for line in lines]), (EXIT_INTERRUPTED, ['interrupted']))
        self.assertEqual(harness.services, [])

    def test_sigint_stops_the_loop_and_handlers_are_restored(self):
        before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        harness = Harness()
        harness.on_run = lambda stop: (signal.raise_signal(signal.SIGINT), stop.wait(5))
        code, lines, _ = run(BASE, harness)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual([line['event'] for line in lines], ['starting', 'stopped'])
        self.assertEqual({s: signal.getsignal(s) for s in before}, before)

    def test_a_second_ctrl_c_forces_the_exit(self):
        # Review M3: a stalled shutdown can still be interrupted.
        harness = Harness()

        def stalled(stop):
            signal.raise_signal(signal.SIGINT)  # first: sets stop, restores the default handler
            self.assertTrue(stop.is_set())
            signal.raise_signal(signal.SIGINT)  # second: KeyboardInterrupt
        harness.on_run = stalled
        code, lines, _ = run(BASE, harness)
        self.assertEqual(code, EXIT_INTERRUPTED)
        self.assertEqual((lines[-1]['event'], lines[-1]['forced']), ('stopped', True))
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)

    def test_module_runs_as_a_process_and_refuses_without_a_password(self):
        environ = {k: v for k, v in os.environ.items() if k != 'AVA_ARI_PASSWORD'}
        done = subprocess.run([sys.executable, '-m', 'services.live', *BASE], capture_output=True, text=True,
                              env=environ, cwd=ROOT, timeout=120)
        self.assertEqual(done.returncode, EXIT_REFUSED, done.stderr)
        self.assertEqual([json.loads(line)['event'] for line in done.stderr.splitlines()], ['refused'])


if __name__ == '__main__':
    unittest.main()
