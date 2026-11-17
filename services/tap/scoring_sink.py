"""Tap -> live scoring bridge (backlog 6.QA/8.1 wiring; PRD FR-T2, FR-I2, FR-I3, FR-I11;
architecture §2.1-§2.4): a `TapApp` sink that feeds tapped caller audio into
`LiveCalls` / `SpeechFeed` / `LivePipeline`.

- `started(call)`: `LiveCalls.start(call_id, from_number=, to=, tap_format=)`; a refused
  call (capacity) is not fed.
- `audio(call_id, pcm16_8k, concealment)`: 8 kHz -> 16 kHz with a per-call
  `soxr.ResampleStream` (HQ, float64, round-half-even, clip): the same samples as the
  file path's one-shot `conditioning.canonicalize` on the same 8 kHz audio (train/serve
  parity, tested). The per-frame concealment becomes a per-sample flag at 16 kHz (output
  sample j covers input sample j // 2) and goes to `SpeechFeed.audio(..., concealed=)`
  for the FR-I2 window gate.
- `quality(call_id, change)`: publishes `system.health` with `reason: media_quality`
  and `state: degraded|recovered` on the hub when one is given (FR-I2).
- `ended(call_id, reason, stats)`: flushes the resampler, `SpeechFeed.finish`, and queues
  the end; `pump()` (sink clock only) drives `schedule`/`step` and ends the call
  (`SpeechFeed.end_call`) once the pipeline holds no window of it, or after `end_grace_s`
  (the tail is then cancelled, FR-I11). An end that fails is retried, and forced through
  `LiveCalls.end` at the deadline; a call already ended elsewhere (`end_all`) is counted;
  a re-tap of a call whose end is pending closes the old session first; pipeline faults
  in `pump` are counted, never raised. A call refused by `LiveCalls` (capacity) is still
  tapped and its audio counted as unfed (spec question: admission before tapping).

Single owner: the thread that runs the TapApp also calls `pump`. Faults in scoring never
reach the tap (the TapApp counts sink exceptions); this sink also counts its own.

Stage timing (17 Nov 2026; PRD FR-O6 "latency per stage", backlog 7.2): `stage_times`
holds the wall time per delivered chunk (`timer`, perf_counter by default) of
- `resample`: 8 -> 16 kHz (`resample_chunk`), the concealment flags at 16 kHz and the
  PCM16 conversion;
- `speech_feed`: `SpeechFeed.audio` for the chunk: VAD, windowing and the offer of any
  window it completes to the pipeline.
A delivered chunk is every 20 ms frame the tap had due at that loop turn (usually a few,
many after a long detector batch), so a chunk's time is not compared with 20 ms. Instead
`stage_times` also sums the 8 kHz audio seconds each stage processed: processing seconds
/ audio seconds is the cost per audio second. With N calls on the one loop thread, the
busy share is about N times that; see services/live/README.md "Tap stages". The
end-of-call flush is timed too (no new audio). An unusable timer reading (raising,
non-finite, backwards) is counted in `timer_failures` and never affects the audio; a
`resample_chunk` that raises is not timed. The jitter buffer (in the RTP receiver) is not
timed: it delays audio by its playout setting (60 ms) by design.
"""
import math
import numbers
import threading
import time

import numpy as np

from services.scorer.speech_windows import MAX_FEED_SAMPLES

END_REASONS = {'bye': 'bye', 'timeout': 'timeout', 'monitor_lost': 'monitor_lost'}
TAP_STAGES = ('resample', 'speech_feed')
# Sub-millisecond resolution (one 20 ms frame resamples in tens of microseconds) up to 1 s.
TAP_STAGE_BUCKETS_S = (0.00005, 0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 1.0)
TAP_RATE = 8000


class StageTimes:
    """Cumulative-ready histograms per stage plus the audio seconds each stage processed,
    written by the owner thread and read by the health thread (`snapshot`), so both go
    through one lock."""

    def __init__(self, stages, bounds):
        self._lock = threading.Lock()
        self._bounds = tuple(bounds)
        self._series = {stage: {'buckets': [0] * len(self._bounds), 'sum': 0.0, 'count': 0} for stage in stages}
        self._audio = {stage: 0.0 for stage in stages}

    def observe(self, stage, seconds, audio_seconds=0.0):
        with self._lock:
            series = self._series[stage]
            for i, bound in enumerate(self._bounds):
                if seconds <= bound:
                    series['buckets'][i] += 1
            series['sum'] += seconds
            series['count'] += 1
            self._audio[stage] += audio_seconds

    def snapshot(self):
        with self._lock:
            return {'bounds': list(self._bounds),
                    'series': {stage: {'buckets': list(s['buckets']), 'sum': s['sum'], 'count': s['count']}
                               for stage, s in self._series.items()},
                    'audio_seconds': dict(self._audio)}


