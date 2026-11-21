"""Claude-authored tests (17 November 2026) for the tap-side stage timing of
services/tap/scoring_sink.py and its rendering on the live /metrics
(services/live/health.py): PRD FR-O6 "latency per stage", backlog 7.2.

Authored by Claude (Claude Code), not by GPT-6 Astra. A scripted timer gives
binary-exact readings (multiples of 2**-14 s), so every expected duration and bucket is
worked by hand; the fake resampler and feed record what they received, so the timing is
shown never to change the audio.
"""
import math
import threading
import unittest

import numpy as np

from services.live.health import HealthServer
from services.scorer.metrics_text import histogram_lines
from services.tap.scoring_sink import TAP_STAGE_BUCKETS_S, TAP_STAGES, StageTimes, TapScoringSink
from tests.unit.test_live_health import ManualClock, make_service, samples
from tests.unit.test_tap_scoring_sink import IdlePipeline, Live, RecordingFeed, call

TICK = 2.0 ** -14  # 61.03515625 us, exact in binary


class ScriptedTimer:
    """Returns the scripted readings in order; an Exception instance is raised."""

    def __init__(self, readings):
        self.readings, self.calls = list(readings), 0

    def __call__(self):
        self.calls += 1
        value = self.readings.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class DoublingResampler:
    """8 kHz in, 16 kHz out by repetition; `hold` samples are kept back until the flush,
    like a real streaming resampler's delay."""

    def __init__(self, hold=0):
        self.hold, self.calls, self.kept = hold, [], np.zeros(0)

    def resample_chunk(self, samples, last=False):
        self.calls.append((len(samples), last))
        data = np.concatenate([self.kept, samples])
        if last:
            self.kept = np.zeros(0)
            return np.repeat(data, 2)
        cut = max(0, len(data) - self.hold)
        self.kept = data[cut:]
        return np.repeat(data[:cut], 2)


def series(sink, stage):
    return sink.stage_times.snapshot()['series'][stage]


def expected_buckets(durations):
    return [sum(1 for d in durations if d <= bound) for bound in TAP_STAGE_BUCKETS_S]


