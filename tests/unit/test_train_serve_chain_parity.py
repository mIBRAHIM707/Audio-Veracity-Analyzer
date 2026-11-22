"""Train/serve chain parity for a G.711 mu-law caller (backlog 3.1 live-side parity;
PRD FR-D12, FR-D13, FR-I2, FR-I3; architecture §2.2 steps 1-4).

Claude-authored test design, 19 November 2026 (Claude Code, not GPT-6 Astra).

One clip goes down both paths the detector can see it through:

- Training: `augment.augment_prepared` makes the mu-law copy with seeded packet loss and
  `repeat_then_silence` concealment (checked loadable by `augment.verify_augmented`); the
  window loader's rule (models/ava/windows.py, pinned by tests/model/test_ava_windows.py;
  that module needs torch, so it is not imported here) takes its first 4 s / 32768.
- Serving: the same 8 kHz mu-law packets (the copy's recorded `encoded_sha256`) are sent
  as RTP over a real loopback UDP socket, minus the packets the copy's recorded loss mask
  lost, to the real `TapApp` (a fake ARI only creates the tap) with its 60 ms
  `JitterBuffer`; `TapApp` delivers frames to `TapScoringSink` (8 -> 16 kHz stream
  resampler), `SpeechFeed` (an always-speech VAD, so VAD gating is not under test) and
  `LivePipeline` to a recording detector. The call hangs up right after its last packet,
  so the end-of-call flush delivers the buffered tail as in a real `bye`.

Every live window must equal the training copy's samples at the same offset, float for
float (int16 / 32768 on both sides, so exact); the live concealment flags must cover
exactly the lost packets; each window's concealed share at the FR-I2 gate must equal its
lost share. Windows after the first are stream parity only: training takes window 0.
A second case forces loss bursts of 5, 4, 2 and 1 packets (seeded Bernoulli loss at
<= 5 % almost never makes bursts over 3) so the "then silence" half of the rule is
compared too; its
training reference is the codec module's concealment, decode and shared upsampling.

Each link is tested on its own elsewhere (test_tap_scoring_sink, test_tap_jitter,
test_conditioning_parity); this checks the composition. Generated noise, not speech:
infrastructure evidence only, no detector claim. Not covered: late (jitter) packets,
whose serving outcome depends on arrival timing; loss of the first packet (serving
anchors on the first received packet, training conceals it with the silence code: a
known divergence) or of the last (serving never plays past the highest received
packet); clips shorter than a window (loader zero-padding vs live partial windows);
A-law (recorded +8 silence divergence, services/tap/jitter.py); GSM/AMR (the receiver
decodes G.711 only; Asterisk transcodes); the agent direction; real Silero VAD; the
FR-I2 >10 % window gate and the LossMonitor episode (no window here loses over 10 %,
and 300 frames do not fill its 500-frame span; see test_tap_scoring_sink and the
scorer tests).
"""
import hashlib
import json
from pathlib import Path
import socket
import struct
import tempfile
import unittest
import wave

import numpy as np

from data.prepare import augment
from data.prepare.codecs import _conceal, decode, encode
from data.prepare.conditioning import canonicalize, resample_to_pcm16
from data.prepare.prepare_audio import process_manifest
from services.events.fanout import EventHub
from services.inference.analysis import HOP_SAMPLES, WINDOW_SAMPLES
from services.inference.capacity import CallGate, MicroBatcher
from services.scorer.config import ScoringConfig
from services.scorer.live import LiveCalls
from services.scorer.pipeline import LivePipeline
from services.scorer.speech_feed import SpeechFeed
from services.tap.scoring_sink import TapScoringSink
from services.tap.tap_app import TapApp
from tests.fixture_cleanup import cleanup_with_retry
from tests.unit.test_tap import FakeAri, answered

RATE = 16000
PACKET = 160  # 20 ms of G.711 at 8 kHz
WINDOW_PACKETS = WINDOW_SAMPLES // 2 // PACKET  # 200
HOP_PACKETS = HOP_SAMPLES // 2 // PACKET        # 100
MASTER_SEED = 11  # chosen so the fixture clip loses packets inside the first window
FIRST_SEQUENCE = 65500  # the stream crosses the 16-bit sequence wrap
CONFIG = ScoringConfig(ema_alpha=1.0)


