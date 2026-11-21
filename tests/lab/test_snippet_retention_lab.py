"""Claude-authored lab test (17 November 2026): FR-O5 alert snippets on a REAL lab call
(backlog 5.3; PRD FR-O5 "one snippet per alert ID persisted at alert.raised, AES-256-GCM
at rest"; architecture §2.4 "the 30 s ring buffer ending at the alert").

Authored by Claude (Claude Code), not by GPT-6 Astra. Opt-in (AVA_TEST_DOCKER_LAB=1);
needs `.local/real-model-qa.json` (public CSALT genuine clips), the Silero model and the
pinned cryptography package (requirements-retention.txt). A real Asterisk 20.6 call
plays the clips into extension 700; `LiveService` taps it with retention on. The
detector is a stand-in that calls every window synthetic (p = 0.95), so the scorer
raises an alert: this is about retention plumbing, not about detection. Checks: the call
completes normally; exactly one snippet file, for the published alert ID; it decrypts
with the key and holds caller audio at 8 kHz that is sample-identical to the decode of
the injected packets over its whole length, ending near the alert, at most 30 s.
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
from lab.asterisk.harness import LabContainer
from services.live.retention import SnippetRecorder
from services.live.service import LiveService
from services.scorer.config import ScoringConfig
from services.tap.ari import AriClient
from tests.lab.test_asterisk_lab import IMAGE, overlap, samples

LAB = unittest.skipIf(os.environ.get('AVA_TEST_DOCKER_LAB') != '1', 'set AVA_TEST_DOCKER_LAB=1')
QA_CONFIG = Path('.local/real-model-qa.json')
VAD_MODEL = Path(os.environ.get('AVA_SILERO_VAD_MODEL', 'D:/AVA-Data/models/silero-vad/6.2.3/silero_vad.onnx'))


class SyntheticDetector:
    """Every window p_synthetic = 0.95 (logit difference log 19)."""

    def __call__(self, batch):
        return np.tile(np.array([[np.log(19.0), 0.0]], np.float32), (len(batch), 1))


@LAB
@unittest.skipUnless(QA_CONFIG.is_file() and VAD_MODEL.is_file(), 'needs the local QA clips and the Silero model')
class SnippetRetentionLabTests(unittest.TestCase):
    def test_an_alert_on_a_real_call_stores_its_encrypted_caller_audio(self):
        try:
            from services.retention.snippets import AesGcmSealer, RetentionPolicy, SnippetStore, open_snippet
            sealer = AesGcmSealer(secrets.token_bytes(32))
        except RuntimeError:
            self.skipTest('requirements-retention.txt not installed')
        from services.inference.silero_vad import SileroVad
        folder = tempfile.TemporaryDirectory(prefix='ava-snippets-')
        self.addCleanup(folder.cleanup)
        store = SnippetStore(folder.name, sealer, clock=time.time)
        password = secrets.token_hex(16)
        lab = LabContainer(IMAGE, network='bridge', publish=['127.0.0.1::8088'], env={'AVA_ARI_PASSWORD': password})
        self.addCleanup(lab.stop)
        lab.start()
        ari = AriClient(f'http://{lab.port("8088/tcp")}', 'ava', password, 'ava-tap')
        service = LiveService(ari, SyntheticDetector(), ScoringConfig(), lambda: SileroVad(VAD_MODEL),
                              receiver_host='host.docker.internal', tap_extensions={'700'},
                              recorder_factory=lambda hub, live: SnippetRecorder(store, RetentionPolicy(True), hub,
                                                                                 live))
        listener = service.hub.subscribe(buffer=4096)
        stop = threading.Event()
        runner = threading.Thread(target=service.run, args=(stop,), daemon=True)
        runner.start()
        self.addCleanup(runner.join, 20)
        self.addCleanup(service.stop, stop)
        self.assertTrue(service.ready.wait(15), 'service did not connect')

        clips = json.loads(QA_CONFIG.read_text(encoding='utf-8'))['genuine'][:3]
        payloads = rtp_pcap.rtp_payloads(b''.join(rtp_pcap.wav_to_pcm8k(path) for path in clips), 'ulaw')
        rtp_pcap.write_pcap(payloads, 0, lab.path('inject.pcap'))
        done = lab.command(['exec', lab.name, 'sipp', '127.0.0.1:5060', '-sf', '/lab/sipp/uac_inject.xml', '-s', '700',
                            '-key', 'caller', f'ret{secrets.token_hex(4)}', '-key', 'pt', '0', '-key', 'codec', 'PCMU',
                            '-m', '1', '-d', str(len(payloads) * 20 + 1500), '-i', '127.0.0.1', '-mi', '127.0.0.1',
                            '-min_rtp_port', '16000', '-max_rtp_port', '16100', '-timeout', '60s', '-timeout_error',
                            '-nostdin'], check=False, timeout=180)
        self.assertEqual(done.returncode, 0, 'the call must complete normally')
        events, deadline = [], time.monotonic() + 60
        while time.monotonic() < deadline and not any(e.type == 'call.ended' for e in events):
            batch, dropped = listener.drain()
            self.assertEqual(dropped, 0)
            events += batch
            time.sleep(0.2)
        service.stop(stop)
        runner.join(20)
        self.assertFalse(runner.is_alive())
        events += listener.drain()[0]
        recorder = service.recorder
        recorder.write_pending()  # what the writer thread would do (no PumpThread in this test)

        alerts = [e for e in events if e.type == 'alert.raised']
        self.assertEqual(len(alerts), 1, [e.type for e in events])
        report = recorder.report()['snippets']
        self.assertEqual((report['alerts_seen'], report['captured'], report['written'], report['write_failed']),
                         (1, 1, 1, 0), report)
        files = sorted(p.name for p in Path(folder.name).iterdir())
        self.assertEqual(len(files), 1, files)
        raw = (Path(folder.name) / files[0]).read_bytes()
        snippet = open_snippet(folder.name, alerts[0].data['alert_id'], sealer)
        self.assertEqual((snippet['call_id'], snippet['direction'], snippet['sample_rate']),
                         (alerts[0].call_id, 'caller', 8000))
        pcm = snippet['pcm']
        self.assertNotIn(pcm[:200].astype('<i2').tobytes(), raw, 'audio is encrypted at rest')
        self.assertLessEqual(len(pcm), 30 * 8000)
        self.assertGreater(len(pcm), 4 * 8000, 'at least the window that raised the alert')
        reference = rtp_pcap.reference_decode(payloads, 'ulaw')
        correlation, ref_start, rec_start, n = overlap(reference, pcm.astype('<i2').tobytes())
        self.assertGreater(correlation, 0.999, (correlation, ref_start, rec_start, n))
        self.assertEqual((rec_start, n), (0, len(pcm)), 'the whole snippet lines up with the injected audio')
        self.assertTrue(np.array_equal(samples(reference, ref_start, n), pcm))
        played_s, alert_s = len(reference) / 2 / 8000, (ref_start + n) / 8000
        print(f'\nobserved: alert after {alert_s:.1f} s of {played_s:.1f} s played; snippet {len(pcm) / 8000:.1f} s; '
              f'report {report}')


if __name__ == '__main__':
    unittest.main()