class TapStageTimingTests(unittest.TestCase):
    def make(self, readings, hold=0):
        self.feed, self.resampler = RecordingFeed(), DoublingResampler(hold)
        self.timer = ScriptedTimer(readings)
        sink = TapScoringSink(Live(), self.feed, IdlePipeline(), lambda: 0.0, timer=self.timer,
                              resampler_factory=lambda: self.resampler)
        sink.started(call('c1'))
        return sink

    def test_one_chunk_is_split_into_resample_and_speech_feed(self):
        # Readings: before resampling, after the PCM16 conversion, after SpeechFeed.
        sink = self.make([1.0, 1.0 + 2 * TICK, 1.0 + 2 * TICK + 40 * TICK])
        sink.audio('c1', np.arange(160, dtype='<i2').tobytes(), ((160, 0),))
        self.assertEqual(self.timer.calls, 3)
        resample, feed = series(sink, 'resample'), series(sink, 'speech_feed')
        self.assertEqual((resample['count'], resample['sum']), (1, 2 * TICK))  # 122 us
        self.assertEqual((feed['count'], feed['sum']), (1, 40 * TICK))         # 2.44 ms
        self.assertEqual(resample['buckets'], expected_buckets([2 * TICK]))
        self.assertEqual(resample['buckets'][:3], [0, 0, 1])                    # first bound >= 122 us: 250 us
        self.assertEqual(feed['buckets'], expected_buckets([40 * TICK]))
        self.assertEqual(feed['buckets'][4:6], [0, 1])                          # > 1 ms, <= 2.5 ms
        self.assertEqual(np.concatenate(self.feed.pcm).tolist(), np.repeat(np.arange(160), 2).tolist())
        self.assertEqual(sink.stage_times.snapshot()['audio_seconds'], {'resample': 0.02, 'speech_feed': 0.02})

    def test_a_multi_frame_chunk_counts_its_audio_for_the_real_time_factor(self):
        # Review M1: the tap delivers every frame due at a loop turn in one chunk (here 50
        # frames, 1 s, after a long detector batch). 30 ms for 1 s of audio is far faster
        # than real time even though it is above the 20 ms bound.
        sink = self.make([0.0, 0.0, 0.0302734375])  # 31 * 2**-10 s of SpeechFeed, exact
        sink.audio('c1', bytes(2 * 8000), ((8000, 0),))
        feed = series(sink, 'speech_feed')
        self.assertEqual(feed['buckets'][TAP_STAGE_BUCKETS_S.index(0.02)], 0)
        audio = sink.stage_times.snapshot()['audio_seconds']
        self.assertEqual(audio, {'resample': 1.0, 'speech_feed': 1.0})
        self.assertLess(feed['sum'] / audio['speech_feed'], 0.05, 'real-time factor')

    def test_a_chunk_with_no_output_times_only_the_resampler(self):
        sink = self.make([0.5, 0.5 + TICK], hold=200)  # 160 samples in, all held back
        sink.audio('c1', bytes(320), ((160, 0),))
        self.assertEqual((self.feed.pcm, self.timer.calls), ([], 2))
        self.assertEqual(series(sink, 'resample')['count'], 1)
        self.assertEqual(series(sink, 'speech_feed')['count'], 0)
        self.assertEqual(sink.stage_times.snapshot()['audio_seconds'], {'resample': 0.02, 'speech_feed': 0.0})

    def test_the_end_of_call_flush_is_timed_with_last(self):
        sink = self.make([0.5, 0.5 + TICK, 2.0, 2.0 + 3 * TICK, 2.0 + 5 * TICK], hold=200)
        sink.audio('c1', bytes(320), ((160, 0),))
        sink.ended('c1', 'bye', {})
        self.assertEqual(self.resampler.calls, [(160, False), (0, True)])
        self.assertEqual(series(sink, 'resample')['sum'], TICK + 3 * TICK)
        self.assertEqual(series(sink, 'speech_feed')['sum'], 2 * TICK)
        self.assertEqual(sum(len(p) for p in self.feed.pcm), 320)
        self.assertEqual(self.feed.finished, ['c1'])
        # The held audio is counted once, when it arrived; the flush feeds it.
        self.assertEqual(sink.stage_times.snapshot()['audio_seconds'], {'resample': 0.02, 'speech_feed': 0.02})

    def test_a_stage_over_the_20_ms_bound_lands_above_it(self):
        sink = self.make([0.0, 0.0, 0.0 + 0.0234375])  # 24 ms of SpeechFeed (binary exact)
        sink.audio('c1', bytes(320), ((160, 0),))
        feed = series(sink, 'speech_feed')
        twenty_ms = TAP_STAGE_BUCKETS_S.index(0.02)
        self.assertEqual(feed['buckets'][twenty_ms], 0)
        self.assertEqual(feed['buckets'][twenty_ms + 1], 1)
        self.assertEqual(series(sink, 'resample')['buckets'][0], 1, 'a zero duration is counted in the first bucket')

    def test_unusable_timer_readings_are_counted_and_never_touch_the_audio(self):
        chunk = np.arange(-80, 80, dtype='<i2').tobytes()
        cases = {
            # name: readings for one chunk, failures, resample count, speech_feed count
            'raises at start': ([RuntimeError('clock'), 1.0, 1.0 + TICK], 1, 0, 1),
            'nan in the middle': ([1.0, math.nan, 1.0 + TICK], 1, 0, 0),
            'bool at the end': ([1.0, 1.0 + TICK, True], 1, 1, 0),
            'string': (['soon', 1.0, 1.0 + TICK], 1, 0, 1),
            'infinite': ([1.0, 1.0 + TICK, math.inf], 1, 1, 0),
            'backwards in feed': ([1.0, 1.0 + 4 * TICK, 1.0 + TICK], 1, 1, 0),
            'backwards in resample': ([1.0, 1.0 - TICK, 1.0], 1, 0, 1),
        }
        reference = self.make([0.0, 0.0, 0.0])
        reference.audio('c1', chunk, ((160, 0),))
        want_pcm = np.concatenate(self.feed.pcm).tolist()
        for name, (readings, failures, resampled, fed) in cases.items():
            with self.subTest(name):
                sink = self.make(readings)
                sink.audio('c1', chunk, ((160, 0),))
                self.assertEqual(np.concatenate(self.feed.pcm).tolist(), want_pcm, 'audio unchanged')
                self.assertEqual(sink.counts['timer_failures'], failures)
                self.assertEqual(sink.counts['errors'], 0)
                self.assertEqual((series(sink, 'resample')['count'], series(sink, 'speech_feed')['count']),
                                 (resampled, fed))

    def test_the_timer_must_be_callable(self):
        with self.assertRaisesRegex(ValueError, 'timer must be callable'):
            TapScoringSink(Live(), RecordingFeed(), IdlePipeline(), lambda: 0.0, timer=1.0)

    def test_real_resampler_and_clock_time_every_chunk(self):
        feed = RecordingFeed()
        sink = TapScoringSink(Live(), feed, IdlePipeline(), lambda: 0.0)
        sink.started(call('c1'))
        rng = np.random.default_rng(5)
        for _ in range(50):
            sink.audio('c1', np.rint(rng.normal(0, 3000, 160)).astype('<i2').tobytes(), ((160, 0),))
        sink.ended('c1', 'bye', {})
        resample, fed = series(sink, 'resample'), series(sink, 'speech_feed')
        self.assertEqual(resample['count'], 51, 'every chunk and the flush')
        self.assertEqual(fed['count'], len(feed.pcm), 'once per push that produced audio')
        self.assertGreater(resample['sum'], 0.0)
        self.assertEqual(sink.counts['timer_failures'], 0)


