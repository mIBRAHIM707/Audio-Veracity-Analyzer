"""Claude-authored tests (14 November 2026) for data/prepare/ffmpeg_codecs.py, backlog 3.3.

Authored by Claude (Claude Code), not by GPT-6 Astra. Two groups:
- injected-runner tests (always run): version/codec/hash refusals, nonzero exit,
  timeout, missing executable, malformed streams, input validation;
- real-ffmpeg tests (skipped without the pinned ffmpeg 9.0.2; set AVA_FFMPEG or install
  Gyan.FFmpeg 9.0.2 with winget): known-answer frame sizes and AMR mode bits, exact
  output length, determinism, signal preservation on fictional synthetic tones, lineage.
No real speech is used; these are codec-plumbing checks, not quality measurements.
"""
import glob
import hashlib
import os
import subprocess
import tempfile
import unittest

import numpy as np

from data.prepare import ffmpeg_codecs as fc


def tone(seconds, rate=8000, freqs=(300, 1100, 2300), amplitude=6000):
    t = np.arange(round(seconds * rate)) / rate
    signal = sum(np.sin(2 * np.pi * f * t) for f in freqs) / len(freqs)
    return np.rint(amplitude * signal).astype('<i2').tobytes()


def real_ffmpeg_path():
    path = os.environ.get('AVA_FFMPEG')
    if path:
        return path
    pattern = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'WinGet', 'Packages',
                           'Gyan.FFmpeg*', f'ffmpeg-{fc.PINNED_VERSION}-*', 'bin', 'ffmpeg.exe')
    found = glob.glob(pattern)
    return found[0] if found else None


class FakeRun:
    """Stands in for subprocess.run: answers -version/-encoders/-decoders, then queued results."""

    def __init__(self, version=f'ffmpeg version {fc.PINNED_VERSION}-full_build', codecs=None, results=()):
        self.version, self.results, self.calls = version, list(results), []
        names = codecs if codecs is not None else ['libgsm', 'libopencore_amrnb']
        self.listing = '\n'.join(f' A....D {name}  description' for name in names).encode()

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if '-version' in args:
            return subprocess.CompletedProcess(args, 0, self.version.encode() + b'\nbuilt with gcc', b'')
        if '-encoders' in args or '-decoders' in args:
            return subprocess.CompletedProcess(args, 0, self.listing, b'')
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return subprocess.CompletedProcess(args, *result)


class InjectedRunnerTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(prefix='fake-ffmpeg-', suffix='.exe', delete=False)
        handle.write(b'not really ffmpeg')
        handle.close()
        self.exe = handle.name
        self.addCleanup(os.unlink, self.exe)

    def make(self, **kwargs):
        return fc.Ffmpeg(self.exe, run=FakeRun(**kwargs))

    def test_missing_executable_refused(self):
        with self.assertRaises(fc.CodecToolError):
            fc.Ffmpeg(os.path.join(tempfile.gettempdir(), 'no-such-ffmpeg-ava.exe'), run=FakeRun())

    def test_wrong_version_refused(self):
        for version in ('ffmpeg version 7.1-essentials', 'ffmpeg version 9.0.20-full_build', 'ffprobe version 9.0.2', ''):
            with self.assertRaisesRegex(fc.CodecToolError, '9.0.2', msg=version):
                self.make(version=version)
        self.assertEqual(self.make(version='ffmpeg version 9.0.2 Copyright').version, '9.0.2')

    def test_bad_quality_or_padded_amr_frames_refused(self):
        for header in (7 << 3, 7 << 3 | 0x04 | 0x80, 7 << 3 | 0x04 | 0x01):  # Q=0; P bits set
            frames = (bytes([header]) + bytes(31)) * 6
            ffmpeg = self.make(results=[(0, fc.AMR_MAGIC + frames, b'')])
            with self.assertRaises(fc.CodecToolError, msg=hex(header)):
                ffmpeg.encode(tone(0.1), 'amr_nb_12.2')

    def test_encoder_without_decoder_refused(self):
        run = FakeRun()
        original = run.__call__

        def no_decoders(args, **kwargs):
            if '-decoders' in args:
                return subprocess.CompletedProcess(args, 0, b' A....D libgsm  gsm\n', b'')
            return original(args, **kwargs)
        with self.assertRaisesRegex(fc.CodecToolError, 'decoders'):
            fc.Ffmpeg(self.exe, run=no_decoders)

    def test_missing_codec_refused(self):
        with self.assertRaisesRegex(fc.CodecToolError, 'libopencore_amrnb'):
            self.make(codecs=['libgsm'])

    def test_hash_pin(self):
        digest = hashlib.sha256(b'not really ffmpeg').hexdigest()
        self.assertEqual(fc.Ffmpeg(self.exe, expected_sha256=digest, run=FakeRun()).sha256, digest)
        with self.assertRaisesRegex(fc.CodecToolError, 'hash'):
            fc.Ffmpeg(self.exe, expected_sha256='0' * 64, run=FakeRun())

    def test_nonzero_exit_timeout_and_oserror_become_codec_tool_errors(self):
        pcm = tone(0.1)
        cases = [((1, b'', b'line one\nInvalid data found\n'), 'Invalid data found'),
                 (subprocess.TimeoutExpired('ffmpeg', 1), 'timed out'),
                 (PermissionError('denied'), 'PermissionError')]
        for result, message in cases:
            ffmpeg = self.make(results=[result])
            with self.assertRaisesRegex(fc.CodecToolError, message):
                ffmpeg.encode(pcm, 'gsm')

    def test_wrong_frame_count_or_mode_refused(self):
        pcm = tone(0.1)  # 800 samples = 5 frames
        good_amr = fc.AMR_MAGIC + bytes([7 << 3 | 0x04]) + bytes(31)
        cases = [('gsm', bytes(4 * 33)),  # one frame short
                 ('amr_nb_12.2', good_amr * 1),  # magic repeated, wrong size
                 ('amr_nb_12.2', fc.AMR_MAGIC + (bytes([7 << 3 | 0x04]) + bytes(31)) * 5),  # no flush frame
                 ('amr_nb_12.2', fc.AMR_MAGIC + (bytes([4 << 3 | 0x04]) + bytes(31)) * 6),  # MR74 header, 12.2 sizes
                 ('amr_nb_4.75', b'#!AMR-WB\n' + bytes(5 * 13 - 3))]
        for codec, stream in cases:
            ffmpeg = self.make(results=[(0, stream, b'')])
            with self.assertRaises(fc.CodecToolError, msg=codec):
                ffmpeg.encode(pcm, codec)

    def test_amr_arguments_pin_bitrate_and_disable_dtx(self):
        frame = bytes([0 << 3 | 0x04]) + bytes(12)
        run = FakeRun(results=[(0, fc.AMR_MAGIC + frame * 6, b'')])  # 5 input frames + flush
        fc.Ffmpeg(self.exe, run=run).encode(tone(0.1), 'amr_nb_4.75')
        args = run.calls[-1][0]
        self.assertEqual(args[args.index('-b:a') + 1], '4750')
        self.assertEqual(args[args.index('-dtx') + 1], '0')
        self.assertEqual(args[args.index('-c:a') + 1], 'libopencore_amrnb')
        self.assertIn('-nostdin', args)
        self.assertEqual(run.calls[-1][1]['timeout'], fc.TIMEOUT_S)

    def test_input_validation(self):
        ffmpeg = self.make()
        for bad in (b'', b'\x00', bytearray(4), 'pcm'):
            with self.assertRaises(ValueError):
                ffmpeg.encode(bad, 'gsm')
        with self.assertRaises(ValueError):
            ffmpeg.encode(tone(0.1), 'g729')
        with self.assertRaises(ValueError):
            ffmpeg.encode(bytes(2 * (fc.MAX_SECONDS * 8000 + 1)), 'gsm')
        with self.assertRaises(ValueError):
            ffmpeg.decode(b'', 'gsm')
        for rate in (7999, 16000.0, True):
            with self.assertRaises(ValueError):
                fc.telephone_codec_roundtrip(tone(0.1), 'gsm', ffmpeg, rate=rate)
        self.assertEqual(ffmpeg._run.calls[3:], [])  # nothing reached the subprocess

    def test_odd_or_empty_decoder_output_refused(self):
        for output in (b'', b'\x01\x02\x03'):
            ffmpeg = self.make(results=[(0, output, b'')])
            with self.assertRaises(fc.CodecToolError):
                ffmpeg.decode(bytes(33), 'gsm')

    def test_find_ffmpeg_uses_environment(self):
        previous = os.environ.get('AVA_FFMPEG')
        os.environ['AVA_FFMPEG'] = self.exe
        try:
            self.assertEqual(fc.find_ffmpeg(), os.path.realpath(self.exe))
        finally:
            if previous is None:
                del os.environ['AVA_FFMPEG']
            else:
                os.environ['AVA_FFMPEG'] = previous


