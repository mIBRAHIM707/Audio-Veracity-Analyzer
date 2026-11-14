"""Live audio -> VAD speech windows -> `LivePipeline` (backlog 3.2 integration; PRD
FR-I3, FR-U1 `no_speech`; architecture §2.2 steps 3-4).

`SpeechFeed(pipeline, vad_factory)` owns one `SpeechWindower` (and one VAD from
`vad_factory()`) per active call direction:

- `audio(call_id, direction, pcm, now)`: feeds 16 kHz int16 PCM; scorable windows are
  offered to the pipeline with their window index (indexes stay increasing, so a
  skipped non-scorable window leaves a gap the ordered boundary accepts); windows
  that are not scorable are counted, not offered. Audio for a call that is not active
  is dropped and counted. A VAD fault is contained: the windower dropped that audio,
  the fault is counted and the call goes on (AVA must not stop a call).
- `no_speech`: when every fed direction of a call has had no speech for at least
  `no_speech_after_s` of audio (never any speech counts from the start), the call's
  `no_speech` state is published once through `LiveCalls.status` with
  `seconds_since_speech` (None before any speech) as the reason detail; it is re-armed
  when speech returns. Speech windows then drive the scorer's own states.
- `finish(call_id, now)`: end of the call's audio: offers each direction's last
  (partial, if scorable) window. Drive `schedule`/`step` until it is scored, then call
  `end_call`; ending first cancels the waiting tail (FR-I11).
- `end_call(call_id, reason)`: ends the call in the pipeline with per-direction speech
  seconds measured by the VAD (only classified frames: call `finish` first to include
  the last part-frame), and forgets the windowers.

Containment: `now` is checked (`LivePipeline.check_clock`) before any windower state
is used, so a clock error loses no windows; audio after `finish` is dropped and
counted; a failing `vad_factory` (missing model, disk fault) is counted and retried up
to 3 times per direction, after which that direction stays without VAD (audio dropped,
counted); bad `pcm` is refused before a windower is created. `sweep()` forgets calls
ended elsewhere (for example `LiveCalls.end_all`, which can use `speech_seconds_for`).

Single owner, not thread-safe (like `LivePipeline`); `end_call` reads the scored
directions and ends in two steps, so a concurrent `human_agent_answered` makes it
raise ValueError (the call stays open; retry). Interpretation choices for the PRD: the
`no_speech` delay (default 4 s, one window); call-level (all directions) rather than
per-direction no-speech; `no_speech` is published even over an open alert (the
publisher's open question); seconds since speech travel only in the `reason` text
(no numeric contract field yet); the rule counts audio time, so a tap that stops
sending audio never reaches `no_speech`; a scored window still queued can publish
after `no_speech`; a non-scorable partial window is dropped rather than shown.
"""
import numpy as np

from .config import DIRECTIONS
from .speech_windows import MAX_FEED_SAMPLES, SpeechWindower, VadFailure

MAX_VAD_CREATE_ATTEMPTS = 3


