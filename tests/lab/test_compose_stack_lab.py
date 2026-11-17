"""Claude-authored lab test (17 November 2026): the Compose deployment of the live stack
(backlog 10.2; PRD FR-O4 model from a read-only local volume, FR-O7 pinned images,
FR-O8; architecture §2.4, §2.7; PRD constraint: no telemetry or internet dependency).

Authored by Claude (Claude Code), not by GPT-6 Astra. Opt-in (AVA_TEST_DOCKER_LAB=1);
needs the locally built images `ava-live:dev` and `ava-lab-asterisk:dev`, the public QA
clips, the Silero model and an exported detector (AVA_LIVE_ONNX; the 14 Nov static-INT8
export with an UNTRAINED head: scores carry no meaning). It brings up a disposable
Compose project (random project name and passwords, env file in a temporary folder),
places a real call into the lab PBX inside the stack, and checks: the live service
becomes ready through its container health check; the call is stored in PostgreSQL
with `bye` and its windows; the JSON event log shows it; the live container cannot
reach the internet; no password appears in the logs. The project and its volumes are
removed afterwards.
"""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import unittest

from lab.asterisk import rtp_pcap
from models.export import model_card as mc
from tests.lab.test_live_service_lab import ONNX, QA_CONFIG, VAD_MODEL

LAB = unittest.skipIf(os.environ.get('AVA_TEST_DOCKER_LAB') != '1', 'set AVA_TEST_DOCKER_LAB=1')
COMPOSE = Path(__file__).resolve().parents[2] / 'deploy' / 'compose' / 'compose.yaml'


@LAB
@unittest.skipUnless(QA_CONFIG.is_file() and VAD_MODEL.is_file() and ONNX.is_file(),
                     'needs the local QA clips, the Silero model and an exported detector')
class ComposeStackLabTests(unittest.TestCase):
    def compose(self, *args, check=True, timeout=300):
        done = subprocess.run([shutil.which('docker'), 'compose', '--env-file', str(self.env_file), '-f', str(COMPOSE),
                               '-p', self.project, '--profile', 'lab', *args],
                              capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
        if check and done.returncode != 0:
            self.fail(f'docker compose {args[0]} failed: {done.stdout[-2000:]} {done.stderr[-2000:]}')
        return done

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
            f'AVA_CARD_DIR={card_dir.as_posix()}', f'AVA_VAD_DIR={VAD_MODEL.parent.as_posix()}']) + '\n')
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

    def test_the_stack_scores_a_real_call_without_internet(self):
        self.compose('up', '-d', '--wait', '--wait-timeout', '180', timeout=400)
        self.wait_healthy('live', 120)

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
        all_logs = self.compose('logs', '--no-color').stdout
        for secret in (self.ari_password, self.db_password):
            self.assertNotIn(secret, all_logs, 'no password in any container log')
        # The lab PBX's own console logs caller IDs (Asterisk's business); AVA's must not.
        self.assertNotIn(caller, logs, 'the caller number is not in the live service log')
        self.assertNotIn('telemetry', logs.lower(), 'no ONNX Runtime telemetry client started (1DS message)')

        # No egress: the internal network has no route out (no telemetry, no downloads).
        probe = self.compose('exec', '-T', 'live', 'python', '-c',
                             'import socket,sys\n'
                             'try:\n socket.create_connection(("1.1.1.1", 443), timeout=3)\n'
                             'except OSError as e:\n print("blocked", type(e).__name__); sys.exit(0)\n'
                             'sys.exit(1)', check=False)
        self.assertEqual(probe.returncode, 0, f'the live container reached the internet: {probe.stdout}')
        user = self.compose('exec', '-T', 'live', 'id', '-u').stdout.strip()
        self.assertEqual(user, '10001', 'the service does not run as root')
        print(f'\nobserved: call {call_id} stored with {windows} windows; probe: {probe.stdout.strip()}')


if __name__ == '__main__':
    unittest.main()
