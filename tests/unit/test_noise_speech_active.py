"""Claude-authored tests (15 November 2026) for the speech-active SNR basis in
data/prepare/noise.py (backlog 3.4; PRD FR-D7 "additive noise at 0 to 20 dB SNR").
Authored by Claude (Claude Code), not by GPT-6 Astra. Pure: fake VADs, synthetic
signals; no model or corpus.
"""
import unittest

import numpy as np

from data.prepare import noise
from services.scorer.speech_windows import SpeechWindower

RATE = 16000


def tone_then_silence(seconds_on=1.0, seconds_off=1.0, amplitude=8000):
    t = np.arange(round(seconds_on * RATE)) / RATE
    on = np.rint(amplitude * np.sin(2 * np.pi * 440 * t))
    return np.concatenate([on, np.zeros(round(seconds_off * RATE))]).astype('<i2').tobytes()


def white(seconds=3.0, seed=1):
    rng = np.random.default_rng(seed)
    return np.rint(rng.normal(0, 1000, round(seconds * RATE))).astype('<i2').tobytes()


class EnergyVad:
    """Fake VAD: probability 1 when the frame's RMS (float scale) exceeds 0.01."""

    def __init__(self):
        self.calls, self.resets = 0, 0

    def reset(self):
        self.resets += 1

    def __call__(self, frame):
        assert frame.dtype == np.float32 and frame.shape == (512,)
        self.calls += 1
        return 1.0 if float(np.sqrt(np.mean(frame ** 2))) > 0.01 else 0.0


class ScriptedVad:
    """Records every frame and answers from a script (cycled): 0.5 exactly counts."""

    def __init__(self, script):
        self.script, self.frames = script, []

    def reset(self):
        pass

    def __call__(self, frame):
        self.frames.append(np.array(frame, copy=True))
        return self.script[(len(self.frames) - 1) % len(self.script)]


class SpeechActiveSnrTests(unittest.TestCase):
    def test_known_answer_half_active_clip(self):
        speech = tone_then_silence()
        mask = np.zeros(2 * RATE, dtype=bool)
        mask[:RATE] = True
        _, whole = noise.mix_at_snr(speech, white(), 10.0, 3)
        _, active = noise.mix_at_snr(speech, white(), 10.0, 3, active_mask=mask,
                                     mask_source={'vad': 'test', 'threshold': 0.5})
        # Same noise excerpt; the active speech power is twice the whole-clip power, so
        # the noise gain is sqrt(2) larger for the same target SNR.
        self.assertEqual(whole['noise_offset'], active['noise_offset'])
        self.assertAlmostEqual(active['noise_gain'] / whole['noise_gain'], np.sqrt(2), places=9)
        self.assertEqual((whole['snr_basis'], active['snr_basis']),
                         ('whole_clip_mean_square', 'speech_active_mean_square'))
        self.assertAlmostEqual(active['achieved_snr_db'], 10.0, delta=0.05)
        self.assertEqual((active['active_fraction'], active['active_samples']), (0.5, RATE))
        self.assertEqual(len(active['active_mask_sha256']), 64)
        self.assertEqual(active['mask_source'], {'vad': 'test', 'threshold': 0.5})
        for key in ('active_fraction', 'active_samples', 'active_mask_sha256', 'mask_source'):
            self.assertNotIn(key, whole, 'records without a mask are unchanged')

    def test_planned_mix_passes_the_mask(self):
        speech = tone_then_silence()
        mask, _ = noise.speech_mask(speech, EnergyVad())
        plan = noise.derive_noise(5, 'clip-a')
        _, record = noise.mix_planned(speech, white(), plan, active_mask=mask)
        self.assertEqual((record['snr_basis'], record['clip_id'], record['snr_db']),
                         ('speech_active_mean_square', 'clip-a', plan['snr_db']))

    def test_mask_uses_the_serving_frame_rule_frame_for_frame(self):
        # Review 5: same frames (scaling, padding of a NON-silent last part-frame) and the
        # same decisions as the serving windower, with a probability of exactly 0.5.
        rng = np.random.default_rng(4)
        speech = np.rint(rng.normal(0, 3000, 512 * 7 + 300)).astype('<i2')  # last part-frame not silent
        script = [0.5, 0.49, 0.9, 0.0, 0.5, 0.51, 0.2, 0.5]
        ours, theirs = ScriptedVad(script), ScriptedVad(script)
        mask, decisions = noise.speech_mask(speech.tobytes(), ours)
        windower = SpeechWindower(theirs)
        windower.feed(speech)
        windower.finish()
        self.assertEqual(len(ours.frames), len(theirs.frames))
        for a, b in zip(ours.frames, theirs.frames):
            self.assertTrue(np.array_equal(a, b), 'identical float32 frames incl. the padded last one')
        expected = [p >= 0.5 for p in script]
        self.assertEqual(list(np.unpackbits(np.frombuffer(bytes.fromhex(decisions), np.uint8))[:8].astype(bool)),
                         expected)
        self.assertEqual([bool(mask[i * 512]) for i in range(8)], expected)
        self.assertEqual(mask.shape, (len(speech),))
        self.assertEqual(windower.speech_seconds, mask.sum() / RATE)

    def test_refusals_and_vad_faults(self):
        speech = tone_then_silence()
        good = np.ones(2 * RATE, dtype=bool)
        cases = [{'active_mask': good[:-1]}, {'active_mask': good.astype(int)},
                 {'active_mask': good, 'mask_source': 'silero'}, {'mask_source': {'vad': 'x'}}]
        for kwargs in cases:
            with self.assertRaises(ValueError):
                noise.mix_at_snr(speech, white(), 5.0, 1, **kwargs)
        with self.assertRaises(noise.InsufficientSpeech) as caught:
            noise.mix_at_snr(speech, white(), 5.0, 1, active_mask=np.zeros(2 * RATE, dtype=bool))
        self.assertEqual(caught.exception.active_samples, 0)
        silent_active = np.zeros(2 * RATE, dtype=bool)
        silent_active[RATE:] = True  # only the silent half is "speech"
        with self.assertRaisesRegex(ValueError, 'silence'):
            noise.mix_at_snr(speech, white(), 5.0, 1, active_mask=silent_active)

        class Raises:
            def __call__(self, frame):
                raise RuntimeError('onnx runtime fault')
        for vad in (ScriptedVad([1.5]), ScriptedVad([float('nan')]), ScriptedVad([-0.1]), Raises()):
            with self.assertRaises(noise.VadError):
                noise.speech_mask(speech, vad)
        for bad in (b'', b'\x00', 'pcm'):
            with self.assertRaises(ValueError):
                noise.speech_mask(bad, EnergyVad())
        for threshold in (0, 1, 1.5, True):
            with self.assertRaises(ValueError):
                noise.speech_mask(speech, EnergyVad(), threshold=threshold)


if __name__ == '__main__':
    unittest.main()
