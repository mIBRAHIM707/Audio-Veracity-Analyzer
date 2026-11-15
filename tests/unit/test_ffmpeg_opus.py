"""Claude-authored tests (15 November 2026) for the Opus VoIP path in
data/prepare/ffmpeg_codecs.py (backlog 3.4; PRD FR-D6 "Opus at 8 to 16 kbps", FR-D13).

Authored by Claude (Claude Code), not by GPT-6 Astra. Three groups:
- pure tests (always run): the Ogg checksum against a published CRC catalogue value,
  page/packet parsing and refusals on hand-built streams, TOC rule, packet rebuild,
  the packet-count formula, ffmpeg arguments through an injected runner;
- real-ffmpeg tests (skipped without the pinned ffmpeg 9.0.2, like test_ffmpeg_codecs):
  packet count for every partial-frame length, header fields, exact output length,
  re-measured 48 kHz delay, alignment, seeded packet loss and jitter, the 16 kHz wrapper;
- augmentation chain with a real Opus channel (writer + verifier).
Synthetic signals only; codec plumbing, not quality measurements.
"""
import hashlib
import json
import os
import struct
import subprocess
import tempfile
import unittest

import numpy as np

from data.prepare import augment, ffmpeg_codecs as fc
from data.prepare.codecs import _late_mask, _loss_mask, derive_jitter, derive_loss
from tests.unit.test_augment_ffmpeg_chain import Fixture, ffmpeg_spec
from tests.unit.test_ffmpeg_codecs import FakeRun, real_ffmpeg_path

OPUS = ('opus_8', 'opus_12', 'opus_16')
REAL = unittest.skipIf(real_ffmpeg_path() is None and os.environ.get('AVA_REQUIRE_FFMPEG') != '1',
                       'pinned ffmpeg 9.0.2 not installed (set AVA_FFMPEG; AVA_REQUIRE_FFMPEG=1 makes this a failure)')


def page(packets, sequence, page_type=0, granule=0, serial=7):
    body = bytearray(b'OggS' + bytes([0, page_type]) + struct.pack('<qIII', granule, serial, sequence, 0)
                     + bytes([len(packets)]) + bytes(len(p) for p in packets) + b''.join(packets))
    body[22:26] = struct.pack('<I', fc.ogg_crc(body))
    return bytes(body)


def opus_head(channels=1, pre_skip=fc.OPUS_PRE_SKIP, rate=8000, gain=0, mapping=0):
    return b'OpusHead' + struct.pack('<BBHIhB', 1, channels, pre_skip, rate, gain, mapping)


def cbr_packet(codec='opus_12', toc=0x0B, fill=0x55):
    """Code-3 packet with one frame (count byte 0x01), padded to the CBR size."""
    size = fc.OPUS_FRAME_BYTES[fc.CODECS[codec]['bit_rate']]
    return bytes([toc, 0x01]) + bytes([fill]) * (size - 2)


def stream(audio_pages, head=None, tags=b'OpusTags' + bytes(8)):
    pages = [page([head or opus_head()], 0, page_type=2), page([tags], 1)]
    for index, packets in enumerate(audio_pages):
        last = index == len(audio_pages) - 1
        pages.append(page(packets, 2 + index, page_type=4 if last else 0, granule=960 * (index + 1)))
    return b''.join(pages)


