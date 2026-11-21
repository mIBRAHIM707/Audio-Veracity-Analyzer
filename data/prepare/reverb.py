"""Room impulse response (RIR) convolution (backlog 3.4; PRD FR-D7 "room impulse
responses for handset and speakerphone", FR-D13 lineage). CPU, no I/O.

`apply_rir` convolves 16 kHz mono PCM16 speech with a 16 kHz mono PCM16 RIR:

1. The RIR is aligned on its direct path, the first arrival: the first sample whose
   magnitude is within 20 dB of the largest (an early reflection can be stronger than
   the direct sound, so the largest sample alone is not used). Samples before it are
   dropped, so the reverberant copy is not delayed against its parent (window and VAD
   timing stay comparable), and the dropped energy is recorded. A negative direct path
   is flipped positive (measured polarity reflects the equipment), recorded as
   `direct_sign`.
2. The aligned RIR is convolved with the speech through a zero-padded real FFT; the
   output keeps the input length (the reverberant tail past the last input sample is
   cut, and its energy share recorded).
3. The result is scaled so its mean square equals the input's (level preserved, so a
   later SNR or loudness check sees the same speech level), rounded half-to-even to
   int16 and clipped; clipped samples are counted. Callers refuse clipping copies.

Recorded per copy: RIR hash and length, direct-path index, direct-to-reverberant ratio
(DRR, energy within +/-2.5 ms of the direct path over the energy after it), dropped
pre-direct energy, cut tail energy, gain, clipping and hashes. DRR describes the RIR; it
is not a handset/speakerphone classifier. Which RIRs stand for a handset or a
speakerphone is the catalogue's label, chosen by a person (an open PRD question).

`derive_reverb` picks one RIR from a pool per clip by a SHA-256 draw under its own tag,
independent of the noise, loss, jitter and codec-mix draws. FFT rounding can differ by
platform in the last bits before int16 rounding; tests use tolerances, not digests.
"""
import hashlib

import numpy as np

RATE = 16000
REVERB_IMPLEMENTATION = 'ava-rir-convolution-v1'
REVERB_DERIVATION = 'ava-rir-pick-v1'
MAX_RIR_SAMPLES = 2 * RATE  # 2 s; room RIRs used for augmentation decay well before this
MAX_DIRECT_INDEX = RATE // 2  # a direct path later than 0.5 s means a bad or padded file
DIRECT_HALF_WIDTH = RATE * 25 // 10000  # 40 samples = 2.5 ms
FIRST_ARRIVAL_RATIO = 10  # first sample within 20 dB (amplitude / 10) of the RIR's peak magnitude
MAX_POOL = 64


def _pcm(raw, name, limit=None):
    if not isinstance(raw, bytes) or not raw or len(raw) % 2:
        raise ValueError(f'{name} must be nonempty mono PCM16 bytes')
    if limit is not None and len(raw) // 2 > limit:
        raise ValueError(f'{name} is longer than {limit} samples')
    return np.frombuffer(raw, dtype='<i2').astype(np.float64)


def rir_profile(rir):
    """Describe a PCM16 RIR: (aligned float64 RIR from the direct path, positive at the
    direct path; profile dict). Refuses silent RIRs, RIRs over MAX_RIR_SAMPLES and a
    direct path after 0.5 s. Callers that load RIR files call it up front, so a bad RIR
    stops a run instead of failing only the clips that drew it."""
    h = _pcm(rir, 'rir', MAX_RIR_SAMPLES)
    magnitude = np.abs(h)
    peak = float(magnitude.max())
    if peak == 0:
        raise ValueError('rir is digital silence')
    # Integer samples against an exact threshold: the same index on every platform.
    direct = int(np.flatnonzero(magnitude * FIRST_ARRIVAL_RATIO >= peak)[0])
    if direct > MAX_DIRECT_INDEX:
        raise ValueError(f'rir direct path at sample {direct} is later than {MAX_DIRECT_INDEX}')
    energy = h ** 2
    total = float(energy.sum())
    near = float(energy[max(0, direct - DIRECT_HALF_WIDTH):direct + DIRECT_HALF_WIDTH + 1].sum())
    late = float(energy[direct + DIRECT_HALF_WIDTH + 1:].sum())
    # Sign of the main lobe (largest sample within +/-2.5 ms), not of a band-limited
    # pulse's first side-lobe, which can be the first arrival (review L7).
    low = max(0, direct - DIRECT_HALF_WIDTH)
    lobe = low + int(np.argmax(magnitude[low:direct + DIRECT_HALF_WIDTH + 1]))
    sign = 1 if h[lobe] > 0 else -1
    aligned = h[direct:] * sign
    return aligned, {
        'rir_sha256': hashlib.sha256(rir).hexdigest(), 'rir_samples': len(h),
        'direct_rule': 'first_sample_within_20_db_of_peak', 'direct_index': direct, 'direct_sign': sign,
        'peak_index': int(np.argmax(magnitude)), 'direct_half_width_samples': DIRECT_HALF_WIDTH,
        'drr_db': None if late == 0 else float(10 * np.log10(near / late)),
        'pre_direct_energy_fraction': float(energy[:direct].sum()) / total,
        'aligned_rir_samples': len(aligned),
    }


