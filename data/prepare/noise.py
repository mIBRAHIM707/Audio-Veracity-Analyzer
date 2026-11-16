"""Additive noise at a target SNR (backlog 3.4 partial; PRD FR-D7 P1). CPU, no I/O.

`mix_at_snr` adds a noise recording to mono PCM16 speech at a signal-to-noise ratio
from 0 to 20 dB. The noise excerpt starts at a seeded offset and wraps around when the
noise is shorter than the speech. By default power is the mean square over the whole
clip (clip-level SNR); with `active_mask` (from `speech_mask` and a VAD, 15 Nov) the
speech power is taken over speech-active samples only. The mix is
rounded to int16 and clipped; clipped samples are counted in the record. Noise corpora
(babble, street, vehicle) are not provided here; room impulse responses are reverb.py.
Inputs must be `bytes` at the same sample rate; the caller resamples first.
"""
import hashlib

import numpy as np

MIN_SNR_DB = 0.0
MAX_SNR_DB = 20.0
NOISE_IMPLEMENTATION = 'ava-additive-noise-v1'
NOISE_DERIVATION = 'ava-noise-snr-v1'


def derive_noise(master_seed, clip_id):
    """Deterministic per-clip noise plan from a master seed and the exact manifest clip ID
    string: SNR approximately uniform in [MIN_SNR_DB, MAX_SNR_DB] at 1e-6 dB resolution
    and an offset seed, both from SHA-256 of a versioned tag (separate from the packet-
    loss tag, so the two plans are independent). Use with mix_planned."""
    if type(master_seed) is not int or master_seed < 0:
        raise ValueError('master_seed must be a non-negative integer')
    if not isinstance(clip_id, str) or not clip_id:
        raise ValueError('clip_id must be a nonempty string')
    digest = hashlib.sha256(f'{NOISE_DERIVATION}\0{master_seed}\0{clip_id}'.encode()).digest()
    draw = int.from_bytes(digest[:8], 'big') / 2 ** 64
    snr = min(round(MIN_SNR_DB + draw * (MAX_SNR_DB - MIN_SNR_DB), 6), MAX_SNR_DB)
    return {'derivation': NOISE_DERIVATION, 'master_seed': master_seed, 'clip_id': clip_id,
            'snr_db': snr, 'seed': int.from_bytes(digest[8:16], 'big')}


def mix_planned(speech, noise, plan, *, active_mask=None, mask_source=None):
    """mix_at_snr with a plan that must come unchanged from derive_noise; the record
    also holds the derivation tag, master seed and clip ID so the plan can be rebuilt.
    The caller owns binding the plan to the right clip's audio (speech_sha256 records it)."""
    try:
        valid = isinstance(plan, dict) and plan == derive_noise(plan.get('master_seed'),
                                                                plan.get('clip_id'))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('plan must come unchanged from derive_noise')
    mixed, record = mix_at_snr(speech, noise, plan['snr_db'], plan['seed'], active_mask=active_mask,
                               mask_source=mask_source)
    record.update(derivation=plan['derivation'], master_seed=plan['master_seed'],
                  clip_id=plan['clip_id'])
    return mixed, record


def _pcm(raw, name):
    if not isinstance(raw, bytes) or not raw or len(raw) % 2:
        raise ValueError(f'{name} must be nonempty mono PCM16 bytes')
    return np.frombuffer(raw, dtype='<i2').astype(np.float64)


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not np.isfinite(value):
        raise ValueError(f'{name} must be a finite number')
    return float(value)


VAD_FRAME_SAMPLES = 512  # Silero at 16 kHz; the serving windower's frame (services/scorer)
VAD_THRESHOLD = 0.5
MIN_ACTIVE_SAMPLES = 8000  # 0.5 s at 16 kHz of speech before a speech-active SNR is defined
# Names the whole mask rule (frame, scaling, threshold, padding, reset per clip, minimum
# active samples); bump it whenever any of those change, so lineage tells datasets apart.
MASK_RULE = 'ava-speech-mask-v1'


class VadError(RuntimeError):
    """The VAD failed or returned an invalid probability: a systemic fault (model or
    runtime), not a property of one clip. Callers abort rather than list it per clip."""


class InsufficientSpeech(ValueError):
    """Too few speech-active samples for a speech-active SNR (a per-clip refusal)."""

    def __init__(self, active_samples):
        super().__init__(f'{active_samples} speech-active samples, fewer than {MIN_ACTIVE_SAMPLES}; '
                         'speech-active SNR undefined')
        self.active_samples = active_samples


