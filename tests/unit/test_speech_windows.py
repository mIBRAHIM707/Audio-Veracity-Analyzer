"""FR-I3 VAD-gated speech windows (backlog 3.2 partial).

Claude-authored test design, 14 November 2026. Synthetic int16 signals and scripted
fake VADs only; no Silero model. Infrastructure evidence, not VAD accuracy.
"""
import unittest

import numpy as np

from services.inference.analysis import RATE, plan_windows
from services.scorer.speech_windows import FRAME_SAMPLES, MAX_FEED_SAMPLES, SpeechWindower, VadFailure


def nonzero_vad(frame):
    """Speech exactly when the frame has any nonzero sample."""
    return 1.0 if np.any(frame) else 0.0


def signal(n, seed=0):
    rng = np.random.default_rng(seed)
    values = rng.integers(-20000, 20000, n).astype(np.int16)
    values[values == 0] = 1  # keep every 'speech' frame nonzero
    return values


def run(windower, pcm, chunks):
    out, position = [], 0
    for size in chunks:
        out += windower.feed(pcm[position:position + size])
        position += size
    assert position >= len(pcm)
    return out + windower.finish()


def chunked(total, seed):
    rng = np.random.default_rng(seed)
    sizes = []
    while sum(sizes) < total:
        sizes.append(int(rng.integers(1, 9000)))
    return sizes


