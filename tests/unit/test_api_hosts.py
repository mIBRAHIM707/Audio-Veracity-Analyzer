"""Claude-authored tests (22 November 2026; not GPT-6 Astra) for the API's Host-header
allowlist, services/api/hosts.py (backlog 11.2 review M4, DNS rebinding; PRD §6.3).

HA01-HA04 are stdlib only (CI): Host parsing; HTTP allowed and refused (421, the app
never called); a WebSocket refused with close 1008 before the app sees it; lifespan
passes; a missing, duplicated or malformed Host is refused. HA05 runs the real API
command line with `--allowed-host` through Starlette's TestClient (skipped without
requirements-api.txt): the dashboard and `/healthz` answer for 127.0.0.1 on any port; a
rebinding name gets 421 on HTTP and cannot open `WS /v1/events`.
"""
import asyncio
import io
import json
import unittest

from services.api.hosts import HostAllowlist, host_of, normalise_hostname


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 10))


class RecordingApp:
    def __init__(self):
        self.scopes = []

    async def __call__(self, scope, receive, send):
        self.scopes.append(scope['type'])
        if scope['type'] == 'http':
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})


async def call(app, scope_type, headers):
    sent = []
    messages = [{'type': 'websocket.connect'}] if scope_type == 'websocket' else [{'type': 'http.request'}]

    async def receive():
        return messages.pop(0) if messages else {'type': 'http.disconnect'}

    async def send(message):
        sent.append(message)
    await app({'type': scope_type, 'headers': headers, 'path': '/'}, receive, send)
    return sent


class HostParsingTests(unittest.TestCase):
    def test_ha01_host_values_and_configured_names(self):
        for value, expected in (('127.0.0.1:8080', '127.0.0.1'), ('LocalHost', 'localhost'), ('api:8000', 'api'),
                                ('localhost.:8080', 'localhost'), ('[::1]:8080', '::1'), ('[::1]', '::1'),
                                ('evil.example:8080', 'evil.example'), ('', None), ('a:b', None), ('[::1', None),
                                ('[::1]x', None), ('bad_name', None), ('127.0.0.1:', None),
                                ('127.0.0.1:999999', None), ('127.0.0.1:65536', None), ('[::1]:70000', None),
                                ('127.0.0.1:65535', '127.0.0.1'), ('x' * 400, None), ('a b', None)):
            with self.subTest(value=value):
                self.assertEqual(host_of(value), expected)
        self.assertEqual(normalise_hostname('LOCALHOST.'), 'localhost')
        self.assertEqual(normalise_hostname('[::1]'), '::1')
        for bad in ('', 'a b', 'host:80', 'http://x'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalise_hostname(bad)
        with self.assertRaises(ValueError):
            HostAllowlist(RecordingApp(), [])


class HostAllowlistTests(unittest.TestCase):
    def setUp(self):
        self.inner = RecordingApp()
        self.app = HostAllowlist(self.inner, ['127.0.0.1', 'localhost', 'api'])

    def test_ha02_http_allowed_passes_and_others_get_421_without_reaching_the_app(self):
        sent = run(call(self.app, 'http', [(b'host', b'127.0.0.1:18080')]))
        self.assertEqual(sent[0]['status'], 200)
        sent = run(call(self.app, 'http', [(b'host', b'api:8000')]))
        self.assertEqual(sent[0]['status'], 200)
        sent = run(call(self.app, 'http', [(b'host', b'evil.example:18080'), (b'origin', b'http://evil.example:18080')]))
        self.assertEqual(sent[0]['status'], 421)
        self.assertEqual(json.loads(sent[1]['body']), {'detail': 'Host not allowed'})
        self.assertEqual((self.inner.scopes, self.app.refused), (['http', 'http'], 1))

    def test_ha03_a_websocket_from_another_host_is_closed_1008_before_the_app(self):
        sent = run(call(self.app, 'websocket', [(b'host', b'evil.example:8080')]))
        self.assertEqual(sent, [{'type': 'websocket.close', 'code': 1008}])
        self.assertEqual(self.inner.scopes, [])
        run(call(self.app, 'websocket', [(b'host', b'localhost:8080')]))
        self.assertEqual(self.inner.scopes, ['websocket'])

    def test_ha04_lifespan_passes_and_missing_duplicate_or_malformed_hosts_are_refused(self):
        async def lifespan():
            async def receive():
                return {'type': 'lifespan.startup'}

            async def send(message):
                pass
            await self.app({'type': 'lifespan'}, receive, send)
        run(lifespan())
        self.assertEqual(self.inner.scopes, ['lifespan'])
        for headers in ([], [(b'host', b'127.0.0.1'), (b'host', b'evil.example')], [(b'host', b'127.0.0.1:x')],
                        [(b'host', b'127.0.0.1.evil.example')]):
            with self.subTest(headers=headers):
                self.assertEqual(run(call(self.app, 'http', headers))[0]['status'], 421)
        self.assertEqual(self.inner.scopes, ['lifespan'])


try:
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from tests.unit import test_api_cli as cli_tests  # a module import: its cases are not re-collected here
    from services.api.__main__ import main
    HAVE_API = True
except ImportError:  # CI installs requirements-audio.txt only
    HAVE_API = False


@unittest.skipUnless(HAVE_API, 'needs requirements-api.txt')
class HostAllowlistCliTests(unittest.TestCase):
    def test_ha05_the_api_command_line_applies_the_allowlist(self):
        fake_service = cli_tests.fake_service
        fixture = cli_tests.ApiCliTests('setUp')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        seen = {}

        def serve(app, sock):
            with TestClient(app, base_url='http://127.0.0.1:18080') as client:
                seen['page'] = client.get('/').status_code
                seen['health'] = client.get('/healthz').status_code
                seen['inside'] = client.get('/healthz', headers={'host': '127.0.0.1:8000'}).status_code
                seen['rebound'] = client.get('/v1/calls', headers={'host': 'evil.example:18080',
                                                                   'origin': 'http://evil.example:18080'}).status_code
                try:
                    with client.websocket_connect('/v1/events', headers={'host': 'evil.example:18080'}):
                        seen['ws'] = 'opened'
                except WebSocketDisconnect as error:
                    seen['ws'] = error.code
            return 'SIGTERM'
        stderr = io.StringIO()
        code = main(['--model-root', str(fixture.model), '--port', '0', '--dashboard', str(fixture.dashboard),
                     '--event-log', 'off', '--allowed-host', '127.0.0.1', '--allowed-host', 'LOCALHOST'],
                    environ={}, stderr=stderr, made={'service': fake_service, 'bind': fixture.bind, 'serve': serve})
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(seen, {'page': 200, 'health': 200, 'inside': 200, 'rebound': 421, 'ws': 1008})
        starting = json.loads(stderr.getvalue().splitlines()[0])
        self.assertEqual(starting['allowed_hosts'], ['127.0.0.1', 'localhost'])
        with self.assertRaises(SystemExit), unittest.mock.patch('sys.stderr', io.StringIO()):
            main(['--model-root', str(fixture.model), '--port', '0', '--allowed-host', 'http://x'], environ={},
                 stderr=io.StringIO(), made={'service': fake_service, 'serve': lambda *a: None})


import unittest.mock  # noqa: E402

if __name__ == '__main__':
    unittest.main()
