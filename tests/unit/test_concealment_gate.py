"""Claude-authored tests (15 November 2026): receiver concealment travels through the
speech windower, SpeechFeed and LivePipeline to the FR-I2 quality gate (backlog 6.3;
PRD FR-I2 "a window with more than 10% concealed samples is not scored and the call
shows quality=low"; architecture §2.2 step 1). Authored by Claude (Claude Code), not by
GPT-6 Astra. Real windower/feed/pipeline/LiveCalls/EventHub; fake VAD and detector
from test_speech_feed; synthetic signals.
"""
import unittest

import numpy as np

from services.scorer.speech_windows import SpeechWindower
from tests.unit.test_speech_feed import AmplitudeVad, drain, make, scores, tone

RATE = 16000


class WindowerConcealmentTests(unittest.TestCase):
    def test_fraction_counts_concealed_speech_samples_of_each_window(self):
        windower = SpeechWindower(AmplitudeVad())
        audio = tone(6.0)
        concealed = np.zeros(len(audio), bool)
        concealed[:6400] = True                  # 0.4 s concealed at the start of the first window
        concealed[5 * RATE:5 * RATE + 3200] = True  # 0.2 s inside the second window only
        windows = []
        for start in range(0, len(audio), 3000):  # chunk sizes not aligned with frames
            windows += windower.feed(audio[start:start + 3000], concealed[start:start + 3000])
        windows += windower.finish()
        self.assertEqual([w.index for w in windows], [0, 1])  # 0-4 s and 2-6 s; no uncovered tail
        self.assertAlmostEqual(windows[0].concealed_fraction, 6400 / (4 * RATE))
        self.assertAlmostEqual(windows[1].concealed_fraction, 3200 / (4 * RATE))  # 2 s - 6 s

    def test_non_speech_concealment_is_ignored_and_default_is_none(self):
        windower = SpeechWindower(AmplitudeVad())
        silence = 32 * 512  # whole VAD frames, so no frame mixes silence and speech
        audio = np.concatenate([np.zeros(silence, np.int16), tone(4.0)])
        concealed = np.zeros(len(audio), bool)
        concealed[:silence] = True  # concealed silence: excluded with the non-speech frames
        windows = windower.feed(audio, concealed)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].concealed_fraction, 0.0)
        self.assertEqual(SpeechWindower(AmplitudeVad()).feed(tone(4.0))[0].concealed_fraction, 0.0)

    def test_long_outage_dropped_as_non_speech_still_counts(self):
        # Review finding 1: 60 ms of repeats then 400 ms of concealed zeros (the VAD drops
        # them) inside the first window.
        windower = SpeechWindower(AmplitudeVad())
        speech, gap = tone(1.0), np.zeros(16 * 400, np.int16)
        audio = np.concatenate([speech, tone(0.06), gap, tone(3.5)])
        concealed = np.zeros(len(audio), bool)
        concealed[RATE:RATE + 960 + len(gap)] = True
        windows = windower.feed(audio, concealed)
        self.assertEqual(windows[0].index, 0)
        # Without the dropped samples the share would be 960 / 64000 = 1.5 %; with them it
        # is about (960 + 6400) / (64000 + 6400) = 10.5 % (the gap's edge frames are
        # classified by the VAD, so allow one 512-sample frame either way).
        self.assertGreater(windows[0].concealed_fraction, 0.10, 'a 460 ms outage gates the window')
        f = windows[0].concealed_fraction  # f = (960 + q) / (64000 + q)  =>  q = (64000 f - 960) / (1 - f)
        recovered = (4 * RATE * f - 960) / (1 - f)
        self.assertAlmostEqual(recovered, len(gap), delta=1024)

    def test_burst_straddling_a_window_boundary_splits_exactly(self):
        # A constant shift of the flags would move samples across the boundary at 4 s.
        windower = SpeechWindower(AmplitudeVad())
        audio = tone(8.0)
        concealed = np.zeros(len(audio), bool)
        concealed[4 * RATE - 1000:4 * RATE + 3000] = True  # 1000 before, 3000 after the 4 s mark
        windows = windower.feed(audio, concealed)
        self.assertEqual([w.index for w in windows], [0, 1, 2])
        self.assertEqual(windows[0].concealed_fraction, 1000 / (4 * RATE))   # 0-4 s
        self.assertEqual(windows[1].concealed_fraction, 4000 / (4 * RATE))   # 2-6 s
        self.assertEqual(windows[2].concealed_fraction, 3000 / (4 * RATE))   # 4-8 s

    def test_partial_tail_uses_its_speech_length_and_the_carry_flags(self):
        windower = SpeechWindower(AmplitudeVad())
        audio = tone(4.0 + 1.0 + 0.7 + 100 / RATE)  # ends with a 100-sample part-frame
        concealed = np.zeros(len(audio), bool)
        concealed[-100:] = True  # only in the part-frame kept for finish()
        windows = windower.feed(audio, concealed) + windower.finish()
        tail = windows[-1]
        self.assertTrue(tail.partial)
        self.assertEqual(tail.concealed_fraction, 100 / (tail.speech_end - tail.speech_start))

    def test_refusals_change_nothing(self):
        windower = SpeechWindower(AmplitudeVad())
        for bad in (np.zeros(10, bool), np.zeros(1600, np.int8), [False] * 1600):
            with self.assertRaises(ValueError):
                windower.feed(tone(0.1), bad)
        self.assertEqual(windower.stream_seconds, 0.0)


class GateTests(unittest.TestCase):
    def test_a_window_over_ten_percent_concealed_is_not_scored_and_quality_turns_low(self):
        feed, pipeline, live, sub, _ = make()
        live.start('c')
        audio = tone(8.0)
        concealed = np.zeros(len(audio), bool)
        # Windows: 0-4 s, 2-6 s, 4-8 s. 0.5 s concealed at 6.5-7 s lies in the last one
        # only: 8000 / 64000 = 12.5 % > 10 %.
        concealed[int(6.5 * RATE):int(6.5 * RATE) + 8000] = True
        for i in range(8):
            span = slice(i * RATE, (i + 1) * RATE)
            feed.audio('c', 'caller', audio[span], now=2.0 * i, concealed=concealed[span])
            drain(pipeline, 2.0 * i)
        drain(pipeline, 20.0)
        updates = [e.data for e in scores(sub) if e.type == 'score.updated']
        self.assertEqual([u['scored'] for u in updates], [True, True, False], updates)
        gated = updates[2]  # gated: concealed share above 10 %
        self.assertEqual((gated['quality'], gated['concealed_fraction'], gated['score']), ('low', 0.125, None))
        self.assertEqual([u['quality'] for u in updates[:2]], ['ok', 'ok'])
        self.assertEqual(pipeline._concealed, {}, 'no per-window concealment left behind')

    def test_offer_refuses_an_invalid_fraction(self):
        _, pipeline, live, _, _ = make()
        live.start('c')
        window = np.zeros(64000, np.float32)
        for bad in (-0.1, 1.1, float('nan'), True, '0.2'):
            with self.assertRaises(ValueError):
                pipeline.offer('c', 'caller', 0, window, 0.0, concealed_fraction=bad)
        self.assertIsNotNone(pipeline.offer('c', 'caller', 0, window, 0.0, concealed_fraction=0.05))


if __name__ == '__main__':
    unittest.main()
