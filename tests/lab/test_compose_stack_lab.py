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
        # Re-review: a run-specific scrape subnet, so another AVA stack on this host (with
        # the 10.231.250.0/24 default) does not collide with the test's.
        octet = 1 + secrets.randbelow(254)
        self.live_metrics_ip = f'10.232.{octet}.10'
        self.env_file = folder / 'stack.env'
        self.env_file.write_text('\n'.join([
            f'AVA_ARI_PASSWORD={self.ari_password}', f'AVA_DB_PASSWORD={self.db_password}',
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
        self.assertEqual((starting['ari_reachable'], starting['redis_reachable'], starting['database_migrations_applied']),
                         (True, True, [1]))
        api_logs = self.compose('logs', '--no-color', '--no-log-prefix', 'api').stdout
        api_start = next(json.loads(line) for line in api_logs.splitlines() if '"starting"' in line)
        self.assertEqual((api_start['live_events'], api_start['redis_reachable'], api_start['dashboard']),
                         (True, True, True))
        monitoring = self.check_monitoring(int(windows), len(seen))
        all_logs = self.compose('logs', '--no-color').stdout
        for secret in (self.ari_password, self.db_password, self.grafana_password):
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
              f'(uncalibrated baseline); API warm-up {json.loads(ready["body"])["warmup_seconds"]:.1f} s; '
              f'monitoring: {monitoring}')

    def call_rows(self):
        """`call_id|end_reason|windows` per calls row, oldest first."""
        return self.compose('exec', '-T', 'postgres', 'psql', '-U', 'ava', '-d', 'ava', '-tAc',
                            "SELECT call_id || '|' || coalesce(end_reason, '') || '|' || "
                            '(SELECT count(*) FROM window_scores w WHERE w.call_pk = c.id) FROM calls c ORDER BY c.id',
                            check=False).stdout.strip().splitlines()

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

        # About 55 s of speech: the lab dialplan's Record() hangs up at 60 s, so the call
        # cannot be longer. Review L1: the remaining-time check after the restart below
        # tells a slow restart apart from a re-tap defect.
        clips = json.loads(QA_CONFIG.read_text(encoding='utf-8'))['genuine'][:3] * 4
        payloads = rtp_pcap.rtp_payloads(b''.join(rtp_pcap.wav_to_pcm8k(path) for path in clips), 'ulaw')
        pcap = self.folder / 'inject.pcap'
        rtp_pcap.write_pcap(payloads, 0, pcap)
        self.compose('cp', str(pcap), 'asterisk:/lab/io/inject.pcap')
        caller = f'rst{secrets.token_hex(4)}'
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
        call_started_at = time.monotonic()

        def stop_sipp():  # on failure: the docker exec client goes; the stack's `down` ends SIPp
            if sipp.poll() is None:
                sipp.kill()
            sipp.wait(timeout=30)
        self.addCleanup(stop_sipp)

        seen = []

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
