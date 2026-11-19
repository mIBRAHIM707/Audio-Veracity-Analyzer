"""Claude-authored tests (16 November 2026) for `python -m services.api`
(services/api/__main__.py; backlog 5.2; PRD FR-A3/A4/A8; architecture §2.5).

Authored by Claude (Claude Code), not by GPT-6 Astra. In-process cases inject the
detector, the Redis opener and the server; `serve` drives the real app through
Starlette's TestClient (lifespan included), so the relay thread, hub, registry and
routes are production code, and the listening socket is really bound. Child-process
cases run `tests/unit/api_cli_child.py` (the real `main` with a fixture detector) with
real uvicorn and signals. Needs requirements-api.txt (so these skip on the Linux CI job,
which installs only requirements-audio.txt). Real-Redis cases run only when
AVA_TEST_REDIS_URL names a disposable Redis 7 (AVA_REQUIRE_REDIS=1 makes the skip a
failure) and also need requirements-redis.txt.
"""
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

from services.events.fanout import EventHub
from services.events.redis_bridge import CHANNEL, RedisForwarder
from services.inference import analysis
from tests.unit import test_analysis_api as api_fixture
from tests.unit.test_event_relay_api import QueuePubSub, wait_for
from tests.unit.test_redis_bridge import message, wire

TestClient = api_fixture.TestClient
if TestClient is not None:
    from services.api import __main__ as cli
    from services.api.__main__ import EXIT_INTERRUPTED, EXIT_OK, EXIT_REFUSED, main

REDIS_URL = os.environ.get('AVA_TEST_REDIS_URL')
REAL = unittest.skipIf(not REDIS_URL and os.environ.get('AVA_REQUIRE_REDIS') != '1',
                       'set AVA_TEST_REDIS_URL to a disposable Redis 7')
SECRET_URL = 'redis://:hunter2@secret-host:6379/0'
REPO = Path(__file__).resolve().parents[2]


class FakeRedisClient:
    def __init__(self, pubsub=None):
        self._pubsub = pubsub or QueuePubSub()
        self.closed = 0

    def pubsub(self, ignore_subscribe_messages=False):
        return self._pubsub

    def close(self):
        self.closed += 1


def fake_service(args):
    return analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')


class RecordingBind:
    """The real bind, recording each socket so tests can check it was closed."""

    def __init__(self):
        self.sockets = []

    def __call__(self, host, port):
        sock = cli._bind(host, port)
        self.sockets.append(sock)
        return sock


def model_dir(root):
    model = Path(root) / 'model'
    model.mkdir()
    (model / 'model.safetensors').write_bytes(b'')
    return model


@unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
class ApiCliTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.model = model_dir(self.root)
        self.dashboard = self.root / 'dist'
        self.dashboard.mkdir()
        (self.dashboard / 'index.html').write_text('<!doctype html><title>AVA</title>', encoding='utf-8')
        self.bind = RecordingBind()

    def run_main(self, extra, *, environ=None, made=None, port='0'):
        stderr = io.StringIO()
        parts = {'service': fake_service, 'bind': self.bind, 'serve': lambda app, sock: None, **(made or {})}
        code = main(['--model-root', str(self.model), '--port', port, *extra], environ=environ or {},
                    stderr=stderr, made=parts)
        records = [json.loads(line) for line in stderr.getvalue().splitlines()]
        return code, records, stderr.getvalue()

    def assert_sockets_closed(self):
        self.assertTrue(all(sock.fileno() == -1 for sock in self.bind.sockets), 'listening socket closed')

    def test_refusals_print_one_json_line_and_exit_2(self):
        def raises(error):
            def opener(*args):
                raise error
            return opener
        env = {'AVA_REDIS_URL': SECRET_URL}
        cases = [
            (['--host', '0.0.0.0'], {}, {}, 'no authentication'),
            (['--host', '192.168.1.5'], {}, {}, 'no authentication'),
            (['--dashboard', str(self.root / 'missing')], {}, {}, 'index.html'),
            (['--redis-url-env', 'AVA_REDIS_URL'], {}, {}, 'AVA_REDIS_URL environment variable'),
            (['--redis-url-env', 'AVA_REDIS_URL'], env, {'redis': raises(ValueError(SECRET_URL))}, 'not valid'),
            (['--redis-url-env', 'AVA_REDIS_URL'], env, {'redis': raises(ImportError('redis', name='redis'))},
             'requirements-redis.txt'),
            (['--redis-url-env', 'AVA_REDIS_URL'], env, {'redis': raises(OSError(SECRET_URL))}, 'OSError'),
            ([], {}, {'service': raises(ImportError('torch', name='torch'))}, 'requirements-model.txt'),
        ]
        for extra, environ, made, expected in cases:
            served = []
            made = {'serve': lambda app, sock: served.append(app), **made}
            code, records, text = self.run_main(extra, environ=environ, made=made)
            self.assertEqual(code, EXIT_REFUSED, extra)
            self.assertEqual(len(records), 1, extra)
            self.assertEqual(records[0]['event'], 'refused')
            self.assertIn(expected, records[0]['reason'])
            self.assertNotIn('hunter2', text)
            self.assertNotIn('secret-host', text)
            self.assertEqual(served, [], 'nothing is served after a refusal')
        self.assert_sockets_closed()
        (self.model / 'model.safetensors').unlink()
        code, records, _ = self.run_main([])
        self.assertEqual((code, records[0]['reason']), (EXIT_REFUSED, 'model root must be a directory holding '
                                                                     'model.safetensors'))

    def test_a_port_in_use_refuses_before_anything_is_opened_or_announced(self):
        busy = socket.create_server(('127.0.0.1', 0))
        self.addCleanup(busy.close)
        opened = []
        code, records, _ = self.run_main(['--redis-url-env', 'AVA_REDIS_URL'], environ={'AVA_REDIS_URL': SECRET_URL},
                                         made={'redis': lambda url: opened.append(url)},
                                         port=str(busy.getsockname()[1]))
        self.assertEqual((code, len(records), records[0]['event'], opened), (EXIT_REFUSED, 1, 'refused', []))
        self.assertIn('cannot listen on 127.0.0.1:', records[0]['reason'])

    def test_missing_torch_is_refused_at_start_up(self):
        with unittest.mock.patch.object(cli.importlib.util, 'find_spec', return_value=None):
            code, records, _ = self.run_main([], made={'service': cli._open_service})
        self.assertEqual((code, records[0]['reason']), (EXIT_REFUSED, 'missing dependency: torch '
                                                                     '(requirements-model.txt)'))

    def test_argument_errors_exit_through_argparse(self):
        for extra in (['--port', '-1'], ['--port', '65536'], ['--host', 'localhost'], ['--host', '::1'],
                      ['--threads', '0'], ['--redis-url-env', 'redis://x'], ['--threads', 'many']):
            with self.assertRaises(SystemExit, msg=extra) as caught, \
                    unittest.mock.patch('sys.stderr', io.StringIO()):
                main(['--model-root', str(self.model), '--port', '0', *extra], environ={},
                     stderr=io.StringIO(), made={'service': fake_service, 'serve': lambda *a: None})
            self.assertEqual(caught.exception.code, 2)

    def test_without_redis_the_api_serves_and_live_routes_answer_503(self):
        seen = {}

        def serve(app, sock):
            seen['address'] = sock.getsockname()
            with TestClient(app) as client:
                seen['calls'] = client.get('/v1/calls').status_code
                seen['health'] = client.get('/healthz').json()
                seen['page'] = client.get('/').status_code
            return 'SIGTERM'
        # --event-log off (17 Nov): this case pins the exact start/stop records.
        code, records, _ = self.run_main(['--dashboard', str(self.dashboard), '--event-log', 'off'],
                                         made={'serve': serve})
        self.assertEqual(code, EXIT_OK)
        host, port = seen.pop('address')
        self.assertEqual((host, seen), ('127.0.0.1', {'calls': 503, 'page': 200, 'health': {
            'status': 'ok', 'detector_loaded': False, 'model_id': 'fx-model'}}))
        self.assertEqual(records, [{'event': 'starting', 'host': '127.0.0.1', 'port': port, 'pid': os.getpid(),
                                    'model_id': 'fx-model', 'threads': 4, 'dashboard': True,
                                    'allow_non_loopback': False, 'live_events': False, 'event_log': 'off',
                                    'call_history': False},
                                   {'event': 'stopped', 'signal': 'SIGTERM'}])
        self.assertGreater(port, 0, '--port 0 reports the port actually bound')
        self.assert_sockets_closed()

    def test_non_loopback_needs_the_explicit_flag(self):
        hosts = []

        def bind(host, port):  # record the host; listen on loopback (no firewall prompt in tests)
            hosts.append(host)
            return self.bind('127.0.0.1', 0)
        code, records, _ = self.run_main(['--host', '0.0.0.0', '--allow-non-loopback'], made={'bind': bind})
        self.assertEqual((code, hosts, records[0]['allow_non_loopback']), (EXIT_OK, ['0.0.0.0'], True))

    def test_redis_relay_feeds_calls_and_the_client_is_closed_after_serving(self):
        client = FakeRedisClient()
        seen = {}

        def serve(app, sock):
            with TestClient(app) as http:
                self.assertTrue(wait_for(lambda: client._pubsub.subscribed == [CHANNEL]))
                client._pubsub.queue.put(message(wire('call.started', 'cli-1', {}, 1)))
                self.assertTrue(wait_for(lambda: http.get('/v1/calls/cli-1').status_code == 200))
                seen['metrics'] = http.get('/metrics').text
            seen['closed_while_serving'] = client.closed
        code, records, text = self.run_main(['--redis-url-env', 'AVA_REDIS_URL'],
                                            environ={'AVA_REDIS_URL': SECRET_URL},
                                            made={'redis': lambda url: (client, False), 'serve': serve})
        self.assertEqual(code, EXIT_OK)
        self.assertEqual((records[0]['live_events'], records[0]['redis_reachable']), (True, False))
        self.assertEqual((records[-1]['event'], records[-1]['signal'], records[-1]['relay']['relayed']),
                         ('stopped', None, 1))
        self.assertIn('ava_event_relay_messages_total{outcome="relayed"} 1\n', seen['metrics'])
        self.assertEqual((seen['closed_while_serving'], client.closed), (0, 1))
        self.assertTrue(client._pubsub.closed)
        self.assertNotIn('hunter2', text)

    def test_an_app_that_fails_to_start_is_a_failure_not_a_clean_stop(self):
        async def broken_app(scope, receive, send):  # an ASGI app whose lifespan start-up fails
            if scope['type'] == 'lifespan':
                await receive()
                await send({'type': 'lifespan.startup.failed', 'message': 'relay could not start'})
        sock = cli._bind('127.0.0.1', 0)
        self.addCleanup(sock.close)
        with self.assertRaises(cli.ServeFailed):
            cli._serve(broken_app, sock)  # real uvicorn
        code, records, _ = self.run_main([], made={'serve': lambda app, sock: cli._serve(broken_app, sock)})
        self.assertEqual((code, [r['event'] for r in records], records[-1]['reason']),
                         (cli.EXIT_FAILED, ['starting', 'failed'], 'the server did not start'))
        self.assert_sockets_closed()

    def test_resources_are_closed_when_serving_exits_or_start_up_is_interrupted(self):
        client = FakeRedisClient()

        def serve(app, sock):
            raise SystemExit(1)  # what uvicorn does when it cannot start
        with self.assertRaises(SystemExit):
            self.run_main(['--redis-url-env', 'AVA_REDIS_URL'], environ={'AVA_REDIS_URL': SECRET_URL},
                          made={'redis': lambda url: (client, True), 'serve': serve})
        self.assertEqual(client.closed, 1)
        self.assert_sockets_closed()

        def interrupted(url):
            raise KeyboardInterrupt
        code, records, _ = self.run_main(['--redis-url-env', 'AVA_REDIS_URL'], environ={'AVA_REDIS_URL': SECRET_URL},
                                         made={'redis': interrupted})
        self.assertEqual((code, records), (EXIT_INTERRUPTED, [{'event': 'interrupted'}]))
        self.assert_sockets_closed()


