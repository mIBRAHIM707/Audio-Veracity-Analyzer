"""Claude-authored tests (15 November 2026): a detector that fails or hangs during a REAL
lab call never affects the call (backlog 6.QA "detector failure never drops calls";
PRD FR-T5 advisory/fail-open, FR-I9/FR-I11 containment; architecture §2.1, §2.4).

Authored by Claude (Claude Code), not by GPT-6 Astra. Opt-in (AVA_TEST_DOCKER_LAB=1);
needs `.local/real-model-qa.json` (public CSALT clips) and the Silero model, so real
speech reaches the detector. The detector is a stand-in that raises, or blocks the
service thread, on every call: these tests are about the call, not about scores.

- raising: SIPp completes its call normally; the service publishes `call.started` and
  exactly one `call.ended reason=bye`, no scored update, and counts the lost windows.
- hanging: the detector's first call blocks the single service thread while the call is
  still playing (a stuck ONNX run), until the test releases it after SIPp has finished;
  SIPp still completes the call normally because the call never waits on AVA (no
  Stasis hand-off). Once released, the service catches up without a reconnect,
  publishes the end for the same call ID and stops. The hang is kept short (about the
  call length): while blocked nothing reads the RTP socket (audio is dropped; scores
  after the release are not asserted) or the ARI websocket.

Both cases also check that no tap bridge or tap channel is left in Asterisk.
Fault-injection infrastructure evidence, not product acceptance.
"""
import json
import os
from pathlib import Path
import secrets
import threading
import time
import unittest

import numpy as np

from lab.asterisk import rtp_pcap
from lab.asterisk.harness import LabContainer
from services.live.service import LiveService
from services.scorer.config import ScoringConfig
from services.tap.ari import AriClient
from tests.lab.test_asterisk_lab import IMAGE

LAB = unittest.skipIf(os.environ.get('AVA_TEST_DOCKER_LAB') != '1', 'set AVA_TEST_DOCKER_LAB=1')
QA_CONFIG = Path('.local/real-model-qa.json')
VAD_MODEL = Path(os.environ.get('AVA_SILERO_VAD_MODEL', 'D:/AVA-Data/models/silero-vad/6.2.3/silero_vad.onnx'))


class RaisingDetector:
    def __init__(self):
        self.calls, self.windows = 0, 0

    def __call__(self, batch):
        self.calls += 1
        self.windows += len(batch)
        raise RuntimeError('onnxruntime: simulated inference failure')


class HangingDetector:
    """Blocks on the first call until released; later calls return neutral logits."""

    def __init__(self):
        self.entered, self.release, self.calls, self.entered_at = threading.Event(), threading.Event(), 0, None

    def __call__(self, batch):
        self.calls += 1
        if self.entered_at is None:
            self.entered_at = time.monotonic()
        self.entered.set()
        self.release.wait(120)
        return np.zeros((len(batch), 2), np.float32)


