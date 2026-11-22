"""Claude-authored lab test (22 November 2026; not GPT-6 Astra): the demo rehearsal of PRD
roadmap 5.9 ("live tapped call, batch upload, timeline, egress proof"), BO-4 and BO-8,
on the real Compose stack with the `demo` profile (backlog 11.2;
docs/operations/demo.md; deploy/compose/README.md "Demo profile").

Opt-in (AVA_TEST_DOCKER_LAB=1) with the same local inputs as test_compose_stack_lab.py
(images, QA clips, Silero, the 14 Nov static-INT8 export with an UNTRAINED head, the
AntiDeepfake checkpoint) plus Playwright's Chromium (`npx playwright install chromium`).
It brings up a disposable project with `--profile lab --profile monitoring --profile
demo` and a free loopback port, then checks:
- the forwarder is published on 127.0.0.1 only: `/readyz` answers through it from the
  host, and the same port on this host's non-loopback address refuses;
- `python -m lab.demo status` reports every service healthy and the dashboard ready;
- the browser rehearsal (tests/browser/compose-demo-rehearsal.spec.cjs, Playwright
  project `compose-demo`) passes: a real tapped call appears on the live board with its
  timeline and ends `bye`, a batch upload shows its verdict and timeline, and the call
  is in the stored history; nothing is loaded from another origin;
- (review M4) a request or WebSocket naming another Host (DNS rebinding) is refused 421
  through the forwarder, while 127.0.0.1 works;
- `python -m lab.demo egress` proves `live` and `api` have no default route and cannot
  connect out;
- the forwarder logs a `listening` record and, after `stop`, a `stopped` record with
  its counts and no client address; no password appears in any log.
Only this module's case runs (load_tests): the inherited Compose cases run in their own
module.
"""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path

from lab import demo
from tests.lab import test_compose_stack_lab as stack

