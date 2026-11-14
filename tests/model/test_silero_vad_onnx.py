"""Real Silero VAD model through the AVA adapter (backlog 3.2; optional model test).

Claude-authored test design, 14 November 2026. Needs the locally provisioned model
(AVA_SILERO_VAD_MODEL, default D:/AVA-Data/models/silero-vad/6.2.3/silero_vad.onnx),
onnxruntime (requirements-vad.txt) and, for the parity case, the silero-vad 6.2.3
package with its torch reference wrapper (requirements-model.txt plus
`--with silero-vad==6.2.3`). Skipped with a reason when any is missing. Synthetic
signals only: this checks the adapter against the reference, not VAD accuracy.
"""
import os
from pathlib import Path
import unittest

import numpy as np

MODEL = Path(os.environ.get('AVA_SILERO_VAD_MODEL', 'D:/AVA-Data/models/silero-vad/6.2.3/silero_vad.onnx'))

try:
    import onnxruntime  # noqa: F401
    HAVE_ORT = True
except ImportError:
    HAVE_ORT = False


def synthetic(seconds=3.0, seed=0):
    """Noise bursts, a harmonic tone with vibrato and silence, float32 in [-1, 1)."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * 16000)) / 16000
    tone = 0.3 * np.sin(2 * np.pi * (180 + 20 * np.sin(2 * np.pi * 4 * t)) * t)
    tone += 0.15 * np.sin(2 * np.pi * 540 * t)
    signal = np.where((t % 1.0) < 0.6, tone, 0.0) + 0.02 * rng.standard_normal(len(t)) * ((t % 1.0) > 0.8)
    return signal.astype(np.float32)


@unittest.skipUnless(HAVE_ORT and MODEL.is_file(), f'needs onnxruntime and the local model at {MODEL}')
class SileroOnnxTests(unittest.TestCase):
    def setUp(self):
        from services.inference.silero_vad import SileroVad
        self.vad = SileroVad(MODEL)

    def test_matches_the_reference_wrapper_frame_by_frame(self):
        try:
            import torch
            from silero_vad.utils_vad import OnnxWrapper
        except ImportError as error:
            self.skipTest(f'reference wrapper unavailable: {error}')
        reference = OnnxWrapper(str(MODEL), force_onnx_cpu=True)
        audio = synthetic()
        frames = audio[:len(audio) // 512 * 512].reshape(-1, 512)
        ours = np.array([self.vad(f) for f in frames])
        theirs = np.array([float(reference(torch.from_numpy(f.copy()), 16000)) for f in frames])
        np.testing.assert_allclose(ours, theirs, atol=1e-5)
        self.assertGreater(ours.max() - ours.min(), 0.0)  # not a constant output

    def test_digital_silence_is_not_speech_and_reset_reproduces_the_sequence(self):
        silence = [self.vad(np.zeros(512, np.float32)) for _ in range(30)]
        self.assertLess(max(silence), 0.5)
        audio = synthetic(seed=1)
        frames = audio[:len(audio) // 512 * 512].reshape(-1, 512)
        self.vad.reset()
        first = [self.vad(f) for f in frames]
        self.vad.reset()
        second = [self.vad(f) for f in frames]
        self.assertEqual(first, second)

    def test_speech_windower_reports_no_speech_for_silence(self):
        from services.scorer.speech_windows import SpeechWindower
        windower = SpeechWindower(self.vad)
        windows = windower.feed(np.zeros(5 * 16000, np.int16)) + windower.finish()
        self.assertEqual(windows, [])
        self.assertTrue(windower.no_speech)


if __name__ == '__main__':
    unittest.main()
