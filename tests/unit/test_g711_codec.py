"""G.711 file codec (backlog 3.3 partial; PRD FR-D6/FR-D13). Claude-authored, 9 November 2026.

CPython 3.11's `audioop` is an independent C implementation of the same G.711 tables and
serves as the oracle for exhaustive known-answer checks (it is removed in Python 3.13;
those cases then skip and the remaining property tests still run).
"""
import hashlib
import unittest
import warnings

import numpy as np
import soxr

from data.prepare.codecs import LAWS, decode, encode, telephone_roundtrip
from data.prepare.conditioning import canonicalize

with warnings.catch_warnings():
    warnings.simplefilter('ignore', DeprecationWarning)
    try:
        import audioop
    except ImportError:  # Python 3.13+
        audioop = None

ALL_PCM16 = np.arange(-32768, 32768, dtype='<i2').tobytes()
ALL_CODES = bytes(range(256))
ORACLE = {'ulaw': ('lin2ulaw', 'ulaw2lin'), 'alaw': ('lin2alaw', 'alaw2lin')}
RECORD_KEYS = {'codec', 'codec_implementation', 'codec_rate', 'input_rate', 'input_sample_count',
               'input_sha256', 'resampler', 'quantization', 'saturated_samples', 'sample_count',
               'encoded_sha256', 'decoded_pcm_sha256', 'packet_loss', 'jitter',
               'numpy_version', 'soxr_version', 'libsoxr_version'}


def pcm(values):
    return np.asarray(values, dtype='<i2').tobytes()


def tone(rate, seconds, frequency, amplitude):
    t = np.arange(round(rate * seconds)) / rate
    return pcm(np.rint(amplitude * np.sin(2 * np.pi * frequency * t)))


@unittest.skipIf(audioop is None, 'audioop oracle unavailable on this Python')
class OracleTests(unittest.TestCase):
    def test_encoder_matches_reference_for_every_pcm16_value(self):
        for law in LAWS:
            with self.subTest(law=law):
                self.assertEqual(encode(ALL_PCM16, law), getattr(audioop, ORACLE[law][0])(ALL_PCM16, 2))

    def test_decoder_matches_reference_for_every_code(self):
        for law in LAWS:
            with self.subTest(law=law):
                self.assertEqual(decode(ALL_CODES, law), getattr(audioop, ORACLE[law][1])(ALL_CODES, 2))


class PropertyTests(unittest.TestCase):
    def test_decode_then_encode_is_identity_except_mu_law_negative_zero(self):
        for law in LAWS:
            with self.subTest(law=law):
                again = encode(decode(ALL_CODES, law), law)
                differing = [code for code, back in zip(ALL_CODES, again) if code != back]
                # mu-law has two zero codes (0x7F and 0xFF); 0x7F decodes to 0 and re-encodes as 0xFF.
                self.assertEqual(differing, [0x7F] if law == 'ulaw' else [])

    def test_known_values_sign_symmetry_and_monotonic_encoding(self):
        self.assertEqual(encode(pcm([0, -1, 32767, -32768]), 'ulaw'), bytes([0xFF, 0x7E, 0x80, 0x00]))
        self.assertEqual(encode(pcm([0, -1, 32767, -32768]), 'alaw'), bytes([0xD5, 0x55, 0xAA, 0x2A]))
        for law in LAWS:
            with self.subTest(law=law):
                levels = np.frombuffer(decode(ALL_CODES, law), dtype='<i2').astype(np.int32)
                positive = np.sort(levels[levels > 0])
                self.assertEqual(sorted(-levels[levels < 0]), list(positive))  # symmetric magnitudes
                self.assertEqual(len(set(positive)), len(positive))  # distinct quantizer levels
                self.assertLessEqual(int(np.max(np.abs(levels))), 32768)
                # Monotonic: a larger input never decodes to a smaller level.
                coded = np.frombuffer(decode(encode(ALL_PCM16, law), law), dtype='<i2').astype(np.int32)
                self.assertTrue(np.all(np.diff(coded) >= 0))

    def test_quantization_error_is_bounded_relative_to_level(self):
        values = np.arange(-32768, 32768, dtype=np.int32)
        for law in LAWS:
            with self.subTest(law=law):
                back = np.frombuffer(decode(encode(ALL_PCM16, law), law), dtype='<i2').astype(np.int32)
                error = np.abs(back - values)
                # Logarithmic quantizer: error is at most ~1/32 of the level, plus a small floor.
                self.assertTrue(np.all(error <= np.abs(values) / 32 + 16), int(np.max(error - np.abs(values) / 32)))

    def test_invalid_inputs_are_rejected(self):
        for law in LAWS:
            for raw in (b'', b'\x00', 'text', None, bytearray(b'\x00\x00')):
                with self.subTest(law=law, raw=raw):
                    with self.assertRaises(ValueError):
                        encode(raw, law)
            for encoded in (b'', None, bytearray(b'\x00')):
                with self.subTest(law=law, encoded=encoded):
                    with self.assertRaises(ValueError):
                        decode(encoded, law)
        for law in ('mulaw', 'ULAW', None, 'g729'):
            with self.subTest(law=law):
                with self.assertRaises(ValueError):
                    encode(pcm([0]), law)
                with self.assertRaises(ValueError):
                    decode(b'\x00', law)
                with self.assertRaises(ValueError):
                    telephone_roundtrip(pcm([0, 0]), law)