class PureOggOpusTests(unittest.TestCase):
    def test_ogg_crc_known_answer(self):
        # reveng CRC catalogue, CRC-32/CKSUM: poly 04C11DB7, init 0, no reflection,
        # xorout FFFFFFFF, check("123456789") = 765E7680. Ogg uses the same CRC without
        # the final xor (RFC 3533 §6).
        self.assertEqual(fc.ogg_crc(b'123456789') ^ 0xFFFFFFFF, 0x765E7680)
        self.assertEqual(fc.ogg_crc(b''), 0)

    def test_valid_stream_parses_and_rebuilds_byte_identically(self):
        packets = [cbr_packet(fill=i) for i in range(7)]
        data = stream([packets[:4], packets[4:]])
        pages = fc._ogg_pages(data)
        self.assertEqual(fc._opus_packets(pages, 'opus_12'), packets)
        fc._check_stream(data, 'opus_12', 7)
        with self.assertRaisesRegex(fc.CodecToolError, 'expected 8'):
            fc._check_stream(data, 'opus_12', 8)
        self.assertEqual(fc._ogg_replace_packets(pages, packets), data)

    def test_replacing_packets_keeps_layout_granules_and_valid_checksums(self):
        packets = [cbr_packet(fill=i) for i in range(7)]
        data = stream([packets[:4], packets[4:]])
        pages = fc._ogg_pages(data)
        lost = [bytes([p[0] & 0xFC]) if i in (1, 5) else p for i, p in enumerate(packets)]
        rebuilt = fc._ogg_replace_packets(pages, lost)
        again = fc._ogg_pages(rebuilt)  # checksums verified by the parser
        self.assertEqual([len(p) for _, p in again], [len(p) for _, p in pages])
        self.assertEqual([h[6:14] for h, _ in again], [h[6:14] for h, _ in pages])  # granules
        got = fc._opus_packets(again, 'opus_12', allow_lost=True)
        self.assertEqual(got, lost)
        self.assertEqual(got[1], b'\x08')  # same configuration, code 0, no frame data
        with self.assertRaises(fc.CodecToolError):
            fc._opus_packets(again, 'opus_12')  # lost packets refused without allow_lost
        for wrong in (lost[:-1], lost + [lost[0]]):
            with self.assertRaises(ValueError):
                fc._ogg_replace_packets(pages, wrong)

    def test_malformed_streams_refused(self):
        good = [cbr_packet() for _ in range(3)]
        data = stream([good])
        flipped = bytearray(data)
        flipped[-1] ^= 0x01
        second_bos = data.replace(page([b'OpusTags' + bytes(8)], 1), page([b'OpusTags' + bytes(8)], 1, page_type=2))
        cases = {
            'checksum': bytes(flipped),
            'truncated': data[:-3],
            'trailing garbage': data + b'Ogg',
            'no EOS': stream([good])[:-len(page(good, 2, page_type=4, granule=960))] + page(good, 2, granule=960),
            'second BOS': second_bos,
            'other serial': stream([good]).replace(page(good, 2, page_type=4, granule=960),
                                                   page(good, 2, page_type=4, granule=960, serial=8)),
            'out of order': stream([good]).replace(page(good, 2, page_type=4, granule=960),
                                                   page(good, 3, page_type=4, granule=960)),
            'continued': stream([good]).replace(page(good, 2, page_type=4, granule=960),
                                                page(good, 2, page_type=5, granule=960)),
            'stereo head': stream([good], head=opus_head(channels=2)),
            'pre-skip': stream([good], head=opus_head(pre_skip=3840)),
            'input rate': stream([good], head=opus_head(rate=48000)),
            'gain': stream([good], head=opus_head(gain=256)),
            'tags': stream([good], tags=b'OpusTagz' + bytes(8)),
            'no audio': page([opus_head()], 0, page_type=2) + page([b'OpusTags'], 1, page_type=4),
            'wrong size': stream([[cbr_packet('opus_8')]]),
            'zero-length packet': stream([[b'']]),
            'stereo TOC': stream([[cbr_packet(toc=0x0F)]]),
            '10 ms frame': stream([[cbr_packet(toc=0x03)]]),
            'two frames (code 3)': stream([[bytes([0x0B, 0x02]) + bytes(28)]]),
            'two frames (code 1)': stream([[bytes([0x09]) + bytes(29)]]),
            'TOC-only code 3': stream([[b'\x0b']]),
        }
        for name, value in cases.items():
            with self.assertRaises(fc.CodecToolError, msg=name):
                fc._opus_packets(fc._ogg_pages(value), 'opus_12', allow_lost=name == 'TOC-only code 3')
        with self.assertRaises(fc.CodecToolError):
            fc._ogg_pages(b'')

    def test_toc_rule(self):
        self.assertIsNone(fc._opus_toc_problem(bytes([0x08])))  # SILK NB 20 ms, code 0
        self.assertIsNone(fc._opus_toc_problem(bytes([0x0B, 0x01])))  # code 3, one frame
        self.assertIsNone(fc._opus_toc_problem(bytes([0x98])))  # CELT NB 20 ms (config 19)
        self.assertIsNone(fc._opus_toc_problem(bytes([0x68])))  # hybrid SWB 20 ms (config 13)
        for toc in (bytes([0x00]), bytes([0x18]), bytes([0x60]), bytes([0x80]), bytes([0x0C]), bytes([0x0B])):
            self.assertIsNotNone(fc._opus_toc_problem(toc), toc.hex())

    def test_packet_count_formula(self):
        # ceil((6n + 312) / 960): hand-worked values (108 -> exactly one packet).
        for samples, packets in ((1, 1), (108, 1), (109, 2), (268, 2), (269, 3), (24037, 151), (24197, 152)):
            for codec in OPUS:
                self.assertEqual(fc.encoded_frames(codec, samples), packets, samples)


class InjectedOpusTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(prefix='fake-ffmpeg-', suffix='.exe', delete=False)
        handle.write(b'not really ffmpeg')
        handle.close()
        self.exe = handle.name
        self.addCleanup(os.unlink, self.exe)

    def test_tool_without_libopus_refused(self):
        with self.assertRaisesRegex(fc.CodecToolError, 'libopus'):
            fc.Ffmpeg(self.exe, run=FakeRun(codecs=['libgsm', 'libopencore_amrnb']))

    def test_arguments_pin_cbr_voip_20ms_bitexact_and_48k_decode(self):
        for codec in OPUS:
            run = FakeRun(results=[(0, stream([[cbr_packet(codec)]]), b'')])  # 0.1 s -> 1 packet
            fc.Ffmpeg(self.exe, run=run).encode(bytes(2 * 100), codec)
            args = run.calls[-1][0]
            pairs = {args[i]: args[i + 1] for i in range(len(args) - 1)}
            self.assertEqual((pairs['-c:a'], pairs['-b:a'], pairs['-vbr'], pairs['-application'],
                              pairs['-frame_duration'], pairs['-fflags'], pairs['-flags:a'], pairs['-f']),
                             ('libopus', str(fc.CODECS[codec]['bit_rate']), 'off', 'voip', '20', '+bitexact',
                              '+bitexact', 'ogg'))
            decode = fc._decode_args(fc.CODECS[codec])
            self.assertEqual(decode[decode.index('-ar') + 1], '48000')
            self.assertEqual(decode[decode.index('-c:a') + 1], 'libopus')

    def test_wrong_stream_from_the_tool_refused(self):
        for output in (b'', fc.AMR_MAGIC + bytes(31), stream([[cbr_packet('opus_16')]])):
            run = FakeRun(results=[(0, output, b'')])
            with self.assertRaises(fc.CodecToolError):
                fc.Ffmpeg(self.exe, run=run).encode(bytes(200), 'opus_12')

    def test_decode_refuses_all_lost_and_unannounced_lost_packets(self):
        pages = fc._ogg_pages(stream([[cbr_packet(), cbr_packet()]]))
        all_lost = fc._ogg_replace_packets(pages, [b'\x08', b'\x08'])
        one_lost = fc._ogg_replace_packets(pages, [b'\x08', cbr_packet()])
        ffmpeg = fc.Ffmpeg(self.exe, run=FakeRun(results=[(0, bytes(4), b'')]))
        with self.assertRaisesRegex(ValueError, 'no received packet'):
            ffmpeg.decode(all_lost, 'opus_12', allow_lost_frames=True)
        with self.assertRaises(ValueError):
            ffmpeg.decode(one_lost, 'opus_12')
        self.assertEqual(ffmpeg.decode(one_lost, 'opus_12', allow_lost_frames=True), bytes(4))


def band_limited_noise(samples, seed, low=200, high=3400, peak=8000):
    rng = np.random.default_rng(seed)
    spectrum = np.fft.rfft(rng.normal(0, 1, samples))
    freqs = np.fft.rfftfreq(samples, 1 / 8000)
    spectrum[(freqs < low) | (freqs > high)] = 0
    signal = np.fft.irfft(spectrum, samples)
    return np.rint(signal / np.abs(signal).max() * peak).astype('<i2')


