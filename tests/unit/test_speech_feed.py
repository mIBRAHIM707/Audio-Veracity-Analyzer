"""Live audio -> speech windows -> LivePipeline (backlog 3.2 integration; FR-I3, FR-U1).

Claude-authored test design, 14 November 2026. Real LiveCalls, CallGate, EventHub,
MicroBatcher and LivePipeline; a fake VAD (speech = any nonzero sample) and a fake
detector whose p_synthetic equals the window's mean absolute amplitude. Synthetic
constant signals only; infrastructure evidence, not detection or VAD accuracy.
"""
import unittest

import numpy as np

from services.events.fanout import EventHub
from services.inference.capacity import CallGate, MicroBatcher
from services.scorer.config import ScoringConfig
from services.scorer.live import LiveCalls
from services.scorer.pipeline import LivePipeline
from services.scorer.speech_feed import SpeechFeed

CONFIG = ScoringConfig(ema_alpha=1.0)
RATE = 16000


class Configs:
    def for_new_call(self):
        return CONFIG


class Detector:
    def __init__(self):
        self.rows = 0

    def __call__(self, batch):
        self.rows += len(batch)
        p = np.clip(np.abs(batch).sum(axis=1) / np.count_nonzero(batch, axis=1).clip(1), 0.01, 0.99)
        return np.stack([np.log(p / (1 - p)), np.zeros_like(p)], axis=1)


class AmplitudeVad:
    def __init__(self):
        self.fail, self.resets = False, 0

    def __call__(self, frame):
        if self.fail:
            raise RuntimeError('vad down')
        return 1.0 if np.any(frame) else 0.0

    def reset(self):
        self.resets += 1


def make():
    hub = EventHub()
    sub = hub.subscribe(buffer=4096)
    live = LiveCalls(Configs(), hub, CallGate(8), new_alert_id=iter(f'alert-{i}' for i in range(1, 99)).__next__)
    batcher = MicroBatcher(capacity_windows_per_s=10.0, collect_s=0.1, max_live=4, max_pending_live=256)
    pipeline = LivePipeline(live, batcher, Detector(), CONFIG)
    vads = []

    def factory():
        vads.append(AmplitudeVad())
        return vads[-1]
    return SpeechFeed(pipeline, factory), pipeline, live, sub, vads


def tone(seconds, value=0.9):
    return np.full(round(seconds * RATE), round(value * 32768), np.int16)


def drain(pipeline, now):
    for i in range(10):
        pipeline.schedule(now + 0.2 * i)
        pipeline.step(now + 0.2 * i + 0.1)


def scores(sub):
    envelopes, dropped = sub.drain()
    assert dropped == 0
    return envelopes


