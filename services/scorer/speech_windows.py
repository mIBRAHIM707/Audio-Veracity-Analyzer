"""VAD-gated speech windows for one call direction (backlog 3.2 partial; PRD FR-I3,
FR-I4; architecture §2.2 steps 3-4).

`SpeechWindower` takes 16 kHz mono PCM16 in any chunk sizes and a frame VAD. Audio is
split into fixed frames (512 samples = 32 ms, the Silero v5 frame at 16 kHz). Frames
whose speech probability is below the threshold (0.5) are excluded from the speech
stream but still advance time. Windows are cut from the speech-only stream with the
same plan as file mode (`services.inference.analysis.plan_windows`): 4 s windows every
2 s of accumulated speech, and at `finish()` a partial window from the next hop start
to the end when at least 1.5 s of speech is not yet covered (zero-padded to 4 s).

Interpretation choices (recorded for the PRD; FR-I3 and §2.2 are not explicit):
- Because windows hold speech frames only, a full window is always 4 s of speech; the
  "scored when at least 2.5 s is speech" rule therefore decides only partial windows:
  a partial window with 1.5-2.5 s of speech is emitted with `scorable=False` (visible,
  not scored), one with >= 2.5 s is scorable.
- A window's `t_start`/`t_end` are stream times of its first and last speech samples,
  so its span includes the excluded non-speech in between.
- The last incomplete frame at `finish()` is zero-padded for the VAD decision only.
- `no_speech` is true until the first speech frame; `seconds_since_speech` is reported
  for the `state=no_speech` display.

The VAD is any callable `vad(frame) -> probability` on a float32 [512] frame in
[-1, 1); it may keep state (Silero does), so use one VAD instance per direction and
give it a `reset()` method when it has state. A VAD fault or an invalid probability
raises `VadFailure` (a ValueError) from `feed` AFTER dropping that audio as
non-speech: time advances by the dropped samples, nothing is added to the speech
stream, `vad_failures`/`dropped_samples` are counted and `vad.reset()` is called if
present, because frames before the fault already moved the VAD state. Do not retry
the same audio. AVA must not stop a call, so the caller contains the error.
Window/hop sizes are file-mode constants until the config task makes them
configurable; no peak normalization is applied here (an integration decision). No
Silero model is loaded here: the ONNX adapter is separate work. Single owner, not
thread-safe.
"""
from dataclasses import dataclass
import math

import numpy as np

from services.inference.analysis import HOP_SAMPLES, MIN_TAIL_SAMPLES, RATE, WINDOW_SAMPLES

FRAME_SAMPLES = 512
SPEECH_THRESHOLD = 0.5
MIN_SPEECH_SAMPLES = int(2.5 * RATE)
MAX_FEED_SAMPLES = 10 * RATE  # bound one call's work and buffer growth per feed


class VadFailure(ValueError):
    """The VAD failed or returned an invalid probability; the audio was dropped."""


@dataclass(frozen=True)
class SpeechWindow:
    index: int
    speech_start: int  # half-open range on the speech-only stream (samples)
    speech_end: int
    t_start: float     # stream seconds of the first speech sample
    t_end: float       # stream seconds just after the last speech sample
    partial: bool
    scorable: bool
    pcm: np.ndarray    # float32 [64000], int16 / 32768, zero-padded when partial


