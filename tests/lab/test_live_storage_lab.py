"""Claude-authored lab test (16 November 2026): a real Asterisk call served by the live
command line with event storage and Redis publishing on (backlog 5.1/5.2/5.QA/8.1; PRD
FR-I11 "scores and state are flushed to PostgreSQL every hop", FR-A3, FR-A4;
architecture §2.4).

Authored by Claude (Claude Code), not by GPT-6 Astra. Opt-in: AVA_TEST_DOCKER_LAB=1,
AVA_TEST_POSTGRES_DSN (a disposable database; the test works in its own schema) and
AVA_TEST_REDIS_URL, plus the local files of test_live_service_lab (public CSALT clips,
Silero, an exported detector with an UNTRAINED head: scores carry no meaning). The
same call's events are compared three ways: the in-process hub, the PostgreSQL rows
written by the pump thread, and the messages on the Redis channel.
"""
import io
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest
import uuid

from lab.asterisk import rtp_pcap
from lab.asterisk.harness import LabContainer
from models.export import model_card as mc
from tests.lab.test_asterisk_lab import IMAGE
from tests.lab.test_live_service_lab import LAB, ONNX, QA_CONFIG, VAD_MODEL

DSN = os.environ.get('AVA_TEST_POSTGRES_DSN')
REDIS_URL = os.environ.get('AVA_TEST_REDIS_URL')


@LAB
@unittest.skipUnless(QA_CONFIG.is_file() and VAD_MODEL.is_file() and ONNX.is_file() and DSN and REDIS_URL,
                     'needs the local QA clips, Silero, an exported detector, PostgreSQL and Redis')