def amr_stream(codec, count):
    """A syntactically valid storage stream of `count` frames (payload zeros)."""
    bit_rate = fc.CODECS[codec]['bit_rate']
    frame = bytes([fc.AMR_MODES[bit_rate] << 3 | 0x04]) + bytes(fc.AMR_FRAME_BYTES[bit_rate] - 1)
    return fc.AMR_MAGIC + frame * count


class AmrFrameLossTests(unittest.TestCase):
    """Claude-authored, 14 Nov 2026: stream-level AMR frame loss (no ffmpeg needed)."""

    def test_mask_record_and_no_data_positions_match_the_g711_rule(self):
        from data.prepare.codecs import _loss_mask
        stream = amr_stream('amr_nb_12.2', 400)
        out, record = fc.drop_amr_frames(stream, 'amr_nb_12.2', 0.05, 99)
        lost, expected = _loss_mask(400, 0.05, 99)
        self.assertEqual({k: record[k] for k in expected}, expected)
        self.assertGreater(record['lost_packets'], 5)
        frames = fc._amr_frames(out, 'amr_nb_12.2')
        self.assertEqual(len(frames), 400)
        self.assertEqual([f == bytes([fc.AMR_NO_DATA]) for f in frames], list(lost))
        self.assertEqual(record['concealment'], fc.FRAME_LOSS_CONCEALMENT)
        self.assertEqual(fc.drop_amr_frames(stream, 'amr_nb_12.2', 0.05, 99), (out, record))
        self.assertEqual(fc.drop_amr_frames(stream, 'amr_nb_12.2', 0.0, 1)[0], stream)

    def test_refusals(self):
        stream = amr_stream('amr_nb_4.75', 20)
        for args in (('gsm', 0.01, 1), ('amr_nb_4.75', 0.06, 1), ('amr_nb_4.75', 0.01, -1),
                     ('amr_nb_4.75', True, 1), ('amr_nb_7.4', 0.01, 1)):
            with self.assertRaises((ValueError, fc.CodecToolError), msg=args):
                fc.drop_amr_frames(stream if args[0] != 'gsm' else bytes(33), *args)
        lossy, _ = fc.drop_amr_frames(amr_stream('amr_nb_4.75', 400), 'amr_nb_4.75', 0.05, 3)
        with self.assertRaisesRegex(ValueError, 'already'):
            fc.drop_amr_frames(lossy, 'amr_nb_4.75', 0.05, 3)
        with self.assertRaises(fc.CodecToolError):
            fc._amr_frames(stream[:-1], 'amr_nb_4.75')
        with self.assertRaises(fc.CodecToolError):
            fc._amr_frames(fc.AMR_MAGIC + bytes([0x7C & ~0x04]), 'amr_nb_4.75')  # NO_DATA with Q=0