class SpeechWindower:
    def __init__(self, vad, *, threshold=SPEECH_THRESHOLD):
        if not callable(vad):
            raise ValueError('vad must be callable')
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) \
                or not 0 < threshold < 1:
            raise ValueError('threshold must be a number strictly between 0 and 1')
        self._vad, self.threshold = vad, float(threshold)
        self._carry = np.zeros(0, np.int16)  # samples not yet forming a whole frame
        self._stream = 0                      # stream samples consumed into frames
        self._speech_total = 0                # speech samples accumulated so far
        self._base = 0                        # speech index of _buffer[0] (next window start)
        self._buffer = np.zeros(0, np.int16)  # speech samples from _base onwards
        self._segments = []                   # [speech_start, stream_start, length] runs
        self._next = 0                        # next full window index
        self._covered = 0                     # speech end of the last full window
        self._last_speech_end = None          # stream sample just after the last speech frame
        self._finished = False
        self.vad_failures = 0
        self.dropped_samples = 0

    # --- observations -----------------------------------------------------------------
    @property
    def speech_seconds(self):
        return self._speech_total / RATE

    @property
    def stream_seconds(self):
        return (self._stream + len(self._carry)) / RATE

    @property
    def no_speech(self):
        return self._speech_total == 0

    @property
    def seconds_since_speech(self):
        """Seconds since the end of the last speech frame, or None before any speech."""
        if self._last_speech_end is None:
            return None
        return (self._stream + len(self._carry) - self._last_speech_end) / RATE

    # --- intake -----------------------------------------------------------------------
    def _probability(self, frame):
        try:
            p = float(self._vad(frame.astype(np.float32) / 32768))
        except Exception as error:
            raise VadFailure(f'vad failed: {type(error).__name__}') from error
        if not math.isfinite(p) or not 0 <= p <= 1:
            raise VadFailure('vad probability must be finite and within [0, 1]')
        return p

    def _classify(self, frames):
        """Speech flags for every frame, or drop the audio and re-raise on a VAD fault."""
        try:
            return [self._probability(frame) >= self.threshold for frame in frames]
        except VadFailure:
            dropped = sum(len(frame) for frame in frames)
            self._stream += dropped
            self.dropped_samples += dropped
            self.vad_failures += 1
            reset = getattr(self._vad, 'reset', None)
            if callable(reset):
                try:
                    reset()
                except Exception as error:
                    raise VadFailure(f'vad reset failed: {type(error).__name__}') from error
            raise

    def feed(self, pcm):
        """Add 16 kHz int16 PCM; returns the full windows completed by it, in order."""
        if self._finished:
            raise ValueError('the windower is finished')
        if not isinstance(pcm, np.ndarray) or pcm.dtype != np.int16 or pcm.ndim != 1:
            raise ValueError('pcm must be a 1-D int16 numpy array')
        if len(pcm) > MAX_FEED_SAMPLES:
            raise ValueError(f'feed at most {MAX_FEED_SAMPLES} samples per call')
        audio = np.concatenate([self._carry, pcm])
        count = len(audio) // FRAME_SAMPLES
        frames = audio[:count * FRAME_SAMPLES].reshape(count, FRAME_SAMPLES)
        self._carry = audio[count * FRAME_SAMPLES:].copy()
        speech = self._classify(frames)  # on a fault the whole frames are dropped as non-speech
        windows = []
        for frame, is_speech in zip(frames, speech):
            windows.extend(self._commit(frame, is_speech))
        return windows

    def finish(self):
        """End of the direction: classify the last incomplete frame (zero-padded for the
        VAD only) and return the remaining windows (at most one partial). A VAD fault
        here drops only that part-frame (counted) and does not raise."""
        if self._finished:
            raise ValueError('the windower is finished')
        windows = []
        if len(self._carry):
            padded = np.zeros(FRAME_SAMPLES, np.int16)
            padded[:len(self._carry)] = self._carry
            carry, self._carry = self._carry, np.zeros(0, np.int16)
            try:
                (is_speech,) = self._classify([padded])  # VAD decision on the padded frame
                windows.extend(self._commit(carry, is_speech))
            except VadFailure:
                # Counted and dropped by _classify (as a whole frame); correct the count to
                # the real samples and still emit the tail from the speech already kept.
                self._stream -= FRAME_SAMPLES - len(carry)
                self.dropped_samples -= FRAME_SAMPLES - len(carry)
        self._finished = True
        if self._speech_total - self._covered >= MIN_TAIL_SAMPLES:
            windows.append(self._window(self._base, self._speech_total, partial=True))
        return windows

    # --- internals --------------------------------------------------------------------
    def _commit(self, frame, is_speech):
        start = self._stream
        self._stream += len(frame)
        if not is_speech:
            return []
        self._last_speech_end = self._stream
        last = self._segments[-1] if self._segments else None
        if last and last[0] + last[2] == self._speech_total and last[1] + last[2] == start:
            last[2] += len(frame)  # contiguous with the previous speech run
        else:
            self._segments.append([self._speech_total, start, len(frame)])
        self._buffer = np.concatenate([self._buffer, frame])
        self._speech_total += len(frame)
        windows = []
        while self._base + WINDOW_SAMPLES <= self._speech_total:
            windows.append(self._window(self._base, self._base + WINDOW_SAMPLES, partial=False))
            self._covered = self._base + WINDOW_SAMPLES
            self._advance(self._base + HOP_SAMPLES)
        return windows

    def _advance(self, base):
        self._buffer = self._buffer[base - self._base:].copy()
        self._base = base
        while self._segments and self._segments[0][0] + self._segments[0][2] <= base:
            self._segments.pop(0)

    def _time(self, speech_index, end=False):
        """Stream seconds of a speech sample (or just after it when `end`)."""
        target = speech_index - 1 if end else speech_index
        for speech_start, stream_start, length in self._segments:
            if speech_start <= target < speech_start + length:
                return (stream_start + target - speech_start + (1 if end else 0)) / RATE
        raise AssertionError('speech index outside the retained segments')

    def _window(self, start, end, *, partial):
        samples = self._buffer[start - self._base:end - self._base]
        pcm = np.zeros(WINDOW_SAMPLES, np.float32)
        pcm[:len(samples)] = samples.astype(np.float32) / 32768
        window = SpeechWindow(self._next, start, end, self._time(start), self._time(end, end=True),
                              partial, (end - start) >= MIN_SPEECH_SAMPLES, pcm)
        self._next += 1
        return window
