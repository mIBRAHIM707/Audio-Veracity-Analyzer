"""Bounded file analysis over the existing CPU baseline detector (backlog 2.1; PRD FR-A1 subset).

Pure orchestration with an injected detector: decode a PCM16 WAV, condition it through
the shared `data/prepare/conditioning.py` path (mono mean, soxr HQ to 16 kHz), cut
4 s windows every 2 s (a final tail of at least 1.5 s is zero-padded and marked
partial), score each window and aggregate. No network, no downloads, CPU only.

File-mode aggregation is the mean `p_synthetic` over full windows (proposed for the
PRD; the streaming EMA/hysteresis of FR-I5 arrives in backlog 4.2). `margin` is the
distance of that uncalibrated mean from 0.5, not a confidence. Per-clip peak
normalization is not applied here: the baseline layer-normalizes every window, which
cancels gain; an AVA SLS model must revisit this for train/serve parity.

Scope limits, stated in every result: the detector is the pretrained AntiDeepfake
MMS-300M baseline with its original head, **uncalibrated** and not trained on AVA
data; no VAD, telephone-band (8 kHz) path, codec decode or language hint yet
(backlog 3.x); verdicts are advisory research output, not a fraud decision.
"""
from dataclasses import dataclass
import io
import math
import threading
import time
import wave

import numpy as np

from data.prepare.conditioning import canonicalize

RATE = 16000
WINDOW_SAMPLES, HOP_SAMPLES, MIN_TAIL_SAMPLES = 4 * RATE, 2 * RATE, RATE * 3 // 2
MAX_BYTES = 100_000_000
MAX_SYNC_SECONDS, MAX_ASYNC_SECONDS = 60.0, 600.0
MIN_EVIDENCE_WINDOWS = 2
DECISION_THRESHOLD = 0.5
CALIBRATION = 'uncalibrated_baseline'
SCOPE = ('Pretrained AntiDeepfake MMS-300M baseline with its original head; uncalibrated, not '
         'trained or validated on AVA Urdu/Punjabi data. No VAD, telephone-band path, codec decode '
         'or language hint. Advisory research output only.')


class AnalysisError(Exception):
    """Base class; `status` is the HTTP status an API layer should map it to."""
    status = 500


class InputTooLarge(AnalysisError):
    status = 413


class UnsupportedAudio(AnalysisError):
    status = 422


class AnalyzerBusy(AnalysisError):
    status = 503


class DetectorFailure(AnalysisError):
    status = 500


class NeedsAsyncJob(Exception):
    """Not a failure. FR-A1: files over 60 s get 202 and a job ID; a job worker then calls
    analyze_wav(payload, max_seconds=MAX_ASYNC_SECONDS). Jobs arrive in backlog 2.2/5.2."""
    status = 202


@dataclass(frozen=True)
class Window:
    index: int
    start_sample: int
    end_sample: int
    partial: bool


def decode_wav(payload, max_seconds=MAX_SYNC_SECONDS):
    """Bounded PCM16 WAV decode -> (raw bytes, rate, channels). Raises typed errors."""
    if not 0 < max_seconds <= MAX_ASYNC_SECONDS:
        raise ValueError('max_seconds must be within the 10 minute file bound')
    if not isinstance(payload, (bytes, bytearray)):
        raise UnsupportedAudio('Expected WAV bytes')
    if len(payload) > MAX_BYTES:
        raise InputTooLarge('File exceeds 100 MB')
    try:
        with wave.open(io.BytesIO(bytes(payload)), 'rb') as audio:
            rate, channels, width, frames = (audio.getframerate(), audio.getnchannels(),
                                             audio.getsampwidth(), audio.getnframes())
            if width != 2 or channels not in (1, 2) or not 8000 <= rate <= 192000:
                raise UnsupportedAudio('Expected 8-192 kHz mono or stereo PCM16 WAV')
            if frames / rate > MAX_ASYNC_SECONDS:
                raise InputTooLarge('Files are limited to 10 minutes')
            if frames / rate > max_seconds:
                raise NeedsAsyncJob('Files over 60 seconds need an asynchronous job')
            raw = audio.readframes(frames)
    except (wave.Error, EOFError) as error:
        raise UnsupportedAudio('Undecodable WAV') from error
    if not frames or len(raw) != frames * channels * 2:
        raise UnsupportedAudio('Empty or truncated WAV')
    return raw, rate, channels


def plan_windows(total):
    """Pure: 4 s windows every 2 s. If at least 1.5 s at the end is not yet covered, one
    partial window from the next hop start to the end is added (zero-padded when scored)."""
    windows, start = [], 0
    while start + WINDOW_SAMPLES <= total:
        windows.append(Window(len(windows), start, start + WINDOW_SAMPLES, False))
        start += HOP_SAMPLES
    covered = windows[-1].end_sample if windows else 0
    if total - covered >= MIN_TAIL_SAMPLES:
        windows.append(Window(len(windows), start, total, True))
    return windows


def p_synthetic(logits):
    """Softmax probability of class 0 ('fake') from the detector's [fake, real] logits."""
    if not isinstance(logits, (list, tuple)) or len(logits) != 2 or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in logits):
        raise DetectorFailure('Detector returned invalid logits')
    top = max(logits)
    fake, real = (math.exp(v - top) for v in logits)
    return fake / (fake + real)