class ChildProcess:
    """`python -m tests.unit.api_cli_child` with its stderr in a file. Stopping waits for
    the server itself (on Windows the venv python.exe is a launcher whose child serves)
    and, as a last resort, kills the pid from the `starting` record."""

    def __init__(self, test, args, env=None):
        folder = tempfile.TemporaryDirectory()
        self.folder = folder

        def remove_folder():  # Windows releases a dead process's handles a moment later
            for _ in range(50):
                try:
                    return folder.cleanup()
                except PermissionError:
                    time.sleep(0.1)
            folder.cleanup()
        test.addCleanup(remove_folder)
        model = model_dir(folder.name)
        self.log = open(Path(folder.name) / 'stderr.txt', 'w+', encoding='utf-8')
        test.addCleanup(self.log.close)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
        self.process = subprocess.Popen([sys.executable, '-m', 'tests.unit.api_cli_child', '--model-root', str(model),
                                         '--port', '0', *args], env={**os.environ, **(env or {})},
                                        stdout=subprocess.DEVNULL, stderr=self.log, cwd=str(REPO),
                                        creationflags=flags)
        test.addCleanup(self.kill)
        self.starting = None

    def records(self):
        self.log.flush()
        with open(self.log.name, encoding='utf-8') as reader:
            lines = reader.read().splitlines()
        return [json.loads(line) for line in lines if line.startswith('{')], '\n'.join(lines)

    def wait_started(self, seconds=30):
        def started():
            found = [r for r in self.records()[0] if r.get('event') == 'starting']
            self.starting = found[0] if found else None
            return self.starting is not None or self.process.poll() is not None
        wait_for(started, seconds)
        if self.starting is None:
            raise AssertionError(f'child did not start (exit {self.process.poll()}): {self.records()[1]}')
        self.base = f'http://127.0.0.1:{self.starting["port"]}'
        return self.starting

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, None
        except TimeoutError:  # answering slowly is not gone
            return 'timeout', None
        except urllib.error.URLError as error:
            return ('timeout', None) if isinstance(error.reason, TimeoutError) else (None, None)
        except ConnectionError:
            return None, None

    def listening(self):
        """Whether anything accepts TCP on the port (a refused connect can take about
        2 s on Windows, so the timeout is longer than that)."""
        try:
            with socket.create_connection(('127.0.0.1', self.starting['port']), timeout=5):
                return True
        except OSError:  # refused, or no answer at all
            return False

    def server_gone(self, seconds=15):
        return wait_for(lambda: not self.listening(), seconds)

    def kill(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(10)
        if self.starting is not None and not self.server_gone(5):
            try:
                os.kill(self.starting['pid'], signal.SIGTERM)  # TerminateProcess on Windows
            except OSError:
                pass


@unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
class ApiCliProcessTests(unittest.TestCase):
    def test_a_stop_signal_shuts_down_gracefully_with_a_stopped_record_and_exit_0(self):
        child = ChildProcess(self, [])
        starting = child.wait_started()
        self.assertEqual((starting['model_id'], starting['live_events']), ('fx-child-model', False))
        self.assertTrue(wait_for(lambda: child.get('/healthz')[0] == 200, 15))
        self.assertEqual(child.get('/v1/calls')[0], 503)
        if sys.platform == 'win32':
            child.process.send_signal(signal.CTRL_BREAK_EVENT)
            expected = 'SIGBREAK'
        else:
            child.process.send_signal(signal.SIGTERM)
            expected = 'SIGTERM'
        try:
            code = child.process.wait(30)
        except subprocess.TimeoutExpired:
            self.fail(f'no exit after {expected}: {child.records()[1]}')
        records, text = child.records()
        self.assertEqual(code, EXIT_OK, text)
        # 17 Nov (Claude-authored addition): the default operations log in a real uvicorn
        # child: one request line for /v1/calls (503, warning), none for the /healthz
        # probes, written before the stopped record, which stays last.
        self.assertEqual(records[-1], {'event': 'stopped', 'signal': expected,
                                       'event_log': {'lines': 1, 'dropped': 0, 'write_failures': 0,
                                                     'stopped_in_time': True}}, text)
        requests = [r for r in records if r.get('event') == 'request']
        self.assertEqual([(r['route'], r['method'], r['status'], r['level']) for r in requests],
                         [('/v1/calls', 'GET', 503, 'warning')], text)
        self.assertEqual(starting['event_log'], 'stderr')
        self.assertTrue(child.server_gone())

    def test_a_port_in_use_refuses_with_exit_2_and_one_record(self):
        busy = socket.create_server(('127.0.0.1', 0))
        self.addCleanup(busy.close)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        refused = subprocess.run([sys.executable, '-m', 'tests.unit.api_cli_child', '--model-root',
                                  str(model_dir(folder.name)), '--port', str(busy.getsockname()[1])],
                                 cwd=str(REPO), capture_output=True, text=True, timeout=60)
        lines = [json.loads(line) for line in refused.stderr.splitlines() if line.startswith('{')]
        self.assertEqual(refused.returncode, EXIT_REFUSED, refused.stderr)
        self.assertEqual([r['event'] for r in lines], ['refused'])


@REAL
@unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
class RealRedisApiCliTests(unittest.TestCase):
    def setUp(self):
        if not REDIS_URL:
            self.fail('AVA_REQUIRE_REDIS=1 but AVA_TEST_REDIS_URL is not set')
        import redis
        self.publisher = redis.Redis.from_url(REDIS_URL, socket_timeout=5)
        self.addCleanup(self.publisher.close)

    def subscribers(self):
        return self.publisher.pubsub_numsub(CHANNEL)[0][1]

    def test_the_command_line_opens_real_redis_and_relays_a_published_call(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        model = model_dir(folder.name)
        before = self.subscribers()
        source = EventHub()
        forwarder = RedisForwarder(source.subscribe(), self.publisher)
        call_id = f'cli-real-{time.time_ns()}'
        seen = {}

        def serve(app, sock):
            with TestClient(app) as http:
                self.assertTrue(wait_for(lambda: self.subscribers() == before + 1, 10))
                source.publish('call.started', call_id, {})
                self.assertEqual(forwarder.pump(), 1)
                self.assertTrue(wait_for(lambda: http.get(f'/v1/calls/{call_id}').status_code == 200, 10))
                seen['subscribed'] = 'ava_event_relay_subscribed 1\n' in http.get('/metrics').text
        stderr = io.StringIO()
        code = main(['--model-root', str(model), '--port', '0', '--redis-url-env', 'AVA_TEST_REDIS_URL'],
                    environ={'AVA_TEST_REDIS_URL': REDIS_URL}, stderr=stderr,
                    made={'service': fake_service, 'serve': serve})
        records = [json.loads(line) for line in stderr.getvalue().splitlines()]
        self.assertEqual(code, EXIT_OK)
        self.assertEqual((records[0]['redis_reachable'], records[-1]['relay']['relayed']), (True, 1))
        self.assertTrue(seen['subscribed'])
        self.assertNotIn(REDIS_URL, stderr.getvalue())

    def test_a_real_process_serves_http_and_relays_from_redis(self):
        before = self.subscribers()
        child = ChildProcess(self, ['--redis-url-env', 'AVA_TEST_REDIS_URL'], env={'AVA_TEST_REDIS_URL': REDIS_URL})
        starting = child.wait_started()
        self.assertEqual(starting['redis_reachable'], True)
        self.assertTrue(wait_for(lambda: child.get('/healthz')[0] == 200, 30))
        self.assertTrue(wait_for(lambda: self.subscribers() == before + 1, 15), child.records()[1])
        source = EventHub()
        forwarder = RedisForwarder(source.subscribe(), self.publisher)
        call_id = f'cli-process-{time.time_ns()}'
        source.publish('call.started', call_id, {})
        self.assertEqual(forwarder.pump(), 1)
        self.assertTrue(wait_for(lambda: child.get(f'/v1/calls/{call_id}')[0] == 200, 10))
        child.process.kill()
        child.process.wait(10)
        self.assertTrue(child.server_gone(), 'the server process is gone')
        self.assertTrue(wait_for(lambda: self.subscribers() == before, 10), 'its subscription too')
        self.assertNotIn(REDIS_URL, child.records()[1])


if __name__ == '__main__':
    unittest.main()