class SpeechFeedTests(unittest.TestCase):
    def test_speech_flows_to_scores_and_the_tail_is_scored_before_the_end(self):
        feed, pipeline, live, sub, _ = make()
        live.start('c')
        offered = 0
        for i in range(10):  # 7.6 s of speech; scoring keeps up (drained between chunks)
            offered += feed.audio('c', 'caller', tone(0.76), now=2.0 * i)
            drain(pipeline, 2.0 * i)
        self.assertEqual(offered, 2)  # windows ending at 4 s and 6 s of speech
        drain(pipeline, 20.0)
        self.assertEqual(feed.finish('c', now=22.0), 1)  # partial 4.0-7.6 s, 3.6 s of speech
        drain(pipeline, 22.0)
        ended = feed.end_call('c', 'bye')
        events = scores(sub)
        updates = [e.data for e in events if e.type == 'score.updated' and e.data['scored']]
        self.assertEqual(len(updates), 3)
        self.assertTrue(all(abs(u['score'] - 0.9) < 1e-3 for u in updates))
        self.assertEqual([e.type for e in ended], ['call.ended'])
        self.assertAlmostEqual(ended[0].data['speech_seconds'], 7.6)
        self.assertFalse(live.is_active('c'))
        self.assertEqual((feed.counts['offered'], feed.counts['stride_skipped']), (3, 0))

    def test_falling_behind_is_counted_as_stride_skips(self):
        feed, pipeline, live, _, _ = make()
        live.start('c')
        offered = sum(feed.audio('c', 'caller', tone(1.0), now=float(i)) for i in range(1, 11))  # never drained
        # Windows at 4, 6 and 8 s; the 10 s one waits for the last part-frame (9.984 s classified).
        self.assertEqual(offered + feed.counts['stride_skipped'], 3)
        self.assertGreater(feed.counts['stride_skipped'], 0)
        self.assertTrue(pipeline.snapshot()['directions']['c/caller']['degraded'])

    def test_ending_before_draining_cancels_the_tail(self):
        feed, pipeline, live, sub, _ = make()
        live.start('c')
        feed.audio('c', 'caller', tone(3.0), now=3.0)
        self.assertEqual(feed.finish('c', now=3.0), 1)
        feed.end_call('c', 'bye')
        drain(pipeline, 4.0)
        self.assertEqual([e for e in scores(sub) if e.type == 'score.updated' and e.data['scored']], [])
        self.assertEqual(pipeline.counts['cancelled_pending'], 1)

    def test_short_tail_is_not_scorable_and_not_offered(self):
        feed, pipeline, live, _, _ = make()
        live.start('c')
        feed.audio('c', 'caller', tone(2.0), now=2.0)
        self.assertEqual(feed.finish('c', now=2.0), 0)
        self.assertEqual(feed.counts['not_scorable'], 1)
        self.assertEqual(feed.finish('c', now=2.5), 0)  # a second finish is harmless

    def test_no_speech_is_published_once_and_rearmed_after_speech(self):
        feed, pipeline, live, sub, _ = make()
        live.start('c')
        silence = np.zeros(RATE, np.int16)
        for second in range(1, 8):
            feed.audio('c', 'caller', silence, now=float(second))
        states = [e.data for e in scores(sub) if e.type == 'score.updated']
        self.assertEqual([(s['state'], s['scored'], s['reason']) for s in states],
                         [('no_speech', False, 'vad_no_speech')])
        feed.audio('c', 'caller', tone(1.0), now=8.0)  # speech returns: re-armed
        for second in range(9, 14):
            feed.audio('c', 'caller', silence, now=float(second))
        states = [e.data for e in scores(sub) if e.type == 'score.updated']
        self.assertEqual([(s['state'], s['reason']) for s in states],
                         [('no_speech', 'vad_no_speech_for_4.0s')])
        self.assertEqual(feed.counts['no_speech_published'], 2)

    def test_no_speech_needs_every_fed_direction_quiet(self):
        feed, pipeline, live, sub, _ = make()
        live.start('c')
        live.human_agent_answered('c')
        for second in range(1, 7):
            feed.audio('c', 'caller', np.zeros(RATE, np.int16), now=float(second))
            feed.audio('c', 'agent', tone(1.0, 0.2), now=float(second))
        self.assertEqual([e for e in scores(sub) if e.type == 'score.updated' and e.data['state'] == 'no_speech'], [])

    def test_vad_fault_is_contained_and_reset(self):
        feed, pipeline, live, _, vads = make()
        live.start('c')
        feed.audio('c', 'caller', tone(1.0), now=1.0)
        vads[0].fail = True
        self.assertEqual(feed.audio('c', 'caller', tone(1.0), now=2.0), 0)
        self.assertEqual((feed.counts['vad_failures'], vads[0].resets), (1, 1))
        self.assertTrue(live.is_active('c'))
        vads[0].fail = False
        self.assertEqual(feed.audio('c', 'caller', tone(4.0), now=6.0), 1)

    def test_audio_for_inactive_calls_is_dropped_and_windowers_forgotten(self):
        feed, pipeline, live, _, vads = make()
        self.assertEqual(feed.audio('nobody', 'caller', tone(5.0), now=1.0), 0)
        self.assertEqual((feed.counts['audio_inactive'], len(vads)), (1, 0))
        live.start('c')
        feed.audio('c', 'caller', tone(1.0), now=1.0)
        feed.end_call('c', 'bye')
        self.assertEqual(feed.speech_seconds('c'), {})
        self.assertEqual(feed.audio('c', 'caller', tone(1.0), now=2.0), 0)
        self.assertEqual(feed.end_call('c', 'bye'), [])  # unknown now: nothing to end

    def test_end_call_gives_exactly_the_scored_directions(self):
        feed, pipeline, live, _, _ = make()
        live.start('c')
        feed.audio('c', 'caller', tone(1.0), now=1.0)
        feed.audio('c', 'agent', tone(2.0), now=1.0)  # agent not answered: not scored
        ended = feed.end_call('c', 'bye')
        # Without finish() the last part-frame (128 samples) is not yet classified.
        self.assertEqual(ended[0].data['speech_seconds'], 31 * 512 / RATE)
        live.start('d')
        live.human_agent_answered('d')  # agent scored but sent no audio
        feed.audio('d', 'caller', tone(1.5), now=1.0)
        feed.finish('d', now=1.0)
        ended = feed.end_call('d', 'bye')
        self.assertEqual(ended[0].data['speech_seconds'], 1.5)
        with self.assertRaises(ValueError):
            live.start('e')
            feed.audio('e', 'caller', tone(1.0), now=1.0)
            feed.end_call('e', 'not-a-reason')
        self.assertTrue(live.is_active('e'))
        self.assertEqual(feed.speech_seconds('e'), {'caller': 31 * 512 / RATE})  # kept for a valid retry

    def test_backwards_clock_loses_no_windows(self):
        feed, pipeline, live, sub, _ = make()
        live.start('c')
        feed.audio('c', 'caller', tone(3.0), now=5.0)
        pipeline.schedule(5.0)
        with self.assertRaises(ValueError):
            feed.audio('c', 'caller', tone(1.5), now=4.0)  # would complete a window
        with self.assertRaises(ValueError):
            feed.finish('c', now=4.0)
        self.assertEqual(feed.speech_seconds('c'), {'caller': 2.976})  # nothing consumed
        self.assertEqual(feed.audio('c', 'caller', tone(1.5), now=6.0), 1)  # retried with a valid clock
        self.assertEqual(feed.counts['offered'], 1)

    def test_late_audio_after_finish_is_dropped_and_counted(self):
        feed, pipeline, live, _, _ = make()
        live.start('c')
        feed.audio('c', 'caller', tone(2.0), now=1.0)
        feed.finish('c', now=2.0)
        self.assertEqual(feed.audio('c', 'caller', tone(1.0), now=3.0), 0)
        self.assertEqual(feed.counts['audio_after_finish'], 1)
        self.assertEqual(feed.finish('c', now=3.0), 0)

    def test_vad_factory_failures_are_contained_and_bounded(self):
        feed, pipeline, live, _, _ = make()
        calls = {'n': 0}

        def broken():
            calls['n'] += 1
            raise FileNotFoundError('silero_vad.onnx missing')
        feed._vad_factory = broken
        live.start('c')
        for second in range(1, 6):
            self.assertEqual(feed.audio('c', 'caller', tone(1.0), now=float(second)), 0)
        self.assertEqual((calls['n'], feed.counts['vad_create_failures'], feed.counts['vad_unavailable']),
                         (3, 3, 2))
        self.assertTrue(live.is_active('c'))
        self.assertEqual(feed.end_call('c', 'bye')[0].data['speech_seconds'], 0.0)

    def test_sweep_forgets_calls_ended_by_end_all_and_a_reused_id_starts_fresh(self):
        feed, pipeline, live, sub, _ = make()
        live.start('c')
        feed.audio('c', 'caller', tone(1.0), now=1.0)
        live.end_all('monitor_lost', speech_seconds_for=feed.speech_seconds_for)
        ended = [e.data for e in scores(sub) if e.type == 'call.ended']
        self.assertEqual([e['speech_seconds'] for e in ended], [31 * 512 / RATE])
        self.assertEqual(feed.sweep(), 1)
        self.assertEqual(feed.speech_seconds('c'), {})
        live.start('c')  # the same ID reused by a new call
        feed.audio('c', 'caller', tone(0.5), now=2.0)
        self.assertEqual(feed.speech_seconds('c'), {'caller': 15 * 512 / RATE})
        self.assertEqual(feed.sweep(), 0)

    def test_invalid_input_raises_and_changes_nothing(self):
        feed, pipeline, live, _, vads = make()
        live.start('c')
        with self.assertRaises(ValueError):
            feed.audio('c', 'listener', tone(1.0), now=1.0)
        for bad in (np.zeros(10, np.float32), np.zeros((2, 2), np.int16), np.zeros(10 * RATE + 1, np.int16)):
            with self.subTest(bad=bad.shape):
                with self.assertRaises(ValueError):
                    feed.audio('c', 'caller', bad, now=1.0)
        self.assertEqual((len(vads), feed.speech_seconds('c')), (0, {}))  # no windower created
        for bad in (0, -1, 601, True, 'x'):
            with self.subTest(no_speech_after_s=bad):
                with self.assertRaises(ValueError):
                    SpeechFeed(pipeline, AmplitudeVad, no_speech_after_s=bad)
        with self.assertRaises(ValueError):
            SpeechFeed(pipeline, None)


if __name__ == '__main__':
    unittest.main()
