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
- (18 Nov, profile `monitoring`, PRD FR-O6) Prometheus scrapes both targets on the
  internal `metrics` network and holds the call's scored windows; every query of the
  provisioned Grafana dashboard evaluates; Grafana (admin login) lists the dashboard and
  reads a live series through its data source; live's health endpoint is unreachable
  from `backend`; neither monitoring container reaches the internet and Grafana logs no
  call home. (18 Nov, attempt 4) every event the WebSocket subscriber received was
  timed in `ava_ws_delivery_seconds`, none unmeasured, no slow-client close.
- (18 Nov, 5.QA) restarts during a real call: of the live container (re-tap, two
  sessions), of PostgreSQL and Redis (no window lost from the store, the Redis outage
  reported to the client), and of the API (registry picks the call up again); (19 Nov)
  a SIGKILL of the live container (inferred `monitor_lost` end in the store, the
  registry supersedes the old session); see each case's docstring.
- (20 Nov, PRD FR-A9) an edit of the scoring file in a host folder reaches live through
  the folder bind mount and is applied, refused or reported, with no restart.
- (20 Nov, backlog 5.1) the API reads stored history as the read-only `ava_reader`
  created by the one-shot `db-setup`, cannot write or read account tables, and a new
  reader password rotates on the next `up`; the full-stack call is in the history too.