class SpeechWindowTests(unittest.TestCase):
    def test_all_speech_matches_the_file_mode_window_plan_for_any_chunking(self):
        for seconds in (0.5, 1.4, 1.5, 2.0, 3.0, 4.0, 5.4, 6.0, 7.6, 10.03):
            total = round(seconds * RATE)
            pcm = signal(total, seed=total)
            largest = [MAX_FEED_SAMPLES] * (total // MAX_FEED_SAMPLES) + [total % MAX_FEED_SAMPLES]
            for chunks in (largest, chunked(total, 1), chunked(total, 2)):
                with self.subTest(seconds=seconds, feeds=len(chunks)):
                    windows = run(SpeechWindower(lambda f: 0.9), pcm, chunks)
                    self.assertEqual([(w.index, w.speech_start, w.speech_end, w.partial) for w in windows],
                                     [(w.index, w.start_sample, w.end_sample, w.partial)
                                      for w in plan_windows(total)])
                    for w in windows:
                        expected = np.zeros(64000, np.float32)
                        expected[:w.speech_end - w.speech_start] = pcm[w.speech_start:w.speech_end] / 32768
                        np.testing.assert_array_equal(w.pcm, expected)
                        self.assertEqual(w.pcm.dtype, np.float32)
                        self.assertEqual((w.t_start, w.t_end), (w.speech_start / RATE, w.speech_end / RATE))
                        self.assertEqual(w.scorable, w.speech_end - w.speech_start >= 2.5 * RATE)

    def test_non_speech_frames_are_excluded_but_advance_time(self):
        # 3 s speech, 1 s silence, 2 s speech, 0.5 s silence, 1.5 s speech (frame aligned).
        f = FRAME_SAMPLES
        parts = [signal(94 * f, 1), np.zeros(31 * f, np.int16), signal(63 * f, 2),
                 np.zeros(16 * f, np.int16), signal(47 * f, 3)]
        pcm = np.concatenate(parts)
        speech = np.concatenate([p for p in parts if p.any()])
        windower = SpeechWindower(nonzero_vad)
        windows = run(windower, pcm, chunked(len(pcm), 4))
        self.assertEqual([(w.speech_start, w.speech_end, w.partial) for w in windows],
                         [(w.start_sample, w.end_sample, w.partial) for w in plan_windows(len(speech))])
        first = windows[0]  # speech 0..64000 spans the 1 s gap
        np.testing.assert_array_equal(first.pcm, speech[:64000] / np.float32(32768))
        self.assertEqual(first.t_start, 0.0)
        self.assertEqual(first.t_end, (64000 + 31 * f) / RATE)
        second = windows[1]  # speech 32000..96000 starts before and ends after the second gap
        self.assertEqual(second.t_start, 32000 / RATE)
        self.assertEqual(second.t_end, (96000 + 31 * f + 16 * f) / RATE)
        self.assertEqual(windower.speech_seconds, len(speech) / RATE)
        self.assertEqual(windower.stream_seconds, len(pcm) / RATE)
        self.assertEqual(windower.seconds_since_speech, 0.0)

    def test_short_calls_partial_scorability_and_no_speech(self):
        cases = {1.4: [], 1.5: [(True, False)], 2.0: [(True, False)], 2.5: [(True, True)],
                 3.0: [(True, True)]}
        for seconds, expected in cases.items():
            with self.subTest(seconds=seconds):
                windows = run(SpeechWindower(lambda f: 1.0), signal(round(seconds * RATE)), [round(seconds * RATE)])
                self.assertEqual([(w.partial, w.scorable) for w in windows], expected)
        silent = SpeechWindower(nonzero_vad)
        self.assertEqual(run(silent, np.zeros(5 * RATE, np.int16), [5 * RATE]), [])
        self.assertTrue(silent.no_speech)
        self.assertIsNone(silent.seconds_since_speech)
        self.assertEqual((silent.speech_seconds, silent.stream_seconds), (0.0, 5.0))

    def test_seconds_since_speech_counts_trailing_non_speech_including_carry(self):
        windower = SpeechWindower(nonzero_vad)
        windower.feed(signal(10 * FRAME_SAMPLES))
        self.assertFalse(windower.no_speech)
        windower.feed(np.zeros(RATE + 100, np.int16))  # 100 samples stay in the carry
        self.assertAlmostEqual(windower.seconds_since_speech, (RATE + 100) / RATE)

    def test_threshold_boundary(self):
        for p, speech in ((0.5, True), (0.4999, False), (0.0, False), (1.0, True)):
            with self.subTest(p=p):
                windower = SpeechWindower(lambda f, p=p: p)
                windower.feed(signal(FRAME_SAMPLES))
                self.assertEqual(windower.no_speech, not speech)
        custom = SpeechWindower(lambda f: 0.6, threshold=0.7)
        custom.feed(signal(FRAME_SAMPLES))
        self.assertTrue(custom.no_speech)

    def test_vad_fault_drops_the_audio_resets_the_vad_and_never_retries(self):
        class StatefulVad:
            """Counts frames like a recurrent VAD; fails on a chosen frame number."""
            def __init__(self):
                self.frames, self.resets, self.fail_at, self.bad = 0, 0, None, None

            def __call__(self, frame):
                self.frames += 1
                if self.fail_at is not None and self.frames == self.fail_at:
                    if isinstance(self.bad, Exception):
                        raise self.bad
                    return self.bad
                return 1.0

            def reset(self):
                self.frames, self.resets = 0, self.resets + 1

        chunk = 20 * FRAME_SAMPLES + 77  # 77 samples stay in the carry
        for bad in (float('nan'), 1.01, -0.01, 'x', None, float('inf'), RuntimeError('vad down')):
            with self.subTest(bad=bad):
                vad = StatefulVad()
                windower = SpeechWindower(vad)
                windower.feed(signal(chunk, 1))
                speech_before = windower.speech_seconds
                vad.fail_at, vad.bad = vad.frames + 5, bad  # the 5th frame of the next feed fails
                with self.assertRaises(VadFailure):
                    windower.feed(signal(chunk, 2))
                self.assertEqual(windower.speech_seconds, speech_before)  # nothing kept
                self.assertEqual(windower.stream_seconds, (2 * chunk) / RATE)  # time advanced
                self.assertEqual((windower.vad_failures, windower.dropped_samples, vad.resets),
                                 (1, 20 * FRAME_SAMPLES, 1))
                self.assertEqual(vad.frames, 0)  # state reset, not left mid-chunk
                vad.fail_at = None
                windower.feed(signal(chunk, 3))  # later audio is accepted again
                self.assertGreater(windower.speech_seconds, speech_before)

    def test_vad_fault_at_finish_drops_only_the_last_part_frame(self):
        calls = {'n': 0}

        def vad(frame):
            calls['n'] += 1
            if calls['n'] == 95:  # the zero-padded part-frame at finish
                raise RuntimeError('vad down')
            return 1.0
        windower = SpeechWindower(vad)
        windower.feed(signal(94 * FRAME_SAMPLES + 300))
        windows = windower.finish()  # does not raise
        self.assertEqual([(w.partial, w.speech_end) for w in windows], [(True, 94 * FRAME_SAMPLES)])
        self.assertEqual((windower.vad_failures, windower.dropped_samples), (1, 300))
        self.assertEqual(windower.stream_seconds, (94 * FRAME_SAMPLES + 300) / RATE)

    def test_partial_window_across_a_gap_ending_in_a_part_frame(self):
        f = FRAME_SAMPLES
        parts = [signal(40 * f, 5), np.zeros(10 * f, np.int16), signal(30 * f + 200, 6)]
        pcm = np.concatenate(parts)
        windows = run(SpeechWindower(nonzero_vad), pcm, chunked(len(pcm), 7))
        self.assertEqual(len(windows), 1)
        w = windows[0]
        # 70 frames + 200 samples = 2.25 s of speech: emitted as partial, below 2.5 s.
        self.assertEqual((w.partial, w.scorable, w.speech_start, w.speech_end), (True, False, 0, 70 * f + 200))
        self.assertEqual((w.t_start, w.t_end), (0.0, len(pcm) / RATE))
        expected = np.zeros(64000, np.float32)
        expected[:70 * f + 200] = np.concatenate([parts[0], parts[2]]) / np.float32(32768)
        np.testing.assert_array_equal(w.pcm, expected)

    def test_vad_sees_float_frames_and_the_zero_padded_last_frame(self):
        seen = []

        def vad(frame):
            seen.append(frame.copy())
            return 1.0
        windower = SpeechWindower(vad)
        pcm = signal(FRAME_SAMPLES + 10)
        windower.feed(pcm)
        windower.finish()
        self.assertEqual([(f.dtype, f.shape) for f in seen], [(np.float32, (FRAME_SAMPLES,))] * 2)
        np.testing.assert_array_equal(seen[0], pcm[:FRAME_SAMPLES] / np.float32(32768))
        np.testing.assert_array_equal(seen[1][:10], pcm[FRAME_SAMPLES:] / np.float32(32768))
        np.testing.assert_array_equal(seen[1][10:], 0)
        self.assertEqual(windower.speech_seconds, (FRAME_SAMPLES + 10) / RATE)

    def test_invalid_input_and_lifecycle(self):
        for bad in (np.zeros(10, np.float32), np.zeros((2, 2), np.int16), [0, 1],
                    np.zeros(MAX_FEED_SAMPLES + 1, np.int16)):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(ValueError):
                    SpeechWindower(lambda f: 1.0).feed(bad)
        for threshold in (0, 1, -0.1, True, float('nan'), '0.5'):
            with self.subTest(threshold=threshold):
                with self.assertRaises(ValueError):
                    SpeechWindower(lambda f: 1.0, threshold=threshold)
        with self.assertRaises(ValueError):
            SpeechWindower(None)
        windower = SpeechWindower(lambda f: 1.0)
        self.assertEqual(windower.feed(np.zeros(0, np.int16)), [])
        windower.finish()
        with self.assertRaises(ValueError):
            windower.feed(signal(10))
        with self.assertRaises(ValueError):
            windower.finish()

    def test_long_stream_keeps_bounded_buffers(self):
        windower = SpeechWindower(nonzero_vad)
        count = 0
        for i in range(60):  # 10 minutes in 10 s feeds, with silence every third feed
            pcm = signal(MAX_FEED_SAMPLES, seed=i) if i % 3 else np.zeros(MAX_FEED_SAMPLES, np.int16)
            count += len(windower.feed(pcm))
            self.assertLessEqual(len(windower._buffer), 64000 + FRAME_SAMPLES)
            self.assertLessEqual(len(windower._segments), 64000 // FRAME_SAMPLES + 2)
        speech = 40 * MAX_FEED_SAMPLES
        self.assertEqual(count, (speech - 64000) // 32000 + 1)


if __name__ == '__main__':
    unittest.main()