ROOT = Path(__file__).resolve().parents[2]


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def non_loopback_address():
    """This host's address on its default route, or None. A UDP connect sends nothing."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(('10.255.255.255', 1))
            address = s.getsockname()[0]
        except OSError:
            return None
    return None if address.startswith('127.') or address == '0.0.0.0' else address


class ComposeDemoLabTests(stack.ComposeStackLabTests):
    def compose_args(self, *args):
        base = super().compose_args()
        return base + ['--profile', 'demo', *args]

    def setUp(self):
        super().setUp()
        self.demo_port = free_port()
        with open(self.env_file, 'a', encoding='utf-8') as handle:
            handle.write(f'AVA_DEMO_PORT={self.demo_port}\n')

    def demo_main(self, *argv):
        out = io.StringIO()
        code = demo.main([*argv, '--env-file', str(self.env_file), '-p', self.project, '--monitoring'], stdout=out)
        return code, [json.loads(line) for line in out.getvalue().splitlines()]

    def test_the_demo_rehearsal_runs_through_the_loopback_forwarder(self):
        self.compose('up', '-d', '--wait', '--wait-timeout', '420', timeout=700)
        self.wait_healthy('live', 120)
        self.wait_healthy('api', 240)
        self.wait_healthy('demo-proxy', 60)
        self.wait_relay_subscribed(60)

        # Published on the host's loopback only.
        published = self.compose('port', 'demo-proxy', '8080').stdout.strip().splitlines()
        self.assertEqual(published, [f'127.0.0.1:{self.demo_port}'], published)
        with urllib.request.urlopen(f'http://127.0.0.1:{self.demo_port}/readyz', timeout=30) as response:
            self.assertEqual(response.status, 200)
            model_id = json.loads(response.read())['model_id']
        outside = non_loopback_address()
        if outside:
            with self.assertRaises(OSError, msg='the demo port answered on the non-loopback address'):
                socket.create_connection((outside, self.demo_port), timeout=3).close()
        print(f'\nnon-loopback refusal checked: {bool(outside)}')

        code, records = self.demo_main('status', '--demo-port', str(self.demo_port))
        self.assertEqual(code, 0, records)
        self.assertEqual(records[-1]['model_id'], model_id)

        clips = json.loads(stack.QA_CONFIG.read_text(encoding='utf-8'))['genuine'][:5]
        npx = shutil.which('npx')
        self.assertIsNotNone(npx, 'npx (Node 22) is needed for the browser rehearsal')
        rehearsal = subprocess.run(
            [npx, 'playwright', 'test', '--project=compose-demo', '--reporter=list'], cwd=ROOT,
            env={**os.environ, 'AVA_DEMO_URL': f'http://127.0.0.1:{self.demo_port}',
                 'AVA_DEMO_ENV_FILE': str(self.env_file), 'AVA_DEMO_PROJECT': self.project,
                 'AVA_DEMO_PYTHON': sys.executable, 'AVA_DEMO_CALL_WAVS': json.dumps(clips)},
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=1200)
        print('\n' + '\n'.join(line for line in rehearsal.stdout.splitlines() if '[11.2 demo]' in line or 'passed' in line))
        self.assertEqual(rehearsal.returncode, 0, f'{rehearsal.stdout[-4000:]}\n{rehearsal.stderr[-2000:]}')
        self.assertIn('3 passed', rehearsal.stdout)

        # Review M4: a DNS-rebinding page's Host is refused by the API, over HTTP and for
        # the WebSocket, through the real forwarder.
        def raw(host, path, upgrade=False):
            extra = ('Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
                     'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n') if upgrade else 'Connection: close\r\n'
            with socket.create_connection(('127.0.0.1', self.demo_port), timeout=10) as s:
                s.sendall(f'GET {path} HTTP/1.1\r\nHost: {host}\r\nOrigin: http://{host}\r\n{extra}\r\n'.encode())
                answer = b''
                while b'\r\n\r\n' not in answer and (chunk := s.recv(65536)):
                    answer += chunk
            return answer.split(b'\r\n', 1)[0]
        evil = f'evil.example:{self.demo_port}'
        good = f'127.0.0.1:{self.demo_port}'
        self.assertEqual(raw(evil, '/v1/calls'), b'HTTP/1.1 421 Misdirected Request')
        # Re-review R1: an answer must arrive, and it must be a refusal (uvicorn answers a
        # close-before-accept with 403), not an empty read from a broken link.
        refused_ws = raw(evil, '/v1/events', upgrade=True)
        self.assertRegex(refused_ws, rb'^HTTP/1\.1 4\d\d ', f'rebinding WebSocket: {refused_ws!r}')
        print(f'rebinding WebSocket answer: {refused_ws.decode()}')
        self.assertEqual(raw(good, '/v1/calls'), b'HTTP/1.1 200 OK')
        self.assertIn(b' 101 ', raw(good, '/v1/events', upgrade=True), 'the allowed name still opens')

        # Review M2: proven isolation = no default route AND the connection fails.
        code, records = self.demo_main('egress')
        self.assertEqual(code, 0, records)
        self.assertEqual([(r['service'], r['blocked']) for r in records], [('live', True), ('api', True)])
        # The forwarder has a route out (README "Demo profile"); not probed here, so each
        # run makes no outbound connection on purpose.

        self.compose('stop', '-t', '10', 'demo-proxy')
        lines = [json.loads(line) for line in self.compose('logs', '--no-color', '--no-log-prefix',
                                                           'demo-proxy').stdout.splitlines() if line.startswith('{')]
        self.assertEqual(lines[0]['event'], 'listening')
        self.assertEqual(lines[0]['target'], 'api:8000')
        self.assertEqual(lines[-1]['event'], 'stopped', 'SIGTERM reaches the forwarder through init')
        self.assertGreater(lines[-1]['accepted'], 0)
        self.assertEqual(lines[-1]['active'], 0)
        self.assertEqual(len(lines), 2, 'nothing is logged per connection')
        all_logs = self.compose('logs', '--no-color').stdout
        for secret in (self.ari_password, self.db_password, self.grafana_password, self.reader_password):
            self.assertNotIn(secret, all_logs, 'no password in any container log')
        print(f'observed: forwarder accepted {lines[-1]["accepted"]} connections, refused '
              f'{lines[-1]["refused_capacity"]}, failed {lines[-1]["failed_connect"]}; '
              f'{lines[-1]["bytes_to_client"]} bytes to the browser')


def load_tests(loader, tests, pattern):
    """Only this module's own case; the inherited ones belong to test_compose_stack_lab."""
    return unittest.TestSuite([ComposeDemoLabTests('test_the_demo_rehearsal_runs_through_the_loopback_forwarder')])


if __name__ == '__main__':
    unittest.main()
