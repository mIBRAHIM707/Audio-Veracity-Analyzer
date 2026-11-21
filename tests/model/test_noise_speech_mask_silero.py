"""Claude-authored test (15 November 2026): `data.prepare.noise.speech_mask` with the
real pinned Silero VAD (backlog 3.4; PRD FR-D7). Authored by Claude (Claude Code), not
by GPT-6 Astra. Needs onnxruntime and the local model (AVA_SILERO_VAD_MODEL, as in
test_silero_vad_onnx.py). Synthetic signal: checks plumbing and repeatability, not VAD
accuracy.
"""
import unittest

import numpy as np

from data.prepare import noise
from tests.model.test_silero_vad_onnx import HAVE_ORT, MODEL


@unittest.skipUnless(HAVE_ORT and MODEL.is_file(), f'needs onnxruntime and the local model at {MODEL}')
class RealSileroMaskTests(unittest.TestCase):
    def test_mask_is_repeatable_with_reset_and_records_the_runtime(self):
        from services.inference.silero_vad import SileroVad
        vad = SileroVad(MODEL)
        rng = np.random.default_rng(9)
        clip = np.rint(rng.normal(0, 2000, 16000 * 3 + 123)).astype('<i2').tobytes()
        first = noise.speech_mask(clip, vad)
        second = noise.speech_mask(clip, vad)  # reset per clip: same decisions again
        self.assertTrue(np.array_equal(first[0], second[0]))
        self.assertEqual(first[1], second[1])
        self.assertEqual(first[0].shape, (16000 * 3 + 123,))
        self.assertTrue(vad.runtime_version.startswith('onnxruntime '))
        silence = noise.speech_mask(bytes(2 * 16000), vad)[0]
        self.assertEqual(int(silence.sum()), 0, 'digital silence is not speech')


if __name__ == '__main__':
    unittest.main()
