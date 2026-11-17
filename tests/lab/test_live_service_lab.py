"""Claude-authored test (15 November 2026): the live service with the REAL exported ONNX
graph against a real lab call (backlog 8.1/6.QA; PRD FR-O4, FR-M7 serving path, FR-T2,
FR-I1-FR-I11, FR-A4).

Authored by Claude (Claude Code), not by GPT-6 Astra. Opt-in (AVA_TEST_DOCKER_LAB=1) and
needs local files only: `.local/real-model-qa.json` (public CSALT clips), the Silero
model, and an exported detector (AVA_LIVE_ONNX, default the 14 Nov static-INT8 8-layer
export under D:/AVA-Data/exports). The test writes a model card for that file into a
temporary folder (untrained SLS head: every metric `not_measured`), starts `LiveService`
through `load_detector` (card, hash, config and warm-up gate), plays three genuine clips
into extension 700 and checks the published events. The head is UNTRAINED: the scores
are real ONNX computations on real speech but carry no meaning; no accuracy claim.
"""
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest

import numpy as np

from lab.asterisk import rtp_pcap
from lab.asterisk.harness import LabContainer, LabError
from models.export import model_card as mc
from services.live.service import LiveService, load_detector
from services.tap.ari import AriClient
from tests.lab.test_asterisk_lab import IMAGE

LAB = unittest.skipIf(os.environ.get('AVA_TEST_DOCKER_LAB') != '1', 'set AVA_TEST_DOCKER_LAB=1')
QA_CONFIG = Path('.local/real-model-qa.json')
VAD_MODEL = Path(os.environ.get('AVA_SILERO_VAD_MODEL', 'D:/AVA-Data/models/silero-vad/6.2.3/silero_vad.onnx'))
ONNX = Path(os.environ.get('AVA_LIVE_ONNX', 'D:/AVA-Data/exports/2026-11-14-ava-l8-static/ava-l8-int8-static.onnx'))


@LAB
@unittest.skipUnless(QA_CONFIG.is_file() and VAD_MODEL.is_file() and ONNX.is_file(),
                     'needs the local QA clips, the Silero model and an exported detector')