class LiveStorageLabTests(unittest.TestCase):
    def test_a_real_call_is_stored_and_published(self):
        import psycopg
        from psycopg.conninfo import make_conninfo
        import redis

        from services.events.redis_bridge import CHANNEL
        from services.live import __main__ as cli
        schema = f'ava_lab_{uuid.uuid4().hex[:12]}'
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'CREATE SCHEMA {schema}')

        def drop():
            with psycopg.connect(DSN, autocommit=True) as admin:
                admin.execute(f'DROP SCHEMA {schema} CASCADE')
        self.addCleanup(drop)
        dsn = make_conninfo(DSN, options=f'-c search_path={schema}')
        channel_listener = redis.Redis.from_url(REDIS_URL, socket_timeout=5).pubsub()
        channel_listener.subscribe(CHANNEL)
        self.addCleanup(channel_listener.close)
        channel_listener.get_message(timeout=2)

        password = secrets.token_hex(16)
        lab = LabContainer(IMAGE, network='bridge', publish=['127.0.0.1::8088'], env={'AVA_ARI_PASSWORD': password})
        self.addCleanup(lab.stop)
        lab.start()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        card = mc.write_card(mc.build_card(
            ONNX, version='lab-storage-untrained', backbone='AntiDeepfake MMS-300M, truncated', kept_layers=8,
            metrics={'dev_eer': mc.NOT_MEASURED}, training_data={'status': 'untrained SLS head (seeded)'},
            failure_modes=['untrained head: scores carry no meaning'],
            licence_restriction='Research and non-commercial use only.'), Path(temp.name) / 'model_card.json')
        made, listeners = cli._defaults(), []

        def service_factory(*args, **kwargs):
            service = made['service'](*args, **kwargs)
            listeners.append((service, service.hub.subscribe(buffer=4096)))
            return service
        factories = {**made, 'service': service_factory}
        stop, err, result = threading.Event(), io.StringIO(), []
        import socket
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            health_port = probe.getsockname()[1]
        argv = ['--card', str(card), '--onnx', str(ONNX), '--vad-model', str(VAD_MODEL), '--threads', '4',
                '--ari-url', f'http://{lab.port("8088/tcp")}', '--ari-user', 'ava',
                '--receiver-host', 'host.docker.internal', '--tap-extension', '700',
                '--database-url-env', 'AVA_LAB_DB', '--redis-url-env', 'AVA_LAB_REDIS',
                '--health-port', str(health_port)]
        environ = {'AVA_ARI_PASSWORD': password, 'AVA_LAB_DB': dsn, 'AVA_LAB_REDIS': REDIS_URL}
        runner = threading.Thread(target=lambda: result.append(cli.main(
            argv, environ=environ, factories=factories, stop_event=stop, stderr=err)), daemon=True)
        runner.start()
        self.addCleanup(runner.join, 20)
        self.addCleanup(stop.set)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (listeners and listeners[0][0].ready.is_set()):
            time.sleep(0.1)
        self.assertTrue(listeners and listeners[0][0].ready.is_set(), err.getvalue())
        service, listener = listeners[0]

        clips = json.loads(QA_CONFIG.read_text(encoding='utf-8'))['genuine'][:3]
        payloads = rtp_pcap.rtp_payloads(b''.join(rtp_pcap.wav_to_pcm8k(path) for path in clips), 'ulaw')
        rtp_pcap.write_pcap(payloads, 0, lab.path('inject.pcap'))
        # SIPp runs on a thread so PostgreSQL can be read while the call is still up
        # (lab.command blocks until SIPp hangs up; re-review R1).
        call_result = []
        caller = threading.Thread(target=lambda: call_result.append(lab.command(
            ['exec', lab.name, 'sipp', '127.0.0.1:5060', '-sf', '/lab/sipp/uac_inject.xml',
             '-s', '700', '-key', 'caller', f'db{secrets.token_hex(4)}', '-key', 'pt', '0',
             '-key', 'codec', 'PCMU', '-m', '1', '-d', str(len(payloads) * 20 + 1500),
             '-i', '127.0.0.1', '-mi', '127.0.0.1', '-min_rtp_port', '16000', '-max_rtp_port', '16100',
             '-timeout', '60s', '-timeout_error', '-nostdin'], check=False, timeout=180)), daemon=True)
        caller.start()
        # FR-I11 per-hop flush: rows counted while SIPp still holds the call. FR-A8: the
        # health endpoint answers ready and counts the active call meanwhile.
        from urllib import request
        rows_during_call, ready_codes, active_seen, deadline = 0, set(), 0, time.monotonic() + 120
        while caller.is_alive() and time.monotonic() < deadline:
            with psycopg.connect(dsn) as reader:
                count = reader.execute('SELECT count(*) FROM window_scores').fetchone()[0]
            with request.urlopen(f'http://127.0.0.1:{health_port}/readyz', timeout=5) as response:
                code = response.status
            with request.urlopen(f'http://127.0.0.1:{health_port}/metrics', timeout=5) as response:
                text = response.read().decode()
            active = [float(line.split()[-1]) for line in text.splitlines()
                      if line.startswith('ava_live_calls_active ')]
            if caller.is_alive():  # only readings taken before the call ended
                rows_during_call = max(rows_during_call, count)
                ready_codes.add(code)
                active_seen = max([active_seen, *active])
            time.sleep(0.2)
        caller.join(60)
        self.assertEqual([r.returncode for r in call_result], [0])
        self.assertGreater(rows_during_call, 0, 'no window stored while the call was running')
        self.assertEqual((ready_codes, active_seen), ({200}, 1))
        events, deadline = [], time.monotonic() + 60
        while time.monotonic() < deadline and not any(e.type == 'call.ended' for e in events):
            events += listener.drain()[0]
            time.sleep(0.2)
        # Every window is in PostgreSQL before the service stops.
        # Every direction window (scored or quality-gated) is one window_scores row.
        windows_published = [e for e in events if e.type == 'score.updated' and e.data.get('direction')]
        scored = [e for e in windows_published if e.data.get('scored')]
        self.assertGreaterEqual(len(scored), 3)
        call_id = scored[0].call_id
        flushed, deadline = 0, time.monotonic() + 10
        while time.monotonic() < deadline and flushed < len(windows_published):
            with psycopg.connect(dsn) as reader:
                flushed = reader.execute('SELECT count(*) FROM window_scores').fetchone()[0]
            time.sleep(0.2)
        self.assertEqual(flushed, len(windows_published), 'the pump thread stores while the service runs')
        service.stop(stop)
        runner.join(30)
        self.assertFalse(runner.is_alive())
        self.assertEqual(result, [cli.EXIT_OK], err.getvalue())
        records = [json.loads(line) for line in err.getvalue().splitlines()]
        self.assertEqual((records[0]['database_migrations_applied'], records[0]['redis_reachable']), ([1], True))
        self.assertNotIn(password, err.getvalue())
        self.assertNotIn(dsn, err.getvalue())

        with psycopg.connect(dsn) as reader:
            calls = reader.execute('SELECT call_id, end_reason, implicit_start FROM calls').fetchall()
            windows = reader.execute('SELECT direction, window_idx, scored, score FROM window_scores WHERE call_pk = '
                                     '(SELECT id FROM calls WHERE call_id = %s) ORDER BY window_idx',
                                     (call_id,)).fetchall()
        self.assertEqual(calls, [(call_id, 'bye', False)])
        self.assertEqual([(w[0], w[1], w[2]) for w in windows],
                         [(e.data['direction'], e.data['window'], e.data['scored']) for e in windows_published])
        for row, event in zip(windows, windows_published):
            if event.data['scored']:
                self.assertAlmostEqual(row[3], event.data['score'], places=6)
        store = records[1]['store']
        self.assertEqual((store['stuck'], store['drained'], store['faults']), (False, True, 0))
        self.assertEqual(store['counts']['retry_waits'] + store['counts']['refused'], 0)

        published = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            message = channel_listener.get_message(timeout=0.5)
            if message and message['type'] == 'message':
                published.append(json.loads(message['data']))
            elif published and message is None:
                break
        self.assertEqual([(m['type'], m['seq']) for m in published if m['call_id'] == call_id],
                         [(e.type, e.seq) for e in events + listener.drain()[0] if e.call_id == call_id])
        self.assertEqual(records[1]['redis']['counts']['publish_failures'], 0)


if __name__ == '__main__':
    unittest.main()
