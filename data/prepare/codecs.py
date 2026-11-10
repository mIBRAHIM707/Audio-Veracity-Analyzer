"""G.711 file codec path (backlog 3.3 partial; PRD FR-D6/FR-D13, FR-I3). CPU, no I/O.

mu-law and A-law follow the ITU-T G.711 segment encoding as implemented by the Sun
reference code (the same tables as CPython's `audioop`, which tests use as an oracle).
`telephone_roundtrip` takes canonical 16 kHz PCM16 to the 8 kHz telephone band, encodes
and decodes it, and returns 8 kHz PCM16; bring it back to the model grid through
`conditioning.canonicalize(decoded, 8000, 1)` so the shared path does the upsampling.
`drop_packets` applies seeded 20 ms packet loss (0-5 %, PRD FR-D6) with concealment;
`drop_late_packets` models jitter as late-packet loss behind a fixed buffer; the
roundtrip combines both from per-clip plans. GSM and AMR-NB are not implemented here yet.
Inputs must be `bytes` (not bytearray/memoryview), matching `conditioning.py`.
"""
import hashlib
import math

import numpy as np

if __package__:
    from .conditioning import library_versions, resample_to_pcm16
else:  # direct script execution, as prepare_audio.py supports
    from conditioning import library_versions, resample_to_pcm16

TELEPHONE_RATE = 8000
G711_IMPLEMENTATION = 'sun_reference_tables_numpy'
LAWS = ('ulaw', 'alaw')
_ULAW_END = np.array([0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF])
_ALAW_END = np.array([0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF])


def _pcm(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) % 2:
        raise ValueError('Expected nonempty mono PCM16 bytes')
    return np.frombuffer(raw, dtype='<i2').astype(np.int64)


def _codes(encoded):
    if not isinstance(encoded, bytes) or not encoded:
        raise ValueError('Expected nonempty G.711 bytes')
    return np.frombuffer(encoded, dtype=np.uint8).astype(np.int64)


def _check_law(law):
    if law not in LAWS:
        raise ValueError(f'Unknown G.711 law {law!r}; expected one of {LAWS}')


def _ulaw_encode(pcm):
    value = pcm >> 2
    mask = np.where(value < 0, 0x7F, 0xFF)
    value = np.minimum(np.abs(value), 8159) + 33
    seg = np.searchsorted(_ULAW_END, value)
    code = (seg << 4) | ((value >> (seg + 1)) & 0x0F)
    return np.where(seg >= 8, 0x7F, code) ^ mask


def _alaw_encode(pcm):
    value = pcm >> 3
    negative = value < 0
    mask = np.where(negative, 0x55, 0xD5)
    value = np.where(negative, -value - 1, value)
    seg = np.searchsorted(_ALAW_END, value)
    # np.where evaluates both branches; maximum() only keeps the unused branch's shift valid.
    low = np.where(seg < 2, value >> 1, value >> np.maximum(seg, 1))
    code = (seg << 4) | (low & 0x0F)
    return np.where(seg >= 8, 0x7F, code) ^ mask


def _ulaw_decode(codes):
    value = ~codes & 0xFF
    t = (((value & 0x0F) << 3) + 0x84) << ((value & 0x70) >> 4)
    return np.where(value & 0x80, 0x84 - t, t - 0x84)


def _alaw_decode(codes):
    value = codes ^ 0x55
    seg = (value & 0x70) >> 4
    t = (value & 0x0F) << 4
    t = np.where(seg == 0, t + 8, (t + 0x108) << np.maximum(seg - 1, 0))
    return np.where(value & 0x80, t, -t)


def encode(raw, law):
    """PCM16 little-endian mono -> one G.711 byte per sample."""
    _check_law(law)
    pcm = _pcm(raw)
    return (_ulaw_encode(pcm) if law == 'ulaw' else _alaw_encode(pcm)).astype(np.uint8).tobytes()


def decode(encoded, law):
    """G.711 bytes -> PCM16 little-endian mono."""
    _check_law(law)
    codes = _codes(encoded)
    return (_ulaw_decode(codes) if law == 'ulaw' else _alaw_decode(codes)).astype('<i2').tobytes()


FRAME_SAMPLES = 160  # 20 ms G.711 packet at 8 kHz
MAX_LOSS_RATE = 0.05  # PRD FR-D6: randomized packet loss from 0 to 5%
CONCEALMENTS = ('silence', 'repeat')
_SILENCE_CODE = {'ulaw': 0xFF, 'alaw': 0xD5}