class GsmFrameLossTests(unittest.TestCase):
    """Claude-authored, 14 Nov 2026: GSM repeat_then_silence substitution (no ffmpeg)."""

    def test_rule_on_hand_masks(self):
        f = [bytes([i]) * 33 for i in range(10)]
        s = b'S' * 33
        cases = [  # (lost mask, expected frame sources: index or 'S')
            ([1, 0, 0, 0, 0, 0, 0, 0, 0, 0], ['S', 1, 2, 3, 4, 5, 6, 7, 8, 9]),  # no previous frame
            ([0, 1, 1, 1, 0, 0, 0, 0, 0, 0], [0, 0, 0, 0, 4, 5, 6, 7, 8, 9]),  # burst of 3 repeats
            ([0, 1, 1, 1, 1, 1, 0, 0, 0, 0], [0, 0, 0, 0, 'S', 'S', 6, 7, 8, 9]),  # 4th+ is silence
            ([0, 0, 1, 0, 1, 1, 1, 1, 0, 1], [0, 1, 1, 3, 3, 3, 3, 'S', 8, 8]),  # run resets
            ([1] * 10, ['S'] * 10)]
        for mask, sources in cases:
            expected = [s if src == 'S' else f[src] for src in sources]
            self.assertEqual(fc.substitute_frames(f, [bool(x) for x in mask], s), expected, mask)
        with self.assertRaises(ValueError):
            fc.substitute_frames(f, [False] * 9, s)

    def test_stream_record_and_refusals(self):
        from data.prepare.codecs import _loss_mask
        stream = b''.join(bytes([i % 251]) * 33 for i in range(400))
        out, record = fc.drop_gsm_frames(stream, 0.05, 99, b'S' * 33)
        lost, expected = _loss_mask(400, 0.05, 99)
        self.assertEqual({k: record[k] for k in expected}, expected)
        self.assertEqual((record['concealment'], record['repeat_limit'], record['applied_via']),
                         ('repeat_then_silence', 3, 'gsm_frame_substitution'))
        frames = [stream[i:i + 33] for i in range(0, len(stream), 33)]
        self.assertEqual(out, b''.join(fc.substitute_frames(frames, lost, b'S' * 33)))
        self.assertEqual(len(out), len(stream))
        self.assertGreater(record['lost_packets'], 5)
        for args in ((stream[:-1], 0.01, 1, b'S' * 33), (stream, 0.06, 1, b'S' * 33),
                     (stream, 0.01, 1, b'S' * 32), (stream, 0.01, 1, 'S' * 33)):
            with self.assertRaises((ValueError, fc.CodecToolError)):
                fc.drop_gsm_frames(*args)


@unittest.skipIf(real_ffmpeg_path() is None and os.environ.get('AVA_REQUIRE_FFMPEG') != '1',
                 'pinned ffmpeg 9.0.2 not installed (set AVA_FFMPEG; AVA_REQUIRE_FFMPEG=1 makes this a failure)')
class RealFfmpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = fc.Ffmpeg(real_ffmpeg_path())
        if os.name == 'nt':  # the installed Windows build is the pinned one
            assert cls.ffmpeg.sha256 == fc.PINNED_WINDOWS_SHA256, cls.ffmpeg.sha256

    def test_known_answer_frame_sizes_and_exact_length(self):
        for seconds in (0.02, 0.5, 1.013):  # 1, 25 and a partial last frame (51 frames)
            pcm = tone(seconds)
            frames = -(-len(pcm) // 2 // 160)
            for codec, spec in fc.CODECS.items():
                encoded = self.ffmpeg.encode(pcm, codec)  # _check_stream verified size and mode
                if spec['format'] == 'gsm':
                    self.assertEqual(len(encoded), 33 * frames)
                else:  # AMR: floor((samples + 50) / 160) + 1 frames
                    self.assertEqual(len(encoded), 6 + fc.AMR_FRAME_BYTES[spec['bit_rate']] * ((len(pcm) // 2 + 50) // 160 + 1))
                decoded, record = self.ffmpeg.roundtrip(pcm, codec)
                self.assertEqual(len(decoded), len(pcm), (codec, seconds))
                self.assertEqual(record['frames'], frames)
                self.assertEqual(record['encoded_frames'], fc.encoded_frames(codec, len(pcm) // 2))
                self.assertEqual(record['decoded_samples_before_alignment'], 160 * record['encoded_frames'], codec)
                self.assertEqual(record['delay_compensated_samples'], fc.CODEC_DELAY_SAMPLES[codec])
                if spec['format'] == 'amr':
                    self.assertIn('-dtx', record['ffmpeg_encode_args'])
                self.assertNotIn(self.ffmpeg.executable, record['ffmpeg_encode_args'])

    def test_amr_frame_rule_for_every_remainder(self):
        # Review finding (14 Nov): the frame count must hold for every partial-frame
        # length, not only the three lengths first measured. encode() refuses any mismatch.
        rng = np.random.default_rng(11)
        for base in (160, 1600):
            for remainder in range(160):
                n = base + remainder
                pcm = np.rint(rng.normal(0, 2000, n)).astype('<i2').tobytes()
                encoded = self.ffmpeg.encode(pcm, 'amr_nb_4.75')
                self.assertEqual((len(encoded) - 6) // 13, (n + 50) // 160 + 1, n)
        for codec in ('amr_nb_7.4', 'amr_nb_12.2', 'gsm'):
            for n in (269, 270, 271, 319, 320):  # around the measured boundary at remainder 110
                self.ffmpeg.encode(bytes(2 * n), codec)

    def test_deterministic(self):
        pcm = tone(0.7)
        for codec in fc.CODECS:
            first, a = self.ffmpeg.roundtrip(pcm, codec)
            second, b = self.ffmpeg.roundtrip(pcm, codec)
            self.assertEqual(first, second, codec)
            self.assertEqual(a, b)

    def test_output_is_aligned_and_lower_rates_lose_more(self):
        # Seeded aperiodic noise (a periodic tone would hide the delay modulo its
        # period): after roundtrip() removes CODEC_DELAY_SAMPLES, the best alignment in a
        # +/-80 ms search must be lag 0 for every codec, i.e. sample i matches sample i.
        rng = np.random.default_rng(7)
        smooth = np.convolve(rng.normal(0, 1, 16000), np.ones(4) / 4, mode='same')
        pcm = np.rint(smooth / np.abs(smooth).max() * 8000).astype('<i2').tobytes()
        source = np.frombuffer(pcm, dtype='<i2').astype(np.float64)
        errors, a = {}, source[800:-800]
        for codec in fc.CODECS:
            decoded, _ = self.ffmpeg.roundtrip(pcm, codec)
            out = np.frombuffer(decoded, dtype='<i2').astype(np.float64)
            scores = {lag: np.corrcoef(a, out[800 + lag:800 + lag + len(a)])[0, 1] for lag in range(-640, 641)}
            best = max(scores, key=scores.get)
            self.assertEqual(best, 0, (codec, best, scores[best]))
            self.assertGreater(scores[0], 0.35, codec)  # measured 0.47-0.90 on this signal
            errors[codec] = np.sqrt(np.mean((a - out[800:-800]) ** 2))
        self.assertGreater(errors['amr_nb_4.75'], errors['amr_nb_12.2'])
        self.assertGreater(errors['amr_nb_12.2'], errors['gsm'] * 0.5)  # sanity: all lossy, same scale

    def test_silence_stays_quiet(self):
        for codec in fc.CODECS:
            decoded, _ = self.ffmpeg.roundtrip(bytes(1600), codec)
            self.assertLess(np.abs(np.frombuffer(decoded, dtype='<i2')).max(), 200, codec)

    def test_telephone_roundtrip_lineage_from_16k(self):
        canonical = tone(1.0, rate=16000)
        decoded, record = fc.telephone_codec_roundtrip(canonical, 'amr_nb_7.4', self.ffmpeg)
        self.assertEqual(len(decoded), 2 * 8000)
        self.assertEqual(record['codec'], 'amr_nb_7.4')
        self.assertEqual(record['bit_rate'], 7400)
        self.assertEqual(record['resampler'], 'soxr_hq')
        self.assertEqual(record['input_sha256'], hashlib.sha256(canonical).hexdigest())
        self.assertEqual(record['decoded_pcm_sha256'], hashlib.sha256(decoded).hexdigest())
        self.assertEqual(record['ffmpeg_sha256'], self.ffmpeg.sha256)
        self.assertEqual(len(record['ffmpeg_sha256']), 64)
        self.assertIn('9.0.2', record['codec_implementation'])
        self.assertIsNone(record['packet_loss'])
        decoded8, record8 = fc.telephone_codec_roundtrip(tone(1.0), 'gsm', self.ffmpeg, rate=8000)
        self.assertEqual(record8['resampler'], 'bypass')
        self.assertEqual(len(decoded8), 16000)

    def test_amr_frame_loss_is_concealed_by_the_decoder_and_keeps_timing(self):
        from data.prepare.codecs import derive_loss
        rng = np.random.default_rng(5)
        pcm = np.rint(np.convolve(rng.normal(0, 3000, 16000), np.ones(4) / 4, mode='same')).astype('<i2').tobytes()
        plan = next(p for p in (derive_loss(1, f'clip-{i}') for i in range(200))
                    if p['loss_rate'] > 0.03)
        clean, _ = self.ffmpeg.roundtrip(pcm, 'amr_nb_12.2')
        lossy, record = self.ffmpeg.roundtrip(pcm, 'amr_nb_12.2', loss_plan=plan)
        loss = record['frame_loss']
        self.assertEqual(len(lossy), len(pcm))
        self.assertEqual((loss['clip_id'], loss['loss_rate'], loss['concealment']),
                         (plan['clip_id'], plan['loss_rate'], fc.FRAME_LOSS_CONCEALMENT))
        self.assertGreater(loss['lost_packets'], 0, 'pick a plan that loses frames')
        from data.prepare.codecs import _loss_mask
        lost, _ = _loss_mask(record['encoded_frames'], plan['loss_rate'], plan['seed'])
        first = int(np.flatnonzero(lost)[0])
        a, b = np.frombuffer(clean, dtype='<i2'), np.frombuffer(lossy, dtype='<i2')
        cut = max(first * 160 - fc.CODEC_DELAY_SAMPLES['amr_nb_12.2'], 0)
        self.assertTrue(np.array_equal(a[:cut], b[:cut]), 'audio before the first lost frame must not change')
        self.assertFalse(np.array_equal(a, b))
        region, reference = b[cut:cut + 160].astype(np.float64), a[cut:cut + 160].astype(np.float64)
        self.assertEqual(len(region), 160, 'pick a plan whose first loss is inside the clip')
        self.assertFalse(np.array_equal(region, reference))
        rms = np.sqrt(np.mean(region ** 2))
        self.assertGreater(rms, 0.02 * np.sqrt(np.mean(reference ** 2)), 'concealed, not muted')
        self.assertEqual(record['decoded_samples_before_alignment'], 160 * record['encoded_frames'])
        self.assertNotEqual(record['received_sha256'], record['encoded_sha256'])
        self.assertEqual(loss['applied_via'], 'amr_no_data_frames')
        again, record2 = self.ffmpeg.roundtrip(pcm, 'amr_nb_12.2', loss_plan=plan)
        self.assertEqual((again, record2), (lossy, record))
        low, low_record = self.ffmpeg.roundtrip(pcm, 'amr_nb_4.75', loss_plan=plan)
        self.assertEqual(len(low), len(pcm))
        self.assertGreater(low_record['frame_loss']['lost_packets'], 0)
        self.assertNotEqual(low, self.ffmpeg.roundtrip(pcm, 'amr_nb_4.75')[0])

    def test_gsm_frame_loss_keeps_timing_and_conceals_by_substitution(self):
        from data.prepare.codecs import _loss_mask, derive_loss
        rng = np.random.default_rng(6)
        pcm = np.rint(np.convolve(rng.normal(0, 3000, 16000), np.ones(4) / 4, mode='same')).astype('<i2').tobytes()
        plan = next(p for p in (derive_loss(2, f'g-{i}') for i in range(200)) if p['loss_rate'] > 0.03)
        clean, _ = self.ffmpeg.roundtrip(pcm, 'gsm')
        lossy, record = self.ffmpeg.roundtrip(pcm, 'gsm', loss_plan=plan)
        loss = record['frame_loss']  # Ffmpeg.roundtrip record; the 16 kHz wrapper adds packet_loss
        self.assertEqual(len(lossy), len(pcm))
        self.assertEqual((loss['clip_id'], loss['applied_via']), (plan['clip_id'], 'gsm_frame_substitution'))
        self.assertEqual(loss['silence_frame_sha256'], hashlib.sha256(self.ffmpeg.gsm_silence_frame()).hexdigest())
        lost, _ = _loss_mask(record['encoded_frames'], plan['loss_rate'], plan['seed'])
        self.assertGreater(int(lost.sum()), 0)
        first = int(np.flatnonzero(lost)[0])
        a, b = np.frombuffer(clean, dtype='<i2'), np.frombuffer(lossy, dtype='<i2')
        self.assertTrue(np.array_equal(a[:first * 160], b[:first * 160]))
        self.assertFalse(np.array_equal(a, b))
        self.assertEqual(self.ffmpeg.roundtrip(pcm, 'gsm', loss_plan=plan), (lossy, record))
        silence = np.frombuffer(self.ffmpeg.decode(self.ffmpeg.gsm_silence_frame(), 'gsm'), dtype='<i2')
        self.assertEqual(len(self.ffmpeg.gsm_silence_frame()), 33)
        self.assertLess(np.abs(silence).max(), 50)

    def test_gsm_silence_frames_inside_speech_decode_quiet(self):
        # Review (14 Nov): the silence frame comes from a fresh encoder but is decoded
        # after speech; check the decoder state does not make it ring. Burst of 8 lost
        # frames at 20-27 in seeded noise: 20-22 repeat, 23-27 silence frames.
        rng = np.random.default_rng(8)
        pcm = np.rint(np.convolve(rng.normal(0, 3000, 8000), np.ones(4) / 4, mode='same')).astype('<i2').tobytes()
        encoded = self.ffmpeg.encode(pcm, 'gsm')
        frames = [encoded[i:i + 33] for i in range(0, len(encoded), 33)]
        lost = [20 <= i < 28 for i in range(len(frames))]
        stream = b''.join(fc.substitute_frames(frames, lost, self.ffmpeg.gsm_silence_frame()))
        out = np.frombuffer(self.ffmpeg.decode(stream, 'gsm'), dtype='<i2').astype(np.float64)
        clean = np.frombuffer(self.ffmpeg.decode(encoded, 'gsm'), dtype='<i2').astype(np.float64)
        speech_rms = np.sqrt(np.mean(clean[23 * 160:28 * 160] ** 2))
        # One transition frame (23) allowed; frames 24-27 must be near silence.
        quiet_rms = np.sqrt(np.mean(out[24 * 160:28 * 160] ** 2))
        self.assertLess(quiet_rms, 0.05 * speech_rms, (quiet_rms, speech_rms))
        self.assertTrue(np.array_equal(out[:20 * 160], clean[:20 * 160]))

    def test_loss_and_jitter_form_one_concealed_mask(self):
        from data.prepare.codecs import _late_mask, _loss_mask, derive_jitter, derive_loss
        pcm = tone(2.0)
        ids = (f'j-{i}' for i in range(500))
        clip = next(c for c in ids if derive_jitter(3, c)['jitter_ms'] > 15 and derive_loss(3, c)['loss_rate'] > 0.02)
        loss, jitter = derive_loss(3, clip), derive_jitter(3, clip)
        for codec in ('amr_nb_7.4', 'gsm'):
            out, record = self.ffmpeg.roundtrip(pcm, codec, loss_plan=loss, jitter_plan=jitter)
            n = record['frame_concealment']['frames']
            lost, _ = _loss_mask(n, loss['loss_rate'], loss['seed'])
            late, _ = _late_mask(n, jitter['jitter_ms'], jitter['buffer_ms'], jitter['seed'])
            union = lost | late
            self.assertGreater(int(late.sum()), 0, codec)
            self.assertEqual(record['frame_concealment']['concealed_frames'], int(union.sum()))
            self.assertEqual(record['frame_concealment']['concealed_indices_sha256'],
                             hashlib.sha256(np.flatnonzero(union).astype('<i4').tobytes()).hexdigest())
            self.assertEqual(record['frame_jitter']['late_packets'], int(late.sum()))
            self.assertEqual(record['frame_concealment']['lost_and_late_frames'], int((lost & late).sum()))
            self.assertEqual(len(out), len(pcm))
            clean, _ = self.ffmpeg.roundtrip(pcm, codec)
            cut = max(int(np.flatnonzero(union)[0]) * 160 - fc.CODEC_DELAY_SAMPLES[codec], 0)
            self.assertEqual(out[:2 * cut], clean[:2 * cut])
            # Loss only: byte-identical to the stream-level functions (regression guard).
            _, only = self.ffmpeg.roundtrip(pcm, codec, loss_plan=loss)
            encoded = self.ffmpeg.encode(pcm, codec)
            stream = (fc.drop_amr_frames(encoded, codec, loss['loss_rate'], loss['seed'])[0] if codec != 'gsm'
                      else fc.drop_gsm_frames(encoded, loss['loss_rate'], loss['seed'], self.ffmpeg.gsm_silence_frame())[0])
            self.assertEqual(only['received_sha256'], hashlib.sha256(stream).hexdigest())
            self.assertIsNone(only['frame_jitter'])
        with self.assertRaisesRegex(ValueError, 'derive_jitter'):
            self.ffmpeg.roundtrip(pcm, 'gsm', jitter_plan={**jitter, 'buffer_ms': 10})
        with self.assertRaisesRegex(ValueError, 'different clips'):
            self.ffmpeg.roundtrip(pcm, 'gsm', loss_plan=derive_loss(3, 'other'), jitter_plan=jitter)
        _, wide = fc.telephone_codec_roundtrip(tone(0.5, rate=16000), 'amr_nb_12.2', self.ffmpeg, jitter_plan=jitter)
        self.assertEqual(wide['jitter'], wide['frame_jitter'])
        self.assertIsNone(wide['packet_loss'])
        # Same schema as G.711's packet_concealment (codecs.telephone_roundtrip).
        from data.prepare.codecs import derive_jitter as dj, telephone_roundtrip
        _, g711 = telephone_roundtrip(tone(0.5, rate=16000), 'ulaw', concealment='repeat_then_silence',
                                      loss_plan=derive_loss(3, clip), jitter_plan=dj(3, clip))
        _, amr = fc.telephone_codec_roundtrip(tone(0.5, rate=16000), 'amr_nb_12.2', self.ffmpeg,
                                              loss_plan=derive_loss(3, clip), jitter_plan=jitter)
        for key in ('mask', 'packets', 'concealed_packets', 'concealed_fraction', 'lost_and_late_packets',
                    'concealed_indices_sha256', 'concealment'):
            self.assertIn(key, amr['packet_concealment'], key)
            self.assertIn(key, g711['packet_concealment'], key)
        self.assertIsNone(fc.telephone_codec_roundtrip(tone(0.5, rate=16000), 'gsm', self.ffmpeg)[1]['packet_concealment'])

    def test_plain_decode_refuses_no_data_frames(self):
        encoded = self.ffmpeg.encode(tone(0.5), 'amr_nb_12.2')
        lossy, _ = fc.drop_amr_frames(encoded, 'amr_nb_12.2', 0.05, 4)
        all_lost = fc.AMR_MAGIC + bytes([fc.AMR_NO_DATA]) * 26
        for stream in (all_lost,) + ((lossy,) if lossy != encoded else ()):
            with self.assertRaises(ValueError):
                self.ffmpeg.decode(stream, 'amr_nb_12.2')
        with self.assertRaises(ValueError):
            self.ffmpeg.decode(all_lost, 'amr_nb_12.2', allow_lost_frames=True)

    def test_frame_loss_plan_validation(self):
        from data.prepare.codecs import derive_loss
        plan = derive_loss(1, 'clip')
        with self.assertRaisesRegex(ValueError, 'derive_loss'):
            self.ffmpeg.roundtrip(tone(0.2), 'gsm', loss_plan={**plan, 'seed': 1})
        with self.assertRaisesRegex(ValueError, 'derive_loss'):
            self.ffmpeg.roundtrip(tone(0.2), 'amr_nb_4.75', loss_plan={**plan, 'loss_rate': 0.05})
        decoded, record = fc.telephone_codec_roundtrip(tone(0.5, rate=16000), 'amr_nb_4.75', self.ffmpeg,
                                                       loss_plan=plan)
        self.assertEqual(record['frame_loss']['clip_id'], 'clip')
        self.assertEqual(record['packet_loss'], record['frame_loss'])  # same key as G.711
        self.assertEqual(len(decoded), 2 * 4000)
        _, plain = self.ffmpeg.roundtrip(tone(0.2), 'gsm')
        self.assertIsNone(plain['frame_loss'])

    def test_foreign_or_damaged_streams_refused_before_decoding(self):
        # ffmpeg 9.0.2 itself decodes an AMR-WB header forced as AMR, so the module checks.
        amr = self.ffmpeg.encode(tone(0.5), 'amr_nb_12.2')
        gsm = self.ffmpeg.encode(tone(0.5), 'gsm')
        for stream, codec in ((b'#!AMR-WB\n' + amr[6:], 'amr_nb_12.2'), (amr[:-1], 'amr_nb_12.2'),
                              (amr, 'amr_nb_7.4'), (gsm[:-1], 'gsm'), (amr[:6], 'amr_nb_12.2')):
            with self.assertRaises(ValueError):
                self.ffmpeg.decode(stream, codec)
        self.assertEqual(len(self.ffmpeg.decode(gsm, 'gsm')), 2 * 4000)


if __name__ == '__main__':
    unittest.main()