def aggregate(scores):
    """Pure: mean p_synthetic over full windows (partial only if no full window exists)."""
    full = [s['p_synthetic'] for s in scores if not s['partial']]
    used = full or [s['p_synthetic'] for s in scores]
    if len(used) < MIN_EVIDENCE_WINDOWS:
        detail = 'only a partial window' if scores and not full else f'fewer than {MIN_EVIDENCE_WINDOWS} full windows'
        return {'verdict': 'undetermined', 'p_synthetic': None, 'margin': None,
                'evidence_windows': len(used), 'reason': detail}
    mean = sum(used) / len(used)
    return {'verdict': 'synthetic' if mean >= DECISION_THRESHOLD else 'genuine', 'p_synthetic': mean,
            'margin': abs(mean - DECISION_THRESHOLD) * 2, 'evidence_windows': len(used), 'reason': None}


class AnalysisService:
    """Loads the detector lazily once; analyses one file at a time and refuses extra work."""

    def __init__(self, detector_factory, *, model_id, clock=time.perf_counter):
        self._factory, self._detector, self._clock = detector_factory, None, clock
        self._lock = threading.Lock()
        self.model_id = model_id
        self.warmup_seconds = None  # set by the first successful warm_up (FR-A8 readiness)

    @property
    def detector_loaded(self):
        return self._detector is not None

    @property
    def ready(self):
        return self.warmup_seconds is not None

    def warm_up(self):
        """FR-A8: load the detector and score one silent window, validating its logits.
        Readiness is sticky once reached (a later detector fault fails that request, not
        readiness), so a busy but warmed service stays ready; before that it refuses
        (AnalyzerBusy) while an analysis runs."""
        if self.ready:
            return self.warmup_seconds
        if not self._lock.acquire(blocking=False):
            raise AnalyzerBusy('Analysis or warm-up in progress; retry later')
        try:
            if self.ready:
                return self.warmup_seconds
            started = self._clock()
            detector, _ = self._get_detector()
            try:
                logits = list(detector(np.zeros(WINDOW_SAMPLES, dtype=np.float32)))
            except Exception as error:
                raise DetectorFailure(f'Warm-up inference failed: {type(error).__name__}') from error
            p_synthetic(logits)  # invalid logits raise DetectorFailure
            self.warmup_seconds = self._clock() - started
            return self.warmup_seconds
        finally:
            self._lock.release()

    def _get_detector(self):
        """Returns (detector, seconds spent loading it in this call). Called under the analysis lock."""
        if self._detector is not None:
            return self._detector, 0.0
        started = self._clock()
        try:
            self._detector = self._factory()
        except Exception as error:  # surfaced as a typed failure; a later request retries
            raise DetectorFailure(f'Detector unavailable: {type(error).__name__}') from error
        return self._detector, self._clock() - started

    def analyze_wav(self, payload, max_seconds=MAX_SYNC_SECONDS):
        """Synchronous by default; a job worker passes max_seconds=MAX_ASYNC_SECONDS."""
        if not self._lock.acquire(blocking=False):
            raise AnalyzerBusy('Another analysis is running; retry later')
        try:
            return self._analyze(payload, max_seconds)
        finally:
            self._lock.release()

    def _analyze(self, payload, max_seconds):
        started = self._clock()
        raw, rate, channels = decode_wav(payload, max_seconds)
        decoded = self._clock()
        try:
            conditioned, record = canonicalize(raw, rate, channels)
        except ValueError as error:
            raise UnsupportedAudio('Audio could not be conditioned') from error
        except Exception as error:  # e.g. resampler or memory fault: typed 500, never a traceback
            raise AnalysisError(f'Conditioning failed: {type(error).__name__}') from error
        conditioned_at = self._clock()
        samples = np.frombuffer(conditioned, dtype='<i2').astype(np.float32) / 32768
        windows = plan_windows(len(samples))
        detector, load_seconds = self._get_detector() if windows else (None, 0.0)
        scores, inference_seconds = [], 0.0
        for window in windows:
            piece = np.zeros(WINDOW_SAMPLES, dtype=np.float32)
            chunk = samples[window.start_sample:window.end_sample]
            piece[:len(chunk)] = chunk
            window_started = self._clock()
            try:
                logits = list(detector(piece))
            except Exception as error:  # any detector fault becomes a typed failure
                raise DetectorFailure(f'Detector failed on a window: {type(error).__name__}') from error
            inference_seconds += self._clock() - window_started
            scores.append({'index': window.index, 't_start': window.start_sample / RATE,
                           't_end': window.end_sample / RATE, 'partial': window.partial,
                           'p_synthetic': p_synthetic(logits)})
        result = aggregate(scores)
        flags = []
        if rate > 8000:
            flags.append('out_of_domain:wideband_source')  # PRD FR-A1; no 8 kHz path yet
        if record['saturated_samples']:
            flags.append('resampling_saturation')
        return {**result, 'calibration': CALIBRATION, 'model_id': self.model_id, 'scope': SCOPE,
                'duration_seconds': len(samples) / RATE, 'input': {'sample_rate': rate, 'channels': channels},
                'flags': flags, 'timeline': scores, 'aggregation': 'mean_of_full_windows',
                'model_load_seconds': load_seconds,
                # FR-O6 per-stage latency of this file (inference = detector calls only).
                'stage_seconds': {'decode': decoded - started, 'condition': conditioned_at - decoded,
                                  'inference': inference_seconds},
                'processing_seconds': self._clock() - started - load_seconds}