@LAB
@unittest.skipUnless(QA_CONFIG.is_file() and VAD_MODEL.is_file(), 'needs the local QA clips and the Silero model')
class DetectorFaultLabTests(unittest.TestCase):
    def start(self, detector):
        from services.inference.silero_vad import SileroVad
        password = secrets.token_hex(16)
        lab = LabContainer(IMAGE, network='bridge', publish=['127.0.0.1::8088'], env={'AVA_ARI_PASSWORD': password})
        self.addCleanup(lab.stop)
        lab.start()
        ari = AriClient(f'http://{lab.port("8088/tcp")}', 'ava', password, 'ava-tap')
        service = LiveService(ari, detector, ScoringConfig(), lambda: SileroVad(VAD_MODEL),
                              receiver_host='host.docker.internal', tap_extensions={'700'})
        listener = service.hub.subscribe(buffer=4096)
        stop = threading.Event()
        runner = threading.Thread(target=service.run, args=(stop,), daemon=True)
        runner.start()
        self.addCleanup(runner.join, 20)
        self.addCleanup(service.stop, stop)
        self.assertTrue(service.ready.wait(15), 'service did not connect')
        self.ari = ari
        return lab, service, listener, stop, runner

    def place_call(self, lab):
        """Plays three public genuine clips into extension 700. Returns SIPp's result,
        the audio length and the time SIPp was started."""
        clips = json.loads(QA_CONFIG.read_text(encoding='utf-8'))['genuine'][:3]
        payloads = rtp_pcap.rtp_payloads(b''.join(rtp_pcap.wav_to_pcm8k(path) for path in clips), 'ulaw')
        rtp_pcap.write_pcap(payloads, 0, lab.path('inject.pcap'))
        token = f'det{secrets.token_hex(4)}'
        started = time.monotonic()
        done = lab.command(['exec', lab.name, 'sipp', '127.0.0.1:5060', '-sf', '/lab/sipp/uac_inject.xml',
                            '-s', '700', '-key', 'caller', token, '-key', 'pt', '0', '-key', 'codec', 'PCMU',
                            '-m', '1', '-d', str(len(payloads) * 20 + 1500), '-i', '127.0.0.1', '-mi', '127.0.0.1',
                            '-min_rtp_port', '16000', '-max_rtp_port', '16100', '-timeout', '60s', '-timeout_error',
                            '-nostdin'], check=False, timeout=180)
        return done, len(payloads) * 0.02, started

    def assert_nothing_left_in_asterisk(self):
        deadline = time.monotonic() + 10
        while True:
            bridges = self.ari.list_bridges()
            channels = [c['name'] for c in self.ari.list_channels() if c['name'].startswith(('Snoop/', 'UnicastRTP/'))]
            if (not bridges and not channels) or time.monotonic() > deadline:
                break
            time.sleep(0.2)
        self.assertEqual((bridges, channels), ([], []))

    def collect_until_ended(self, listener, seconds):
        events, deadline = [], time.monotonic() + seconds
        while time.monotonic() < deadline and not any(e.type == 'call.ended' for e in events):
            batch, dropped = listener.drain()
            self.assertEqual(dropped, 0)
            events += batch
            time.sleep(0.2)
        return events

    def stop_service(self, service, stop, runner, listener, events):
        service.stop(stop)
        runner.join(20)
        self.assertFalse(runner.is_alive(), 'the service loop must stop')
        return events + listener.drain()[0]

    def test_a_failing_detector_never_affects_the_call(self):
        detector = RaisingDetector()
        lab, service, listener, stop, runner = self.start(detector)
        done, _, _ = self.place_call(lab)
        self.assertEqual(done.returncode, 0, 'the call must complete normally')
        events = self.stop_service(service, stop, runner, listener, self.collect_until_ended(listener, 60))
        self.assert_one_call_ended_bye(events)
        self.assertEqual([e for e in events if e.type == 'score.updated' and e.data.get('scored')], [])
        counts = service.pipeline.counts
        self.assertGreaterEqual(detector.calls, 2, counts)  # real speech reached the detector
        self.assertEqual(counts['detector_failures'], detector.calls)
        self.assertEqual(counts['windows_lost_to_detector'], detector.windows)
        self.assert_healthy(service)
        self.assert_nothing_left_in_asterisk()

    def test_a_hung_detector_never_holds_the_call(self):
        detector = HangingDetector()
        lab, service, listener, stop, runner = self.start(detector)
        self.addCleanup(detector.release.set)
        done, played_s, started = self.place_call(lab)
        call_s = time.monotonic() - started
        self.assertTrue(detector.entered.is_set(), 'the detector was never called')
        # Stuck while the caller was still talking: at least 2 s before SIPp finished.
        self.assertLess(detector.entered_at, started + call_s - 2, (detector.entered_at - started, call_s))
        self.assertFalse(detector.release.is_set())
        self.assertEqual(done.returncode, 0, 'the call must complete while AVA is stuck')
        # The call ran its scripted length (audio + 1.5 s pause; observed 15.8 s for 13.9 s),
        # not until AVA recovered (the detector would block for 120 s).
        self.assertLess(call_s, played_s + 10, (call_s, played_s))
        detector.release.set()
        events = self.stop_service(service, stop, runner, listener, self.collect_until_ended(listener, 60))
        self.assert_one_call_ended_bye(events)
        self.assertGreaterEqual(detector.calls, 2)  # scoring resumed after the release
        self.assert_healthy(service)
        self.assert_nothing_left_in_asterisk()
        print(f'\nobserved: call {call_s:.1f} s for {played_s:.1f} s of audio; detector stuck from '
              f'{detector.entered_at - started:.1f} s; detector calls {detector.calls}; tap {service.tap.stats}')

    def assert_one_call_ended_bye(self, events):
        started = [e for e in events if e.type == 'call.started']
        ended = [e for e in events if e.type == 'call.ended']
        self.assertEqual((len(started), [e.data['reason'] for e in ended]), (1, ['bye']), [e.type for e in events])
        self.assertEqual(ended[0].call_id, started[0].call_id)

    def assert_healthy(self, service):
        # No reconnect hid the fault, nothing escaped into the loop or the tap.
        self.assertEqual((service.counts['connects'], service.counts['disconnects'], service.counts['loop_errors'],
                          service.tap.stats['sink_errors']), (1, 0, 0, 0), service.counts)


if __name__ == '__main__':
    unittest.main()