class StageTimesConcurrencyTests(unittest.TestCase):
    def test_snapshots_taken_while_observing_always_render(self):
        times = StageTimes(TAP_STAGES, TAP_STAGE_BUCKETS_S)
        stop, failures, rendered = threading.Event(), [], [0]

        def read():
            while not stop.is_set():
                try:
                    histogram_lines('x', 'y', 'stage', TAP_STAGES, times.snapshot())
                    rendered[0] += 1
                except ValueError as error:
                    failures.append(error)
        reader = threading.Thread(target=read)
        reader.start()
        try:
            for i in range(20000):
                times.observe(TAP_STAGES[i % 2], (i % 13) * 0.0003)
        finally:
            stop.set()
            reader.join(10)
        self.assertEqual(failures, [])
        self.assertGreater(rendered[0], 0)
        final = times.snapshot()['series']
        self.assertEqual(final['resample']['count'] + final['speech_feed']['count'], 20000)

    def test_audio_seconds_default_to_zero(self):
        times = StageTimes(TAP_STAGES, TAP_STAGE_BUCKETS_S)
        times.observe('resample', 0.001)
        self.assertEqual(times.snapshot()['audio_seconds'], {'resample': 0.0, 'speech_feed': 0.0})


class LiveMetricsTests(unittest.TestCase):
    def test_the_live_metrics_render_the_tap_stages_and_timer_failures(self):
        service = make_service(ManualClock())
        health = HealthServer(service, port=0)
        self.addCleanup(health.stop)
        service.sink.stage_times.observe('resample', 0.0001, 0.02)
        service.sink.stage_times.observe('speech_feed', 0.03, 1.0)
        service.sink.counts['timer_failures'] = 2
        text = health.metrics()
        values = samples(text)
        self.assertIn('# TYPE ava_live_tap_stage_seconds histogram', text)
        self.assertEqual(values['ava_live_tap_stage_seconds_bucket{stage="resample",le="0.0001"}'], 1)
        self.assertEqual(values['ava_live_tap_stage_seconds_bucket{stage="resample",le="5e-05"}'], 0)
        self.assertEqual(values['ava_live_tap_stage_seconds_bucket{stage="speech_feed",le="0.02"}'], 0)
        self.assertEqual(values['ava_live_tap_stage_seconds_bucket{stage="speech_feed",le="0.05"}'], 1)
        self.assertEqual(values['ava_live_tap_stage_seconds_count{stage="speech_feed"}'], 1)
        self.assertAlmostEqual(values['ava_live_tap_stage_seconds_sum{stage="speech_feed"}'], 0.03)
        self.assertEqual(values['ava_live_sink_events_total{kind="timer_failures"}'], 2)
        self.assertEqual(values['ava_live_tap_audio_seconds_total{stage="resample"}'], 0.02)
        self.assertEqual(values['ava_live_tap_audio_seconds_total{stage="speech_feed"}'], 1.0)
        self.assertIn('# TYPE ava_live_tap_audio_seconds_total counter', text)
        self.assertEqual(values['ava_live_metrics_render_errors_total'], 0)

    def test_a_malformed_stage_histogram_drops_only_the_tap_stage_families(self):
        service = make_service(ManualClock())
        health = HealthServer(service, port=0)
        self.addCleanup(health.stop)
        good = service.sink.stage_times.snapshot
        for name, broken in {
            'buckets above the count': lambda: {**good(), 'series': {
                **good()['series'], 'resample': {'buckets': [1] * len(TAP_STAGE_BUCKETS_S), 'sum': 0.0, 'count': 0}}},
            'a stage missing from the audio': lambda: {**good(), 'audio_seconds': {'resample': 0.0}},
            'a negative audio total': lambda: {**good(), 'audio_seconds': {'resample': -1.0, 'speech_feed': 0.0}},
        }.items():
            with self.subTest(name):
                service.sink.stage_times.snapshot = broken
                before = health.counts['render_errors']
                values = samples(health.metrics())
                self.assertEqual(health.counts['render_errors'], before + 1)
                self.assertFalse([key for key in values if key.startswith('ava_live_tap_stage_seconds')
                                  or key.startswith('ava_live_tap_audio_seconds')], 'tap stage families dropped')
                self.assertIn('ava_live_sink_events_total{kind="started"}', values, 'tap counters still render')
                self.assertIn('ava_live_tap_events_total{kind="events"}', values)

    def test_a_sink_without_timer_failures_drops_the_tap_counter_section(self):
        service = make_service(ManualClock())
        health = HealthServer(service, port=0)
        self.addCleanup(health.stop)
        del service.sink.counts['timer_failures']  # a malformed sink: the section fails, counted
        values = samples(health.metrics())
        self.assertEqual(values['ava_live_metrics_render_errors_total'], 1)
        self.assertNotIn('ava_live_sink_events_total{kind="started"}', values)
        self.assertIn('ava_live_tap_stage_seconds_count{stage="resample"}', values, 'its own section still renders')
        self.assertIn('ava_live_calls_active', values, 'other families still render')


if __name__ == '__main__':
    unittest.main()