LOSS_DERIVATION = 'ava-packet-loss-v1'


def derive_loss(master_seed, clip_id):
    """Deterministic per-clip loss plan from a master seed and the exact manifest clip ID
    string (not Unicode-normalized). The rate is approximately uniform in
    [0, MAX_LOSS_RATE] at 1e-6 resolution (PRD FR-D6 "randomized"), so clips do not
    share one loss pattern. Pass the plan to telephone_roundtrip(loss_plan=...), which
    records every field."""
    if type(master_seed) is not int or master_seed < 0:
        raise ValueError('master_seed must be a non-negative integer')
    if not isinstance(clip_id, str) or not clip_id:
        raise ValueError('clip_id must be a nonempty string')
    digest = hashlib.sha256(f'{LOSS_DERIVATION}\0{master_seed}\0{clip_id}'.encode()).digest()
    rate_draw = int.from_bytes(digest[:8], 'big') / 2 ** 64
    return {'derivation': LOSS_DERIVATION, 'master_seed': master_seed, 'clip_id': clip_id,
            'loss_rate': min(round(rate_draw * MAX_LOSS_RATE, 6), MAX_LOSS_RATE),
            'seed': int.from_bytes(digest[8:16], 'big')}


def _conceal(codes, lost, law, concealment):
    """Replace each lost 20 ms packet by the previous received packet ('repeat') or the
    silence code (no previous packet, or 'silence')."""
    output, previous = codes.copy(), None
    for index in range(len(lost)):
        frame = slice(index * FRAME_SAMPLES, (index + 1) * FRAME_SAMPLES)
        if not lost[index]:
            previous = codes[frame]
            continue
        if concealment == 'repeat' and previous is not None:
            output[frame] = np.resize(previous, len(output[frame]))
        else:
            output[frame] = _SILENCE_CODE[law]
    return output


MAX_JITTER_MS = 100  # bound on the mean extra network delay accepted for augmentation
MAX_BUFFER_MS = 400


def _indices_sha256(mask):
    return hashlib.sha256(np.flatnonzero(mask).astype('<i4').tobytes()).hexdigest()


