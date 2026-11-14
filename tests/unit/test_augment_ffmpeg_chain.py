"""Claude-authored tests (14 November 2026): GSM/AMR-NB chains in data/prepare/augment.py
(backlog 3.3 manifest wiring; PRD FR-D6 chain recorded per clip, FR-D13 lineage).

Authored by Claude (Claude Code), not GPT-6 Astra. A fake codec tool keeps the wiring
tests runnable without ffmpeg (as in CI); the real-ffmpeg cases (skipped without the
pinned ffmpeg 9.0.2) run the writer, the read-only verifier and the CLI end to end.
Fictional generated PCM in temporary directories; plumbing evidence only.
"""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from data.prepare import augment, ffmpeg_codecs
from data.prepare.conditioning import canonicalize, resample_to_pcm16
from data.prepare.prepare_audio import process_manifest
from tests.unit.test_augment_chain import RATE, speechlike, spec, tree_hashes, write_wav
from tests.unit.test_ffmpeg_codecs import real_ffmpeg_path


def ffmpeg_spec(codec='amr_nb_12.2', **overrides):
    value = spec(codec=codec, concealment=None, packet_loss=False, jitter=False)
    value.update(overrides)
    return value


class FakeTool:
    """Duck-typed stand-in for ffmpeg_codecs.Ffmpeg: a deterministic 'codec' that halves
    the signal, with an optional failure on the n-th call."""

    SHA256 = 'f' * 64

    def __init__(self, fail_on=None, gain=0.5, sha256=SHA256):
        self.calls, self.fail_on, self.gain, self.sha256 = 0, fail_on, gain, sha256

    def roundtrip(self, narrow, codec, loss_plan=None):
        self.calls += 1
        if self.fail_on == self.calls:
            raise ffmpeg_codecs.CodecToolError('fake tool fault')
        out = (np.frombuffer(narrow, dtype='<i2') * self.gain).astype('<i2').tobytes()
        return out, {'codec': codec, 'codec_implementation': 'fake', 'gain': self.gain,
                     'frame_loss': loss_plan, 'ffmpeg_sha256': self.sha256,
                     'decoded_pcm_sha256': hashlib.sha256(out).hexdigest()}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        source = self.base / 'raw'
        source.mkdir()
        audio = np.concatenate([speechlike(3.5, 1), np.zeros(int(0.3 * RATE), '<i2'), speechlike(4.0, 2)]).tobytes()
        write_wav(source / 'a.wav', audio)
        sha = hashlib.sha256((source / 'a.wav').read_bytes()).hexdigest()
        manifest = self.base / 'manifest.json'
        manifest.write_text(json.dumps({'files': [{'path': 'a.wav', 'speaker_id': 'spk_1', 'sha256': sha,
                                                   'label': 'bonafide'}]}))
        self.prepared = self.base / 'prepared'
        process_manifest(manifest, source, self.prepared, condition=True)
        self.assertEqual(len(json.loads((self.prepared / 'report.json').read_text(encoding='utf-8'))['clips']), 2)


class SpecTests(unittest.TestCase):
    def test_gsm_and_amr_specs_need_explicit_no_loss(self):
        for codec in ffmpeg_codecs.CODECS:
            self.assertEqual(augment.validate_spec(ffmpeg_spec(codec)), ffmpeg_spec(codec))
            self.assertTrue(augment.needs_ffmpeg(ffmpeg_spec(codec)))
        for codec in ('amr_nb_4.75', 'amr_nb_7.4', 'amr_nb_12.2'):  # AMR frame loss is allowed
            augment.validate_spec(ffmpeg_spec(codec, packet_loss=True))
        bad = [ffmpeg_spec(concealment='repeat'), ffmpeg_spec('gsm', packet_loss=True), ffmpeg_spec(jitter=True),
               ffmpeg_spec(jitter=None), ffmpeg_spec(packet_loss=1), ffmpeg_spec(packet_loss=None),
               ffmpeg_spec('amr_nb_7.4', packet_loss=True, concealment='repeat'),
               ffmpeg_spec('amr_wb_23.85'), ffmpeg_spec([]), ffmpeg_spec({'x': 1}),
               spec(codec='g711_ulaw', concealment=None)]
        for value in bad:
            with self.assertRaises(ValueError, msg=value['codec']):
                augment.validate_spec(value)
        self.assertFalse(augment.needs_ffmpeg(spec()))
        self.assertFalse(augment.needs_ffmpeg({'codec': []}))


