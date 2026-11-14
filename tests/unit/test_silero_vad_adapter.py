"""Silero VAD adapter contract with a fake ONNX session (backlog 3.2).

Claude-authored test design, 14 November 2026. No model or onnxruntime needed; the
real-model parity check is tests/model/test_silero_vad_onnx.py.
"""
import hashlib
from pathlib import Path
import tempfile
import unittest

import numpy as np

from services.inference.silero_vad import CONTEXT_SAMPLES, FRAME_SAMPLES, STATE_SHAPE, SileroVad
from services.scorer.speech_windows import SpeechWindower, VadFailure
from tests.fixture_cleanup import cleanup_with_retry


class Input:
    def __init__(self, name):
        self.name = name


class FakeSession:
    """Records every run; probability = mean |input|, state = previous state + 1."""

    def __init__(self, model, threads, *, inputs=('input', 'state', 'sr'), fail=None):
        self.model, self.threads, self.calls, self._inputs, self.fail = model, threads, [], inputs, fail

    def get_inputs(self):
        return [Input(n) for n in self._inputs]

    def run(self, outputs, feeds):
        self.calls.append({k: np.array(v, copy=True) for k, v in feeds.items()})
        if self.fail == 'raise':
            raise RuntimeError('onnx failure')
        if self.fail == 'bad_state':
            return [np.array([[0.5]], np.float32), np.zeros((1, 1, 1), np.float32)]
        if self.fail == 'nan_state':
            return [np.array([[0.5]], np.float32), np.full((2, 1, 128), np.nan, np.float32)]
        if self.fail == 'probability_above_one':
            return [np.array([[1.5]], np.float32), feeds['state'] + 1]
        return [np.array([[float(np.mean(np.abs(feeds['input'])))]], np.float32), feeds['state'] + 1]


class SileroVadAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_with_retry, temporary)  # Windows scanners can hold new files
        self.model = Path(temporary.name) / 'silero_vad.onnx'
        self.model.write_bytes(b'fake model bytes')
        self.sha = hashlib.sha256(b'fake model bytes').hexdigest()
        self.sessions = []

    def factory(self, **kwargs):
        def make(model, threads):
            session = FakeSession(model, threads, **kwargs)
            self.sessions.append(session)
            return session
        return make

    def vad(self, **kwargs):
        return SileroVad(self.model, expected_sha256=self.sha, session_factory=self.factory(**kwargs))

    def test_feeds_follow_the_reference_16k_context_and_state_contract(self):
        vad = self.vad()
        frames = [np.full(FRAME_SAMPLES, v, np.float32) for v in (0.1, -0.2, 0.3)]
        frames[1][-CONTEXT_SAMPLES:] = np.linspace(-1, 0.5, CONTEXT_SAMPLES, dtype=np.float32)
        probabilities = [vad(f) for f in frames]
        calls = self.sessions[0].calls
        self.assertEqual([c['input'].shape for c in calls], [(1, CONTEXT_SAMPLES + FRAME_SAMPLES)] * 3)
        np.testing.assert_array_equal(calls[0]['input'][0, :CONTEXT_SAMPLES], 0)  # zero context first
        np.testing.assert_array_equal(calls[1]['input'][0, :CONTEXT_SAMPLES], frames[0][-CONTEXT_SAMPLES:])
        np.testing.assert_array_equal(calls[2]['input'][0, :CONTEXT_SAMPLES], frames[1][-CONTEXT_SAMPLES:])
        for i, call in enumerate(calls):
            np.testing.assert_array_equal(call['input'][0, CONTEXT_SAMPLES:], frames[i])
            self.assertEqual(call['state'].shape, STATE_SHAPE)
            np.testing.assert_array_equal(call['state'], i)  # state carried from the previous output
            self.assertEqual((call['sr'].dtype, int(call['sr'])), (np.int64, 16000))
            self.assertEqual(call['input'].dtype, np.float32)
        self.assertAlmostEqual(probabilities[0], (FRAME_SAMPLES * 0.1) / (FRAME_SAMPLES + CONTEXT_SAMPLES), places=6)
        self.assertEqual((self.sessions[0].model, self.sessions[0].threads), (b'fake model bytes', 1))

    def test_reset_clears_state_and_context(self):
        vad = self.vad()
        vad(np.ones(FRAME_SAMPLES, np.float32))
        vad.reset()
        vad(np.ones(FRAME_SAMPLES, np.float32))
        second = self.sessions[0].calls[1]
        np.testing.assert_array_equal(second['state'], 0)
        np.testing.assert_array_equal(second['input'][0, :CONTEXT_SAMPLES], 0)

    def test_model_file_hash_size_and_inputs_are_checked(self):
        with self.assertRaisesRegex(ValueError, 'does not match'):
            SileroVad(self.model, session_factory=self.factory())  # pinned real hash
        with self.assertRaisesRegex(ValueError, 'not found'):
            SileroVad(self.model.with_name('missing.onnx'), expected_sha256=self.sha,
                      session_factory=self.factory())
        with self.assertRaisesRegex(ValueError, 'inputs'):
            SileroVad(self.model, expected_sha256=self.sha, session_factory=self.factory(inputs=('x',)))
        for threads in (0, 9, 1.0, True):
            with self.subTest(threads=threads):
                with self.assertRaises(ValueError):
                    SileroVad(self.model, expected_sha256=self.sha, threads=threads,
                              session_factory=self.factory())
        self.assertEqual(self.vad().model_sha256, self.sha)
        big = self.model.with_name('big.onnx')
        with open(big, 'wb') as handle:  # sparse where supported; just over the cap
            handle.seek(16_000_000)
            handle.write(b'\x00')
        with self.assertRaisesRegex(ValueError, 'larger than expected'):
            SileroVad(big, expected_sha256=None, session_factory=self.factory())

    def test_invalid_frames_and_session_faults_leave_state_unchanged(self):
        vad = self.vad()
        for bad in (np.zeros(FRAME_SAMPLES, np.float64), np.zeros(FRAME_SAMPLES + 1, np.float32),
                    np.full(FRAME_SAMPLES, np.nan, np.float32)):
            with self.subTest(dtype=bad.dtype, n=len(bad)):
                with self.assertRaises(ValueError):
                    vad(bad)
        self.assertEqual(self.sessions[0].calls, [])
        vad(np.ones(FRAME_SAMPLES, np.float32))
        for fail in ('raise', 'bad_state', 'nan_state', 'probability_above_one'):
            with self.subTest(fail=fail):
                self.sessions[0].fail = fail
                with self.assertRaises((RuntimeError, ValueError)):
                    vad(np.full(FRAME_SAMPLES, 0.5, np.float32))
        self.sessions[0].fail = None
        vad(np.full(FRAME_SAMPLES, 0.25, np.float32))
        last = self.sessions[0].calls[-1]
        np.testing.assert_array_equal(last['state'], 1)  # only the one successful frame counted
        np.testing.assert_array_equal(last['input'][0, :CONTEXT_SAMPLES], 1)

    def test_drives_the_speech_windower_and_its_failure_reset(self):
        vad = self.vad()
        windower = SpeechWindower(vad)
        loud = np.full(int(5.6 * 16000), 20000, np.int16)  # one full window + a 1.6 s tail
        windows = windower.feed(loud) + windower.finish()
        self.assertEqual([w.partial for w in windows], [False, True])
        self.sessions[0].fail = 'raise'
        failing = SpeechWindower(vad)
        with self.assertRaises(VadFailure):
            failing.feed(loud[:16000])
        self.sessions[0].fail = None
        vad(np.zeros(FRAME_SAMPLES, np.float32))
        np.testing.assert_array_equal(self.sessions[0].calls[-1]['state'], 0)  # reset by the windower


if __name__ == '__main__':
    unittest.main()