class SpeechFeed:
    def __init__(self, pipeline, vad_factory, *, no_speech_after_s=4.0):
        if not callable(vad_factory):
            raise ValueError('vad_factory must be callable')
        if isinstance(no_speech_after_s, bool) or not isinstance(no_speech_after_s, (int, float)) \
                or not 0 < no_speech_after_s <= 600:
            raise ValueError('no_speech_after_s must be a number in (0, 600]')
        self.pipeline, self._vad_factory = pipeline, vad_factory
        self.no_speech_after_s = float(no_speech_after_s)
        self._windowers = {}       # (call_id, direction) -> SpeechWindower
        self._no_speech = set()    # calls whose no_speech state is currently published
        self._finished = set()     # calls whose audio was finished (late audio is dropped)
        self._vad_create_failures = {}  # (call_id, direction) -> consecutive factory failures
        self.counts = {'audio_inactive': 0, 'vad_failures': 0, 'offered': 0, 'not_scorable': 0,
                       'offered_inactive': 0, 'stride_skipped': 0, 'no_speech_published': 0,
                       'audio_after_finish': 0, 'vad_create_failures': 0, 'vad_unavailable': 0}

    def _windower(self, call_id, direction):
        """The direction's windower, or None when its VAD cannot be created (counted; up
        to MAX_VAD_CREATE_ATTEMPTS tries, then the direction stays without VAD)."""
        key = (call_id, direction)
        windower = self._windowers.get(key)
        if windower is None:
            failures = self._vad_create_failures.get(key, 0)
            if failures >= MAX_VAD_CREATE_ATTEMPTS:
                self.counts['vad_unavailable'] += 1
                return None
            try:
                windower = SpeechWindower(self._vad_factory())
            except Exception:  # missing model, disk or session fault: never stop the call
                self._vad_create_failures[key] = failures + 1
                self.counts['vad_create_failures'] += 1
                return None
            self._windowers[key] = windower
        return windower

    def _forget(self, call_id):
        for key in [k for k in self._windowers if k[0] == call_id]:
            del self._windowers[key]
        for key in [k for k in self._vad_create_failures if k[0] == call_id]:
            del self._vad_create_failures[key]
        self._no_speech.discard(call_id)
        self._finished.discard(call_id)

    def sweep(self):
        """Forget windowers of calls that are no longer active (e.g. ended by
        `LiveCalls.end_all`), so a reused call ID starts fresh. Call it regularly, like
        `LivePipeline.schedule`. Returns the number of calls forgotten."""
        calls = {k[0] for k in self._windowers} | set(self._finished) | {k[0] for k in self._vad_create_failures}
        gone = [c for c in calls if not self.pipeline.live.is_active(c)]
        for call_id in gone:
            self._forget(call_id)
        return len(gone)

    def speech_seconds_for(self, call_id, directions):
        """Callback for `LiveCalls.end_all(speech_seconds_for=...)`."""
        measured = self.speech_seconds(call_id)
        return {d: measured.get(d, 0.0) for d in directions}

    def _offer(self, call_id, direction, windows, now):
        offered = 0
        for window in windows:
            if not window.scorable:
                self.counts['not_scorable'] += 1
                continue
            outcome = self.pipeline.offer(call_id, direction, window.index, window.pcm, now)
            if outcome is None:
                self.counts['offered_inactive'] += 1
                continue
            if not outcome['accepted']:  # degraded mode skipped it (stride, FR-I10)
                self.counts['stride_skipped'] += 1
                continue
            offered += 1
        self.counts['offered'] += offered
        return offered

    def audio(self, call_id, direction, pcm, now):
        """Feed audio for one direction; returns the number of windows offered.
        Invalid `direction` or `pcm` raises ValueError and changes nothing."""
        if direction not in DIRECTIONS:
            raise ValueError(f'direction must be one of {DIRECTIONS}')
        if not isinstance(pcm, np.ndarray) or pcm.dtype != np.int16 or pcm.ndim != 1 \
                or len(pcm) > MAX_FEED_SAMPLES:
            raise ValueError(f'pcm must be a 1-D int16 numpy array of at most {MAX_FEED_SAMPLES} samples')
        self.pipeline.check_clock(now)  # before any windower state is used up
        if not self.pipeline.live.is_active(call_id):
            self.counts['audio_inactive'] += 1
            self._forget(call_id)
            return 0
        if call_id in self._finished:
            self.counts['audio_after_finish'] += 1
            return 0
        windower = self._windower(call_id, direction)
        if windower is None:
            return 0
        try:
            windows = windower.feed(pcm)
        except VadFailure:
            self.counts['vad_failures'] += 1
            windows = []
        offered = self._offer(call_id, direction, windows, now)
        self._update_no_speech(call_id)
        return offered

    def _update_no_speech(self, call_id):
        windowers = [w for (c, _), w in self._windowers.items() if c == call_id]
        quiet = []
        for w in windowers:
            since = w.seconds_since_speech
            quiet.append(w.stream_seconds >= self.no_speech_after_s if since is None
                         else since >= self.no_speech_after_s)
        if windowers and all(quiet):
            if call_id not in self._no_speech:
                times = [w.seconds_since_speech for w in windowers if w.seconds_since_speech is not None]
                since = min(times) if times else None
                status = self.pipeline.live.status(
                    call_id, 'no_speech', degraded=False, effective_stride_s=self.pipeline.config.hop_s,
                    reason='vad_no_speech' if since is None else f'vad_no_speech_for_{since:.1f}s')
                self._no_speech.add(call_id)
                self.counts['no_speech_published'] += bool(status)
        else:
            self._no_speech.discard(call_id)

    def finish(self, call_id, now):
        """Offer each direction's last window (end of audio). Returns the number offered.
        Later audio for the call is dropped and counted."""
        self.pipeline.check_clock(now)  # before the windowers are finished
        if not self.pipeline.live.is_active(call_id) or call_id in self._finished:
            return 0
        self._finished.add(call_id)
        offered = 0
        for (c, direction), windower in list(self._windowers.items()):
            if c != call_id:
                continue
            try:
                windows = windower.finish()
            except ValueError:  # already finished
                continue
            offered += self._offer(call_id, direction, windows, now)
        return offered

    def speech_seconds(self, call_id):
        return {d: w.speech_seconds for (c, d), w in self._windowers.items() if c == call_id}

    def end_call(self, call_id, reason):
        """End the call in the pipeline with VAD speech seconds for exactly its scored
        directions (0.0 for a scored direction that sent no audio) and forget its
        windowers. Invalid `reason` raises ValueError and keeps the call and windowers."""
        directions = self.pipeline.live.scored_directions(call_id)
        if directions is None:  # unknown or already ended
            self._forget(call_id)
            return []
        measured = self.speech_seconds(call_id)
        speech = {d: measured.get(d, 0.0) for d in directions}
        envelopes = self.pipeline.end_call(call_id, reason, speech_seconds=speech)
        self._forget(call_id)
        return envelopes
