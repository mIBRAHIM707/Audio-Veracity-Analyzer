"""Phase 3 QA: malformed WAV refusal across every shared-audio entry point and
immutable originals (backlog 3.QA; PRD FR-D6, FR-D12, FR-D13; architecture §2.2).

Claude-authored test cases, 14 November 2026. Fixtures are generated in memory; no
real audio. These are infrastructure checks, not evidence of detector accuracy.
"""
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
import wave

import numpy as np

import contextlib
import os
import stat

from data.prepare import augment
from data.prepare.prepare_audio import process_manifest
from data.prepare.wav_format import SCAN_LIMIT, is_plain_pcm, pcm_format_problem
from scripts import verify_prepared_output
from services.inference.analysis import (AnalysisService, InputTooLarge, NeedsAsyncJob,
                                         UnsupportedAudio, decode_wav)
from tests.fixture_cleanup import cleanup_with_retry

RATE = 16000


def wav_bytes(raw, rate=RATE, channels=1, width=2):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as stream:
        stream.setparams((channels, width, rate, 0, 'NONE', 'not compressed'))
        stream.writeframes(raw)
    return buffer.getvalue()


def tone(seconds, rate=RATE, amplitude=6000, seed=0):
    rng = np.random.default_rng(seed)
    return np.rint(rng.normal(0, amplitude, round(seconds * rate))).clip(-32768, 32767).astype('<i2').tobytes()


def patched(payload, offset, fmt, value):
    """Overwrite one little-endian header field of a canonical 44-byte WAV header."""
    data = bytearray(payload)
    struct.pack_into(fmt, data, offset, value)
    return bytes(data)


def riff(*chunks):
    body = b'WAVE' + b''.join(name + struct.pack('<I', len(data)) + data + b'\x00' * (len(data) & 1)
                              for name, data in chunks)
    return b'RIFF' + struct.pack('<I', len(body)) + body


PCM_GUID = b'\x01\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71'


def extensible_pcm16(raw, rate=RATE):
    """WAVE_FORMAT_EXTENSIBLE with a PCM16 subformat: readable by Python 3.12+ `wave`."""
    fmt = struct.pack('<HHIIHHHHI', 0xFFFE, 1, rate, rate * 2, 2, 16, 22, 16, 4) + PCM_GUID
    return riff((b'fmt ', fmt), (b'data', raw))


VALID = wav_bytes(tone(1.0))
assert VALID[36:40] == b'data' and len(VALID) == 44 + 32000, 'canonical 44-byte header expected'

# Each case is a 16 kHz mono PCM16 WAV damaged in one way (or not a WAV at all).
MALFORMED = {
    'empty': b'',
    'riff_tag_only': b'RIFF',
    'header_without_data': VALID[:44],
    'not_riff': b'RIFX' + VALID[4:],
    'not_wave': VALID[:8] + b'AVI ' + VALID[12:],
    'odd_trailing_byte_missing': VALID[:-1],
    'half_of_declared_data': VALID[:44 + 16000],
    'zero_frames': wav_bytes(b''),
    'float_format_tag': patched(VALID, 20, '<H', 3),
    'unknown_format_tag': patched(VALID, 20, '<H', 0x55),
    'zero_channels': patched(VALID, 22, '<H', 0),
    'zero_sample_rate': patched(VALID, 24, '<I', 0),
    'zero_bits_per_sample': patched(VALID, 34, '<H', 0),
    'huge_declared_data_size': patched(VALID, 40, '<I', 0xFFFFFFFE),
    'missing_fmt_chunk': VALID[:12] + VALID[36:],
    'mu_law_format_tag': patched(VALID, 20, '<H', 7),
    'extensible_pcm16': extensible_pcm16(tone(1.0)),
    'inflated_declared_size_70s': patched(VALID, 40, '<I', 70 * RATE * 2),
    'duplicate_fmt_pcm_then_extensible': riff((b'fmt ', VALID[20:36]),
                                              (b'fmt ', extensible_pcm16(b'')[20:60]),
                                              (b'data', tone(1.0))),
    'fmt_chunk_cut_after_tag': VALID[:30],
}
# Cases refused by the explicit format-tag rule; Python 3.11 `wave` alone would refuse
# most of them too, so the tests assert the rule's own message.
TAG_RULE_CASES = ('float_format_tag', 'unknown_format_tag', 'mu_law_format_tag',
                  'extensible_pcm16', 'duplicate_fmt_pcm_then_extensible')