The project and its volumes are removed afterwards.
"""
import base64
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
from urllib.parse import quote

from lab.asterisk import rtp_pcap
from models.export import model_card as mc
from services.store.migrate import load_migrations
from tests.lab.test_live_service_lab import ONNX, QA_CONFIG, VAD_MODEL

LAB = unittest.skipIf(os.environ.get('AVA_TEST_DOCKER_LAB') != '1', 'set AVA_TEST_DOCKER_LAB=1')
ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / 'deploy' / 'compose' / 'compose.yaml'
DASHBOARD = ROOT / 'deploy' / 'compose' / 'monitoring' / 'grafana' / 'dashboards' / 'ava-overview.json'
REAL_MODEL_QA = ROOT / '.local' / 'real-model-qa.json'


def _model_root():
    try:
        root = Path(json.loads(REAL_MODEL_QA.read_text(encoding='utf-8'))['model_root'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return root if (root / 'model.safetensors').is_file() else None


MODEL_ROOT = _model_root()

# Run inside the api container (python -c SCRIPT SECONDS [ENDS]): subscribe to WS
# /v1/events, say "subscribed", then print one JSON line per message until the ENDS-th
# `call.ended` (default 1) or the deadline. Only type, call ID, seq, an end's reason and
# a `stream.gap`'s counts are printed.
WS_SUBSCRIBER = textwrap.dedent(r'''
    import json, sys, time
    from websockets.sync.client import connect
    deadline = time.monotonic() + float(sys.argv[1])
    ends = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    with connect('ws://127.0.0.1:8000/v1/events', open_timeout=10) as ws:
        print('subscribed', flush=True)
        while time.monotonic() < deadline:
            try:
                text = ws.recv(timeout=1)
            except TimeoutError:
                continue
            m = json.loads(text)
            out = {'type': m.get('type'), 'call_id': m.get('call_id'), 'seq': m.get('seq')}
            if m.get('type') == 'call.ended':
                out['reason'] = (m.get('data') or {}).get('reason')
            if m.get('type') == 'stream.gap':
                out.update({k: m[k] for k in ('dropped', 'upstream_missing', 'upstream_interruptions') if k in m})
            print(json.dumps(out), flush=True)
            if m.get('type') == 'call.ended':
                ends -= 1
                if ends == 0:
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
                '-p', self.project, '--profile', 'lab', '--profile', 'monitoring', *args]

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
        self.grafana_password = secrets.token_hex(16)
        # 20 Nov: the API's read-only login (db-setup); printable ASCII, URL-hostile too.
        self.reader_password = f'{secrets.token_hex(10)}/@+=%x'
        # Re-review: a run-specific scrape subnet, so another AVA stack on this host (with
        # the 10.231.250.0/24 default) does not collide with the test's.
        octet = 1 + secrets.randbelow(254)
        self.live_metrics_ip = f'10.232.{octet}.10'
        self.env_file = folder / 'stack.env'
        self.env_file.write_text('\n'.join([
            f'AVA_ARI_PASSWORD={self.ari_password}', f'AVA_DB_PASSWORD={self.db_password}',
            f'AVA_DB_READER_PASSWORD={self.reader_password}',
            f'AVA_GRAFANA_ADMIN_PASSWORD={self.grafana_password}',
            f'AVA_METRICS_SUBNET=10.232.{octet}.0/24', f'AVA_METRICS_DYNAMIC_RANGE=10.232.{octet}.128/25',
            f'AVA_LIVE_METRICS_IP={self.live_metrics_ip}',
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

    def prom_get(self, path):
        """GET a Prometheus HTTP API path inside its container (busybox wget); JSON."""
        done = self.compose('exec', '-T', 'prometheus', 'wget', '-q', '-O', '-', f'http://127.0.0.1:9090{path}')
        return json.loads(done.stdout)

    def prom_value(self, query):
        """Sum of an instant query's sample values, or None with no samples."""
        result = self.prom_get('/api/v1/query?query=' + quote(query, safe=''))
        self.assertEqual(result['status'], 'success', result)
        samples = result['data']['result']
        return sum(float(sample['value'][1]) for sample in samples) if samples else None

    def grafana(self, path, body=None):
        """Grafana HTTP API inside its container as the admin (curl); (status, JSON)."""
        token = base64.b64encode(f'admin:{self.grafana_password}'.encode()).decode()
        args = ['exec', '-T', 'grafana', 'curl', '-s', '-o', '-', '-w', '\n%{http_code}', '-H',
                f'Authorization: Basic {token}']
        if body is not None:
            args += ['-H', 'Content-Type: application/json', '-X', 'POST', '--data-binary', json.dumps(body)]
        done = self.compose(*args, f'http://127.0.0.1:3000{path}')
        text, _, status = done.stdout.rpartition('\n')
        return int(status), json.loads(text) if text.strip() else None

    def wait_targets_up(self, seconds):
        deadline, targets = time.monotonic() + seconds, []
        while time.monotonic() < deadline:
            targets = self.prom_get('/api/v1/targets')['data']['activeTargets']
            if sorted((t['labels']['job'], t['health']) for t in targets) == [('ava-api', 'up'), ('ava-live', 'up')]:
                return
            time.sleep(2)
        self.fail(f'scrape targets not up: {[(t["labels"], t["health"], t.get("lastError")) for t in targets]}')

    def wait_relay_subscribed(self, seconds):
        """Review L1: pub/sub drops what is published before the relay subscribes, and the
        health check waits for the model, not for Redis."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if 'ava_event_relay_subscribed 1' in self.api_get('/metrics')['body'].splitlines():
                return
            time.sleep(1)
        self.fail('the API relay did not subscribe to Redis')

    def start_subscriber(self, seconds, ends=1):
        """A WebSocket subscriber inside the api container; returns a queue of its lines
        (None after the last). If the test ends first, the in-container process runs to
        its deadline or until `compose down` removes the container (review L3)."""
        errors = tempfile.TemporaryFile()
        self.addCleanup(errors.close)
        process = subprocess.Popen(self.compose_args('exec', '-T', 'api', 'python', '-c', WS_SUBSCRIBER, str(seconds),
                                                     str(ends)),
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
        self.wait_healthy('prometheus', 60)
        self.wait_healthy('grafana', 120)
        self.wait_targets_up(60)  # first scrape within one 15 s interval

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
        self.assertEqual((starting['ari_reachable'], starting['redis_reachable']), (True, True))
        # 20 Nov: db-setup (for the API's reader role) and live both migrate at start, under
        # one advisory lock; whichever comes first applies them, and each applies once.
        [setup] = [json.loads(line) for line in self.compose('logs', '--no-color', '--no-log-prefix',
                                                             'db-setup').stdout.splitlines() if line.startswith('{')]
        applied = starting['database_migrations_applied'] + setup['migrated']
        self.assertEqual(sorted(applied), [m.version for m in load_migrations()], (starting, setup))
        api_logs = self.compose('logs', '--no-color', '--no-log-prefix', 'api').stdout
        api_start = next(json.loads(line) for line in api_logs.splitlines() if '"starting"' in line)
        self.assertEqual((api_start['live_events'], api_start['redis_reachable'], api_start['dashboard']),
                         (True, True, True))
        monitoring = self.check_monitoring(int(windows), len(seen))
        all_logs = self.compose('logs', '--no-color').stdout
        for secret in (self.ari_password, self.db_password, self.grafana_password, self.reader_password):
            self.assertNotIn(secret, all_logs, 'no password in any container log')
        # 20 Nov: the call is also in the stored history the API reads as `ava_reader`.
        stored = self.api_get('/v1/history/calls')
        self.assertEqual(stored['status'], 200, stored)
        self.assertIn(call_id, [c['call_id'] for c in json.loads(stored['body'])['calls']])
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
              f'(uncalibrated baseline); API warm-up {json.loads(ready["body"])["warmup_seconds"]:.1f} s; '
              f'monitoring: {monitoring}')

    def call_rows(self):
        """`call_id|end_reason|windows` per calls row, oldest first."""
        return self.compose('exec', '-T', 'postgres', 'psql', '-U', 'ava', '-d', 'ava', '-tAc',
                            "SELECT call_id || '|' || coalesce(end_reason, '') || '|' || "
                            '(SELECT count(*) FROM window_scores w WHERE w.call_pk = c.id) FROM calls c ORDER BY c.id',
                            check=False).stdout.strip().splitlines()

    def start_long_call(self, prefix):
        """Start a SIPp call of about 55 s of speech in the background; returns (process,
        output file, call length in ms, start time). The lab dialplan's Record() hangs up at
        60 s, so the call cannot be longer."""
        clips = json.loads(QA_CONFIG.read_text(encoding='utf-8'))['genuine'][:3] * 4
        payloads = rtp_pcap.rtp_payloads(b''.join(rtp_pcap.wav_to_pcm8k(path) for path in clips), 'ulaw')
        pcap = self.folder / 'inject.pcap'
        rtp_pcap.write_pcap(payloads, 0, pcap)
        self.compose('cp', str(pcap), 'asterisk:/lab/io/inject.pcap')
        caller = f'{prefix}{secrets.token_hex(4)}'
        call_ms = len(payloads) * 20
        # Review M3: SIPp's screen output goes to a file, not a pipe nobody reads until the
        # end (a full pipe would block SIPp and disturb the call's timing).
        sipp_out = tempfile.TemporaryFile()
        self.addCleanup(sipp_out.close)
        sipp = subprocess.Popen(
            self.compose_args('exec', '-T', 'asterisk', 'sipp', '127.0.0.1:5060', '-sf', '/lab/sipp/uac_inject.xml',
                              '-s', '700', '-key', 'caller', caller, '-key', 'pt', '0', '-key', 'codec', 'PCMU',
                              '-m', '1', '-d', str(call_ms + 1500), '-i', '127.0.0.1', '-mi', '127.0.0.1',
                              '-min_rtp_port', '16000', '-max_rtp_port', '16100', '-timeout', '120s',
                              '-timeout_error', '-nostdin'),
            stdout=sipp_out, stderr=subprocess.STDOUT)
        started_at = time.monotonic()

        def stop_sipp():  # on failure: the docker exec client goes; the stack's `down` ends SIPp
            if sipp.poll() is None:
                sipp.kill()
            sipp.wait(timeout=30)
        self.addCleanup(stop_sipp)
        return sipp, sipp_out, call_ms, started_at

    @staticmethod
    def collector(lines, seen):
        """collect(until, seconds): append subscriber events to `seen` until `until(event)`
        is true (True) or the time runs out or the subscriber ends (False)."""
        def collect(until, seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                try:
                    line = lines.get(timeout=max(0.1, deadline - time.monotonic()))
                except queue.Empty:
                    return False
                if line is None:
                    return False
                seen.append(json.loads(line))
                if until(seen[-1]):
                    return True
            return False
        return collect

    def live_metrics(self):
        """The live process's `/metrics`, read from the api container over the internal
        `metrics` network: {'name{labels}': value}."""
        text = self.compose('exec', '-T', 'api', 'python', '-c', 'import urllib.request,sys; sys.stdout.write('
                            f'urllib.request.urlopen("http://{self.live_metrics_ip}:9100/metrics", timeout=10)'
                            '.read().decode())').stdout
        values = {}
        for line in text.splitlines():
            if line and not line.startswith('#'):
                key, _, value = line.rpartition(' ')
                values[key] = float(value)
        return values

    def live_records(self):
        """The JSON lines of live's operations log (not every stderr line is JSON)."""
        records = []
        for line in self.compose('logs', '--no-color', '--no-log-prefix', 'live').stdout.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def test_the_api_reads_stored_history_through_the_read_only_login(self):
        """Backlog 5.1 read-only role through the Compose wiring (Claude-authored, 20 Nov
        2026; PRD FR-A3, FR-A6, §6.3). `up api` runs the one-shot `db-setup` first (as the
        owner: migrations, then `ava_reader`), then the API with the reader's login only:
        - db-setup exits 0 with one `reader_role` record (created, every migration);
        - a call row written by the owner is served by `/v1/history/calls`,
          `/v1/calls/{id}/history` and `/v1/alerts`;
        - inside the api container, its own login is `ava_reader`: it cannot write any
          history table even after turning read-only off, cannot read `users` or
          `audit_log`, and the owner password is not in its environment;
        - a new reader password in the env file plus `up` again rotates it (db-setup
          reruns with `created: false`) and the API reads with the new one;
        - no password appears in any log. Live is not started: it does not depend on this."""
        def up():
            self.compose('up', '-d', '--wait', '--wait-timeout', '360', 'api', timeout=600)
            self.wait_healthy('api', 240)

        def setup_records():
            lines = self.compose('logs', '--no-color', '--no-log-prefix', 'db-setup').stdout.splitlines()
            return [json.loads(line) for line in lines if line.startswith('{')]

        def psql(sql):
            return self.compose('exec', '-T', 'postgres', 'psql', '-U', 'ava', '-d', 'ava', '-v', 'ON_ERROR_STOP=1',
                                '-tAc', sql).stdout.strip()

        up()
        [record] = setup_records()
        self.assertEqual({k: record[k] for k in ('event', 'role', 'created', 'schema', 'migrated')},
                         {'event': 'reader_role', 'role': 'ava_reader', 'created': True, 'schema': 'public',
                          'migrated': [m.version for m in load_migrations()]}, record)
        state = self.compose('ps', '-a', '--format', '{{.Service}} {{.State}} {{.ExitCode}}', 'db-setup').stdout
        self.assertIn('db-setup exited 0', state)

        call_id = f'lab-hist-{secrets.token_hex(4)}'
        psql('INSERT INTO calls (call_id, started_at, last_event_at, last_seq) '
             f"VALUES ('{call_id}', now(), now(), 1)")
        listed = self.api_get('/v1/history/calls')
        self.assertEqual(listed['status'], 200, listed)
        self.assertEqual([c['call_id'] for c in json.loads(listed['body'])['calls']], [call_id])
        detail = self.api_get(f'/v1/calls/{call_id}/history')
        self.assertEqual(detail['status'], 200, detail)
        self.assertEqual(json.loads(detail['body'])['sessions'][0]['call_id'], call_id)
        alerts = self.api_get('/v1/alerts')
        self.assertEqual((alerts['status'], json.loads(alerts['body'])['alerts']), (200, []), alerts)

        probe = textwrap.dedent(r'''
            import json, os, psycopg
            from psycopg import errors
            out = {}
            with psycopg.connect(os.environ['AVA_DATABASE_URL'], autocommit=True) as c:
                out['user'] = c.execute('SELECT current_user').fetchone()[0]
                out['read_only'] = c.execute('SHOW default_transaction_read_only').fetchone()[0]
                c.execute('SET default_transaction_read_only = off')
                for sql in ("DELETE FROM calls", "UPDATE calls SET verdict = 'x'", "DELETE FROM window_scores",
                            "DELETE FROM alerts", "TRUNCATE calls", "SELECT * FROM users", "SELECT * FROM audit_log",
                            "CREATE TABLE stolen (x int)", "CREATE TEMP TABLE stolen_temp (x int)",
                            "SELECT lo_create(0)"):
                    try:
                        c.execute(sql)
                        out[sql] = 'allowed'
                    except errors.InsufficientPrivilege:
                        out[sql] = 'denied'
            print(json.dumps(out))
        ''')
        result = json.loads(self.compose('exec', '-T', 'api', 'python', '-c', probe).stdout)
        self.assertEqual((result.pop('user'), result.pop('read_only')), ('ava_reader', 'on'))
        self.assertEqual(set(result.values()), {'denied'}, result)
        environment = self.compose('exec', '-T', 'api', 'env').stdout
        self.assertNotIn(self.db_password, environment, 'the API container never has the owner password')
        self.assertIn(call_id, psql(f"SELECT call_id FROM calls WHERE call_id = '{call_id}'"), 'nothing deleted')

        # Rotation: a new reader password, `up` again.
        old_password = self.reader_password
        self.reader_password = f'{secrets.token_hex(10)}+rotated'
        text = self.env_file.read_text(encoding='utf-8').replace(f'AVA_DB_READER_PASSWORD={old_password}',
                                                                 f'AVA_DB_READER_PASSWORD={self.reader_password}')
        self.env_file.write_text(text, encoding='utf-8')
        up()
        records = setup_records()
        self.assertEqual([r.get('created') for r in records][-1], False, records)
        again = self.api_get('/v1/history/calls')
        self.assertEqual(again['status'], 200, again)
        self.assertEqual([c['call_id'] for c in json.loads(again['body'])['calls']], [call_id])
        old_login = self.compose('exec', '-T', '-e', f'PGPASSWORD={old_password}', 'api', 'python', '-c',
                                 'import os, psycopg; psycopg.connect(os.environ["AVA_DATABASE_URL"]).close()',
                                 check=False)
        self.assertNotEqual(old_login.returncode, 0, 'the old reader password no longer logs in')
        self.assertIn('password authentication failed', old_login.stderr)

        logs = self.compose('logs', '--no-color').stdout
        for secret in (self.db_password, old_password, self.reader_password):
            self.assertNotIn(secret, logs, 'no password in any container log')
        print(f'\nobserved: db-setup records {[(r["event"], r["created"]) for r in records]}; history served '
              f'through ava_reader; writes and account tables denied; rotation applied')

    def test_a_scoring_config_edit_on_the_host_reaches_live_without_a_restart(self):
        """FR-A9 config file plus reload through the Compose wiring (Claude-authored, 20 Nov
        2026; PRD FR-A9, FR-I4; architecture §7.3). The scoring file sits in a host folder
        bind-mounted read-only into live (AVA_SCORING_CONFIG_DIR). Each edit replaces the
        file by rename, as the README asks; the container must see the new inode through
        the folder mount and apply, refuse or report it, with no restart:
        - a threshold change is applied (`config_reloaded`, `ava_live_config_version` + 1);
        - a hop change is refused as "needs a restart" and the version stays;
        - a removed file is `config_unreadable`; putting the applied text back is
          `config_unchanged` (no new version).
        The per-call effect of a reload (FR-I4) is test_live_config_reload.py's; this case
        is the deployment path: host folder, Docker Desktop bind mount, rename, live.
        Kept strict on purpose (review L3): an unexpected `config_unreadable` just before
        an expected outcome means the host's file-sharing layer showed the rename
        non-atomically, which is worth knowing, not a live defect to retry away."""
        scoring = self.folder / 'scoring'
        scoring.mkdir()
        shipped = (ROOT / 'deploy' / 'compose' / 'config' / 'scoring.json').read_text(encoding='utf-8')
        (scoring / 'scoring.json').write_text(shipped, encoding='utf-8')
        with self.env_file.open('a', encoding='utf-8') as env:
            env.write(f'AVA_SCORING_CONFIG_DIR={scoring.as_posix()}\nAVA_SCORING_CONFIG_WATCH_S=2\n')
        self.compose('up', '-d', '--wait', '--wait-timeout', '300', 'live', 'asterisk', timeout=600)
        self.wait_healthy('live', 120)
        metrics_script = ('import urllib.request,sys; sys.stdout.write(urllib.request.urlopen('
                          f'"http://{self.live_metrics_ip}:9100/metrics", timeout=10).read().decode())')

        def metrics():
            values = {}
            for line in self.compose('exec', '-T', 'live', 'python', '-c', metrics_script).stdout.splitlines():
                if line and not line.startswith('#'):
                    key, _, value = line.rpartition(' ')
                    values[key] = float(value)
            return values

        def replace(text):  # write a temporary file in the folder, then rename it over the old one
            temporary = scoring / 'scoring.json.tmp'
            temporary.write_text(text, encoding='utf-8')
            os.replace(temporary, scoring / 'scoring.json')

        def wait_for(event, after, seconds=60):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                records = [r for r in self.live_records() if str(r.get('event', '')).startswith('config_')]
                if len(records) > after and records[after].get('event') == event:
                    return records[after]
                if len(records) > after:
                    self.fail(f'expected {event}, got {records[after:]}')
                time.sleep(1)
            self.fail(f'no {event} within {seconds} s: {self.live_records()[-10:]}')

        before = metrics()
        version = before['ava_live_config_version']
        self.assertEqual(before['ava_live_config_reloads_total{outcome="applied"}'], 0, before)
        starting = [r for r in self.live_records() if r.get('event') == 'starting']
        self.assertEqual(len(starting), 1)
        self.assertEqual(starting[0].get('scoring_config_watch_s'), 2.0, starting[0])

        document = json.loads(shipped)
        document['scoring']['thresholds']['alert_enter'] = 0.8
        applied_text = json.dumps(document, indent=2)
        replace(applied_text)
        applied = wait_for('config_reloaded', 0)
        self.assertEqual(applied['version'], version + 1, applied)
        after_apply = metrics()
        self.assertEqual(after_apply['ava_live_config_version'], version + 1)
        self.assertEqual(after_apply['ava_live_config_reloads_total{outcome="applied"}'], 1)

        document['audio']['hop_s'] = 1.0
        replace(json.dumps(document, indent=2))
        rejected = wait_for('config_rejected', 1)
        self.assertEqual(rejected['problems'], ['audio.hop_s applies to every call and needs a restart'], rejected)
        self.assertEqual(metrics()['ava_live_config_version'], version + 1, 'a refused edit changes nothing')

        os.remove(scoring / 'scoring.json')
        unreadable = wait_for('config_unreadable', 2)
        self.assertEqual(unreadable['error'], 'FileNotFoundError', unreadable)
        replace(applied_text)
        unchanged = wait_for('config_unchanged', 3)
        self.assertEqual(unchanged['version'], version + 1, unchanged)

        final = metrics()
        self.assertEqual({outcome: final[f'ava_live_config_reloads_total{{outcome="{outcome}"}}']
                          for outcome in ('applied', 'unchanged', 'rejected', 'unreadable')},
                         {'applied': 1, 'unchanged': 1, 'rejected': 1, 'unreadable': 1})
        records = self.live_records()
        self.assertEqual(sum(1 for r in records if r.get('event') == 'starting'), 1, 'live never restarted')
        logs = self.compose('logs', '--no-color', '--no-log-prefix', 'live').stdout
        self.assertNotIn(scoring.as_posix(), logs, 'no host path in the log')
        self.assertNotIn('/config/scoring', logs, 'no container path in the log')
        print(f'\nobserved: version {version:g} -> {version + 1:g} on a host-side rename through the folder '
              'bind mount; hop edit refused; removal reported, restore unchanged; no restart')

    def test_a_live_restart_mid_call_never_stops_the_call(self):
        """5.QA process restart (Claude-authored, 18 Nov 2026; PRD "AVA failure must not
        stop a call", FR-I11; architecture §2.4 `monitor_lost`). The live container is
        restarted (SIGTERM, graceful) while a real call is being scored: the call itself
        completes; the old process ends it `monitor_lost`; the new one re-taps the still
        answered channel (`TapApp.resume`) and scores it to `bye`; PostgreSQL holds the
        two monitoring sessions as two rows; the API's subscriber sees both through Redis
        and the relay reports the source restart."""
        self.compose('up', '-d', '--wait', '--wait-timeout', '360', timeout=600)
        self.wait_healthy('live', 120)
        self.wait_healthy('api', 240)
        self.wait_relay_subscribed(60)
        # Review L2: longer than the sum of every wait below, so the subscriber never ends first.
        lines = self.start_subscriber(900, ends=2)

        # Review L1: the remaining-time check after the restart below tells a slow restart
        # apart from a re-tap defect.
        sipp, sipp_out, call_ms, call_started_at = self.start_long_call('rst')
        seen = []
        collect = self.collector(lines, seen)

        self.assertTrue(collect(lambda e: e['type'] == 'score.updated', 60), f'no score before the restart: {seen}')
        call_id = seen[-1]['call_id']
        restarted_at = time.monotonic()
        self.compose('restart', '-t', '20', 'live', timeout=120)
        self.wait_healthy('live', 120)
        live_back_s = time.monotonic() - restarted_at
        remaining_s = call_ms / 1000 - (time.monotonic() - call_started_at)
        self.assertGreater(remaining_s, 15, f'live took {live_back_s:.1f} s to come back: the call had almost '
                                            'ended, so a missing re-tap below would not be a re-tap defect')

        def rescored(event):  # a score of this call after its monitor_lost end
            return event['type'] == 'score.updated' and event['call_id'] == call_id and any(
                e['type'] == 'call.ended' and e['call_id'] == call_id for e in seen[:-1])
        self.assertTrue(collect(rescored, 60), f'not scored again after the restart: {seen}')
        # The API's call registry shows the re-tapped call as monitored again (it ignored
        # the new session's start before the 18 Nov registry fix).
        during = json.loads(self.api_get(f'/v1/calls/{call_id}')['body'])
        self.assertEqual((during['status'], during['end_reason']), ('active', None), during)

        returncode = sipp.wait(timeout=call_ms / 1000 + 120)
        sipp_out.seek(0)
        self.assertEqual(returncode, 0, f'the call did not complete: {sipp_out.read()[-1500:]!r}')
        self.assertTrue(collect(lambda e: e['type'] == 'call.ended' and e.get('reason') == 'bye', 60),
                        f'no bye end after the restart: {seen}')

        ours = [e for e in seen if e.get('call_id') == call_id]
        ends = [e.get('reason') for e in ours if e['type'] == 'call.ended']
        self.assertEqual(ends, ['monitor_lost', 'bye'], f'events: {seen}')
        first_end = next(i for i, e in enumerate(ours) if e['type'] == 'call.ended')
        after = [e['type'] for e in ours[first_end + 1:]]
        self.assertEqual(after[0], 'call.started', 're-tapped after the restart')
        self.assertIn('score.updated', after, 'scored again after the restart')
        gaps = [e for e in seen if e['type'] == 'stream.gap']
        self.assertTrue(gaps and all(g['dropped'] == 0 for g in gaps), f'gap report: {gaps}')
        self.assertGreaterEqual(sum(g.get('upstream_interruptions', 0) for g in gaps), 1,
                                'the scorer restart is reported to the client')

        rows, deadline = [], time.monotonic() + 60
        while time.monotonic() < deadline:
            rows = self.call_rows()
            if [r.split('|')[1] for r in rows] == ['monitor_lost', 'bye']:
                break
            time.sleep(1)
        self.assertEqual([r.split('|')[:2] for r in rows], [[call_id, 'monitor_lost'], [call_id, 'bye']], rows)
        windows = [int(r.split('|')[2]) for r in rows]
        self.assertTrue(all(n >= 1 for n in windows), f'windows stored in both sessions: {rows}')
        detail = self.api_get(f'/v1/calls/{call_id}')
        self.assertEqual(detail['status'], 200, detail)
        final = json.loads(detail['body'])
        self.assertEqual((final['status'], final['end_reason']), ('ended', 'bye'), final)
        print(f'\nobserved: live back healthy {live_back_s:.1f} s after the restart began; call of '
              f'{call_ms / 1000:.0f} s completed; windows before/after {windows[0]}/{windows[1]}; '
              f'gap reports {gaps}')

    def test_a_killed_live_process_never_stops_the_call(self):
        """5.QA abrupt stop (Claude-authored, 19 Nov 2026; PRD "AVA failure must not stop a
        call", FR-A3, FR-I11; architecture §2.4 `monitor_lost`). The live container is
        killed with SIGKILL while a real call is scored: no `call.ended` is sent and the
        old process's unsent events are lost. The container is started again. The call
        completes; the new process sweeps the old process's leftover channels, re-taps
        the call and scores it to `bye`. The API registry supersedes the old session
        (18 Nov rule); PostgreSQL ends the old row `monitor_lost` with an inferred end (no
        speech seconds) and keeps the new session as a second row (19 Nov rule); no alert
        stays open; the relay reports the source restart to the client."""
        self.compose('up', '-d', '--wait', '--wait-timeout', '360', timeout=600)
        self.wait_healthy('live', 120)
        self.wait_healthy('api', 240)
        self.wait_relay_subscribed(60)
        lines = self.start_subscriber(900)
        sipp, sipp_out, call_ms, call_started_at = self.start_long_call('kil')
        seen = []
        collect = self.collector(lines, seen)

        self.assertTrue(collect(lambda e: e['type'] == 'score.updated', 60), f'no score before the kill: {seen}')
        call_id = seen[-1]['call_id']
        self.assertRegex(call_id, r'^[0-9A-Za-z._-]+$', 'safe to quote in SQL')

        def psql(sql):
            return self.compose('exec', '-T', 'postgres', 'psql', '-U', 'ava', '-d', 'ava', '-tAc', sql,
                                check=False).stdout.strip()
        # Review L5: the old session has a window in PostgreSQL before the kill (a score
        # seen through Redis does not prove the store pump had written it).
        deadline = time.monotonic() + 30
        while psql("SELECT count(*) FROM window_scores w JOIN calls c ON c.id = w.call_pk "
                   f"WHERE c.call_id = '{call_id}'") in ('', '0'):
            self.assertLess(time.monotonic(), deadline, 'no window of the call stored before the kill')
            time.sleep(0.5)
        killed_at = time.monotonic()
        self.compose('kill', '-s', 'SIGKILL', 'live', timeout=60)
        # The restart policy may already have started it again; `start` is then a no-op.
        self.compose('start', 'live', timeout=120)
        self.wait_healthy('live', 120)
        live_back_s = time.monotonic() - killed_at
        remaining_s = call_ms / 1000 - (time.monotonic() - call_started_at)
        self.assertGreater(remaining_s, 15, f'live took {live_back_s:.1f} s to come back: the call had almost '
                                            'ended, so a missing re-tap below would not be a re-tap defect')

        def restarted(event):  # the new session's start of this call
            return event['type'] == 'call.started' and event['call_id'] == call_id
        self.assertTrue(collect(restarted, 60), f'not re-tapped after the kill: {seen}')
        returncode = sipp.wait(timeout=call_ms / 1000 + 120)
        sipp_out.seek(0)
        self.assertEqual(returncode, 0, f'the call did not complete: {sipp_out.read()[-1500:]!r}')
        self.assertTrue(collect(lambda e: e['type'] == 'call.ended' and e['call_id'] == call_id, 60),
                        f'no end after the kill: {seen}')

        ours = [e for e in seen if e.get('call_id') == call_id]
        self.assertEqual([e.get('reason') for e in ours if e['type'] == 'call.ended'], ['bye'],
                         f'the killed process sent no end: {seen}')
        starts = [i for i, e in enumerate(ours) if e['type'] == 'call.started']
        self.assertEqual(len(starts), 2, f'two monitoring sessions: {seen}')
        self.assertIn('score.updated', [e['type'] for e in ours[starts[1]:]], 'scored again after the kill')
        gaps = [e for e in seen if e['type'] == 'stream.gap']
        self.assertTrue(gaps and all(g['dropped'] == 0 for g in gaps), f'gap report: {gaps}')
        self.assertGreaterEqual(sum(g.get('upstream_interruptions', 0) for g in gaps), 1,
                                'the scorer restart is reported to the client')

        # The API registry: the old session was superseded, not merged; the call ended bye.
        final = json.loads(self.api_get(f'/v1/calls/{call_id}')['body'])
        self.assertEqual((final['status'], final['end_reason']), ('ended', 'bye'), final)
        stats = json.loads(self.api_get('/v1/calls')['body'])['registry']
        self.assertEqual((stats['superseded_session'], stats['apply_errors']), (1, 0), stats)

        # PostgreSQL: two rows; the old one ended by inference, the new one by its bye.
        query = ("SELECT end_reason || '|' || end_inferred || '|' || coalesce(speech_seconds::text, 'null') || '|' || "
                 "(SELECT count(*) FROM window_scores w WHERE w.call_pk = c.id) || '|' || "
                 "(SELECT count(*) FROM alerts a WHERE a.call_pk = c.id AND a.status = 'open') "
                 f"FROM calls c WHERE c.call_id = '{call_id}' AND c.ended_at IS NOT NULL ORDER BY c.id")
        rows, deadline = [], time.monotonic() + 60
        while time.monotonic() < deadline:
            rows = psql(query).splitlines()
            if len(rows) == 2:
                break
            time.sleep(1)
        self.assertEqual(len(rows), 2, rows)
        old, new = (row.split('|') for row in rows)
        self.assertEqual(old[:3], ['monitor_lost', 'true', 'null'], rows)
        self.assertEqual(new[:2], ['bye', 'false'], rows)
        self.assertNotEqual(new[2], 'null', rows)
        self.assertTrue(int(old[3]) >= 1 and int(new[3]) >= 1, f'windows stored in both sessions: {rows}')
        self.assertEqual((old[4], new[4]), ('0', '0'), f'no alert left open: {rows}')
        self.assertEqual(psql(f"SELECT count(*) FROM calls WHERE call_id = '{call_id}' AND ended_at IS NULL"), '0',
                         'no row of the call is left open')
        self.assertEqual(self.live_metrics().get('ava_live_store_superseded_total'), 1, 'the new process superseded it')

        # The killed process wrote no `stopped` record; live started twice.
        live_events = []
        for line in self.compose('logs', '--no-color', '--no-log-prefix', 'live').stdout.splitlines():
            try:
                live_events.append(json.loads(line).get('event'))
            except (ValueError, AttributeError):
                continue
        self.assertEqual(live_events.count('starting'), 2, live_events)
        self.assertNotIn('stopped', live_events, 'SIGKILL: no graceful stop')
        print(f'\nobserved: live back healthy {live_back_s:.1f} s after the kill with {remaining_s:.0f} s of the '
              f'call left; stored rows {rows}; gap reports {gaps}')

    def test_postgres_and_redis_restarts_mid_call_lose_no_stored_window(self):
        """5.QA backing-service restarts (Claude-authored, 18 Nov 2026; PRD "AVA failure
        must not stop a call", FR-I11, FR-A3/A4; architecture §2.5-§2.6). While a real call
        is scored, PostgreSQL and then Redis are restarted (Compose `restart`, SIGTERM). The
        call completes; the live process keeps the call (one `call.ended`, `bye`, no
        `monitor_lost`, no live restart); the store pump meets the broken database
        connection, retries, and stores every scored window (this call's scored rows equal
        the scorer's `windows_by_direction`, backlog 0, nothing refused or skipped); the API relay resubscribes after the Redis
        restart, reports the interruption to the client as a `stream.gap`, and the call's
        `bye` end reaches the API registry. Events published while Redis was down are lost
        to the API by design (pub/sub); the test asserts the loss is reported, not hidden."""
        self.compose('up', '-d', '--wait', '--wait-timeout', '360', timeout=600)
        self.wait_healthy('live', 120)
        self.wait_healthy('api', 240)
        self.wait_relay_subscribed(60)
        lines = self.start_subscriber(900)
        sipp, sipp_out, call_ms, call_started_at = self.start_long_call('svc')
        seen = []
        collect = self.collector(lines, seen)

        self.assertTrue(collect(lambda e: e['type'] == 'score.updated', 60), f'no score before the restarts: {seen}')
        call_id = seen[-1]['call_id']
        outage_started_at = time.monotonic()
        self.compose('restart', '-t', '10', 'postgres', timeout=120)
        self.wait_healthy('postgres', 60)
        self.compose('restart', '-t', '10', 'redis', timeout=120)
        self.wait_healthy('redis', 60)
        # Review L2: the subscribed gauge can still read 1 before the relay notices the
        # outage; wait for the interruption to be counted first, then for resubscription.
        deadline = time.monotonic() + 60
        while True:
            found = re.search(r'^ava_event_relay_interruptions_total (\S+)$', self.api_get('/metrics')['body'],
                              re.MULTILINE)
            self.assertIsNotNone(found, 'the API renders the relay interruption counter')
            if float(found.group(1)) >= 1:
                break
            self.assertLess(time.monotonic(), deadline, 'the API relay never noticed the Redis restart')
            time.sleep(1)
        self.wait_relay_subscribed(60)
        outage_s = time.monotonic() - outage_started_at
        remaining_s = call_ms / 1000 - (time.monotonic() - call_started_at)
        self.assertGreater(remaining_s, 10, f'the restarts took {outage_s:.1f} s: the call had almost ended, so '
                                            'the checks after them would show nothing')

        returncode = sipp.wait(timeout=call_ms / 1000 + 120)
        sipp_out.seek(0)
        self.assertEqual(returncode, 0, f'the call did not complete: {sipp_out.read()[-1500:]!r}')
        self.assertTrue(collect(lambda e: e['type'] == 'call.ended' and e['call_id'] == call_id, 60),
                        f'no end of the call after the restarts: {seen}')
        ours = [e for e in seen if e.get('call_id') == call_id]
        self.assertEqual([e.get('reason') for e in ours if e['type'] == 'call.ended'], ['bye'], f'events: {seen}')
        self.assertEqual(ours[0]['type'], 'call.started', f'events: {seen}')  # review L4
        self.assertNotIn('call.started', [e['type'] for e in ours[1:]], 'the call was never re-tapped')
        gaps = [e for e in seen if e['type'] == 'stream.gap']
        self.assertTrue(gaps and all(g['dropped'] == 0 for g in gaps), f'gap report: {gaps}')
        self.assertGreaterEqual(sum(g.get('upstream_interruptions', 0) for g in gaps), 1,
                                'the Redis outage is reported to the client')
        final = json.loads(self.api_get(f'/v1/calls/{call_id}')['body'])
        self.assertEqual((final['status'], final['end_reason']), ('ended', 'bye'), final)

        # The store: one row, every scored window stored once the database came back.
        metrics, rows, deadline = {}, [], time.monotonic() + 60
        while time.monotonic() < deadline:
            metrics, rows = self.live_metrics(), self.call_rows()
            if metrics.get('ava_live_store_backlog') == 0 and [r.split('|')[1] for r in rows] == ['bye']:
                break
            time.sleep(1)
        self.assertEqual([r.split('|')[:2] for r in rows], [[call_id, 'bye']], rows)
        self.assertEqual(metrics.get('ava_live_store_backlog'), 0, 'the store pump caught up')
        # Review M1: compare with the scorer's own per-call count (`windows_by_direction`
        # in the stored end), not the pipeline counter, which also counts results the
        # scorer ignores or quality-gates without a scored row.
        self.assertRegex(call_id, r'^[0-9A-Za-z._-]+$', 'safe to quote in SQL')
        stored_scored, scorer_windows = (int(value) for value in self.compose(
            'exec', '-T', 'postgres', 'psql', '-U', 'ava', '-d', 'ava', '-tAc',
            "SELECT (SELECT count(*) FROM window_scores w WHERE w.call_pk = c.id AND w.scored) || '|' || "
            "(SELECT coalesce(sum(value::int), 0) FROM jsonb_each_text(c.end_data->'windows_by_direction')) "
            f"FROM calls c WHERE c.call_id = '{call_id}'").stdout.strip().split('|'))
        live_scored = metrics['ava_live_windows_resolved_total{outcome="scored"}']
        self.assertGreaterEqual(scorer_windows, 10, 'the call was scored throughout')
        self.assertEqual(stored_scored, scorer_windows, 'every scored window is in PostgreSQL')
        for outcome in ('not_stored', 'refused', 'skipped_faulty'):
            self.assertEqual(metrics[f'ava_live_store_events_total{{outcome="{outcome}"}}'], 0, outcome)
        for kind in ('faults', 'store_misconfigured'):  # review L3
            self.assertEqual(metrics[f'ava_live_store_waits_total{{kind="{kind}"}}'], 0, kind)
        # Review L1: at least one write failed on the connection the restart broke (even
        # with no event during the outage, so this proves the connection loss was met,
        # not a wait through the outage), and the pump recovered.
        self.assertGreaterEqual(metrics['ava_live_store_waits_total{kind="retry_waits"}'], 1,
                                'the pump met the broken database connection (else the restart tested nothing)')
        self.assertEqual(metrics['ava_live_store_consecutive_failures'], 0, 'the pump recovered')
        # Redis-side proof of the outage is the client's `stream.gap` above; the forwarder's
        # failure count is only reported (redis-py may retry a publish across the restart).
        publish_failures = metrics['ava_live_redis_publish_failures_total']
        pump_faults = {key: value for key, value in metrics.items() if key.startswith('ava_live_pump_faults_total')}
        self.assertTrue(pump_faults and not any(pump_faults.values()), f'unexpected pump exceptions: {pump_faults}')

        # The live process itself never restarted and never lost the call.
        live_logs = self.compose('logs', '--no-color', '--no-log-prefix', 'live').stdout
        records = []
        for line in live_logs.splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        self.assertEqual([r.get('event') for r in records].count('starting'), 1, 'live started once')
        self.assertNotIn('monitor_lost', live_logs)
        retry_waits = metrics['ava_live_store_waits_total{kind="retry_waits"}']
        print(f'\nobserved: restarts took {outage_s:.1f} s with {remaining_s:.0f} s of the call left; '
              f'{stored_scored} scored windows stored = scorer count {scorer_windows} (live counter '
              f'{live_scored:.0f}); store retry waits {retry_waits:.0f}; '
              f'Redis publish failures {publish_failures:.0f}; gap reports {gaps}')

    def test_an_api_restart_mid_call_never_stops_the_call(self):
        """5.QA API restart (Claude-authored, 18 Nov 2026; PRD "AVA failure must not stop a
        call", FR-A3/A4; architecture §2.6). The api container is restarted (SIGTERM) while
        a real call is scored. The call completes; live never notices (one `bye`, no
        re-tap, started once, every scored window stored); the new API process resubscribes
        to Redis, a new WebSocket client receives the rest of the call, and the in-memory
        registry, which lost the call with the old process, creates it from the next event
        (implicit start) and ends it `bye`."""
        self.compose('up', '-d', '--wait', '--wait-timeout', '360', timeout=600)
        self.wait_healthy('live', 120)
        self.wait_healthy('api', 240)
        self.wait_relay_subscribed(60)
        before = self.start_subscriber(900)
        sipp, sipp_out, call_ms, call_started_at = self.start_long_call('api')
        seen = []
        self.assertTrue(self.collector(before, seen)(lambda e: e['type'] == 'score.updated', 60),
                        f'no score before the restart: {seen}')
        call_id = seen[-1]['call_id']
        restarted_at = time.monotonic()
        self.compose('restart', '-t', '20', 'api', timeout=120)
        # Not `api_get`: exec fails while the process starts. The relay starts with the app,
        # before the model warm-up that the health check waits for.
        deadline = time.monotonic() + 120
        while True:
            done = self.compose('exec', '-T', 'api', 'python', '-c', HTTP_GET, '/metrics', check=False)
            try:
                body = json.loads(done.stdout)['body'] if done.returncode == 0 else ''
            except ValueError:
                body = ''
            if 'ava_event_relay_subscribed 1' in body.splitlines():
                break
            self.assertLess(time.monotonic(), deadline, f'the new API did not subscribe: {done.stderr[-1500:]}')
            time.sleep(1)
        api_back_s = time.monotonic() - restarted_at
        after = self.start_subscriber(900)
        remaining_s = call_ms / 1000 - (time.monotonic() - call_started_at)
        self.assertGreater(remaining_s, 10, f'the API took {api_back_s:.1f} s to come back: the call had almost '
                                            'ended, so the checks after it would show nothing')

        returncode = sipp.wait(timeout=call_ms / 1000 + 120)
        sipp_out.seek(0)
        self.assertEqual(returncode, 0, f'the call did not complete: {sipp_out.read()[-1500:]!r}')
        later = []
        self.assertTrue(self.collector(after, later)(lambda e: e['type'] == 'call.ended' and e['call_id'] == call_id,
                                                     60), f'the new client never saw the end: {later}')
        ours = [e for e in later if e.get('call_id') == call_id]
        self.assertIn('score.updated', [e['type'] for e in ours], 'the new client got the call\'s scores')
        # Review nit: weak on its own (the client joined after the restart); the single
        # `bye` row and live's log below are the real no-re-tap evidence.
        self.assertNotIn('call.started', [e['type'] for e in ours], 'no new monitoring session after the restart')
        self.assertEqual(ours[-1].get('reason'), 'bye')
        record = json.loads(self.api_get(f'/v1/calls/{call_id}')['body'])
        self.assertEqual((record['status'], record['end_reason']), ('ended', 'bye'), record)
        # Review L1: the new process's registry (counters from zero) created the call from
        # a mid-call event, applied its events, and refused none as late or failed.
        stats = json.loads(self.api_get('/v1/calls')['body'])['registry']
        self.assertEqual((stats['implicit_start'], stats['active'], stats['ended']), (1, 0, 1), stats)
        metrics_text = self.api_get('/metrics')['body']
        registry = {name: float(value) for name, value in
                    re.findall(r'^ava_call_registry_events_total\{outcome="(\w+)"\} (\S+)$', metrics_text, re.MULTILINE)}
        self.assertGreaterEqual(registry.get('applied', 0), 2, registry)
        self.assertEqual((registry.get('after_end'), registry.get('apply_errors')), (0, 0), registry)

        rows, deadline = [], time.monotonic() + 60
        while time.monotonic() < deadline:
            rows = self.call_rows()
            if [r.split('|')[1] for r in rows] == ['bye']:
                break
            time.sleep(1)
        self.assertEqual([r.split('|')[:2] for r in rows], [[call_id, 'bye']], rows)
        self.assertRegex(call_id, r'^[0-9A-Za-z._-]+$', 'safe to quote in SQL')
        stored_scored, scorer_windows = (int(value) for value in self.compose(
            'exec', '-T', 'postgres', 'psql', '-U', 'ava', '-d', 'ava', '-tAc',
            "SELECT (SELECT count(*) FROM window_scores w WHERE w.call_pk = c.id AND w.scored) || '|' || "
            "(SELECT coalesce(sum(value::int), 0) FROM jsonb_each_text(c.end_data->'windows_by_direction')) "
            f"FROM calls c WHERE c.call_id = '{call_id}'").stdout.strip().split('|'))
        self.assertGreaterEqual(scorer_windows, 10, 'the call was scored throughout')
        self.assertEqual(stored_scored, scorer_windows, 'every scored window is in PostgreSQL')
        live_logs = self.compose('logs', '--no-color', '--no-log-prefix', 'live').stdout
        events = []
        for line in live_logs.splitlines():
            try:
                events.append(json.loads(line).get('event'))
            except (ValueError, AttributeError):
                continue
        self.assertEqual(events.count('starting'), 1, 'live started once')
        self.assertNotIn('monitor_lost', live_logs)
        print(f'\nobserved: the new API subscribed {api_back_s:.1f} s after the restart began with '
              f'{remaining_s:.0f} s of the call left; the new client got {len(ours)} events of the call '
              '(not every event after the restart: it connects after the relay subscribes); '
              f'{stored_scored} scored windows stored = scorer count {scorer_windows}')

    def check_monitoring(self, windows, ws_events):
        """Profile `monitoring` (18 Nov, FR-O6): Prometheus holds the call, every dashboard
        query evaluates, Grafana serves the dashboard and reads through its data source,
        and the scrape network stays closed. `ws_events`: events the WebSocket subscriber
        received; each was timed by the API (`ava_ws_delivery_seconds`, 18 Nov)."""
        # The scored windows reach Prometheus at the next scrape (15 s interval).
        scored, delivered, deadline = None, None, time.monotonic() + 60
        while time.monotonic() < deadline:
            scored = self.prom_value('ava_live_windows_resolved_total{outcome="scored"}')
            delivered = self.prom_value('ava_ws_delivery_seconds_count')
            if scored is not None and scored >= windows and delivered is not None and delivered >= ws_events:
                break
            time.sleep(3)
        self.assertGreaterEqual(scored or 0, windows, 'Prometheus holds the scored windows of the call')
        self.assertGreaterEqual(delivered or 0, ws_events, 'every event the subscriber got was timed')
        # One host, one kernel clock: the scorer's ts is never ahead of the API's clock.
        self.assertEqual(self.prom_value('ava_ws_delivery_unmeasured_total'), 0)
        self.assertEqual(self.prom_value('ava_ws_slow_client_closes_total'), 0)
        self.assertGreaterEqual(ws_events, 1, 'the subscriber received events to time')
        ws_sum = self.prom_value('ava_ws_delivery_seconds_sum')
        self.assertIsNotNone(ws_sum, 'the delivery histogram has a sum')
        ws_mean = ws_sum / delivered
        self.assertGreaterEqual(self.prom_value('ava_live_calls_capacity') or 0, 1)
        self.assertGreaterEqual(self.prom_value('ava_live_rtp_receiver_total{kind="packets"}') or 0, 100)
        self.assertIsNotNone(self.prom_value('ava_api_requests_total'), 'the API is scraped too')

        # Every query on the dashboard is valid PromQL against the live data, and the
        # stage-latency panel has data after a call.
        dashboard = json.loads(DASHBOARD.read_text(encoding='utf-8'))
        exprs = [target['expr'] for panel in dashboard['panels'] for target in panel.get('targets', [])]
        for expr in exprs:
            result = self.prom_get('/api/v1/query?query=' + quote(expr, safe=''))
            self.assertEqual(result['status'], 'success', (expr, result))
        # Review L6: after a call and a file analysis, every query has data.
        empty = [expr for expr in exprs if self.prom_value(expr) is None]
        self.assertEqual(empty, [], 'dashboard queries without data after a call')
        with_data = len(exprs) - len(empty)
        self.assertIsNotNone(self.prom_value(
            'histogram_quantile(0.95, sum by (le, stage) (rate(ava_live_stage_seconds_bucket[5m])))'))

        # Grafana: health, the provisioned dashboard, and a query through the data source.
        status, health = self.grafana('/api/health')
        self.assertEqual((status, health.get('database')), (200, 'ok'), health)
        status, found = self.grafana('/api/search?query=AVA')
        self.assertEqual(status, 200, found)
        self.assertIn('ava-overview', [item.get('uid') for item in found])
        status, board = self.grafana('/api/dashboards/uid/ava-overview')
        self.assertEqual(status, 200, board)
        self.assertEqual(len(board['dashboard']['panels']), len(dashboard['panels']))
        done = self.compose('exec', '-T', 'grafana', 'curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
                            'http://127.0.0.1:3000/api/dashboards/uid/ava-overview')
        self.assertEqual(done.stdout.strip(), '401', 'no anonymous access')
        status, answer = self.grafana('/api/ds/query', {
            'from': 'now-5m', 'to': 'now',
            'queries': [{'refId': 'A', 'datasource': {'type': 'prometheus', 'uid': 'ava-prometheus'},
                         'expr': 'ava_live_calls_capacity', 'instant': True}]})
        self.assertEqual(status, 200, answer)
        frames = answer['results']['A']['frames']
        self.assertTrue(frames and frames[0]['data']['values'][-1], f'Grafana read no live series: {answer}')

        # Isolation: live's health endpoint is closed to `backend` (Asterisk tries both
        # live's backend name and its metrics address); Prometheus and Grafana cannot
        # reach `backend` or the internet.
        # Each probe has a positive control, so a missing tool cannot pass as "blocked".
        def tcp_from_asterisk(target):
            return self.compose('exec', '-T', 'asterisk', 'timeout', '5', 'bash', '-c',
                                f'exec 3<>/dev/tcp/{target}', check=False).returncode
        self.assertEqual(tcp_from_asterisk('postgres/5432'), 0, 'control: asterisk reaches backend')
        for target in ('live/9100', f'{self.live_metrics_ip}/9100'):
            self.assertNotEqual(tcp_from_asterisk(target), 0, f'asterisk reached live health at {target}')
        reach = self.compose('exec', '-T', 'api', 'python', '-c', 'import urllib.request; urllib.request.urlopen('
                             f'"http://{self.live_metrics_ip}:9100/healthz", timeout=5)', check=False)
        self.assertEqual(reach.returncode, 0, f'control: the metrics network reaches live health {reach.stderr}')
        # Review L6: probe `backend` by address, not by a name that only fails to resolve.
        postgres_ip = self.compose('exec', '-T', 'postgres', 'hostname', '-i').stdout.split()[0]
        for service, control in (('prometheus', 'http://api:8000/healthz'),
                                 ('grafana', 'http://prometheus:9090/-/ready')):
            def wget(url):
                return self.compose('exec', '-T', service, 'wget', '-q', '-T', '3', '-O', '/dev/null', url,
                                    check=False).returncode
            self.assertEqual(wget(control), 0, f'control: {service} reaches {control}')
            for url in ('http://1.1.1.1/', 'http://postgres:5432/'):
                self.assertNotEqual(wget(url), 0, f'{service} reached {url}')
        # By address: curl's connect time is 0 unless a TCP connection opened (a wget exit
        # code cannot tell "blocked" from "PostgreSQL does not speak HTTP").
        def connect_time(target):
            done = self.compose('exec', '-T', 'grafana', 'curl', '-s', '-m', '3', '-o', '/dev/null', '-w',
                                '%{time_connect}', f'telnet://{target}', check=False)
            return float(done.stdout.strip() or 0)
        self.assertGreater(connect_time('prometheus:9090'), 0.0, 'control: curl reports a connection')
        opened = self.compose('exec', '-T', 'api', 'python', '-c', 'import socket; socket.create_connection('
                              f'("{postgres_ip}", 5432), timeout=3).close()', check=False)
        self.assertEqual(opened.returncode, 0, f'control: {postgres_ip}:5432 is a live backend address')
        self.assertEqual(connect_time(f'{postgres_ip}:5432'), 0.0, 'grafana opened a connection into backend')
        # The real guard against calling home is the network (no route, no outside DNS);
        # the log check only shows that Grafana did not try and fail noisily.
        grafana_logs = self.compose('logs', '--no-color', '--no-log-prefix', 'grafana').stdout
        self.assertNotIn('grafana.com', grafana_logs, 'Grafana tried to call home')
        self.assertNotIn('stats.grafana.org', grafana_logs)
        return (f'{scored:.0f} scored windows in Prometheus; {with_data}/{len(exprs)} dashboard queries with data; '
                f'{delivered:.0f} WebSocket deliveries timed, mean {ws_mean * 1000:.0f} ms (publish to sent)')


if __name__ == '__main__':
    unittest.main()
