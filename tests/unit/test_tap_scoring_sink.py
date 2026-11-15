"""Claude-authored tests (15 November 2026) for services/tap/scoring_sink.py (backlog
6.QA/8.1 wiring; PRD FR-T2, FR-I2, FR-I3, FR-I11; train/serve conditioning parity).
Authored by Claude (Claude Code), not by GPT-6 Astra. Real LiveCalls, SpeechFeed,
LivePipeline and EventHub with the fake VAD and detector of test_speech_feed.
"""
from types import SimpleNamespace
import unittest

import numpy as np

from data.prepare.conditioning import canonicalize
from services.tap.scoring_sink import TapScoringSink
from tests.unit.test_speech_feed import make, scores


class RecordingFeed:
    def __init__(self):
        self.pcm, self.flags, self.finished, self.ended = [], [], [], []

    def audio(self, call_id, direction, pcm, now, concealed=None):
        assert direction == 'caller' and pcm.dtype == np.int16
        self.pcm.append(pcm.copy())
        self.flags.append(concealed.copy())

    def finish(self, call_id, now):
        self.finished.append(call_id)

    def end_call(self, call_id, reason):
        if getattr(self, 'end_fails', 0):
            self.end_fails -= 1
            raise ValueError('concurrent human_agent_answered')
        self.ended.append((call_id, reason))
        self.live.active.discard(call_id) if hasattr(self, 'live') else None


class Live:
    def __init__(self, admit=True):
        self.admit, self.started, self.active, self.forced = admit, [], set(), []

    def start(self, call_id, **data):
        self.started.append((call_id, data))
        if self.admit:
            self.active.add(call_id)
        return self.admit

    def is_active(self, call_id):
        return call_id in self.active

    def scored_directions(self, call_id):
        return ('caller',)

    def end(self, call_id, reason, *, speech_seconds):
        self.forced.append((call_id, reason, speech_seconds))
        self.active.discard(call_id)
        return []


class IdlePipeline:
    def __init__(self):
        self.busy = False

    def schedule(self, now):
        return 0

    def step(self, now):
        return 0

    def has_work(self, call_id):
        return self.busy


def call(call_id='c1'):
    return SimpleNamespace(call_id=call_id, caller='ava-1', dialled='700', codec='ulaw')


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 0.001
        return self.t