class RecordingDetector:
    def __init__(self):
        self.calls = 0

    def __call__(self, piece):
        self.calls += 1
        return [0.0, 0.0]


def snapshot(root):
    """Content, size and modification time of every file, plus the set of entries."""
    root = Path(root)
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_size,
                                       p.stat().st_mtime_ns) if p.is_file() else 'dir'
            for p in sorted(root.rglob('*'))}


class MalformedWavTests(unittest.TestCase):
    """Every damaged payload is refused by the analysis service, quarantined by
    preparation (other rows continue) and refused by the augmentation reader."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_with_retry, temporary)
        self.root = Path(temporary.name).resolve()

    def test_analysis_service_refuses_each_case_without_calling_the_detector(self):
        for name, payload in MALFORMED.items():
            with self.subTest(case=name):
                detector = RecordingDetector()
                service = AnalysisService(lambda: detector, model_id='fixture')
                with self.assertRaises(UnsupportedAudio) as caught:
                    service.analyze_wav(payload)
                self.assertEqual(detector.calls, 0)
                if name in TAG_RULE_CASES:
                    self.assertIn('plain PCM', str(caught.exception))

    def test_inflated_header_sizes_are_refused_as_truncated_not_routed_by_duration(self):
        # QA L2: before the fix these became 413 (huge) or a 202 async job (70 s).
        for name in ('huge_declared_data_size', 'inflated_declared_size_70s'):
            with self.subTest(case=name):
                with self.assertRaises(UnsupportedAudio) as caught:
                    decode_wav(MALFORMED[name])
                self.assertIn('truncated', str(caught.exception))
        # A genuine 70 s file is still routed to an asynchronous job.
        with self.assertRaises(NeedsAsyncJob):
            decode_wav(wav_bytes(b'\x00\x00' * 70 * RATE))

    def test_format_rule_and_its_independent_verifier_mirror_agree(self):
        fmt = VALID[20:36]
        accepted = {
            'canonical': VALID, 'bytearray': bytearray(VALID), 'memoryview': memoryview(VALID),
            'odd_chunk_before_fmt': riff((b'LIST', b'abc'), (b'fmt ', fmt), (b'data', b'\x00\x00')),
            'trailing_chunk': riff((b'fmt ', fmt), (b'data', b'\x00\x00'), (b'LIST', b'x')),
        }
        refused = {
            'empty': (b'', 'not a RIFF/WAVE'), 'riff_only': (b'RIFF', 'not a RIFF/WAVE'),
            'no_chunks': (VALID[:12], 'no fmt chunk found'),
            'tag_cut': (VALID[:21], 'fmt chunk is truncated'),
            'short_fmt': (riff((b'fmt ', b'\x01')), 'fmt chunk is truncated'),
            'data_first': (riff((b'data', b'\x00\x00'), (b'fmt ', fmt)), 'no fmt chunk before the data'),
            'extensible': (extensible_pcm16(b'\x00\x00'), 'format tag 0xfffe'),
            'float': (MALFORMED['float_format_tag'], 'format tag 0x0003'),
            'duplicate': (MALFORMED['duplicate_fmt_pcm_then_extensible'], 'more than one fmt'),
            'beyond_scan_limit': (riff((b'JUNK', b'\x00' * SCAN_LIMIT), (b'fmt ', fmt), (b'data', b'\x00\x00')),
                                  'first 1 MB'),
        }
        for name, payload in accepted.items():
            with self.subTest(accepted=name):
                self.assertIsNone(pcm_format_problem(payload))
                self.assertTrue(is_plain_pcm(payload))
                self.assertEqual(verify_prepared_output.format_tags(payload), [1])
        for name, (payload, reason) in refused.items():
            with self.subTest(refused=name):
                self.assertIn(reason, pcm_format_problem(payload))
                self.assertFalse(is_plain_pcm(payload))
                self.assertNotEqual(verify_prepared_output.format_tags(payload), [1])

    def test_verifier_rejects_non_plain_pcm_sources_like_preparation(self):
        # QA re-review finding 1: on Python 3.12+ `wave` would decode extensible PCM16.
        for name in TAG_RULE_CASES:
            with self.subTest(case=name):
                with self.assertRaisesRegex(verify_prepared_output.SourceRejected, 'plain PCM'):
                    verify_prepared_output.canonical_pcm(MALFORMED[name], 'condition')

    def test_augmentation_reader_refuses_each_case_with_a_value_error(self):
        for name, payload in MALFORMED.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError) as caught:
                    augment.read_pcm16_wav(payload, 'Parent clip')
                message = str(caught.exception)
                self.assertTrue(message.startswith('Parent clip '), message)
                if name in TAG_RULE_CASES:
                    self.assertIn('plain PCM', message)
                self.assertFalse(message.rstrip().endswith(':'), f'no reason given: {message!r}')

    def test_preparation_quarantines_each_case_and_still_processes_the_valid_row(self):
        sources = self.root / 'sources'
        sources.mkdir()
        good = wav_bytes(tone(4.0, seed=1))
        rows = []
        for name, payload in {**MALFORMED, 'valid': good}.items():
            (sources / f'{name}.wav').write_bytes(payload)
            rows.append({'path': f'{name}.wav', 'speaker_id': 'fictional-speaker',
                         'sha256': hashlib.sha256(payload).hexdigest()})
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'files': rows}), encoding='utf-8')
        for condition in (False, True):
            with self.subTest(condition=condition):
                output = self.root / f'out-{condition}'
                summary = process_manifest(manifest, sources, output, condition=condition)
                report = json.loads((output / 'report.json').read_text(encoding='utf-8'))
                by_path = {s['path']: s for s in report['sources']}
                for name in MALFORMED:
                    source = by_path[f'{name}.wav']
                    self.assertEqual(source['status'], 'quarantined', name)
                    self.assertTrue(source.get('error'), name)
                    self.assertNotIn('sample_count', source, name)
                    if name in TAG_RULE_CASES:
                        self.assertIn('plain PCM', source['error'])
                    if name == 'fmt_chunk_cut_after_tag':  # messageless EOFError from wave
                        self.assertIn('EOFError', source['error'])
                self.assertEqual(by_path['valid.wav']['status'], 'processed')
                self.assertEqual(summary['errors'], len(MALFORMED))
                self.assertTrue(report['clips'])
                self.assertEqual({c['source_path'] for c in report['clips']}, {'valid.wav'})
                listed = {c['path'] for c in report['clips']}
                written = {f'clips/{p.name}' for p in (output / 'clips').iterdir()}
                self.assertEqual(written, listed)
                self.assertFalse((output / 'INCOMPLETE').exists())


class ImmutableOriginalTests(unittest.TestCase):
    """Originals (sources, manifest, prepared parents, payload buffers) are never
    written, renamed or touched, including on refusal."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_with_retry, temporary)
        self.root = Path(temporary.name).resolve()
        self.sources = self.root / 'sources'
        (self.sources / 'nested').mkdir(parents=True)
        audio = np.concatenate([np.frombuffer(tone(3.5, seed=2), '<i2'), np.zeros(int(0.3 * RATE), '<i2'),
                                np.frombuffer(tone(4.0, seed=3), '<i2')]).tobytes()
        payloads = {'a.wav': wav_bytes(audio), 'nested/b.wav': wav_bytes(tone(3.2, rate=44100, seed=4),
                                                                         rate=44100),
                    'bad.wav': MALFORMED['half_of_declared_data'],
                    'ext.wav': MALFORMED['extensible_pcm16']}
        rows = []
        for path, payload in payloads.items():
            (self.sources / path).write_bytes(payload)
            rows.append({'path': path, 'speaker_id': 'fictional-speaker',
                         'sha256': hashlib.sha256(payload).hexdigest()})
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text(json.dumps({'files': rows}), encoding='utf-8')

    def originals(self):
        return snapshot(self.sources), (hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
                                        self.manifest.stat().st_mtime_ns)

    def test_preparation_leaves_sources_and_manifest_untouched(self):
        before = self.originals()
        for condition in (False, True):
            with self.subTest(condition=condition):
                process_manifest(self.manifest, self.sources, self.root / f'out-{condition}',
                                 condition=condition)
                self.assertEqual(self.originals(), before)

    def test_overlapping_or_existing_outputs_are_refused_before_any_write(self):
        before = self.originals()
        for name, output in {'output_is_source_root': self.sources,
                             'output_inside_source_root': self.sources / 'prepared',
                             'output_is_existing_parent': self.root}.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    process_manifest(self.manifest, self.sources, output, condition=True)
                self.assertEqual(self.originals(), before)
                self.assertFalse((self.sources / 'prepared').exists())
        # No staging directory is left beside the refused outputs.
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['manifest.json', 'sources'])

    def test_augmentation_and_verification_leave_the_prepared_parent_untouched(self):
        prepared = self.root / 'prepared'
        process_manifest(self.manifest, self.sources, prepared, condition=True)
        before = snapshot(prepared), self.originals()
        chain = {'chain_version': augment.CHAIN_VERSION, 'master_seed': 7, 'codec': 'g711_alaw',
                 'concealment': 'repeat_then_silence', 'packet_loss': True, 'jitter': True,
                 'noise': None}
        out = self.root / 'augmented'
        summary = augment.augment_prepared(prepared, out, chain)
        self.assertGreater(summary['clips'], 0)
        self.assertEqual(augment.verify_augmented(prepared, out), [])
        for name, target in {'inside_parent': prepared / 'aug', 'parent_itself': prepared}.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    augment.augment_prepared(prepared, target, chain)
        self.assertEqual((snapshot(prepared), self.originals()), before)

    def test_read_only_originals_prepare_augment_and_both_verifiers_change_nothing(self):
        # QA L4: originals marked read-only (the read-only attribute on Windows) still
        # prepare and augment, and neither verifier changes any tree.
        files = [p for p in self.sources.rglob('*') if p.is_file()] + [self.manifest]
        for path in files:
            os.chmod(path, stat.S_IREAD)
            self.addCleanup(os.chmod, path, stat.S_IREAD | stat.S_IWRITE)
        before = self.originals()
        prepared = self.root / 'prepared'
        summary = process_manifest(self.manifest, self.sources, prepared, condition=True)
        self.assertEqual((summary['sources'], summary['errors']), (4, 2))
        chain = {'chain_version': augment.CHAIN_VERSION, 'master_seed': 11, 'codec': 'g711_ulaw',
                 'concealment': 'repeat_then_silence', 'packet_loss': False, 'jitter': True,
                 'noise': None}
        out = self.root / 'augmented'
        augment.augment_prepared(prepared, out, chain)
        trees = snapshot(prepared), snapshot(out), self.originals()
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            code = verify_prepared_output.main(['--manifest', str(self.manifest), '--source-root',
                                                str(self.sources), '--output-root', str(prepared),
                                                '--mode', 'condition'])
        self.assertEqual(code, 0, printed.getvalue())
        self.assertEqual(augment.verify_augmented(prepared, out, expected_spec=chain), [])
        self.assertEqual((snapshot(prepared), snapshot(out), self.originals()), trees)
        self.assertEqual(self.originals(), before)

    def test_analysis_does_not_modify_a_mutable_payload_buffer(self):
        payload = bytearray((self.sources / 'nested/b.wav').read_bytes())
        copy = bytes(payload)
        detector = RecordingDetector()
        AnalysisService(lambda: detector, model_id='fixture').analyze_wav(payload)
        self.assertGreater(detector.calls, 0)
        self.assertEqual(bytes(payload), copy)


