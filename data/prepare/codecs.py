"""G.711 file codec path (backlog 3.3 partial; PRD FR-D6/FR-D13, FR-I3). CPU, no I/O.

mu-law and A-law follow the ITU-T G.711 segment encoding as implemented by the Sun
reference code (the same tables as CPython's `audioop`, which tests use as an oracle).
`telephone_roundtrip` takes canonical 16 kHz PCM16 to the 8 kHz telephone band, encodes
and decodes it, and returns 8 kHz PCM16; bring it back to the model grid through
`conditioning.canonicalize(decoded, 8000, 1)` so the shared path does the upsampling.
GSM, AMR-NB, packet loss and jitter are not implemented here yet.
Inputs must be `bytes` (not bytearray/memoryview), matching `conditioning.py`.
"""
import hashlib

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


def telephone_roundtrip(canonical, law, *, rate=16000):
    """Canonical PCM16 at `rate` -> soxr HQ 8 kHz -> G.711 encode/decode -> 8 kHz PCM16.
    Returns (decoded bytes, lineage record). Raises ValueError on invalid input."""
    _check_law(law)
    if type(rate) is not int or not TELEPHONE_RATE <= rate <= 192000:
        raise ValueError('Expected integer input rate from 8000 to 192000')
    samples = _pcm(canonical).astype(np.float64)
    narrow, saturated = resample_to_pcm16(samples, rate, TELEPHONE_RATE)
    encoded = encode(narrow, law)
    decoded = decode(encoded, law)
    return decoded, {
        'codec': f'g711_{law}', 'codec_implementation': G711_IMPLEMENTATION,
        'codec_rate': TELEPHONE_RATE, 'input_rate': rate,
        'input_sample_count': len(samples), 'input_sha256': hashlib.sha256(canonical).hexdigest(),
        'resampler': 'bypass' if rate == TELEPHONE_RATE else 'soxr_hq',
        'quantization': 'round_nearest_even_clip_pcm16',
        'saturated_samples': saturated, 'sample_count': len(decoded) // 2,
        'encoded_sha256': hashlib.sha256(encoded).hexdigest(),
        'decoded_pcm_sha256': hashlib.sha256(decoded).hexdigest(),
        'packet_loss': None, 'jitter': None, **library_versions(),
    }