class FakeToolTests(Fixture):
    def test_clip_needs_a_tool_and_matches_an_independent_composition(self):
        clip = speechlike(1.0, 9).tobytes()
        with self.assertRaisesRegex(ValueError, 'needs an ffmpeg'):
            augment.augment_clip(clip, 'id', ffmpeg_spec())
        tool = FakeTool()
        out, record = augment.augment_clip(clip, 'id', ffmpeg_spec(), ffmpeg=tool)
        narrow, _ = resample_to_pcm16(np.frombuffer(clip, dtype='<i2').astype(np.float64), RATE, 8000)
        halved = (np.frombuffer(narrow, dtype='<i2') * 0.5).astype('<i2').tobytes()
        expected, _ = canonicalize(halved, 8000, 1)
        self.assertEqual(out, expected)
        step = record['steps'][0]
        self.assertEqual((step['step'], step['codec'], step['codec_implementation']),
                         ('telephone_roundtrip', 'amr_nb_12.2', 'fake'))
        self.assertIsNone(step['packet_loss'])
        self.assertEqual(record['steps'][1]['step'], 'return_to_model_grid')

    def test_writer_and_verifier_with_a_tool(self):
        out = self.base / 'aug'
        summary = augment.augment_prepared(self.prepared, out, ffmpeg_spec('gsm'), ffmpeg=FakeTool())
        self.assertEqual((summary['clips'], summary['errors']), (2, 0))
        report = json.loads((out / 'report.json').read_text(encoding='utf-8'))
        self.assertTrue(all(c['path'].endswith('_gsm.wav') for c in report['clips']))
        self.assertEqual(augment.verify_augmented(self.prepared, out, ffmpeg=FakeTool()), [])
        self.assertEqual(augment.recorded_tool_hash(report), FakeTool.SHA256)
        problems = augment.verify_augmented(self.prepared, out)
        self.assertEqual(problems, ['codec gsm needs the ffmpeg codec tool (--ffmpeg) to rebuild'])
        # Same audio, another binary: named as a tool mismatch, not as row drift.
        other = augment.verify_augmented(self.prepared, out, ffmpeg=FakeTool(sha256='e' * 64))
        self.assertEqual(len(other), 1)
        self.assertIn(f'made with ffmpeg sha256 {FakeTool.SHA256}; verifying with {"e" * 64}', other[0])
        drifted = augment.verify_augmented(self.prepared, out, ffmpeg=FakeTool(gain=0.25))
        self.assertTrue(any('does not rebuild byte-for-byte' in p for p in drifted), drifted)
        self.assertTrue(any('copy row' in p for p in drifted), drifted)

    def test_cli_refuses_tool_options_for_g711_and_invalid_specs_before_tool_lookup(self):
        for chain, extra, message in ((spec(), ['--ffmpeg', 'x.exe'], 'apply only to GSM/AMR-NB'),
                                      (spec(), ['--ffmpeg-sha256', '0' * 64], 'apply only to GSM/AMR-NB'),
                                      (ffmpeg_spec(jitter=True), ['--ffmpeg', 'missing.exe'], 'jitter must be false')):
            path = self.base / 'spec.json'
            path.write_text(json.dumps(chain))
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = augment.main(['--prepared-root', str(self.prepared), '--spec', str(path),
                                     '--output-root', str(self.base / 'never'), *extra])
            self.assertEqual(code, 1)
            self.assertIn(message, err.getvalue())
            self.assertFalse((self.base / 'never').exists())

    def test_tool_failure_aborts_before_writing(self):
        out = self.base / 'aug-fail'
        before = tree_hashes(self.prepared)
        with self.assertRaises(ffmpeg_codecs.CodecToolError):
            augment.augment_prepared(self.prepared, out, ffmpeg_spec(), ffmpeg=FakeTool(fail_on=2))
        self.assertFalse(out.exists())
        self.assertEqual(list(self.base.glob('ava-aug-*')), [])  # staging removed
        self.assertEqual(tree_hashes(self.prepared), before)
        with self.assertRaisesRegex(ValueError, 'needs an ffmpeg'):
            augment.augment_prepared(self.prepared, self.base / 'aug-none', ffmpeg_spec())
        self.assertFalse((self.base / 'aug-none').exists())

    def test_verifier_reports_a_tool_failure_instead_of_raising(self):
        out = self.base / 'aug'
        augment.augment_prepared(self.prepared, out, ffmpeg_spec(), ffmpeg=FakeTool())
        problems = augment.verify_augmented(self.prepared, out, ffmpeg=FakeTool(fail_on=1))
        self.assertTrue(any('codec tool failed' in p for p in problems), problems)