def deep_json(depth=100_000):
    return '[' * depth + ']' * depth


class ReportAndLineageTests(unittest.TestCase):
    """QA L3 (nested JSON refused as ValueError), L5 (odd-length parents) and L6 (copies
    do not inherit review/listening QA)."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_with_retry, temporary)
        self.root = Path(temporary.name).resolve()
        self.chain = {'chain_version': augment.CHAIN_VERSION, 'master_seed': 5, 'codec': 'g711_ulaw',
                      'concealment': 'repeat_then_silence', 'packet_loss': True, 'jitter': False,
                      'noise': None}

    def fabricated_parent(self, raw, **row_fields):
        """A minimal prepared root with one clip row (fixture; real roots come from
        prepare_audio)."""
        prepared = self.root / 'prepared'
        (prepared / 'clips').mkdir(parents=True)
        payload = wav_bytes(raw)
        (prepared / 'clips' / 'c.wav').write_bytes(payload)
        row = {'path': 'clips/c.wav', 'sha256': hashlib.sha256(payload).hexdigest(),
               'speaker_id': 'fictional-speaker', 'source_sha256': 'a' * 64,
               'start_sample': 100, 'end_sample': 100 + len(raw) // 2, **row_fields}
        (prepared / 'report.json').write_text(json.dumps({'clips': [row]}), encoding='utf-8')
        return prepared

    def test_deeply_nested_json_is_a_value_error_everywhere(self):
        manifest = self.root / 'manifest.json'
        manifest.write_text(deep_json(), encoding='utf-8')
        (self.root / 'sources').mkdir()
        with self.assertRaisesRegex(ValueError, 'nested too deeply'):
            process_manifest(manifest, self.root / 'sources', self.root / 'out')
        self.assertFalse((self.root / 'out').exists())
        prepared = self.root / 'deep-prepared'
        prepared.mkdir()
        (prepared / 'report.json').write_text(deep_json(), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'nested too deeply'):
            augment.augment_prepared(prepared, self.root / 'aug', self.chain)
        good = self.fabricated_parent(tone(3.0))
        out = self.root / 'aug-good'
        augment.augment_prepared(good, out, self.chain)
        os.replace(out / 'report.json', out / 'old.json')
        (out / 'report.json').write_text(deep_json(), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'nested too deeply'):
            augment.verify_augmented(good, out)
        spec = self.root / 'spec.json'
        spec.write_text(deep_json(10_000), encoding='utf-8')
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = augment.main(['--prepared-root', str(good), '--spec', str(spec),
                                 '--output-root', str(self.root / 'aug-cli')])
        self.assertEqual(code, 1)
        self.assertIn('nested too deeply', errors.getvalue())
        self.assertNotIn('Traceback', errors.getvalue())

    def test_odd_length_parent_records_its_own_output_length(self):
        for samples in (48001, 48003, 48000):
            with self.subTest(samples=samples):
                raw = tone(samples / RATE, seed=samples)
                self.assertEqual(len(raw), samples * 2)
                out, record = augment.augment_clip(raw, f'{"b" * 64}:0:{samples}', self.chain)
                self.assertEqual(record['input_sample_count'], samples)
                self.assertEqual(record['output_sample_count'], len(out) // 2)
                self.assertLessEqual(abs(len(out) // 2 - samples), 1)
        prepared = self.fabricated_parent(tone(48001 / RATE, seed=9))
        out = self.root / 'aug-odd'
        augment.augment_prepared(prepared, out, self.chain)
        copy = json.loads((out / 'report.json').read_text(encoding='utf-8'))['clips'][0]
        with wave.open(str(out / copy['path']), 'rb') as stream:
            written = stream.getnframes()
        self.assertEqual((copy['start_sample'], copy['end_sample']), (100, 100 + 48001))
        self.assertEqual(copy['chain']['output_sample_count'], written)
        self.assertEqual(copy['duration_seconds'], written / RATE)
        self.assertEqual(augment.verify_augmented(prepared, out), [])

    def test_copies_start_unreviewed_and_keep_parent_statuses_separately(self):
        prepared = self.fabricated_parent(tone(3.0), review_status='approved',
                                          listening_status='listened_ok', label='bonafide')
        out = self.root / 'aug-status'
        augment.augment_prepared(prepared, out, self.chain)
        copy = json.loads((out / 'report.json').read_text(encoding='utf-8'))['clips'][0]
        self.assertEqual(copy['review_status'], 'unreviewed')
        self.assertEqual(copy['parent_review_status'], 'approved')
        self.assertEqual(copy['parent_listening_status'], 'listened_ok')
        self.assertNotIn('listening_status', copy)
        self.assertEqual(copy['label'], 'bonafide')
        self.assertEqual(augment.verify_augmented(prepared, out), [])
        # A row without statuses gets no parent_* fields, only the copy's own status.
        bare = self.root / 'bare'
        os.replace(prepared, bare)
        prepared = self.fabricated_parent(tone(3.0))
        out = self.root / 'aug-bare'
        augment.augment_prepared(prepared, out, self.chain)
        copy = json.loads((out / 'report.json').read_text(encoding='utf-8'))['clips'][0]
        self.assertEqual(copy['review_status'], 'unreviewed')
        self.assertFalse({'parent_review_status', 'parent_listening_status'} & copy.keys())


if __name__ == '__main__':
    unittest.main()
