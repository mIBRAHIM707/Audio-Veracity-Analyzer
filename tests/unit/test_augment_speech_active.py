"""Claude-authored tests (15 November 2026): speech-active SNR in the augmentation chain
(backlog 3.4; PRD FR-D7, FR-D13). Authored by Claude (Claude Code), not by GPT-6 Astra.
A fake VAD with a model hash stands in for Silero (the real model is exercised by
tests/model); the fixture is the generated prepared root of test_augment_chain.
"""
import contextlib
import io
import json
import unittest

import numpy as np

from data.prepare import augment
from data.prepare.noise import VadError, derive_noise, mix_planned, speech_mask
from tests.unit.test_augment_chain import AugmentChainTests, pcm_of, spec

VAD_SHA = 'ab' * 32


class FakeSilero:
    """Energy VAD with the SileroVad interface the chain relies on."""

    def __init__(self, model_sha256=VAD_SHA):
        self.model_sha256, self.resets = model_sha256, 0

    def reset(self):
        self.resets += 1

    def __call__(self, frame):
        return 1.0 if float(np.sqrt(np.mean(frame ** 2))) > 0.02 else 0.0


class SpeechActiveChainTests(unittest.TestCase):
    def setUp(self):
        AugmentChainTests.setUp(self)  # same generated prepared root and noise file
        self.active_noise = dict(self.noise_spec, snr_basis='speech_active', vad_sha256=VAD_SHA)

    def test_spec_rules(self):
        self.assertTrue(augment.needs_vad(spec(noise=self.active_noise)))
        self.assertFalse(augment.needs_vad(spec(noise=self.noise_spec)))
        self.assertEqual(augment.validate_spec(spec(noise=self.active_noise))['noise'], self.active_noise)
        bad = [dict(self.noise_spec, snr_basis='speech_active'),  # VAD hash missing
               dict(self.active_noise, snr_basis='whole_clip'), dict(self.active_noise, vad_sha256='AB' * 32),
               dict(self.active_noise, vad_sha256=None), dict(self.noise_spec, vad_sha256=VAD_SHA)]
        for noise in bad:
            with self.assertRaises(ValueError, msg=noise):
                augment.validate_spec(spec(noise=noise))

    def test_writer_and_verifier_use_the_named_vad(self):
        chain = spec(noise=self.active_noise, codec='g711_alaw')
        out = self.base / 'aug-active'
        summary = augment.augment_prepared(self.prepared, out, chain, self.noise_path, vad=FakeSilero())
        self.assertEqual(summary['clips'], 2)
        report = json.loads((out / 'report.json').read_text(encoding='utf-8'))
        parent, copy = self.report['clips'][0], report['clips'][0]
        step = copy['chain']['steps'][0]
        self.assertEqual((step['step'], step['snr_basis'], step['noise_snr_basis'], step['noise_vad_sha256']),
                         ('additive_noise', 'speech_active_mean_square', 'speech_active', VAD_SHA))
        source = step['mask_source']
        self.assertEqual((source['vad_sha256'], source['mask_rule'], source['threshold'], source['min_active_samples']),
                         (VAD_SHA, 'ava-speech-mask-v1', 0.5, 8000))
        # Independent composition: the same mask and plan give the same mixed audio.
        clip = pcm_of(self.prepared / parent['path'])
        mask, decisions = speech_mask(clip, FakeSilero())
        self.assertEqual(source['frame_decisions_hex'], decisions)
        mixed, record = mix_planned(clip, self.noise_pcm, derive_noise(2026, augment.derivation_id(parent)),
                                    active_mask=mask)
        self.assertEqual(step['mixed_pcm_sha256'], record['mixed_pcm_sha256'])
        self.assertEqual(step['active_fraction'], record['active_fraction'])
        self.assertLess(step['active_fraction'], 1.0)  # the fixture has quiet stretches the VAD drops
        self.assertEqual(augment.verify_augmented(self.prepared, out, self.noise_path, vad=FakeSilero()), [])
        self.assertEqual(augment.verify_augmented(self.prepared, out, self.noise_path),
                         ['the speech-active SNR needs the VAD whose model_sha256 the specification names'])
        other = augment.verify_augmented(self.prepared, out, self.noise_path, vad=FakeSilero('cd' * 32))
        self.assertEqual(other, ['the speech-active SNR needs the VAD whose model_sha256 the specification names'])
        # A VAD that decides differently (another runtime, a tampered record) is caught.
        class Stricter(FakeSilero):
            def __call__(self, frame):
                return 1.0 if float(np.sqrt(np.mean(frame ** 2))) > 0.2 else 0.0
        drift = augment.verify_augmented(self.prepared, out, self.noise_path, vad=Stricter())
        self.assertTrue(drift, 'different VAD decisions must not verify')
        report['clips'][0]['chain']['steps'][0]['active_fraction'] = 0.123
        (out / 'report.json').write_text(json.dumps(report, indent=2, sort_keys=True), encoding='utf-8')
        tampered = augment.verify_augmented(self.prepared, out, self.noise_path, vad=FakeSilero())
        self.assertTrue(any('copy row 0' in p for p in tampered), tampered)

    def test_vad_fault_aborts_before_writing_and_is_reported_by_the_verifier(self):
        class Broken(FakeSilero):
            def __call__(self, frame):
                raise RuntimeError('onnxruntime fault')
        chain = spec(noise=self.active_noise)
        with self.assertRaises(VadError):
            augment.augment_prepared(self.prepared, self.base / 'aug-broken', chain, self.noise_path, vad=Broken())
        self.assertFalse((self.base / 'aug-broken').exists())
        out = self.base / 'aug-ok'
        augment.augment_prepared(self.prepared, out, chain, self.noise_path, vad=FakeSilero())
        problems = augment.verify_augmented(self.prepared, out, self.noise_path, vad=Broken())
        self.assertEqual(len(problems), 1)
        self.assertIn('VAD failed during rebuild', problems[0])

    def test_codec_mix_with_speech_active_noise(self):
        chain = {'chain_version': augment.CHAIN_VERSION, 'master_seed': 7, 'noise': self.active_noise,
                 'codec_mix': [dict(codec='g711_ulaw', concealment='repeat_then_silence', packet_loss=False,
                                    jitter=False, weight=1),
                               dict(codec='g711_alaw', concealment='repeat_then_silence', packet_loss=True,
                                    jitter=False, weight=1)]}
        out = self.base / 'aug-mix'
        summary = augment.augment_prepared(self.prepared, out, chain, self.noise_path, vad=FakeSilero())
        self.assertEqual((summary['clips'], summary['insufficient_speech_refusals']), (2, 0))
        report = json.loads((out / 'report.json').read_text(encoding='utf-8'))
        for copy in report['clips']:
            self.assertEqual(copy['chain']['steps'][0]['snr_basis'], 'speech_active_mean_square')
        self.assertEqual(augment.verify_augmented(self.prepared, out, self.noise_path, vad=FakeSilero()), [])

    def test_whole_clip_specs_are_unchanged_and_refuse_a_vad(self):
        out = self.base / 'aug-whole'
        augment.augment_prepared(self.prepared, out, spec(noise=self.noise_spec), self.noise_path)
        step = json.loads((out / 'report.json').read_text(encoding='utf-8'))['clips'][0]['chain']['steps'][0]
        self.assertEqual(step['snr_basis'], 'whole_clip_mean_square')
        self.assertNotIn('mask_source', step)
        with self.assertRaisesRegex(ValueError, 'no speech-active'):
            augment.augment_prepared(self.prepared, self.base / 'aug-x', spec(noise=self.noise_spec),
                                     self.noise_path, vad=FakeSilero())
        with self.assertRaisesRegex(ValueError, 'model_sha256'):
            augment.augment_prepared(self.prepared, self.base / 'aug-y', spec(noise=self.active_noise),
                                     self.noise_path, vad=FakeSilero('cd' * 32))
        self.assertFalse((self.base / 'aug-x').exists() or (self.base / 'aug-y').exists())

    def test_clip_with_too_little_speech_is_an_error_row(self):
        class Deaf(FakeSilero):
            def __call__(self, frame):
                return 0.0
        out = self.base / 'aug-deaf'
        summary = augment.augment_prepared(self.prepared, out, spec(noise=self.active_noise), self.noise_path,
                                           vad=Deaf())
        self.assertEqual((summary['clips'], summary['errors'], summary['insufficient_speech_refusals']), (0, 2, 2))
        report = json.loads((out / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual({(e['refusal'], e['active_samples']) for e in report['errors']}, {('insufficient_speech', 0)})

    def test_cli_needs_and_refuses_the_vad_model_option(self):
        for chain, extra, message in ((spec(noise=self.active_noise), [], 'give --vad-model'),
                                      (spec(noise=self.noise_spec), ['--vad-model', 'x.onnx'], 'applies only')):
            path = self.base / 'spec.json'
            path.write_text(json.dumps(chain))
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = augment.main(['--prepared-root', str(self.prepared), '--spec', str(path), '--noise',
                                     str(self.noise_path), '--output-root', str(self.base / 'never'), *extra])
            self.assertEqual(code, 1)
            self.assertIn(message, err.getvalue())
        self.assertFalse((self.base / 'never').exists())


if __name__ == '__main__':
    unittest.main()
