"""Class-independent CPU PCM conditioning; no file I/O or network operations.

Offsets after canonicalize refer to the returned 16 kHz sample grid. Apply
peak_normalize AFTER choosing a segment to keep gain out of pause detection.
"""
import hashlib
import math

import numpy as np
import soxr

RATE = 16000
TARGET_DBFS = -3.0
TARGET_PEAK = round(32768 * 10 ** (TARGET_DBFS / 20))


def _samples(raw, channels=1):
    if not isinstance(raw, bytes) or not raw or len(raw) % (2 * channels):
        raise ValueError('Expected nonempty complete PCM16 frames')
    return np.frombuffer(raw, dtype='<i2').astype(np.float64)


def _encode(samples):
    if not np.isfinite(samples).all():
        raise ValueError('Non-finite conditioned samples')
    return np.clip(np.rint(samples), -32768, 32767).astype('<i2').tobytes()


def canonicalize(raw: bytes, rate: int, channels: int):
    """Downmix and band-limit native PCM16; no gain normalization here."""
    if type(rate) is not int or not 8000 <= rate <= 192000:
        raise ValueError('Expected integer sample rate from 8000 to 192000')
    if type(channels) is not int or channels not in (1, 2):
        raise ValueError('Expected mono or stereo input')
    native = _samples(raw, channels).reshape(-1, channels)
    # Preserve worst-channel clipping evidence before cancellation/downmixing.
    native_near_full = float(np.max(np.mean(np.abs(native) / 32768 >= .999, axis=0)))
    samples = native.mean(axis=1)
    native_count = len(samples)
    if rate != RATE:
        samples = soxr.resample(samples, rate, RATE, quality='HQ')
    if not len(samples):
        raise ValueError('Resampling produced no samples')
    saturated = int(np.count_nonzero((samples < -32768) | (samples > 32767)))
    result = _encode(samples)
    return result, {
        'input_rate': rate, 'input_channels': channels,
        'input_sample_count': native_count, 'output_sample_count': len(samples),
        'output_rate': RATE, 'resampler': 'bypass' if rate == RATE else 'soxr_hq',
        'downmix': 'arithmetic_mean' if channels == 2 else 'bypass',
        'quantization': 'round_nearest_even_clip_pcm16',
        'saturated_samples': saturated,
        'native_near_full_scale_fraction': native_near_full,
        'canonical_pcm_sha256': hashlib.sha256(result).hexdigest(),
        'numpy_version': np.__version__, 'soxr_version': soxr.__version__,
        'libsoxr_version': soxr.__libsoxr_version__,
    }


def peak_normalize(raw: bytes):
    """Set sample peak to approximately -3 dBFS, preserving exact silence."""
    samples = _samples(raw)
    peak = float(np.max(np.abs(samples)))
    gain = TARGET_PEAK / peak if peak else 1.0
    result = _encode(samples * gain)
    return result, {
        'target_peak_dbfs': TARGET_DBFS,
        'input_peak_dbfs': 20 * math.log10(peak / 32768) if peak else None,
        'output_peak_dbfs': 20 * math.log10(TARGET_PEAK / 32768) if peak else None,
        'gain_db': 20 * math.log10(gain),
    }