def _resampler():
    import soxr
    return soxr.ResampleStream(8000, 16000, 1, dtype='float64', quality='HQ')


def _pcm16(samples):
    return np.clip(np.rint(samples), -32768, 32767).astype(np.int16)


class _CallStream:
    def __init__(self, resampler):
        self.resampler = resampler
        self.flags = np.zeros(0, bool)  # 8 kHz concealment flags from input index flag_base on
        self.flag_base = 0
        self.out_total = 0              # 16 kHz samples emitted so far


class TapScoringSink:
    def __init__(self, live, feed, pipeline, clock, *, hub=None, end_grace_s=10.0, resampler_factory=_resampler,
                 recorder=None, timer=time.perf_counter):
        """`recorder` (optional, `services/live/retention.SnippetRecorder`, FR-O5) gets
        `started(call_id)` for admitted calls and their raw 8 kHz audio; its faults are
        counted in `recorder_errors`, never raised into the tap. `timer` measures the
        stage times only (module docstring)."""
        if not callable(timer):
            raise ValueError('timer must be callable')
        self._timer = timer
        self.stage_times = StageTimes(TAP_STAGES, TAP_STAGE_BUCKETS_S)
        self.live, self.feed, self.pipeline, self.clock, self.hub = live, feed, pipeline, clock, hub
        self.recorder = recorder
        self.end_grace_s = end_grace_s
        self._resampler_factory = resampler_factory
        self._streams = {}
        self._ending = {}  # call_id -> (reason, deadline)
        self.counts = {'started': 0, 'refused': 0, 'audio_chunks': 0, 'audio_unfed': 0, 'ended': 0,
                       'ended_elsewhere': 0, 'end_retries': 0, 'ends_forced': 0, 'end_cancelled_tail': 0,
                       'health_published': 0, 'health_failures': 0, 'pump_errors': 0, 'errors': 0,
                       'recorder_errors': 0, 'timer_failures': 0}

    # -- TapApp sink -------------------------------------------------------------------
    def started(self, call):
        if call.call_id in self._ending:
            # Re-tapped (e.g. after an ARI reconnect) before the old end completed: close
            # that session first so the new tap starts a fresh one (review M1).
            reason, _ = self._ending.pop(call.call_id)
            self._end_now(call.call_id, reason, forced=True)
        admitted = self.live.start(call.call_id, from_number=call.caller, to=call.dialled, tap_format=call.codec)
        if not admitted:
            self.counts['refused'] += 1  # the tap still runs (spec question: admission before tapping)
            return
        self._streams[call.call_id] = _CallStream(self._resampler_factory())
        self.counts['started'] += 1
        self._record('started', call.call_id, getattr(call, 'started', None))  # PBX creation time

    def audio(self, call_id, pcm, concealment):
        stream = self._streams.get(call_id)
        if stream is None:
            self.counts['audio_unfed'] += 1
            return
        self._record('audio', call_id, pcm)
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float64)
        flags = np.concatenate([np.full(n, c > 0) for n, c in concealment]) if concealment else np.zeros(0, bool)
        if len(flags) != len(samples):
            self.counts['errors'] += 1
            flags = np.zeros(len(samples), bool)
        self._push(call_id, stream, samples, flags)
        self.counts['audio_chunks'] += 1

    def quality(self, call_id, change):
        if self.hub is None or call_id not in self._streams:
            return
        try:
            self.hub.publish('system.health', call_id, {'reason': 'media_quality', 'media_state': change})
        except Exception:  # advisory; never into the tap
            self.counts['health_failures'] += 1
            return
        self.counts['health_published'] += 1

    def ended(self, call_id, reason, receiver_stats):
        stream = self._streams.pop(call_id, None)
        if stream is None:
            return
        # Queued first, so a fault below cannot leave the call without an end (review M2).
        self._ending[call_id] = (END_REASONS.get(reason, 'monitor_lost'), float('-inf'))
        try:
            deadline = self.clock() + self.end_grace_s
        except Exception:
            deadline = float('-inf')  # no usable clock: end at the next pump
        self._ending[call_id] = (END_REASONS.get(reason, 'monitor_lost'), deadline)
        self._push(call_id, stream, np.zeros(0), np.zeros(0, bool), last=True)
        self.feed.finish(call_id, self.clock())

    def failed(self, call_id, error):
        pass

    # -- owner loop ----------------------------------------------------------------------
    def pump(self):
        """Drive the pipeline (with the sink's own clock) and complete ends whose windows
        are done; an end that fails is retried until its deadline, then forced. Never
        raises: faults are counted (review M3). Returns the number of results applied."""
        applied = 0
        try:
            now = self.clock()
            self.pipeline.schedule(now)
            applied = self.pipeline.step(now)
        except Exception:
            self.counts['pump_errors'] += 1
            now = None
        for call_id, (reason, deadline) in list(self._ending.items()):
            try:
                current = self.clock() if now is None else now
                expired = current >= deadline
                if self.pipeline.has_work(call_id) and not expired:
                    continue
            except Exception:
                self.counts['pump_errors'] += 1
                expired = True
            if self._end_now(call_id, reason, forced=expired):
                del self._ending[call_id]
            else:
                self.counts['end_retries'] += 1
        return applied

    def flush_ends(self):
        """Complete every queued end now with its own reason (shutdown): windows still
        waiting are cancelled. Returns the number completed."""
        completed = 0
        for call_id, (reason, _) in list(self._ending.items()):
            del self._ending[call_id]
            self._end_now(call_id, reason, forced=True)
            completed += 1
        return completed

    def _end_now(self, call_id, reason, *, forced):
        """End one call; True when done (or nothing left to end)."""
        if forced and self.pipeline.has_work(call_id):
            self.counts['end_cancelled_tail'] += 1
        try:
            if not self.live.is_active(call_id):
                self.counts['ended_elsewhere'] += 1  # e.g. LiveCalls.end_all
                return True
            self.feed.end_call(call_id, reason)
            self.counts['ended'] += 1
            return True
        except Exception:
            if not forced:
                return False  # e.g. a concurrent human_agent_answered: retry next pump
        try:  # past the deadline: end it without VAD speech seconds rather than leak the slot
            self.live.end(call_id, reason, speech_seconds={d: 0.0 for d in (self.live.scored_directions(call_id) or [])})
            self.counts['ends_forced'] += 1
        except Exception:
            self.counts['errors'] += 1
        return True

    # -- internals -----------------------------------------------------------------------
    def _record(self, method, *args):
        if self.recorder is None:
            return
        try:
            getattr(self.recorder, method)(*args)
        except Exception:  # retention is advisory: never into the tap or scoring
            self.counts['recorder_errors'] += 1

    def _read_timer(self):
        """A finite reading, or None (counted in `timer_failures`)."""
        try:
            value = self._timer()
        except Exception:
            value = None
        if isinstance(value, numbers.Real) and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
        self.counts['timer_failures'] += 1
        return None

    def _observe(self, stage, start, end, audio_seconds):
        if start is None or end is None:
            return  # the failed reading is already counted
        if end < start:
            self.counts['timer_failures'] += 1
            return
        self.stage_times.observe(stage, end - start, audio_seconds)

    def _push(self, call_id, stream, samples, flags, *, last=False):
        started = self._read_timer()
        out = stream.resampler.resample_chunk(samples, last=last)
        stream.flags = np.concatenate([stream.flags, flags])
        if not len(out):
            self._observe('resample', started, self._read_timer(), len(samples) / TAP_RATE)
            return
        # Output sample j of the whole stream covers input sample j // 2.
        positions = (stream.out_total + np.arange(len(out))) // 2 - stream.flag_base
        if len(stream.flags):
            concealed = stream.flags[np.clip(positions, 0, len(stream.flags) - 1)]
        else:
            concealed = np.zeros(len(out), bool)
        stream.out_total += len(out)
        done = max(0, stream.out_total // 2 - stream.flag_base)  # inputs whose outputs are all out
        stream.flags, stream.flag_base = stream.flags[done:], stream.flag_base + done
        pcm16 = _pcm16(out)
        resampled = self._read_timer()
        self._observe('resample', started, resampled, len(samples) / TAP_RATE)
        for begin in range(0, len(pcm16), MAX_FEED_SAMPLES):
            self.feed.audio(call_id, 'caller', pcm16[begin:begin + MAX_FEED_SAMPLES], self.clock(),
                            concealed=concealed[begin:begin + MAX_FEED_SAMPLES])
        # The feed's audio in 8 kHz seconds (16 kHz samples / 2), so the two stages compare.
        self._observe('speech_feed', resampled, self._read_timer(), len(pcm16) / (2 * TAP_RATE))
