"""Claude-authored lab test (17 November 2026): the Compose deployment of the live stack
(backlog 10.2; PRD FR-O4 model from a read-only local volume, FR-O7 pinned images,
FR-O8; FR-A1/A3/A4/A8 and FR-U4 through the API image; architecture §2.4-§2.7; PRD
constraint: no telemetry or internet dependency).

Authored by Claude (Claude Code), not by GPT-6 Astra. Opt-in (AVA_TEST_DOCKER_LAB=1);
needs the locally built images `ava-live:dev`, `ava-api:dev` and `ava-lab-asterisk:dev`,
the public QA clips, the Silero model, an exported detector (AVA_LIVE_ONNX; the 14 Nov
static-INT8 export with an UNTRAINED head: scores carry no meaning) and the pinned
AntiDeepfake checkpoint (`model_root` in `.local/real-model-qa.json`; the API's
uncalibrated baseline). It brings up a disposable Compose project (random project name
and passwords, env file in a temporary folder), places a real call into the lab PBX
inside the stack, and checks:
- the live service and the API become ready through their container health checks
  (the API's `/readyz` verifies, loads and warms up the real checkpoint);
- the call is stored in PostgreSQL with `bye` and its windows; the JSON event log
  shows it;
- (17 Nov, attempt 2) the same call reaches a WebSocket subscriber on the API through
  Redis (`call.started` ... `call.ended`, seqs increasing) and `/v1/calls/{id}`;
- the API serves the built dashboard and analyses a public clip with the real model;
- neither container can reach the internet; both run as uid 10001; no password
  appears in the logs.
The project and its volumes are removed afterwards.
"""
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest

from lab.asterisk import rtp_pcap
from models.export import model_card as mc
from tests.lab.test_live_service_lab import ONNX, QA_CONFIG, VAD_MODEL

LAB = unittest.skipIf(os.environ.get('AVA_TEST_DOCKER_LAB') != '1', 'set AVA_TEST_DOCKER_LAB=1')
ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / 'deploy' / 'compose' / 'compose.yaml'
REAL_MODEL_QA = ROOT / '.local' / 'real-model-qa.json'


