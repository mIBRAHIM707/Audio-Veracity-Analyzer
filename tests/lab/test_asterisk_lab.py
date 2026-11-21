"""Claude-authored tests (15 November 2026) for the Asterisk/SIPp lab (backlog 6.1; PRD
FR-T1: Asterisk PBX, a call generator that plays audio files into calls, codec forcing
for G.711 and GSM).

Authored by Claude (Claude Code), not by GPT-6 Astra. Lab tests start their own
container from the locally built image (`docker build -t ava-lab-asterisk:dev
lab/asterisk`), place SIPp calls inside it and stop it afterwards; they run only with
AVA_TEST_DOCKER_LAB=1 (never in CI). The pure RTP capture tests are in
tests/unit/test_lab_rtp_pcap.py. Synthetic signals only; this is lab plumbing
evidence, not detector evidence.
"""
import os
from pathlib import Path
import time
import unittest
import uuid
import wave

import numpy as np

from lab.asterisk import rtp_pcap
from lab.asterisk.harness import LabContainer, LabError
from tests.unit.test_lab_rtp_pcap import speechlike

IMAGE = os.environ.get('AVA_LAB_IMAGE', 'ava-lab-asterisk:dev')
LAB = unittest.skipIf(os.environ.get('AVA_TEST_DOCKER_LAB') != '1', 'set AVA_TEST_DOCKER_LAB=1 (needs Docker and the lab image)')


def overlap(reference, received):
    """Align by FFT cross-correlation. Returns (correlation, ref_start, rec_start, n):
    reference[ref_start:ref_start+n] lines up with received[rec_start:rec_start+n]
    (the recording may start after the first packets or carry leading silence)."""
    a = np.frombuffer(reference, dtype='<i2').astype(np.float64)
    b = np.frombuffer(received, dtype='<i2').astype(np.float64)
    size = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    xcorr = np.fft.irfft(np.fft.rfft(b, size) * np.conj(np.fft.rfft(a, size)), size)
    shift = int(np.argmax(xcorr))
    lag = shift if shift < size // 2 else shift - size  # received[i + lag] ~ reference[i]
    ref_start, rec_start = max(0, -lag), max(0, lag)
    n = min(len(a) - ref_start, len(b) - rec_start)
    x, y = a[ref_start:ref_start + n], b[rec_start:rec_start + n]
    return float(np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y))), ref_start, rec_start, n


def samples(pcm, start, n):
    return np.frombuffer(pcm, dtype='<i2')[start:start + n]