class TelephoneRoundtripTests(unittest.TestCase):
    def test_wideband_input_is_band_limited_coded_and_returns_through_shared_path(self):
        source = tone(16000, 2.0, 1000, 12000)
        source = np.frombuffer(source, dtype='<i2') + np.frombuffer(tone(16000, 2.0, 6000, 6000), dtype='<i2')
        source = source.astype('<i2').tobytes()
        for law in LAWS:
            with self.subTest(law=law):
                decoded, record = telephone_roundtrip(source, law)
                # Independent expectation: soxr HQ to 8 kHz, round/clip, then the codec.
                narrow = soxr.resample(np.frombuffer(source, dtype='<i2').astype(np.float64), 16000, 8000,
                                       quality='HQ')
                expected = decode(encode(pcm(np.clip(np.rint(narrow), -32768, 32767)), law), law)
                self.assertEqual(decoded, expected)
                levels = set(np.frombuffer(decode(ALL_CODES, law), dtype='<i2').tolist())
                self.assertTrue(set(np.frombuffer(decoded, dtype='<i2').tolist()) <= levels)  # codec ran
                self.assertEqual(record['sample_count'], 16000)
                self.assertEqual((record['codec'], record['codec_rate'], record['resampler']),
                                 (f'g711_{law}', 8000, 'soxr_hq'))
                self.assertEqual(set(record), RECORD_KEYS)
                self.assertEqual(record['input_sha256'], hashlib.sha256(source).hexdigest())
                self.assertEqual(record['input_sample_count'], 32000)
                self.assertEqual(record['decoded_pcm_sha256'], hashlib.sha256(decoded).hexdigest())
                self.assertIsNone(record['packet_loss'])
                self.assertEqual(decoded, telephone_roundtrip(source, law)[0])  # deterministic
                back, conditioning = canonicalize(decoded, 8000, 1)
                self.assertEqual(conditioning['output_sample_count'], 32000)
                spectrum = np.abs(np.fft.rfft(np.frombuffer(back, dtype='<i2')[4000:28000] * np.hanning(24000)))
                freqs = np.fft.rfftfreq(24000, 1 / 16000)
                in_band = spectrum[np.argmin(np.abs(freqs - 1000))]
                removed = spectrum[np.argmin(np.abs(freqs - 6000))]
                self.assertGreater(in_band / max(removed, 1e-9), 1000)  # 6 kHz lies above 4 kHz Nyquist

    def test_8khz_input_bypasses_resampling_and_matches_direct_codec(self):
        source = tone(8000, 1.0, 440, 9000)
        decoded, record = telephone_roundtrip(source, 'alaw', rate=8000)
        self.assertEqual(record['resampler'], 'bypass')
        self.assertEqual(decoded, decode(encode(source, 'alaw'), 'alaw'))

    def test_overshoot_is_counted_and_clipped_not_wrapped(self):
        square = np.where(np.arange(16000) % 40 < 20, 32767, -32768)
        decoded, record = telephone_roundtrip(pcm(square), 'ulaw')
        self.assertGreater(record['saturated_samples'], 0)
        narrow = soxr.resample(square.astype(np.float64), 16000, 8000, quality='HQ')
        self.assertGreater(float(np.max(np.abs(narrow))), 32767)  # the fixture really overshoots
        self.assertEqual(decoded, decode(encode(pcm(np.clip(np.rint(narrow), -32768, 32767)), 'ulaw'), 'ulaw'))
        values = np.frombuffer(decoded, dtype='<i2').astype(np.int32)
        centres = np.arange(5, 8000, 10)  # plateau centres at 8 kHz: + then - every 20 samples
        signs = np.where((centres // 10) % 2 == 0, 1, -1)
        self.assertTrue(np.all(np.sign(values[centres[5:-5]]) == signs[5:-5]))  # no wraparound

    def test_invalid_rates_are_rejected(self):
        for rate in (7999, 192001, 16000.0, True, None):
            with self.subTest(rate=rate):
                with self.assertRaises(ValueError):
                    telephone_roundtrip(pcm([0, 0]), 'ulaw', rate=rate)


if __name__ == '__main__':
    unittest.main()
