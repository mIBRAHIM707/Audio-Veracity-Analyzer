"""Claude-authored test (15 November 2026): public speech through a real lab call into
live scoring (backlog 6.QA; PRD FR-T1, FR-T2, FR-I1-FR-I3, FR-I11, FR-A4; architecture
§2.1-§2.4, §3 data flow).

Authored by Claude (Claude Code), not by GPT-6 Astra. Opt-in (AVA_TEST_DOCKER_LAB=1)
and needs local, gitignored inputs: `.local/real-model-qa.json` naming prepared public
CSALT clips (as in the real-model browser test) and the pinned Silero VAD model
(AVA_SILERO_VAD_MODEL). SIPp plays three genuine clips (16 kHz -> 8 kHz mu-law RTP) into
extension 700; the ARI tap receives the caller audio, the jitter buffer, the streaming
8 -> 16 kHz step, the real Silero VAD, the speech windower, the pipeline and LiveCalls
publish FR-A4 events. The detector is a FAKE with a fixed score: this is evidence that
real speech flows end to end and the call lifecycle closes, not detector evidence.
No audio or clip path is written to the repository or to test output.
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
from lab.asterisk.harness import LabContainer, LabError
from services.events.fanout import EventHub
from services.inference.capacity import CallGate, MicroBatcher
from services.scorer.config import ScoringConfig
from services.scorer.live import LiveCalls
from services.scorer.pipeline import LivePipeline
from services.scorer.speech_feed import SpeechFeed
from services.tap.ari import AriClient
from services.tap.scoring_sink import TapScoringSink
from tests.lab.test_ari_tap import IMAGE, TapRunner

LAB = unittest.skipIf(os.environ.get('AVA_TEST_DOCKER_LAB') != '1', 'set AVA_TEST_DOCKER_LAB=1')
QA_CONFIG = Path('.local/real-model-qa.json')
MODEL = Path(os.environ.get('AVA_SILERO_VAD_MODEL', 'D:/AVA-Data/models/silero-vad/6.2.3/silero_vad.onnx'))
CONFIG = ScoringConfig()


class FixedDetector:
    """Fake: every window gets p_synthetic = 0.2 (logits for [synthetic, genuine])."""

    def __call__(self, batch):
        return np.tile(np.array([[np.log(0.2 / 0.8), 0.0]], np.float32), (len(batch), 1))


class Configs:
    def for_new_call(self):
        return CONFIG


@LAB
@unittest.skipUnless(QA_CONFIG.is_file() and MODEL.is_file(), 'needs .local/real-model-qa.json and the Silero model')
class TapEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        password = secrets.token_hex(16)
        try:
            cls.lab = LabContainer(IMAGE, network='bridge', publish=['127.0.0.1::8088'],
                                   env={'AVA_ARI_PASSWORD': password})
        except LabError as error:
            raise unittest.SkipTest(str(error))
        cls.addClassCleanup(cls.lab.stop)
        cls.lab.start()
        cls.ari = AriClient(f'http://{cls.lab.port("8088/tcp")}', 'ava', password, 'ava-tap')

    def test_public_speech_through_a_lab_call_is_scored_and_the_call_closes(self):
        from services.inference.silero_vad import SileroVad
        clips = json.loads(QA_CONFIG.read_text(encoding='utf-8'))['genuine'][:3]
        pcm8k = b''.join(rtp_pcap.wav_to_pcm8k(path) for path in clips)
        seconds = len(pcm8k) / 2 / 8000
        payloads = rtp_pcap.rtp_payloads(pcm8k, 'ulaw')
        rtp_pcap.write_pcap(payloads, 0, self.lab.path('inject.pcap'))  # temporary lab folder, removed after

        hub = EventHub()
        listener = hub.subscribe(buffer=4096)
        live = LiveCalls(Configs(), hub, CallGate(8))
        pipeline = LivePipeline(live, MicroBatcher(capacity_windows_per_s=10.0, collect_s=0.05, max_live=4,
                                                    max_pending_live=64), FixedDetector(), CONFIG)
        feed = SpeechFeed(pipeline, lambda: SileroVad(MODEL))
        sink = TapScoringSink(live, feed, pipeline, time.monotonic, hub=hub)
        runner = TapRunner(self.ari, sink, on_tick=sink.pump)
        self.addCleanup(runner.stop)

        token = f'e2e{secrets.token_hex(4)}'
        done = self.lab.command(['exec', self.lab.name, 'sipp', '127.0.0.1:5060', '-sf', '/lab/sipp/uac_inject.xml',
                                 '-s', '700', '-key', 'caller', token, '-key', 'pt', '0', '-key', 'codec', 'PCMU',
                                 '-m', '1', '-d', str(len(payloads) * 20 + 1500), '-i', '127.0.0.1',
                                 '-mi', '127.0.0.1', '-min_rtp_port', '16000', '-max_rtp_port', '16100',
                                 '-timeout', '60s', '-timeout_error', '-nostdin'], check=False, timeout=180)
        self.assertEqual(done.returncode, 0, 'the lab call must complete')
        deadline, events = time.monotonic() + 30, []
        while time.monotonic() < deadline:
            batch, dropped = listener.drain()
            self.assertEqual(dropped, 0)
            events += batch
            if any(e.type == 'call.ended' for e in events):
                break
            time.sleep(0.1)
        runner.stop()
        self.assertIsNone(runner.error)
        types = [e.type for e in events]
        self.assertIn('call.started', types)
        started = next(e for e in events if e.type == 'call.started')
        self.assertEqual((started.data['from_number'], started.data['to']), (token, '700'))
        scored = [e for e in events if e.type == 'score.updated' and e.data.get('scored')]
        # About 14 s of speech gives 4 s windows every 2 s of speech: expect several.
        self.assertGreaterEqual(len(scored), int((seconds - 4) // 2) - 1, (seconds, len(scored)))
        self.assertTrue(all(abs(e.data['score'] - 0.2) < 1e-6 for e in scored))
        self.assertTrue(all(e.data['quality'] == 'ok' for e in scored))
        ended = [e for e in events if e.type == 'call.ended']
        self.assertEqual(len(ended), 1)
        self.assertEqual(ended[0].data['reason'], 'bye')
        # The real VAD found speech for most of the played clips (they are speech).
        self.assertGreater(ended[0].data['speech_seconds'], 0.5 * seconds)
        self.assertLessEqual(ended[0].data['speech_seconds'], seconds + 0.1)
        self.assertEqual((sink.counts['ended'], sink.counts['errors'], runner.app.stats['sink_errors']), (1, 0, 0))
        self.assertFalse(pipeline.has_work(started.call_id))


if __name__ == '__main__':
    unittest.main()