class ConditioningParityTests(unittest.TestCase):
    def test_streamed_16k_equals_file_mode_canonicalize_and_flags_align(self):
        rng = np.random.default_rng(3)
        audio8k = np.rint(rng.normal(0, 4000, 8000 * 3 + 37)).astype('<i2')
        feed, pipeline = RecordingFeed(), IdlePipeline()
        sink = TapScoringSink(Live(), feed, pipeline, Clock())
        sink.started(call())
        concealed_frames = {7, 8, 40}
        sizes = [160, 37, 1000, 160 * 5 + 3, 999, 2]  # odd and frame-misaligned chunk sizes
        offset, turn = 0, 0
        while offset < len(audio8k):
            end = min(len(audio8k), offset + sizes[turn % len(sizes)])
            pairs, start = [], offset
            while start < end:  # split each chunk at 20 ms frame boundaries
                stop = min(end, (start // 160 + 1) * 160)
                pairs.append((stop - start, stop - start if start // 160 in concealed_frames else 0))
                start = stop
            sink.audio('c1', audio8k[offset:end].tobytes(), tuple(pairs))
            offset, turn = end, turn + 1
        sink.ended('c1', 'bye', {})
        streamed = np.concatenate(feed.pcm)
        expected, _ = canonicalize(audio8k.tobytes(), 8000, 1)
        self.assertEqual(streamed.tobytes(), expected, 'bit-identical to the file path conditioning')
        flags = np.concatenate(feed.flags)
        self.assertEqual(len(flags), len(streamed))
        want = np.zeros(len(streamed), bool)
        for frame in concealed_frames:
            want[2 * 160 * frame:2 * 160 * (frame + 1)] = True
        self.assertTrue(np.array_equal(flags, want[:len(flags)]))
        self.assertEqual(feed.finished, ['c1'])

    def sink(self, live=None, **options):
        self.feed, self.pipeline, self.clock = RecordingFeed(), IdlePipeline(), Clock()
        self.live = live or Live()
        self.feed.live = self.live
        return TapScoringSink(self.live, self.feed, self.pipeline, self.clock, **options)

    def test_refused_start_end_grace_and_errors(self):
        sink = self.sink(Live(admit=False))
        sink.started(call('r'))
        sink.audio('r', bytes(320), ((160, 0),))
        self.assertEqual((self.feed.pcm, sink.counts['refused'], sink.counts['audio_unfed']), ([], 1, 1))
        sink = self.sink(end_grace_s=1.0)
        sink.started(call('g'))
        sink.audio('g', bytes(3200), ((160, 0),))  # mismatched concealment: counted, treated as none
        self.assertEqual(sink.counts['errors'], 1)
        self.pipeline.busy = True
        sink.ended('g', 'timeout', {})
        self.clock.t += 0.5
        sink.pump()
        self.assertEqual(self.feed.ended, [])  # still scoring its tail
        self.clock.t += 2.0
        sink.pump()
        self.assertEqual(self.feed.ended, [('g', 'timeout')])
        self.assertEqual(sink.counts['end_cancelled_tail'], 1)
        sink.ended('unknown', 'bye', {})  # never started: ignored
        self.assertEqual(sink.counts['ended'], 1)

    def test_retap_before_the_old_end_completed_starts_a_fresh_session(self):
        # Review M1: close() then resume() re-taps the same call with no pump between.
        sink = self.sink()
        sink.started(call('m1'))
        sink.ended('m1', 'monitor_lost', {})
        sink.started(call('m1'))
        self.assertEqual(self.feed.ended, [('m1', 'monitor_lost')])
        self.assertEqual([c for c, _ in self.live.started], ['m1', 'm1'])
        sink.audio('m1', bytes(3200), ((1600, 0),))
        self.assertEqual(sink.counts['audio_unfed'], 0)  # the new session takes the audio
        self.assertNotIn('m1', sink._ending)

    def test_failed_end_is_retried_then_forced_at_the_deadline(self):
        # Review M2: a ValueError from end_call must not leak the call.
        sink = self.sink(end_grace_s=1.0)
        sink.started(call('m2'))
        self.feed.end_fails = 5
        sink.ended('m2', 'bye', {})
        sink.pump()
        self.assertEqual((sink.counts['end_retries'], self.live.forced), (1, []))
        self.clock.t += 2.0
        sink.pump()
        self.assertEqual(self.live.forced, [('m2', 'bye', {'caller': 0.0})])
        self.assertEqual((sink.counts['ends_forced'], self.live.is_active('m2')), (1, False))

    def test_pump_contains_pipeline_faults_and_calls_ended_elsewhere(self):
        # Review M3 and L3.
        sink = self.sink()

        def boom(now):
            raise ValueError('now went backwards')
        self.pipeline.schedule = boom
        sink.started(call('m3'))
        self.live.active.discard('m3')  # LiveCalls.end_all ended it
        sink.ended('m3', 'bye', {})
        self.assertEqual(sink.pump(), 0)
        self.assertEqual((sink.counts['pump_errors'], sink.counts['ended_elsewhere'], self.feed.ended),
                         (1, 1, []))

        class FailingHub:
            def publish(self, *args):
                raise RuntimeError('hub down')
        sink = self.sink(hub=FailingHub())
        sink.started(call('h'))
        sink.quality('h', 'degraded')
        self.assertEqual(sink.counts['health_failures'], 1)


class FlowTests(unittest.TestCase):
    def test_tapped_audio_is_scored_and_the_call_ends_with_the_tap(self):
        feed, pipeline, live, sub, _ = make()
        clock = Clock()
        sink = TapScoringSink(live, feed, pipeline, clock)
        sink.started(call('f1'))
        tone8k = np.full(160, round(0.9 * 32767), '<i2').tobytes()
        for k in range(8 * 50):  # 8 s in 20 ms frames; no loss for 5 s, then 2 frames in 10
            lost = k >= 250 and k % 10 in (3, 4)
            sink.audio('f1', tone8k, ((160, 160 if lost else 0),))
            if k % 25 == 0:
                for _ in range(3):
                    clock.t += 0.2
                    sink.pump()
        sink.ended('f1', 'bye', {})
        for _ in range(30):
            clock.t += 0.2
            sink.pump()
        events = scores(sub)
        types = [e.type for e in events]
        self.assertEqual(types[0], 'call.started')
        self.assertEqual((events[0].data['from_number'], events[0].data['to']), ('ava-1', '700'))
        updates = [e.data for e in events if e.type == 'score.updated']
        # The first window (0-4 s) is clean and scored; a window over the lossy last 3 s
        # (20 % concealed) is gated: the concealment mask changes scoring.
        self.assertTrue(updates[0]['scored'] and updates[0]['quality'] == 'ok')
        gated = [u for u in updates if not u['scored']]
        self.assertTrue(gated and gated[0]['quality'] == 'low' and gated[0]['concealed_fraction'] > 0.1, updates)
        self.assertEqual(types[-1], 'call.ended')
        self.assertEqual(events[-1].data['reason'], 'bye')
        self.assertAlmostEqual(events[-1].data['speech_seconds'], 8.0, delta=0.1)
        self.assertEqual(sink.counts['ended'], 1)

    def test_quality_change_is_published_as_system_health(self):
        from services.events.fanout import EventHub
        hub = EventHub()
        listener = hub.subscribe()
        sink = TapScoringSink(Live(), RecordingFeed(), IdlePipeline(), Clock(), hub=hub)
        sink.quality('c8', 'degraded')  # not a call this sink scores: nothing published
        sink.started(call('c9'))
        sink.quality('c9', 'degraded')
        (envelope,), _ = listener.drain()
        self.assertEqual((envelope.type, envelope.call_id, envelope.data),
                         ('system.health', 'c9', {'reason': 'media_quality', 'media_state': 'degraded'}))


if __name__ == '__main__':
    unittest.main()