class LiveServiceLabTests(unittest.TestCase):
    def test_real_onnx_graph_scores_a_real_call_end_to_end(self):
        from services.inference.silero_vad import SileroVad
        password = secrets.token_hex(16)
        lab = LabContainer(IMAGE, network='bridge', publish=['127.0.0.1::8088'], env={'AVA_ARI_PASSWORD': password})
        self.addCleanup(lab.stop)
        lab.start()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        card = mc.write_card(mc.build_card(
            ONNX, version='lab-l8-int8-static-untrained', backbone='AntiDeepfake MMS-300M, truncated',
            kept_layers=8, metrics={'dev_eer': mc.NOT_MEASURED},
            training_data={'status': 'untrained SLS head (seeded); no AVA training data'},
            failure_modes=['untrained head: scores carry no meaning'],
            licence_restriction='Research and non-commercial use only.'), Path(temp.name) / 'model_card.json')
        started = time.monotonic()
        detector, card_data, config = load_detector(card, ONNX, {}, threads=4, per_window=False)
        load_seconds = time.monotonic() - started

        ari = AriClient(f'http://{lab.port("8088/tcp")}', 'ava', password, 'ava-tap')
        service = LiveService(ari, detector, config, lambda: SileroVad(VAD_MODEL), receiver_host='host.docker.internal',
                              tap_extensions={'700'})
        listener = service.hub.subscribe(buffer=4096)
        stop = threading.Event()
        runner = threading.Thread(target=service.run, args=(stop,), daemon=True)
        runner.start()
        self.addCleanup(runner.join, 20)
        self.addCleanup(service.stop, stop)
        self.assertTrue(service.ready.wait(15), 'service did not connect')

        clips = json.loads(QA_CONFIG.read_text(encoding='utf-8'))['genuine'][:3]
        pcm8k = b''.join(rtp_pcap.wav_to_pcm8k(path) for path in clips)
        payloads = rtp_pcap.rtp_payloads(pcm8k, 'ulaw')
        rtp_pcap.write_pcap(payloads, 0, lab.path('inject.pcap'))
        token = f'svc{secrets.token_hex(4)}'
        done = lab.command(['exec', lab.name, 'sipp', '127.0.0.1:5060', '-sf', '/lab/sipp/uac_inject.xml',
                            '-s', '700', '-key', 'caller', token, '-key', 'pt', '0', '-key', 'codec', 'PCMU',
                            '-m', '1', '-d', str(len(payloads) * 20 + 1500), '-i', '127.0.0.1', '-mi', '127.0.0.1',
                            '-min_rtp_port', '16000', '-max_rtp_port', '16100', '-timeout', '60s', '-timeout_error',
                            '-nostdin'], check=False, timeout=180)
        self.assertEqual(done.returncode, 0, 'the call must complete')
        events, deadline = [], time.monotonic() + 60
        while time.monotonic() < deadline and not any(e.type == 'call.ended' for e in events):
            batch, dropped = listener.drain()
            self.assertEqual(dropped, 0)
            events += batch
            time.sleep(0.2)
        service.stop(stop)
        runner.join(20)
        self.assertFalse(runner.is_alive(), 'the service loop must stop')
        events += listener.drain()[0]  # whatever shutdown published: no second end

        scored = [e.data for e in events if e.type == 'score.updated' and e.data.get('scored')]
        self.assertGreaterEqual(len(scored), 3, [e.type for e in events])
        self.assertTrue(all(0.0 <= s['score'] <= 1.0 and np.isfinite(s['score']) for s in scored))
        ended = [e.data for e in events if e.type == 'call.ended']
        self.assertEqual([e['reason'] for e in ended], ['bye'])
        self.assertEqual(card_data['onnx']['file'], ONNX.name)
        self.assertEqual((service.counts['hook_errors'], service.tap.stats['sink_errors']), (0, 0))
        # Recorded for the run log (not asserted): model load + warm-up time, windows scored.
        self.observed = {'load_and_warm_up_s': round(load_seconds, 1), 'scored_windows': len(scored)}
        print(f'\nobserved: {self.observed}')

    def test_command_line_serves_a_real_call(self):
        """`python -m services.live` main() with the real factories (card, ONNX, Silero,
        ARI client); run on a worker thread (no signal handlers) and stopped by the event."""
        import io
        from services.live import __main__ as cli
        password = secrets.token_hex(16)
        lab = LabContainer(IMAGE, network='bridge', publish=['127.0.0.1::8088'], env={'AVA_ARI_PASSWORD': password})
        self.addCleanup(lab.stop)
        lab.start()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        card = mc.write_card(mc.build_card(
            ONNX, version='lab-cli-untrained', backbone='AntiDeepfake MMS-300M, truncated', kept_layers=8,
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
        argv = ['--card', str(card), '--onnx', str(ONNX), '--vad-model', str(VAD_MODEL), '--threads', '4',
                '--ari-url', f'http://{lab.port("8088/tcp")}', '--ari-user', 'ava',
                '--receiver-host', 'host.docker.internal', '--tap-extension', '700']
        runner = threading.Thread(target=lambda: result.append(cli.main(
            argv, environ={'AVA_ARI_PASSWORD': password}, factories=factories, stop_event=stop, stderr=err)),
            daemon=True)
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
        caller = f'cli{secrets.token_hex(4)}'  # the caller number Asterisk sees
        done = lab.command(['exec', lab.name, 'sipp', '127.0.0.1:5060', '-sf', '/lab/sipp/uac_inject.xml',
                            '-s', '700', '-key', 'caller', caller, '-key', 'pt', '0',
                            '-key', 'codec', 'PCMU', '-m', '1', '-d', str(len(payloads) * 20 + 1500),
                            '-i', '127.0.0.1', '-mi', '127.0.0.1', '-min_rtp_port', '16000', '-max_rtp_port', '16100',
                            '-timeout', '60s', '-timeout_error', '-nostdin'], check=False, timeout=180)
        self.assertEqual(done.returncode, 0)
        events, deadline = [], time.monotonic() + 60
        while time.monotonic() < deadline and not any(e.type == 'call.ended' for e in events):
            events += listener.drain()[0]
            time.sleep(0.2)
        service.stop(stop)
        runner.join(30)
        self.assertFalse(runner.is_alive())
        self.assertEqual(result, [cli.EXIT_OK], err.getvalue())
        records = [json.loads(line) for line in err.getvalue().splitlines()]
        # 17 Nov: the FR-O6 event log (on by default) writes JSON lines between the two records.
        self.assertEqual((records[0]['event'], records[-1]['event']), ('starting', 'stopped'))
        self.assertEqual((records[0]['model_version'], records[0]['ari_reachable']), ('lab-cli-untrained', True))
        self.assertNotIn(password, err.getvalue())
        self.assertGreaterEqual(len([e for e in events if e.type == 'score.updated' and e.data.get('scored')]), 3)
        self.assertEqual([e.data['reason'] for e in events if e.type == 'call.ended'], ['bye'])
        self.assertEqual(records[-1]['service']['disconnects'], 0)
        call_id = next(e.call_id for e in events if e.type == 'call.started')
        logged = [(r['event'], r.get('call_id'), r.get('reason')) for r in records[1:-1]
                  if r['event'] in ('call_started', 'call_ended')]
        self.assertEqual(logged, [('call_started', call_id, None), ('call_ended', call_id, 'bye')])
        self.assertTrue(all({'ts', 'level', 'event'} <= set(r) for r in records[1:-1]))
        self.assertNotIn(caller, err.getvalue(), 'the caller number is never logged')
        self.assertNotIn('score_updated', err.getvalue())


if __name__ == '__main__':
    unittest.main()