@REAL
class RealOpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = fc.Ffmpeg(real_ffmpeg_path())
        if os.name == 'nt':
            assert cls.ffmpeg.sha256 == fc.PINNED_WINDOWS_SHA256, cls.ffmpeg.sha256

    def test_packet_count_for_every_partial_frame_and_header_fields(self):
        rng = np.random.default_rng(3)
        for remainder in range(160):  # encode() refuses any count other than the formula
            n = 160 + remainder
            self.ffmpeg.encode(np.rint(rng.normal(0, 2000, n)).astype('<i2').tobytes(), 'opus_8')
        for codec in OPUS:
            encoded = self.ffmpeg.encode(band_limited_noise(8000, 1).tobytes(), codec)
            pages = fc._ogg_pages(encoded)
            head = pages[0][1][0]
            self.assertEqual(struct.unpack('<BBHIhB', head[8:]), (1, 1, 312, 8000, 0, 0))
            packets = fc._opus_packets(pages, codec)
            self.assertEqual(len(packets), fc.encoded_frames(codec, 8000))
            self.assertEqual({len(p) for p in packets}, {fc.OPUS_FRAME_BYTES[fc.CODECS[codec]['bit_rate']]})
            last_granule = struct.unpack('<q', pages[-1][0][6:14])[0]
            self.assertEqual(last_granule, 8000 * 6 + 312)  # the stream ends exactly at the input

    def test_roundtrip_length_record_and_determinism(self):
        for samples in (1, 159, 160, 4013, 24037):
            pcm = band_limited_noise(max(samples, 64), 2)[:samples].tobytes()
            for codec in OPUS:
                decoded, record = self.ffmpeg.roundtrip(pcm, codec)
                self.assertEqual(len(decoded), len(pcm), (codec, samples))
                padded = samples + fc.OPUS_INPUT_PADDING
                self.assertEqual(record['encoded_frames'], fc.encoded_frames(codec, padded))
                self.assertEqual(record['decoded_samples_before_alignment'], 6 * padded)
                self.assertEqual((record['decode_rate'], record['delay_rate'], record['delay_compensated_samples'],
                                  record['input_padding_samples'], record['decode_resampler']),
                                 (48000, 48000, 4, 160, 'soxr_hq'))
                self.assertEqual(record['container'], 'ogg')
                self.assertNotIn(self.ffmpeg.executable, ' '.join(record['ffmpeg_encode_args']))
                if samples == 4013:
                    self.assertEqual(self.ffmpeg.roundtrip(pcm, codec), (decoded, record))

    def test_measured_48k_delay_still_holds(self):
        # Re-measure the constant the module removes: decode without alignment and find
        # the 48 kHz offset (0-12) that best matches the input after the soxr step.
        from data.prepare.conditioning import resample_to_pcm16
        n = 16000
        x = band_limited_noise(n, 4).astype(np.float64)
        for codec in OPUS:
            raw = self.ffmpeg.decode(self.ffmpeg.encode(np.concatenate([x, np.zeros(160)]).astype('<i2').tobytes(),
                                                        codec), codec)
            wide = np.frombuffer(raw, dtype='<i2').astype(np.float64)
            scores = {}
            for offset in range(13):
                narrow, _ = resample_to_pcm16(wide[offset:], 48000, 8000)
                y = np.frombuffer(narrow, dtype='<i2')[:n].astype(np.float64)
                scores[offset] = np.dot(x, y) / np.linalg.norm(x) / np.linalg.norm(y)
            self.assertEqual(max(scores, key=scores.get), fc.OPUS_DELAY_48K, (codec, scores))

    def test_aligned_output_and_lower_rates_lose_more(self):
        x = band_limited_noise(16000, 9)
        a = x[800:-800].astype(np.float64)
        errors = {}
        for codec in OPUS:
            decoded, _ = self.ffmpeg.roundtrip(x.tobytes(), codec)
            out = np.frombuffer(decoded, dtype='<i2').astype(np.float64)
            scores = {lag: np.corrcoef(a, out[800 + lag:800 + lag + len(a)])[0, 1] for lag in range(-640, 641)}
            self.assertEqual(max(scores, key=scores.get), 0, codec)
            errors[codec] = np.sqrt(np.mean((a - out[800:-800]) ** 2))
        self.assertGreater(errors['opus_8'], errors['opus_12'])
        self.assertGreater(errors['opus_12'], errors['opus_16'])

    def test_packet_loss_is_concealed_by_libopus_and_keeps_timing(self):
        x = band_limited_noise(16000, 5).tobytes()
        plan = next(p for p in (derive_loss(3, f'o-{i}') for i in range(300)) if p['loss_rate'] > 0.03)
        clean, _ = self.ffmpeg.roundtrip(x, 'opus_12')
        lossy, record = self.ffmpeg.roundtrip(x, 'opus_12', loss_plan=plan)
        self.assertEqual(len(lossy), len(x))
        loss = record['frame_loss']
        self.assertEqual((loss['clip_id'], loss['concealment'], loss['applied_via'], loss['unit']),
                         (plan['clip_id'], 'opus_decoder_native_plc', 'opus_toc_only_packets', 'opus_packet_20ms'))
        lost, expected = _loss_mask(record['encoded_frames'], plan['loss_rate'], plan['seed'])
        self.assertEqual({k: loss[k] for k in expected}, expected)
        self.assertGreater(int(lost.sum()), 0, 'pick a plan that loses packets')
        first = int(np.flatnonzero(lost)[0])
        # Packet k covers 48 kHz samples from 960k - 312 (pre-skip). The decoder output
        # before that is bit-identical; after the soxr step (block FFT filtering) earlier
        # 8 kHz samples may move by one rounding step, never more (measured 15 Nov).
        start48 = 960 * first - 312
        self.assertGreater(start48, 0, 'pick a plan whose first loss is not at the start')
        encoded = self.ffmpeg.encode(x + bytes(2 * fc.OPUS_INPUT_PADDING), 'opus_12')
        pages = fc._ogg_pages(encoded)
        packets = fc._opus_packets(pages, 'opus_12')
        self.assertEqual(len(packets), len(lost))
        received = fc._ogg_replace_packets(pages, [bytes([p[0] & 0xFC]) if g else p for p, g in zip(packets, lost)])
        self.assertEqual(hashlib.sha256(received).hexdigest(), record['received_sha256'])
        wide_clean = np.frombuffer(self.ffmpeg.decode(encoded, 'opus_12'), dtype='<i2')
        wide_lossy = np.frombuffer(self.ffmpeg.decode(received, 'opus_12', allow_lost_frames=True), dtype='<i2')
        self.assertTrue(np.array_equal(wide_clean[:start48], wide_lossy[:start48]))
        self.assertFalse(np.array_equal(wide_clean[start48:start48 + 960], wide_lossy[start48:start48 + 960]))
        a, b = np.frombuffer(clean, dtype='<i2'), np.frombuffer(lossy, dtype='<i2')
        cut = (start48 - fc.OPUS_DELAY_48K) // 6 - 80  # 10 ms margin for the soxr filter
        self.assertLessEqual(int(np.abs(a[:cut].astype(int) - b[:cut].astype(int)).max()), 1)
        start = (start48 - fc.OPUS_DELAY_48K) // 6
        region, reference = b[start:start + 160].astype(np.float64), a[start:start + 160].astype(np.float64)
        self.assertEqual(len(region), 160, 'pick a plan whose first loss is inside the clip')
        self.assertFalse(np.array_equal(region, reference))
        self.assertGreater(np.sqrt(np.mean(region ** 2)), 0.02 * np.sqrt(np.mean(reference ** 2)), 'concealed, not muted')
        self.assertNotEqual(record['received_sha256'], record['encoded_sha256'])
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), record['encoded_sha256'])
        # Review L3 (15 Nov): a concealed packet must still yield its full 20 ms.
        self.assertEqual(record['decoded_samples_before_alignment'], 6 * (len(x) // 2 + fc.OPUS_INPUT_PADDING))
        self.assertEqual(len(wide_lossy), len(wide_clean))
        self.assertEqual(self.ffmpeg.roundtrip(x, 'opus_12', loss_plan=plan), (lossy, record))

    def test_loss_of_the_first_packet_keeps_timing(self):
        # Review L3 (15 Nov): no decoder state exists yet when packet 0 is lost.
        x = band_limited_noise(8000, 12).tobytes()
        frames = fc.encoded_frames('opus_16', 8000 + fc.OPUS_INPUT_PADDING)
        plan = next(p for p in (derive_loss(9, f'f-{i}') for i in range(5000))
                    if _loss_mask(frames, p['loss_rate'], p['seed'])[0][0])
        clean, _ = self.ffmpeg.roundtrip(x, 'opus_16')
        lossy, record = self.ffmpeg.roundtrip(x, 'opus_16', loss_plan=plan)
        self.assertEqual(len(lossy), len(x))
        self.assertEqual(record['decoded_samples_before_alignment'], 6 * (8000 + fc.OPUS_INPUT_PADDING))
        first = (960 - fc.OPUS_PRE_SKIP) // 6  # 8 kHz samples carried by packet 0
        self.assertNotEqual(clean[:2 * first], lossy[:2 * first])

    def test_loss_and_jitter_join_one_mask(self):
        x = band_limited_noise(24000, 6).tobytes()
        frames = fc.encoded_frames('opus_8', 24000 + fc.OPUS_INPUT_PADDING)

        def masks(clip):
            loss, jitter = derive_loss(4, clip), derive_jitter(4, clip)
            return (_loss_mask(frames, loss['loss_rate'], loss['seed'])[0],
                    _late_mask(frames, jitter['jitter_ms'], jitter['buffer_ms'], jitter['seed'])[0])
        clip = next(c for c in (f'j-{i}' for i in range(2000)) if all(m.any() for m in masks(c)))
        loss_plan, jitter_plan = derive_loss(4, clip), derive_jitter(4, clip)
        decoded, record = self.ffmpeg.roundtrip(x, 'opus_8', loss_plan=loss_plan, jitter_plan=jitter_plan)
        self.assertEqual(record['encoded_frames'], frames)
        self.assertEqual(len(decoded), len(x))
        self.assertEqual(record['decoded_samples_before_alignment'], 6 * (24000 + fc.OPUS_INPUT_PADDING))
        lost, _ = _loss_mask(frames, loss_plan['loss_rate'], loss_plan['seed'])
        late, _ = _late_mask(frames, jitter_plan['jitter_ms'], jitter_plan['buffer_ms'], jitter_plan['seed'])
        concealment = record['frame_concealment']
        self.assertEqual(concealment['concealed_frames'], int((lost | late).sum()))
        self.assertEqual(concealment['lost_and_late_frames'], int((lost & late).sum()))
        self.assertEqual(concealment['frames'], frames)
        self.assertEqual(record['frame_jitter']['applied_via'], 'opus_toc_only_packets')
        with self.assertRaises(ValueError):
            self.ffmpeg.roundtrip(x, 'opus_8', loss_plan=loss_plan, jitter_plan=derive_jitter(5, clip))

    def test_telephone_wrapper_from_16k_uses_the_g711_record_names(self):
        canonical = np.repeat(band_limited_noise(8000, 8), 2).tobytes()  # 1 s at 16 kHz
        plan = derive_loss(6, 'clip-w')
        decoded, record = fc.telephone_codec_roundtrip(canonical, 'opus_16', self.ffmpeg, loss_plan=plan,
                                                       jitter_plan=derive_jitter(6, 'clip-w'))
        self.assertEqual(len(decoded), 2 * 8000)
        self.assertEqual((record['codec'], record['bit_rate'], record['resampler']), ('opus_16', 16000, 'soxr_hq'))
        self.assertEqual(record['packet_loss'], record['frame_loss'])
        self.assertEqual(record['jitter'], record['frame_jitter'])
        self.assertEqual(record['packet_concealment']['packets'], record['frame_concealment']['frames'])
        self.assertEqual(record['input_sha256'], hashlib.sha256(canonical).hexdigest())

    def test_silence_stays_quiet(self):
        for codec in OPUS:
            decoded, _ = self.ffmpeg.roundtrip(bytes(3200), codec)
            self.assertLess(np.abs(np.frombuffer(decoded, dtype='<i2')).max(), 200, codec)


@REAL
class RealOpusChainTests(Fixture):
    def test_opus_chain_with_loss_and_jitter_writes_and_verifies(self):
        tool = fc.Ffmpeg(real_ffmpeg_path())
        chain = ffmpeg_spec('opus_12', packet_loss=True, jitter=True, master_seed=21)
        self.assertEqual(augment.validate_spec(chain), chain)
        for bad in (ffmpeg_spec('opus_12', packet_loss=True, concealment='repeat_then_silence'),
                    ffmpeg_spec('opus_24'), ffmpeg_spec('opus_8', jitter=1)):
            with self.assertRaises(ValueError):
                augment.validate_spec(bad)
        out = self.base / 'aug-opus'
        summary = augment.augment_prepared(self.prepared, out, chain, ffmpeg=tool)
        self.assertEqual((summary['clips'], summary['errors']), (2, 0))
        report = json.loads((out / 'report.json').read_text(encoding='utf-8'))
        for copy in report['clips']:
            self.assertTrue(copy['path'].endswith('_opus_12.wav'), copy['path'])
            step = copy['chain']['steps'][0]
            plan = derive_loss(21, copy['chain']['derivation_clip_id'])
            self.assertEqual((step['packet_loss']['seed'], step['packet_loss']['clip_id']),
                             (plan['seed'], plan['clip_id']))
            self.assertEqual(step['ffmpeg_sha256'], tool.sha256)
            self.assertEqual(step['delay_compensated_samples'], fc.OPUS_DELAY_48K)
        self.assertEqual(augment.verify_augmented(self.prepared, out, expected_spec=chain, ffmpeg=tool), [])


if __name__ == '__main__':
    unittest.main()