class Configs:
    def for_new_call(self):
        return CONFIG


class RecordingDetector:
    def __init__(self):
        self.rows = []

    def __call__(self, batch):
        self.rows.extend(np.array(row, copy=True) for row in batch)
        return np.zeros((len(batch), 2), np.float32)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def write_wav(path, pcm):
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        audio.writeframes(pcm.tobytes())


def pcm_of(path):
    with wave.open(str(path), 'rb') as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (RATE, 1, 2)
        return np.frombuffer(audio.readframes(audio.getnframes()), dtype='<i2')


def rtp(sequence, timestamp, payload, ssrc=0x5EED):
    return struct.pack('!BBHII', 0x80, 0, sequence & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc) + payload


def generated_noise(seconds, seed):
    """Level-varying generated noise (not speech)."""
    rng = np.random.default_rng(seed)
    envelope = 0.6 + 0.4 * np.sin(2 * np.pi * 3 * np.arange(seconds * RATE) / RATE)
    return np.clip(np.rint(rng.normal(0, 5000, seconds * RATE) * envelope), -32768, 32767).astype('<i2')


class TrainServeChainParityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_with_retry, temporary)
        self.base = Path(temporary.name).resolve()

    def training_copy(self):
        """Prepared clip -> augmented mu-law copy with loss. Returns (clip PCM, copy PCM,
        telephone step record, clip ID)."""
        raw = self.base / 'raw'
        raw.mkdir()
        write_wav(raw / 'a.wav', generated_noise(6, 19))
        row = {'path': 'a.wav', 'speaker_id': 'spk_a', 'label': 'genuine', 'split': 'train',
               'language': 'urd', 'training_eligible': True,
               'sha256': hashlib.sha256((raw / 'a.wav').read_bytes()).hexdigest()}
        (self.base / 'manifest.json').write_text(json.dumps({'files': [row]}), encoding='utf-8')
        prepared = self.base / 'prepared'
        process_manifest(self.base / 'manifest.json', raw, prepared, condition=True)
        clips = json.loads((prepared / 'report.json').read_text(encoding='utf-8'))['clips']
        self.assertEqual(len(clips), 1, 'fixture must give one clip')
        spec = {'chain_version': augment.CHAIN_VERSION, 'master_seed': MASTER_SEED, 'codec': 'g711_ulaw',
                'concealment': 'repeat_then_silence', 'packet_loss': True, 'jitter': False, 'noise': None}
        copies = self.base / 'augmented'
        augment.augment_prepared(prepared, copies, spec)
        self.assertEqual(augment.verify_augmented(prepared, copies), [], 'the loader would refuse this copy')
        copy_rows = json.loads((copies / 'report.json').read_text(encoding='utf-8'))['clips']
        self.assertEqual(len(copy_rows), 1)
        chain = copy_rows[0]['chain']
        telephone = next(step for step in chain['steps'] if step['step'] == 'telephone_roundtrip')
        return (pcm_of(prepared / clips[0]['path']), pcm_of(copies / copy_rows[0]['path']), telephone,
                chain['derivation_clip_id'])

    def serve(self, packets, lost):
        """Send the packets not lost as RTP over loopback, one per 20 ms of a fake clock,
        to a real TapApp feeding the sink, feed and pipeline; hang up after the last one.
        Returns (detector rows, concealed flags at 16 kHz, offered windows as (index,
        concealed_fraction), receiver stats)."""
        clock = Clock()
        live = LiveCalls(Configs(), EventHub(), CallGate(4),
                         new_alert_id=iter(f'alert-{i}' for i in range(1, 99)).__next__)
        detector = RecordingDetector()
        batcher = MicroBatcher(capacity_windows_per_s=10.0, collect_s=0.1, max_live=4, max_pending_live=256)
        pipeline = LivePipeline(live, batcher, detector, CONFIG)
        offered, original_offer = [], pipeline.offer

        def offer(call_id, direction, index, window, now, *, concealed_fraction=0.0):
            offered.append((direction, index, concealed_fraction))
            return original_offer(call_id, direction, index, window, now, concealed_fraction=concealed_fraction)
        pipeline.offer = offer
        feed = SpeechFeed(pipeline, lambda: (lambda frame: 1.0))  # always speech
        flags, original_audio = [], feed.audio

        def audio(call_id, direction, pcm, now, concealed=None):
            flags.append(np.array(concealed, copy=True))
            return original_audio(call_id, direction, pcm, now, concealed=concealed)
        feed.audio = audio
        sink = TapScoringSink(live, feed, pipeline, clock)
        ari = FakeAri()
        app = TapApp(ari, sink, receiver_host='127.0.0.1', bind_host='127.0.0.1', clock=clock)
        self.addCleanup(app.close)
        app.handle(answered('c1', number='ava-1'))
        self.assertIn('c1', app.calls)
        session = app.calls['c1'].session
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sender.close)
        for index, payload in enumerate(packets):
            clock.t = 100.0 + index * 0.02
            if lost[index]:
                app.poll(0.0)
            else:
                sender.sendto(rtp(FIRST_SEQUENCE + index, index * PACKET, payload), ('127.0.0.1', session.port))
                received = session.stats['packets']
                app.poll(2.0)
                self.assertEqual(session.stats['packets'], received + 1, f'packet {index} not received')
            sink.pump()  # as LiveService's loop: tap poll, then the sink's pump
        app.handle({'type': 'ChannelHangupRequest', 'channel': {'id': 'c1'}})  # bye: final flush
        stats = session.receiver_stats()
        for _ in range(30):
            clock.t += 0.1
            sink.pump()
        self.assertEqual((sink.counts['ended'], app.stats['ended'], app.stats['sink_errors']), (1, 1, 0))
        self.assertEqual(sink.counts['errors'] + sink.counts['pump_errors'] + sink.counts['ends_forced'], 0)
        self.assertEqual(feed.counts['stride_skipped'], 0)
        return detector.rows, np.concatenate(flags), offered, stats

    def assert_serving_equals(self, training, lost, packets, *, repeated, silence):
        """Run the serving path and compare with the training copy (int16 PCM at 16 kHz)."""
        rows, flags, offered, stats = self.serve(packets, lost)
        count = len(packets)
        self.assertEqual((stats['jitter_concealed_frames'], stats['jitter_repeated_frames'],
                          stats['jitter_silence_frames']), (int(lost.sum()), repeated, silence))
        for key in ('jitter_late', 'jitter_resyncs', 'jitter_talkspurts', 'jitter_drift_adjustments',
                    'jitter_dropped_handover', 'jitter_dropped_pending', 'late_or_duplicate'):
            self.assertEqual(stats[key], 0, key)
        self.assertEqual(stats['packets'], count - int(lost.sum()))
        streamed = count * 2 * PACKET
        # Concealment flags: exactly the lost packets (one 8 kHz packet = 320 samples at 16 kHz).
        self.assertTrue(np.array_equal(flags, np.repeat(lost, 2 * PACKET)),
                        'live concealment flags differ from the lost packets')
        # The stream is whole windows only (no uncovered tail), so every row is a full window.
        self.assertEqual((streamed - WINDOW_SAMPLES) % HOP_SAMPLES, 0, 'fixture must end on a window')
        windows = (streamed - WINDOW_SAMPLES) // HOP_SAMPLES + 1
        self.assertEqual(len(rows), windows)
        self.assertEqual([(d, i) for d, i, _ in offered], [('caller', i) for i in range(windows)])
        reference = training.astype(np.float32) / 32768
        self.assertEqual(len(reference), streamed)
        for index, row in enumerate(rows):
            start = index * HOP_SAMPLES
            with self.subTest(window=index):
                np.testing.assert_array_equal(row, reference[start:start + WINDOW_SAMPLES])
                share = lost[index * HOP_PACKETS:index * HOP_PACKETS + WINDOW_PACKETS].mean()
                self.assertAlmostEqual(offered[index][2], share, places=12)
        return rows

    def test_live_windows_equal_the_training_copy_of_a_lossy_mulaw_clip(self):
        clip, copy, telephone, clip_id = self.training_copy()
        # The serving packets are the training copy's encoding, before its loss.
        narrow, _ = resample_to_pcm16(clip.astype(np.float64), RATE, 8000)
        encoded = encode(narrow, 'ulaw')
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), telephone['encoded_sha256'])
        self.assertIsNone(telephone['jitter'])
        loss = telephone['packet_loss']
        self.assertEqual((loss['derivation'], loss['clip_id'], loss['concealment'], loss['repeat_limit']),
                         ('ava-packet-loss-v1', clip_id, 'repeat_then_silence', 3))
        self.assertEqual(len(encoded) % PACKET, 0)
        packets = [encoded[i:i + PACKET] for i in range(0, len(encoded), PACKET)]
        # The recorded mask, rebuilt by the rule the record states and checked by its hash.
        self.assertEqual(loss['packets'], len(packets))
        lost = np.random.default_rng(loss['seed']).random(loss['packets']) < loss['loss_rate']
        self.assertEqual(hashlib.sha256(np.flatnonzero(lost).astype('<i4').tobytes()).hexdigest(),
                         loss['lost_indices_sha256'])
        # Preconditions that keep the case meaningful (see the module docstring).
        self.assertGreaterEqual(int(lost[:WINDOW_PACKETS].sum()), 2, 'fixture must lose packets in window 0')
        self.assertFalse(lost[0] or lost[-1])
        runs = np.diff(np.flatnonzero(np.diff(np.concatenate([[0], lost.astype(int), [0]]))))[::2]
        self.assertLessEqual(int(runs.max()), 3, 'this seed has a burst: update the expected counts')

        rows = self.assert_serving_equals(copy, lost, packets, repeated=int(lost.sum()), silence=0)
        # Window 0 is what the training loader takes from this copy (first 4 s / 32768),
        # and the concealed audio there is a repeat, not silence.
        first_lost = int(np.flatnonzero(lost)[0])
        self.assertTrue(np.any(rows[0][2 * PACKET * first_lost:2 * PACKET * (first_lost + 1)] != 0))

    def test_loss_bursts_repeat_three_frames_then_silence_on_both_paths(self):
        narrow, _ = resample_to_pcm16(generated_noise(6, 23).astype(np.float64), RATE, 8000)
        encoded = encode(narrow, 'ulaw')
        packets = [encoded[i:i + PACKET] for i in range(0, len(encoded), PACKET)]
        self.assertEqual(len(packets), 300)
        lost = np.zeros(len(packets), bool)
        lost[30:35] = True    # 3 repeats, then 2 silence frames
        lost[120:124] = True  # 3 repeats, then 1 silence frame
        lost[160:162] = True  # 2 repeats
        lost[250] = True      # in window 1 only
        codes = np.frombuffer(encoded, dtype=np.uint8)
        concealed = _conceal(codes, lost, 'ulaw', 'repeat_then_silence').astype(np.uint8).tobytes()
        decoded = decode(concealed, 'ulaw')
        # The reference really applies the rule at 8 kHz: the 4th and later frames of a
        # burst are digital silence, the first three repeat the last received frame. (At
        # 16 kHz the resampler's filter rings into them, so this is checked before.)
        pcm8 = np.frombuffer(decoded, dtype='<i2')
        frame = lambda k: pcm8[k * PACKET:(k + 1) * PACKET]
        for silent in (33, 34, 123):
            self.assertFalse(frame(silent).any(), silent)
        for repeat, source in ((30, 29), (32, 29), (120, 119), (161, 159), (250, 249)):
            self.assertTrue(np.array_equal(frame(repeat), frame(source)), repeat)
        training, _ = canonicalize(decoded, 8000, 1)  # as augment.py's chain
        self.assert_serving_equals(np.frombuffer(training, dtype='<i2'), lost, packets,
                                   repeated=3 + 3 + 2 + 1, silence=2 + 1)


if __name__ == '__main__':
    unittest.main()