def _model_root():
    try:
        root = Path(json.loads(REAL_MODEL_QA.read_text(encoding='utf-8'))['model_root'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return root if (root / 'model.safetensors').is_file() else None


MODEL_ROOT = _model_root()

# Run inside the api container (python -c SCRIPT SECONDS): subscribe to WS /v1/events,
# say "subscribed", then print one JSON line per message until `call.ended` or the
# deadline. Only type, call ID and seq are printed.
WS_SUBSCRIBER = textwrap.dedent(r'''
    import json, sys, time
    from websockets.sync.client import connect
    deadline = time.monotonic() + float(sys.argv[1])
    with connect('ws://127.0.0.1:8000/v1/events', open_timeout=10) as ws:
        print('subscribed', flush=True)
        while time.monotonic() < deadline:
            try:
                text = ws.recv(timeout=1)
            except TimeoutError:
                continue
            m = json.loads(text)
            print(json.dumps({'type': m.get('type'), 'call_id': m.get('call_id'), 'seq': m.get('seq')}), flush=True)
            if m.get('type') == 'call.ended':
                break
''')

# Run inside the api container (python -c SCRIPT PATH): GET PATH on the API and print
# {"status", "headers", "body"} as JSON (body as text).
HTTP_GET = textwrap.dedent(r'''
    import json, sys, urllib.error, urllib.request
    try:
        r = urllib.request.urlopen('http://127.0.0.1:8000' + sys.argv[1], timeout=30)
        status, headers, body = r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        status, headers, body = e.code, dict(e.headers), e.read()
    print(json.dumps({'status': status, 'headers': headers, 'body': body.decode('utf-8', 'replace')}))
''')

# Run inside the api container: POST the WAV on stdin to /v1/analyze as multipart and
# print {"status", "body"}.
ANALYZE = textwrap.dedent(r'''
    import json, sys, urllib.error, urllib.request, uuid
    wav = sys.stdin.buffer.read()
    b = uuid.uuid4().hex
    data = (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="clip.wav"\r\n'
            f'Content-Type: audio/wav\r\n\r\n').encode() + wav + f'\r\n--{b}--\r\n'.encode()
    req = urllib.request.Request('http://127.0.0.1:8000/v1/analyze', data=data, method='POST',
                                 headers={'Content-Type': f'multipart/form-data; boundary={b}'})
    try:
        r = urllib.request.urlopen(req, timeout=120)
        status, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read()
    print(json.dumps({'status': status, 'body': json.loads(body)}))
''')

EGRESS_PROBE = ('import socket,sys\n'
                'try:\n socket.create_connection(("1.1.1.1", 443), timeout=3)\n'
                'except OSError as e:\n print("blocked", type(e).__name__); sys.exit(0)\n'
                'sys.exit(1)')


@LAB
@unittest.skipUnless(QA_CONFIG.is_file() and VAD_MODEL.is_file() and ONNX.is_file() and MODEL_ROOT is not None,
                     'needs the local QA clips, the Silero model, an exported detector and the AntiDeepfake checkpoint')
class ComposeStackLabTests(unittest.TestCase):
    def compose_args(self, *args):
        return [shutil.which('docker'), 'compose', '--env-file', str(self.env_file), '-f', str(COMPOSE),
                '-p', self.project, '--profile', 'lab', *args]

    def compose(self, *args, check=True, timeout=300):
        done = subprocess.run(self.compose_args(*args),
                              capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
        if check and done.returncode != 0:
            self.fail(f'docker compose {args[0]} failed: {done.stdout[-2000:]} {done.stderr[-2000:]}')
        return done

    def api_get(self, path):
        done = self.compose('exec', '-T', 'api', 'python', '-c', HTTP_GET, path)
        return json.loads(done.stdout)

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ava-compose-')
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        card_dir = folder / 'card'
        card_dir.mkdir()
        mc.write_card(mc.build_card(
            ONNX, version='compose-l8-int8-static-untrained', backbone='AntiDeepfake MMS-300M, truncated',
            kept_layers=8, metrics={'dev_eer': mc.NOT_MEASURED},
            training_data={'status': 'untrained SLS head (seeded); no AVA training data'},
            failure_modes=['untrained head: scores carry no meaning'],
            licence_restriction='Research and non-commercial use only.'), card_dir / 'model_card.json')
        # Review M1: a database password with URL-hostile characters must still work.
        self.ari_password, self.db_password = secrets.token_hex(16), f'{secrets.token_hex(8)}/@+=%#x'
        self.env_file = folder / 'stack.env'
        self.env_file.write_text('\n'.join([
            f'AVA_ARI_PASSWORD={self.ari_password}', f'AVA_DB_PASSWORD={self.db_password}',
            f'AVA_ONNX_DIR={ONNX.parent.as_posix()}', f'AVA_ONNX_FILE={ONNX.name}',
            f'AVA_CARD_DIR={card_dir.as_posix()}', f'AVA_VAD_DIR={VAD_MODEL.parent.as_posix()}',
            f'AVA_MODEL_ROOT={MODEL_ROOT.as_posix()}']) + '\n')
        self.project = f'ava-ct-{secrets.token_hex(4)}'
        self.folder = folder
        self.addCleanup(self.compose, 'down', '-v', '--remove-orphans', '-t', '10', check=False)

    def wait_healthy(self, service, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            done = self.compose('ps', '--format', 'json', service, check=False)
            text = done.stdout.strip()
            try:  # one object per line (Compose v2.21+), or one array (older versions)
                states = json.loads(text) if text.startswith('[') else \
                    [json.loads(line) for line in text.splitlines() if line.strip()]
            except ValueError:
                states = []
            if states and states[0].get('Health') == 'healthy':
                return
            if states and states[0].get('State') in ('exited', 'dead'):
                break
            time.sleep(2)
        logs = self.compose('logs', '--no-color', service, check=False)
        self.fail(f'{service} not healthy: {logs.stdout[-3000:]}')

    def wait_relay_subscribed(self, seconds):
        """Review L1: pub/sub drops what is published before the relay subscribes, and the
        health check waits for the model, not for Redis."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if 'ava_event_relay_subscribed 1' in self.api_get('/metrics')['body'].splitlines():
                return
            time.sleep(1)
        self.fail('the API relay did not subscribe to Redis')

    def start_subscriber(self, seconds):
        """A WebSocket subscriber inside the api container; returns a queue of its lines
        (None after the last). If the test ends first, the in-container process runs to
        its deadline or until `compose down` removes the container (review L3)."""
        errors = tempfile.TemporaryFile()
        self.addCleanup(errors.close)
        process = subprocess.Popen(self.compose_args('exec', '-T', 'api', 'python', '-c', WS_SUBSCRIBER, str(seconds)),
                                   stdout=subprocess.PIPE, stderr=errors, text=True, encoding='utf-8',
                                   errors='replace')
        lines = queue.Queue()

        def read():
            for line in process.stdout:
                lines.put(line.strip())
            lines.put(None)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()

        def stop():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=30)
            reader.join(timeout=30)
            process.stdout.close()
        self.addCleanup(stop)
        try:
            first = lines.get(timeout=60)
        except queue.Empty:
            self.fail('the WebSocket subscriber did not connect within 60 s')
        if first != 'subscribed':
            process.wait(timeout=30)
            errors.seek(0)
            self.fail(f'subscriber: {first!r} {errors.read()[-1500:]!r}')
        return lines

    def test_the_stack_scores_a_real_call_without_internet(self):
        self.compose('up', '-d', '--wait', '--wait-timeout', '360', timeout=600)
        self.wait_healthy('live', 120)
        self.wait_healthy('api', 240)

        # The API: ready with the real checkpoint (the health check ran the warm-up).
        ready = self.api_get('/readyz')
        self.assertEqual(ready['status'], 200, ready)
        model_id = json.loads(ready['body'])['model_id']
        # The built dashboard on the same origin, with its script reachable.
        index = self.api_get('/')
        self.assertEqual(index['status'], 200)
        self.assertIn('<div id="root">', index['body'])
        # Review L4: every script and stylesheet the page loads is a relative, bundled
        # file served by the API (no CDN); the page's CSP also limits sources to 'self'.
        references = re.findall(r'\s(?:src|href)="([^"]*)"', index['body'])
        self.assertTrue(any(ref.endswith('.js') for ref in references), references)
        self.assertTrue(any(ref.endswith('.css') for ref in references), references)
        for ref in references:
            self.assertTrue(ref.startswith('./'), f'not a relative bundled file: {ref}')
            asset = self.api_get('/' + ref[2:])
            self.assertEqual(asset['status'], 200, ref)
            self.assertNotRegex(asset['body'], r'import\(\s*["\']https?:', f'{ref} imports code from a URL')

        self.wait_relay_subscribed(60)
        lines = self.start_subscriber(150)

        clips = json.loads(QA_CONFIG.read_text(encoding='utf-8'))['genuine'][:3]
        payloads = rtp_pcap.rtp_payloads(b''.join(rtp_pcap.wav_to_pcm8k(path) for path in clips), 'ulaw')
        pcap = self.folder / 'inject.pcap'
        rtp_pcap.write_pcap(payloads, 0, pcap)
        self.compose('cp', str(pcap), 'asterisk:/lab/io/inject.pcap')
        caller = f'cmp{secrets.token_hex(4)}'
        done = self.compose('exec', '-T', 'asterisk', 'sipp', '127.0.0.1:5060', '-sf', '/lab/sipp/uac_inject.xml',
                            '-s', '700', '-key', 'caller', caller, '-key', 'pt', '0', '-key', 'codec', 'PCMU',
                            '-m', '1', '-d', str(len(payloads) * 20 + 1500), '-i', '127.0.0.1', '-mi', '127.0.0.1',
                            '-min_rtp_port', '16000', '-max_rtp_port', '16100', '-timeout', '60s', '-timeout_error',
                            '-nostdin', check=False, timeout=180)
        self.assertEqual(done.returncode, 0, f'sipp: {done.stdout[-1500:]} {done.stderr[-1500:]}')

        rows, deadline = '', time.monotonic() + 60
        while time.monotonic() < deadline:
            rows = self.compose('exec', '-T', 'postgres', 'psql', '-U', 'ava', '-d', 'ava', '-tAc',
                                "SELECT call_id || '|' || coalesce(end_reason, '') || '|' || "
                                '(SELECT count(*) FROM window_scores w WHERE w.call_pk = c.id) FROM calls c',
                                check=False).stdout.strip()
            if rows.count('|bye|') == 1:
                break
            time.sleep(1)
        self.assertEqual(rows.count('\n'), 0, f'one call row: {rows!r}')
        call_id, reason, windows = rows.split('|')
        self.assertEqual(reason, 'bye', rows)
        self.assertGreaterEqual(int(windows), 3, 'windows flushed to PostgreSQL')

        # The same call through Redis to the API's WebSocket.
        seen, deadline = [], time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                break
            seen.append(json.loads(line))
            if seen[-1]['type'] == 'call.ended':
                break
        # Review L2: a stream.gap control message has no seq, so check for it first.
        self.assertNotIn('stream.gap', [event['type'] for event in seen], f'relay loss reported: {seen}')
        ours = [event for event in seen if event['call_id'] == call_id]
        types = [event['type'] for event in ours]
        self.assertTrue(types and types[0] == 'call.started' and types[-1] == 'call.ended', f'events: {seen}')
        self.assertIn('score.updated', types)
        seqs = [event['seq'] for event in seen]
        self.assertEqual(seqs, sorted(set(seqs)), 'seqs strictly increase (no duplicate or reordering)')
        detail = self.api_get(f'/v1/calls/{call_id}')
        self.assertEqual(detail['status'], 200, detail)
        record = json.loads(detail['body'])
        self.assertEqual(record['call_id'], call_id)
        self.assertEqual(record['status'], 'ended', record)

        # File analysis with the real checkpoint inside the api container.
        clip = Path(clips[0]).read_bytes()
        analysed = subprocess.run(self.compose_args('exec', '-T', 'api', 'python', '-c', ANALYZE), input=clip,
                                  capture_output=True, timeout=300)
        self.assertEqual(analysed.returncode, 0, analysed.stderr[-1500:])
        result = json.loads(analysed.stdout)
        self.assertEqual(result['status'], 200, result)
        body = result['body']
        self.assertIn(body['verdict'], ('genuine', 'synthetic', 'undetermined'))
        self.assertEqual(body['model_id'], model_id)
        self.assertGreaterEqual(body['evidence_windows'], 1, body)
        self.assertTrue(body['timeline'], 'per-window scores returned')

        logs = self.compose('logs', '--no-color', '--no-log-prefix', 'live').stdout
        records = []
        for line in logs.splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue  # not every stderr line is JSON (README)
        events = [(r.get('event'), r.get('call_id')) for r in records]
        self.assertIn(('call_started', call_id), events)
        self.assertIn(('call_ended', call_id), events)
        starting = next(r for r in records if r.get('event') == 'starting')
        self.assertEqual((starting['ari_reachable'], starting['redis_reachable'], starting['database_migrations_applied']),
                         (True, True, [1]))
        api_logs = self.compose('logs', '--no-color', '--no-log-prefix', 'api').stdout
        api_start = next(json.loads(line) for line in api_logs.splitlines() if '"starting"' in line)
        self.assertEqual((api_start['live_events'], api_start['redis_reachable'], api_start['dashboard']),
                         (True, True, True))
        all_logs = self.compose('logs', '--no-color').stdout
        for secret in (self.ari_password, self.db_password):
            self.assertNotIn(secret, all_logs, 'no password in any container log')
        # The lab PBX's own console logs caller IDs (Asterisk's business); AVA's must not.
        self.assertNotIn(caller, logs + api_logs, 'the caller number is not in the AVA service logs')
        self.assertNotIn('telemetry', logs.lower(), 'no ONNX Runtime telemetry client started (1DS message)')

        # No egress: the internal network has no route out (no telemetry, no downloads).
        for service in ('live', 'api'):
            probe = self.compose('exec', '-T', service, 'python', '-c', EGRESS_PROBE, check=False)
            self.assertEqual(probe.returncode, 0, f'{service} reached the internet: {probe.stdout}')
            user = self.compose('exec', '-T', service, 'id', '-u').stdout.strip()
            self.assertEqual(user, '10001', f'{service} does not run as root')
        print(f'\nobserved: call {call_id} stored with {windows} windows; {len(ours)} events over the API '
              f'WebSocket; file verdict {body["verdict"]} from {body["evidence_windows"]} windows '
              f'(uncalibrated baseline); API warm-up {json.loads(ready["body"])["warmup_seconds"]:.1f} s')


if __name__ == '__main__':
    unittest.main()