def _late_mask(count, jitter_ms, buffer_ms, seed):
    """Validated late-packet mask and its record (no concealment fields)."""
    for name, value, upper in (('jitter_ms', jitter_ms, MAX_JITTER_MS),
                               ('buffer_ms', buffer_ms, MAX_BUFFER_MS)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not np.isfinite(value) or not 0 <= value <= upper:
            raise ValueError(f'{name} must be a finite number from 0 to {upper}')
    if jitter_ms and math.exp(-buffer_ms / jitter_ms) > MAX_LOSS_RATE:
        raise ValueError('expected late-packet fraction exp(-buffer_ms/jitter_ms) exceeds '
                         f'the FR-D6 loss bound {MAX_LOSS_RATE}')
    if type(seed) is not int or seed < 0:
        raise ValueError('seed must be a non-negative integer for reproducible jitter')
    late = np.random.default_rng(seed).exponential(1.0, count) * jitter_ms > buffer_ms
    return late, {
        'jitter_ms': float(jitter_ms), 'buffer_ms': float(buffer_ms), 'seed': seed,
        'model': 'exponential_delay_fixed_buffer_late_loss',
        'jitter_definition': 'mean of independent per-packet extra delay (not RFC 3550 interarrival jitter)',
        'expected_late_fraction': math.exp(-buffer_ms / jitter_ms) if jitter_ms else 0.0,
        'rng': 'numpy.random.default_rng(seed).exponential(1.0, packets) * jitter_ms > buffer_ms',
        'numpy_version': np.__version__, 'packet_ms': 20, 'packets': count,
        'late_packets': int(late.sum()), 'late_indices_sha256': _indices_sha256(late),
    }


def drop_late_packets(encoded, law, jitter_ms, buffer_ms, seed, concealment='repeat'):
    """Seeded jitter as late-packet loss (PRD FR-D6 jitter, FR-I2 jitter buffer): each
    20 ms packet gets an independent exponential extra delay with mean `jitter_ms`; a
    packet whose delay exceeds the fixed playout buffer `buffer_ms` misses its slot and
    is concealed like a lost packet. `jitter_ms` is that mean extra delay, not RFC 3550
    interarrival jitter, and delays are uncorrelated. Settings whose expected late fraction
    exp(-buffer_ms/jitter_ms) exceeds MAX_LOSS_RATE are refused, so jitter stays inside
    the FR-D6 loss range. Packets are never reordered in the output (a fixed
    buffer plays in sequence order). Returns (concealed bytes, record)."""
    _check_law(law)
    codes = _codes(encoded).astype(np.uint8)
    if concealment not in CONCEALMENTS:
        raise ValueError(f'concealment must be one of {CONCEALMENTS}')
    late, record = _late_mask(-(-len(codes) // FRAME_SAMPLES), jitter_ms, buffer_ms, seed)
    return _conceal(codes, late, law, concealment).tobytes(), {**record, 'concealment': concealment}


JITTER_DERIVATION = 'ava-jitter-v1'
PLAN_BUFFER_MS = 60  # fixed playout buffer used by derived plans
PLAN_MAX_JITTER_MS = 20  # exp(-60/20) = 4.98 % expected late packets, inside FR-D6's 5 %


def derive_jitter(master_seed, clip_id):
    """Deterministic per-clip jitter plan from a master seed and the exact manifest clip
    ID string: mean extra delay approximately uniform in [0, PLAN_MAX_JITTER_MS] ms at
    1e-6 ms resolution, buffer PLAN_BUFFER_MS, and a seed. Its tag differs from
    derive_loss, so loss and jitter draws are independent for the same clip. Uniform
    delay is not uniform late loss: at 10 ms the expected late fraction is exp(-6),
    about 0.25 %, so most clips get far less than 5 % late packets (unconfirmed choice)."""
    if type(master_seed) is not int or master_seed < 0:
        raise ValueError('master_seed must be a non-negative integer')
    if not isinstance(clip_id, str) or not clip_id:
        raise ValueError('clip_id must be a nonempty string')
    digest = hashlib.sha256(f'{JITTER_DERIVATION}\0{master_seed}\0{clip_id}'.encode()).digest()
    draw = int.from_bytes(digest[:8], 'big') / 2 ** 64
    return {'derivation': JITTER_DERIVATION, 'master_seed': master_seed, 'clip_id': clip_id,
            'jitter_ms': min(round(draw * PLAN_MAX_JITTER_MS, 6), PLAN_MAX_JITTER_MS),
            'buffer_ms': PLAN_BUFFER_MS, 'seed': int.from_bytes(digest[8:16], 'big')}


def drop_packets(encoded, law, loss_rate, seed, concealment='repeat'):
    """Seeded Bernoulli loss of 20 ms G.711 packets, concealed by the silence code or by
    repeating the previous received packet (silence when none precedes). Returns
    (concealed bytes, record). A final short packet is a packet too."""
    _check_law(law)
    codes = _codes(encoded).astype(np.uint8)
    if concealment not in CONCEALMENTS:
        raise ValueError(f'concealment must be one of {CONCEALMENTS}')
    lost, record = _loss_mask(-(-len(codes) // FRAME_SAMPLES), loss_rate, seed)
    return _conceal(codes, lost, law, concealment).tobytes(), {**record, 'concealment': concealment}


def _loss_mask(count, loss_rate, seed):
    """Validated Bernoulli loss mask and its record (no concealment fields)."""
    if isinstance(loss_rate, bool) or not isinstance(loss_rate, (int, float)) \
            or not 0 <= loss_rate <= MAX_LOSS_RATE:
        raise ValueError(f'loss_rate must be a number from 0 to {MAX_LOSS_RATE}')
    if type(seed) is not int or seed < 0:
        raise ValueError('seed must be a non-negative integer for reproducible loss')
    lost = np.random.default_rng(seed).random(count) < loss_rate
    return lost, {
        'loss_rate': float(loss_rate), 'seed': seed, 'model': 'bernoulli_per_packet',
        'rng': 'numpy.random.default_rng(seed).random(packets) < loss_rate',
        'numpy_version': np.__version__,
        'packet_ms': 20, 'packets': count, 'lost_packets': int(lost.sum()),
        'lost_indices_sha256': _indices_sha256(lost),
    }


def _plan_fields(plan):
    return {key: plan[key] for key in ('derivation', 'master_seed', 'clip_id')}


def telephone_roundtrip(canonical, law, *, rate=16000, loss_rate=0.0, seed=None,
                        concealment='repeat', loss_plan=None, jitter_plan=None):
    """Canonical PCM16 at `rate` -> soxr HQ 8 kHz -> G.711 encode -> optional seeded packet
    loss and/or jitter (late packets, `jitter_plan` from derive_jitter) -> decode -> 8 kHz
    PCM16. Returns (decoded bytes, lineage record). Lost and late packets form one mask,
    concealed once, so 'repeat' only ever repeats a packet that arrived in time (concealing
    loss and then jitter separately would differ when the two are adjacent). The 5 % loss
    bound and the about 5 % late-packet bound are expected rates for each mechanism
    separately, not a cap on the realized union: `packet_concealment` records the union
    count and fraction, which can exceed 5 % of a clip's packets. Raises ValueError on
    invalid input."""
    _check_law(law)
    if concealment not in CONCEALMENTS:
        raise ValueError(f'concealment must be one of {CONCEALMENTS}')
    if type(rate) is not int or not TELEPHONE_RATE <= rate <= 192000:
        raise ValueError('Expected integer input rate from 8000 to 192000')
    if loss_plan is not None:
        if loss_rate or seed is not None:
            raise ValueError('Give either loss_plan or loss_rate/seed, not both')
        if not isinstance(loss_plan, dict) or loss_plan != derive_loss(
                loss_plan.get('master_seed'), loss_plan.get('clip_id')):
            raise ValueError('loss_plan must come unchanged from derive_loss')
    elif loss_rate or seed is not None:
        _loss_mask(0, loss_rate, seed)  # validate before resampling
    if jitter_plan is not None:
        if not isinstance(jitter_plan, dict) or jitter_plan != derive_jitter(
                jitter_plan.get('master_seed'), jitter_plan.get('clip_id')):
            raise ValueError('jitter_plan must come unchanged from derive_jitter')
        if loss_plan is not None and (loss_plan['clip_id'], loss_plan['master_seed']) != (
                jitter_plan['clip_id'], jitter_plan['master_seed']):
            raise ValueError('loss_plan and jitter_plan name different clips or master seeds')
    samples = _pcm(canonical).astype(np.float64)
    narrow, saturated = resample_to_pcm16(samples, rate, TELEPHONE_RATE)
    encoded = encode(narrow, law)
    codes = np.frombuffer(encoded, dtype=np.uint8)
    count = -(-len(codes) // FRAME_SAMPLES)
    missing, loss, jitter = np.zeros(count, dtype=bool), None, None
    if loss_plan is not None:
        lost, loss = _loss_mask(count, loss_plan['loss_rate'], loss_plan['seed'])
        loss.update(**_plan_fields(loss_plan))
    elif loss_rate or seed is not None:
        lost, loss = _loss_mask(count, loss_rate, seed)
    if loss is not None:
        loss.update(concealment=concealment, applied_via='packet_concealment')
        missing |= lost
    if jitter_plan is not None:
        late, jitter = _late_mask(count, jitter_plan['jitter_ms'], jitter_plan['buffer_ms'],
                                  jitter_plan['seed'])
        jitter.update(concealment=concealment, applied_via='packet_concealment',
                      **_plan_fields(jitter_plan))
        missing |= late
    impairment = None
    if loss is None and jitter is None:
        received = encoded
    else:
        received = _conceal(codes, missing, law, concealment).tobytes()
        both = loss is not None and jitter is not None
        impairment = {
            'mask': 'lost_or_late', 'concealment': concealment, 'packets': count,
            'concealed_packets': int(missing.sum()),
            'concealed_fraction': float(missing.sum()) / count,
            'lost_and_late_packets': int((lost & late).sum()) if both else None,
            'concealed_indices_sha256': _indices_sha256(missing),
        }
    decoded = decode(received, law)
    return decoded, {
        'codec': f'g711_{law}', 'codec_implementation': G711_IMPLEMENTATION,
        'codec_rate': TELEPHONE_RATE, 'input_rate': rate,
        'input_sample_count': len(samples), 'input_sha256': hashlib.sha256(canonical).hexdigest(),
        'resampler': 'bypass' if rate == TELEPHONE_RATE else 'soxr_hq',
        'quantization': 'round_nearest_even_clip_pcm16',
        'saturated_samples': saturated, 'sample_count': len(decoded) // 2,
        'encoded_sha256': hashlib.sha256(encoded).hexdigest(),
        'decoded_pcm_sha256': hashlib.sha256(decoded).hexdigest(),
        'packet_loss': loss, 'jitter': jitter, 'packet_concealment': impairment,
        **library_versions(),
    }