def speech_mask(pcm16k, vad, *, threshold=VAD_THRESHOLD):
    """Per-sample speech mask for 16 kHz mono PCM16 from a VAD callable on 512-sample
    float32 frames scaled int16/32768, with the serving windower's frame rule and
    threshold (services/scorer/speech_windows.py: probability >= threshold is speech,
    the last partial frame zero-padded). It runs on the given clip with the VAD reset
    first (`vad.reset()` when present), whereas serving runs on the received stream with
    state carried across the call. Returns (numpy bool mask, per-frame decisions as a
    hex string). A VAD exception or a probability outside 0..1 (or NaN) raises VadError."""
    samples = np.frombuffer(_pcm_bytes(pcm16k, 'pcm16k'), dtype='<i2')
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold < 1:
        raise ValueError('threshold must be a number strictly between 0 and 1')
    try:
        if hasattr(vad, 'reset'):
            vad.reset()
        count = -(-len(samples) // VAD_FRAME_SAMPLES)
        padded = np.zeros(count * VAD_FRAME_SAMPLES, dtype=np.float32)
        padded[:len(samples)] = samples.astype(np.float32) / 32768
        decisions = np.zeros(count, dtype=bool)
        for index in range(count):
            probability = float(vad(padded[index * VAD_FRAME_SAMPLES:(index + 1) * VAD_FRAME_SAMPLES]))
            if not 0.0 <= probability <= 1.0:  # also refuses NaN
                raise VadError('VAD returned a probability outside 0..1')
            decisions[index] = probability >= threshold
    except VadError:
        raise
    except Exception as error:  # model or runtime fault, whatever its type
        raise VadError(f'VAD failed: {type(error).__name__}: {error}') from error
    mask = np.repeat(decisions, VAD_FRAME_SAMPLES)[:len(samples)]
    return mask, np.packbits(decisions).tobytes().hex()


def _pcm_bytes(raw, name):
    if not isinstance(raw, bytes) or not raw or len(raw) % 2:
        raise ValueError(f'{name} must be nonempty mono PCM16 bytes')
    return raw


def mix_at_snr(speech, noise, snr_db, seed, *, active_mask=None, mask_source=None):
    """Return (mixed PCM16 bytes, record). The noise is scaled so that
    10*log10(P_speech / P_noise_excerpt) equals snr_db before rounding. Clipping is
    counted, not refused: callers must check `clipped_samples` and `achieved_snr_db`.
    For per-clip randomized SNR use derive_noise and mix_planned.

    `active_mask` (a bool array, one entry per speech sample, e.g. from `speech_mask`)
    makes P_speech the mean square over speech-active samples only (`snr_basis:
    speech_active_mean_square`, the FR-D7 SNR of speech rather than of speech plus
    pauses); the noise power stays over the whole excerpt. At least MIN_ACTIVE_SAMPLES
    must be active. `mask_source` (a small JSON-able dict naming the VAD, its model
    hash and threshold) is recorded with the mask's hash and active fraction."""
    clean = _pcm(speech, 'speech')
    source = _pcm(noise, 'noise')
    snr = _number(snr_db, 'snr_db')
    if not MIN_SNR_DB <= snr <= MAX_SNR_DB:
        raise ValueError(f'snr_db must be from {MIN_SNR_DB} to {MAX_SNR_DB} dB (PRD FR-D7)')
    if type(seed) is not int or seed < 0:
        raise ValueError('seed must be a non-negative integer for a reproducible offset')
    basis, active = 'whole_clip_mean_square', None
    if active_mask is not None:
        active = np.asarray(active_mask)
        if active.dtype != bool or active.shape != clean.shape:
            raise ValueError('active_mask must be a bool array with one entry per speech sample')
        if int(active.sum()) < MIN_ACTIVE_SAMPLES:
            raise InsufficientSpeech(int(active.sum()))
        if mask_source is not None and not isinstance(mask_source, dict):
            raise ValueError('mask_source must be a dict')
        basis = 'speech_active_mean_square'
    elif mask_source is not None:
        raise ValueError('mask_source applies only with an active_mask')
    speech_power = float(np.mean(clean[active] ** 2)) if active is not None else float(np.mean(clean ** 2))
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
    extra = {}
    if active is not None:
        extra = {'active_fraction': float(active.mean()), 'active_samples': int(active.sum()),
                 'active_mask_sha256': hashlib.sha256(np.packbits(active).tobytes()).hexdigest(),
                 'mask_source': mask_source}
    return out, {
        **extra,
        'augmentation': 'additive_noise', 'implementation': NOISE_IMPLEMENTATION,
        'snr_db': snr, 'achieved_snr_db': achieved,
        'snr_basis': basis, 'seed': seed,
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