def apply_rir(speech, rir):
    """Return (reverberant PCM16 bytes of the speech's length, record). See the module
    docstring for the alignment, length and level rules. Clipping is counted, not
    refused: callers must check `clipped_samples`."""
    x = _pcm(speech, 'speech')
    mean_square = float(np.mean(x ** 2))
    if mean_square == 0:
        raise ValueError('speech is digital silence; the level rule is undefined')
    h, profile = rir_profile(rir)
    size = len(x) + len(h) - 1
    fft_size = 1 << (size - 1).bit_length()
    full = np.fft.irfft(np.fft.rfft(x, fft_size) * np.fft.rfft(h, fft_size), fft_size)[:size]
    wet = full[:len(x)]
    wet_power = float(np.mean(wet ** 2))
    if wet_power == 0:
        raise ValueError('convolution produced silence')
    full_energy = float(np.sum(full ** 2))
    gain = float(np.sqrt(mean_square / wet_power))
    scaled = np.rint(wet * gain)
    clipped = int(np.count_nonzero((scaled > 32767) | (scaled < -32768)))
    out = np.clip(scaled, -32768, 32767).astype('<i2').tobytes()
    return out, {
        'augmentation': 'room_impulse_response', 'implementation': REVERB_IMPLEMENTATION,
        **profile,
        'alignment': 'drop_before_direct_path_direct_positive', 'length_rule': 'keep_input_length_cut_tail',
        'level_rule': 'match_input_whole_clip_mean_square', 'convolution': 'numpy.fft.rfft_zero_padded',
        'tail_energy_fraction': 0.0 if full_energy == 0 else float(np.sum(full[len(x):] ** 2)) / full_energy,
        'gain': gain, 'quantization': 'round_half_even_clip_pcm16', 'clipped_samples': clipped,
        'samples': len(x), 'speech_sha256': hashlib.sha256(speech).hexdigest(),
        'reverberant_pcm_sha256': hashlib.sha256(out).hexdigest(), 'numpy_version': np.__version__,
    }


def derive_reverb(master_seed, clip_id, pool_size):
    """Deterministic per-clip RIR choice: index = SHA-256 draw over (REVERB_DERIVATION,
    master seed, exact clip ID) modulo the pool size (bias < pool / 2**64)."""
    if type(master_seed) is not int or master_seed < 0:
        raise ValueError('master_seed must be a non-negative integer')
    if not isinstance(clip_id, str) or not clip_id:
        raise ValueError('clip_id must be a nonempty string')
    if type(pool_size) is not int or not 1 <= pool_size <= MAX_POOL:
        raise ValueError(f'pool_size must be an integer from 1 to {MAX_POOL}')
    digest = hashlib.sha256(f'{REVERB_DERIVATION}\0{master_seed}\0{clip_id}'.encode()).digest()
    return {'derivation': REVERB_DERIVATION, 'master_seed': master_seed, 'clip_id': clip_id,
            'pool_size': pool_size, 'index': int.from_bytes(digest[:8], 'big') % pool_size}


def apply_planned(speech, pool, plan):
    """apply_rir with the pool entry a derive_reverb plan names. `pool` is the ordered
    list of RIR PCM16 bytes the plan was drawn for; the record adds the plan fields."""
    try:
        valid = isinstance(plan, dict) and plan == derive_reverb(
            plan.get('master_seed'), plan.get('clip_id'), plan.get('pool_size'))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('plan must come unchanged from derive_reverb')
    if not isinstance(pool, (list, tuple)) or len(pool) != plan['pool_size']:
        raise ValueError('pool must be the list of RIRs the plan was drawn for')
    out, record = apply_rir(speech, pool[plan['index']])
    record.update(derivation=plan['derivation'], master_seed=plan['master_seed'], clip_id=plan['clip_id'],
                  pool_size=plan['pool_size'], pool_index=plan['index'])
    return out, record
