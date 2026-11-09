"""Additive noise at a target SNR (backlog 3.4 partial; PRD FR-D7 P1). CPU, no I/O.

`mix_at_snr` adds a noise recording to mono PCM16 speech at a signal-to-noise ratio
from 0 to 20 dB. The noise excerpt starts at a seeded offset and wraps around when the
noise is shorter than the speech. Power is the mean square over the whole clip (no VAD
yet, backlog 3.2), so SNR here is clip-level, not speech-active-level. The mix is
rounded to int16 and clipped; clipped samples are counted in the record. Noise corpora
(babble, street, vehicle) and room impulse responses are not provided here.
Inputs must be `bytes` at the same sample rate; the caller resamples first.
"""
import hashlib

import numpy as np

MIN_SNR_DB = 0.0
MAX_SNR_DB = 20.0
NOISE_IMPLEMENTATION = 'ava-additive-noise-v1'


def _pcm(raw, name):
    if not isinstance(raw, bytes) or not raw or len(raw) % 2:
        raise ValueError(f'{name} must be nonempty mono PCM16 bytes')
    return np.frombuffer(raw, dtype='<i2').astype(np.float64)


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not np.isfinite(value):
        raise ValueError(f'{name} must be a finite number')
    return float(value)


def mix_at_snr(speech, noise, snr_db, seed):
    """Return (mixed PCM16 bytes, record). The noise is scaled so that
    10*log10(P_speech / P_noise_excerpt) equals snr_db before rounding. Clipping is
    counted, not refused: callers must check `clipped_samples` and `achieved_snr_db`.
    The caller chooses snr_db and seed; a per-clip derivation is not implemented yet."""
    clean = _pcm(speech, 'speech')
    source = _pcm(noise, 'noise')
    snr = _number(snr_db, 'snr_db')
    if not MIN_SNR_DB <= snr <= MAX_SNR_DB:
        raise ValueError(f'snr_db must be from {MIN_SNR_DB} to {MAX_SNR_DB} dB (PRD FR-D7)')
    if type(seed) is not int or seed < 0:
        raise ValueError('seed must be a non-negative integer for a reproducible offset')
    speech_power = float(np.mean(clean ** 2))
    if speech_power == 0:
        raise ValueError('speech is digital silence; SNR is undefined')
    offset = int(np.random.default_rng(seed).integers(len(source)))
    excerpt = np.take(source, np.arange(offset, offset + len(clean)), mode='wrap')
    noise_power = float(np.mean(excerpt ** 2))
    if noise_power == 0:
        raise ValueError('noise excerpt is digital silence; it cannot reach a finite SNR')
    gain = float(np.sqrt(speech_power / (noise_power * 10 ** (snr / 10))))
    mixed = np.rint(clean + gain * excerpt)
    clipped = int(np.count_nonzero((mixed > 32767) | (mixed < -32768)))
    out_samples = np.clip(mixed, -32768, 32767)
    added_power = float(np.mean((out_samples - clean) ** 2))
    achieved = float(10 * np.log10(speech_power / added_power)) if added_power else None
    out = out_samples.astype('<i2').tobytes()
    return out, {
        'augmentation': 'additive_noise', 'implementation': NOISE_IMPLEMENTATION,
        'snr_db': snr, 'achieved_snr_db': achieved,
        'snr_basis': 'whole_clip_mean_square', 'seed': seed,
        'quantization': 'round_half_even_clip_pcm16',
        'speech_sha256': hashlib.sha256(speech).hexdigest(),
        'mixed_pcm_sha256': hashlib.sha256(out).hexdigest(),
        'noise_repeats': -(-(offset + len(clean)) // len(source)),
        'rng': 'numpy.random.default_rng(seed).integers(noise_samples)',
        'numpy_version': np.__version__, 'noise_offset': offset,
        'noise_samples': len(source), 'noise_wrapped': offset + len(clean) > len(source),
        'noise_sha256': hashlib.sha256(noise).hexdigest(), 'noise_gain': gain,
        'clipped_samples': clipped, 'samples': len(clean),
    }