@LAB
class AsteriskLabTests(unittest.TestCase):
    """One container for the class; each case uses its own caller token and recording."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.lab = LabContainer(IMAGE, network='none')  # SIPp and Asterisk use the container loopback
        except LabError as error:
            raise unittest.SkipTest(str(error))
        cls.addClassCleanup(cls.lab.stop)  # registered first: runs even if start() fails
        cls.lab.start()
        cls.io = cls.lab.io

    def sipp(self, token, payload_type, codec_name, duration_ms, service='600'):
        return self.lab.command(['exec', self.lab.name, 'sipp', '127.0.0.1:5060', '-sf', '/lab/sipp/uac_inject.xml',
                                 '-s', service, '-key', 'caller', token, '-key', 'pt', str(payload_type),
                                 '-key', 'codec', codec_name, '-m', '1', '-d', str(duration_ms), '-i', '127.0.0.1',
                                 '-mi', '127.0.0.1', '-min_rtp_port', '16000', '-max_rtp_port', '16100',
                                 '-timeout', '60s', '-timeout_error', '-nostdin', '-trace_err'],
                                check=False, timeout=120)

    def place_call(self, pcm, codec, ffmpeg=None, extra_ms=1500):
        payloads = rtp_pcap.rtp_payloads(pcm, codec, ffmpeg)
        payload_type, name = rtp_pcap.PAYLOAD_TYPES[codec]
        rtp_pcap.write_pcap(payloads, payload_type, Path(self.io, 'inject.pcap'))
        token = f'case{uuid.uuid4().hex[:8]}'
        duration_ms = len(payloads) * 20 + extra_ms
        done = self.sipp(token, payload_type, name, duration_ms)
        self.assertEqual(done.returncode, 0, f'sipp: {done.stdout[-1500:]} {done.stderr[-1500:]}')
        # SIPp has its 200 OK to the BYE; wait for Asterisk to close the recording.
        recording = Path(self.io, f'rec-{token}.wav')
        deadline = time.monotonic() + 30
        size = -1
        while time.monotonic() < deadline:
            if recording.exists() and recording.stat().st_size == size and size > 44:
                break
            size = recording.stat().st_size if recording.exists() else -1
            time.sleep(1.0)
        self.assertTrue(recording.exists(), f'no recording; sipp said: {done.stdout[-1500:]} {done.stderr[-1500:]}')
        with wave.open(str(recording), 'rb') as handle:
            self.assertEqual((handle.getnchannels(), handle.getsampwidth(), handle.getframerate()), (1, 2, 8000))
            received = handle.readframes(handle.getnframes())
        return payloads, received

    def check_g711(self, law, seconds, seed):
        pcm = speechlike(seconds, seed)
        payloads, received = self.place_call(pcm, law)
        reference = rtp_pcap.reference_decode(payloads, law)
        correlation, ref_start, rec_start, n = overlap(reference, received)
        self.assertGreater(correlation, 0.999, (correlation, ref_start, rec_start, n))
        # Most of the call reached the recording (it starts just after the answer).
        self.assertGreater(n, 0.9 * len(reference) // 2, (ref_start, rec_start, n))
        # G.711 to signed linear is a table lookup: what Asterisk recorded is the decode
        # of what SIPp sent, sample for sample, over the whole overlap.
        self.assertTrue(np.array_equal(samples(reference, ref_start, n), samples(received, rec_start, n)))
        return ref_start, rec_start, n

    def test_ulaw_call_delivers_the_injected_audio_sample_exactly(self):
        self.check_g711('ulaw', 4.0, 3)

    def test_alaw_call_is_forced_and_delivered(self):
        self.check_g711('alaw', 3.0, 4)

    def test_gsm_call_is_forced_and_delivered(self):
        from data.prepare.ffmpeg_codecs import Ffmpeg
        from tests.unit.test_ffmpeg_codecs import real_ffmpeg_path
        path = real_ffmpeg_path()
        if path is None:
            self.skipTest('GSM payloads need the pinned ffmpeg')
        ffmpeg = Ffmpeg(path)
        pcm = speechlike(4.0, 5)
        payloads, received = self.place_call(pcm, 'gsm', ffmpeg)
        reference = rtp_pcap.reference_decode(payloads, 'gsm', ffmpeg)
        correlation, ref_start, rec_start, n = overlap(reference, received)
        # Asterisk's codec_gsm and ffmpeg's libgsm decode the same GSM 06.10 frames.
        self.assertGreater(correlation, 0.99, (correlation, ref_start, rec_start, n))
        self.assertGreater(n, 0.9 * len(reference) // 2, (ref_start, rec_start, n))
        # Codec forcing: only GSM frames were sent (pt 3, a single-codec offer), so a
        # recording that matches their GSM decode shows the call ran on GSM end to end.
        # Lab review L5: it must match the GSM decode better than the original signal.
        to_original, *_ = overlap(pcm, received)
        self.assertGreater(correlation, to_original)

    def test_offer_with_a_codec_the_pbx_does_not_allow_is_refused(self):
        # Lab review M3: G.722 (pt 9) is not in the endpoint's allow list; Asterisk must
        # refuse the call (SIPp then fails: the scenario expects a 200) and record nothing.
        rtp_pcap.write_pcap([b'\x00' * 160], 9, Path(self.io, 'inject.pcap'))
        token = f'case{uuid.uuid4().hex[:8]}'
        done = self.sipp(token, 9, 'G722', 1000)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('488', done.stdout + done.stderr + self.lab.command(
            ['exec', self.lab.name, 'sh', '-c', 'cat /lab/sipp/*_errors.log /*_errors.log 2>/dev/null || true'],
            check=False).stdout)
        time.sleep(1.0)
        self.assertFalse(Path(self.io, f'rec-{token}.wav').exists())