@unittest.skipIf(real_ffmpeg_path() is None, 'pinned ffmpeg 9.0.2 not installed (set AVA_FFMPEG)')
class RealFfmpegChainTests(Fixture):
    def test_amr_chain_writes_verifies_and_records_the_tool(self):
        tool = ffmpeg_codecs.Ffmpeg(real_ffmpeg_path())
        out = self.base / 'aug-amr'
        summary = augment.augment_prepared(self.prepared, out, ffmpeg_spec('amr_nb_4.75'), ffmpeg=tool)
        self.assertEqual((summary['clips'], summary['errors']), (2, 0))
        report = json.loads((out / 'report.json').read_text(encoding='utf-8'))
        for copy in report['clips']:
            step = copy['chain']['steps'][0]
            self.assertEqual(step['ffmpeg_sha256'], tool.sha256)
            self.assertEqual(step['delay_compensated_samples'], 40)
            self.assertEqual(copy['chain']['input_sample_count'], copy['end_sample'] - copy['start_sample'])
            self.assertLessEqual(abs(copy['chain']['output_sample_count'] - copy['chain']['input_sample_count']), 1)
        self.assertEqual(augment.verify_augmented(self.prepared, out, expected_spec=ffmpeg_spec('amr_nb_4.75'),
                                                  ffmpeg=tool), [])

    def test_amr_chain_with_frame_loss_uses_per_clip_plans(self):
        from data.prepare.codecs import derive_loss
        tool = ffmpeg_codecs.Ffmpeg(real_ffmpeg_path())
        chain = ffmpeg_spec('amr_nb_12.2', packet_loss=True, master_seed=12)
        out = self.base / 'aug-amr-loss'
        summary = augment.augment_prepared(self.prepared, out, chain, ffmpeg=tool)
        self.assertEqual((summary['clips'], summary['errors']), (2, 0))
        report = json.loads((out / 'report.json').read_text(encoding='utf-8'))
        for copy in report['clips']:
            loss = copy['chain']['steps'][0]['packet_loss']
            plan = derive_loss(12, copy['chain']['derivation_clip_id'])
            self.assertEqual((loss['loss_rate'], loss['seed'], loss['clip_id']),
                             (plan['loss_rate'], plan['seed'], plan['clip_id']))
            self.assertEqual(loss['applied_via'], 'amr_no_data_frames')
        self.assertEqual(augment.verify_augmented(self.prepared, out, ffmpeg=tool), [])

    def test_cli_with_ffmpeg_and_hash_pin(self):
        spec_path = self.base / 'spec.json'
        spec_path.write_text(json.dumps(ffmpeg_spec('gsm')))
        tool = ffmpeg_codecs.Ffmpeg(real_ffmpeg_path())
        out = self.base / 'aug-cli'
        common = ['--prepared-root', str(self.prepared), '--spec', str(spec_path), '--ffmpeg', real_ffmpeg_path()]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(augment.main(common + ['--ffmpeg-sha256', tool.sha256, '--output-root', str(out)]), 0)
            self.assertEqual(augment.main(common + ['--verify', str(out)]), 0)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(augment.main(common + ['--ffmpeg-sha256', '0' * 64, '--verify', str(out)]), 1)
        self.assertIn('hash', err.getvalue())


if __name__ == '__main__':
    unittest.main()
