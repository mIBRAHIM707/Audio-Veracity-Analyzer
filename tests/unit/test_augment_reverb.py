"""Claude-authored tests (16 November 2026): room impulse responses in the augmentation
chain (backlog 3.4; PRD FR-D7 RIRs, FR-D13 lineage). Authored by Claude (Claude Code),
not by GPT-6 Astra. Fixture: the generated prepared root of test_augment_chain plus
hand-built 16 kHz RIRs (no corpus audio). Expected audio comes from an independent
composition of the public step functions on the same machine, not from pinned digests.
"""
import contextlib
import hashlib
import io
import json
import unittest
import wave

import numpy as np

from data.prepare import augment
from data.prepare.codecs import derive_jitter, derive_loss, telephone_roundtrip
from data.prepare.conditioning import canonicalize
from data.prepare.noise import derive_noise, mix_planned
from data.prepare.prepare_audio import process_manifest
from data.prepare.reverb import apply_planned, derive_reverb
from tests.unit import test_augment_chain
from tests.unit.test_augment_chain import pcm_of, spec, write_wav
from tests.unit.test_reverb import decaying_rir, pcm


def entry(raw, label):
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'label': label, 'source_id': f'fixture-{label}',
            'licence_ref': 'generated'}


class ReverbChainTests(unittest.TestCase):
    def setUp(self):
        test_augment_chain.AugmentChainTests.setUp(self)  # module import: not re-collected here
        self.rir_pcm = [decaying_rir(seed=11, rt=0.05), decaying_rir(seed=12, pre=30, rt=0.3),
                        decaying_rir(seed=13, length=8000, rt=0.5)]
        self.rir_paths = []
        for index, raw in enumerate(self.rir_pcm):
            path = self.base / f'rir{index}.wav'
            write_wav(path, raw)
            self.rir_paths.append(path)
        self.reverb = {'rirs': [entry(raw, label) for raw, label in
                                zip(self.rir_pcm, ('handset', 'speakerphone', 'speakerphone_large'))]}
        self.rirs = {e['sha256']: raw for e, raw in zip(self.reverb['rirs'], self.rir_pcm)}

    def test_spec_rules(self):
        self.assertTrue(augment.needs_rirs(spec(reverb=self.reverb)))
        self.assertFalse(augment.needs_rirs(spec()))
        self.assertEqual(augment.validate_spec(spec(reverb=self.reverb))['reverb'], self.reverb)
        first = self.reverb['rirs'][0]
        bad = [None, {}, {'rirs': []}, {'rirs': [first], 'extra': 1}, {'rirs': first},
               {'rirs': [first, dict(first)]},  # duplicate hash
               {'rirs': [dict(first, sha256='AB' * 32)]}, {'rirs': [dict(first, label='has space')]},
               {'rirs': [dict(first, licence_ref='')]}, {'rirs': [{k: first[k] for k in ('sha256', 'label')}]},
               {'rirs': [dict(first, sha256=f'{i:064x}') for i in range(65)]}]
        for reverb in bad:
            with self.assertRaises(ValueError, msg=reverb):
                augment.validate_spec(spec(reverb=reverb))
        # A spec without the key keeps its hash (optional key; no default inserted).
        self.assertNotIn('reverb', augment.validate_spec(spec()))

    def test_mix_spec_carries_reverb_to_each_channel(self):
        channels = [{'codec': 'g711_ulaw', 'concealment': 'repeat_then_silence', 'packet_loss': False,
                     'jitter': False, 'weight': 1},
                    {'codec': 'g711_alaw', 'concealment': 'repeat_then_silence', 'packet_loss': True,
                     'jitter': False, 'weight': 2}]
        mix = {'chain_version': augment.CHAIN_VERSION, 'master_seed': 5, 'codec_mix': channels, 'noise': None,
               'reverb': self.reverb}
        single, choice = augment.derive_channel(mix, 'clip-1')
        self.assertEqual(single['reverb'], self.reverb)
        self.assertEqual(choice['channel_spec_sha256'], augment.spec_sha256(single))
        with self.assertRaises(ValueError) as caught:
            augment.validate_spec(dict(mix, reverb={'rirs': []}))
        self.assertEqual(str(caught.exception).count('reverb.rirs'), 1)  # reported once, not per channel
        out = self.base / 'aug-mix'
        summary = augment.augment_prepared(self.prepared, out, mix, rir_paths=self.rir_paths)
        self.assertEqual(summary['clips'], 2)
        self.assertEqual(sum(summary['rir_counts'].values()), 2)
        self.assertEqual(augment.verify_augmented(self.prepared, out, rir_paths=self.rir_paths), [])

    def test_copies_match_an_independent_composition(self):
        chain = spec(reverb=self.reverb)
        out = self.base / 'aug-reverb'
        summary = augment.augment_prepared(self.prepared, out, chain, rir_paths=self.rir_paths)
        self.assertEqual((summary['clips'], summary['errors'], summary['reverb_clipping_refusals']), (2, 0, 0))
        self.assertEqual(set(summary['rir_counts']), set(self.rirs))
        report = json.loads((out / 'report.json').read_text(encoding='utf-8'))
        for parent, copy in zip(self.report['clips'], report['clips']):
            cid = augment.derivation_id(parent)
            plan = derive_reverb(2026, cid, 3)
            wet, _ = apply_planned(pcm_of(self.prepared / parent['path']), self.rir_pcm, plan)
            narrow, _ = telephone_roundtrip(wet, 'ulaw', concealment='repeat_then_silence',
                                            loss_plan=derive_loss(2026, cid), jitter_plan=derive_jitter(2026, cid))
            expected, _ = canonicalize(narrow, 8000, 1)
            self.assertEqual(pcm_of(out / copy['path']), expected)
            step = copy['chain']['steps'][0]
            self.assertEqual((step['step'], step['pool_index'], step['rir_sha256']),
                             ('room_impulse_response', plan['index'], self.reverb['rirs'][plan['index']]['sha256']))
            self.assertEqual(step['rir_label'], self.reverb['rirs'][plan['index']]['label'])
            self.assertEqual(step['speech_sha256'], copy['chain']['input_pcm_sha256'])
            self.assertEqual([s['step'] for s in copy['chain']['steps']],
                             ['room_impulse_response', 'telephone_roundtrip', 'return_to_model_grid'])
        self.assertEqual(augment.verify_augmented(self.prepared, out, rir_paths=self.rir_paths), [])
        # Order of --rir files does not matter; a missing or extra RIR is a problem, not a crash.
        self.assertEqual(augment.verify_augmented(self.prepared, out, rir_paths=self.rir_paths[::-1]), [])
        self.assertTrue(augment.verify_augmented(self.prepared, out, rir_paths=self.rir_paths[:2]))
        self.assertTrue(augment.verify_augmented(self.prepared, out))
        # A different RIR under the recorded spec is caught by the hash check.
        other = self.base / 'other.wav'
        write_wav(other, decaying_rir(seed=99))
        self.assertTrue(augment.verify_augmented(self.prepared, out, rir_paths=self.rir_paths[:2] + [other]))
        # Tampered audio is caught.
        copy_path = out / report['clips'][0]['path']
        payload = bytearray(copy_path.read_bytes())
        payload[-1] ^= 1
        copy_path.write_bytes(bytes(payload))
        self.assertTrue(any('does not rebuild' in p for p in
                            augment.verify_augmented(self.prepared, out, rir_paths=self.rir_paths)))

    def test_reverb_comes_before_noise(self):
        chain = spec(reverb=self.reverb, noise=self.noise_spec, packet_loss=False, jitter=False)
        clip_pcm = pcm_of(self.prepared / self.report['clips'][1]['path'])
        cid = augment.derivation_id(self.report['clips'][1])
        out, record = augment.augment_clip(clip_pcm, cid, chain, self.noise_pcm, rirs=self.rirs)
        names = [s['step'] for s in record['steps']]
        self.assertEqual(names[:2], ['room_impulse_response', 'additive_noise'])
        wet, _ = apply_planned(clip_pcm, self.rir_pcm, derive_reverb(2026, cid, 3))
        mixed, _ = mix_planned(wet, self.noise_pcm, derive_noise(2026, cid))
        self.assertEqual(record['steps'][1]['speech_sha256'], hashlib.sha256(wet).hexdigest())
        self.assertEqual(record['steps'][1]['mixed_pcm_sha256'], hashlib.sha256(mixed).hexdigest())

    def test_rir_inputs_must_match_the_spec_exactly(self):
        chain = spec(reverb=self.reverb)
        clip_pcm = pcm_of(self.prepared / self.report['clips'][0]['path'])
        first = next(iter(self.rirs))
        for rirs in (None, {}, {first: self.rirs[first]}, dict(self.rirs, **{'0' * 64: pcm([1])}),
                     dict(self.rirs, **{first: pcm([5])}), list(self.rirs.values())):
            with self.assertRaises(ValueError):
                augment.augment_clip(clip_pcm, 'c', chain, rirs=rirs)
        with self.assertRaises(ValueError):
            augment.augment_clip(clip_pcm, 'c', spec(), rirs=self.rirs)
        # Writer-side file checks run before anything is written.
        for paths in ([], self.rir_paths[:2], self.rir_paths + [self.rir_paths[0]]):
            target = self.base / f'never-{len(paths)}'
            with self.assertRaises(ValueError):
                augment.augment_prepared(self.prepared, target, chain, rir_paths=paths)
            self.assertFalse(target.exists())
        with self.assertRaises(ValueError):
            augment.augment_prepared(self.prepared, self.base / 'never-x', spec(), rir_paths=self.rir_paths)
        stereo = self.base / 'stereo.wav'
        with wave.open(str(stereo), 'wb') as audio:
            audio.setparams((2, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(pcm([1000, 1000, 0, 0]))
        with self.assertRaises(ValueError):
            augment.augment_prepared(self.prepared, self.base / 'never-s', chain,
                                     rir_paths=self.rir_paths[:2] + [stereo])

    def test_clipping_copy_is_refused_and_the_refusal_rebuilds(self):
        # x[n] + x[n-1] of an alternating signal is near zero, so the level rule drives it to clip.
        alternating = pcm([20000, -20000] * 4000)
        blur = pcm([1000, 1000])
        blur_sha = hashlib.sha256(blur).hexdigest()
        chain = spec(reverb={'rirs': [entry(blur, 'blur')]}, packet_loss=False, jitter=False)
        with self.assertRaises(augment.ReverbClipped) as caught:
            augment.augment_clip(alternating, 'c', chain, rirs={blur_sha: blur})
        self.assertGreater(caught.exception.clipped, 0)
        self.assertEqual(caught.exception.rir_sha256, blur_sha)
        # Through a real prepared root: one alternating parent next to a speech-like one.
        source = self.base / 'raw-alt'
        source.mkdir()
        rng = np.random.default_rng(4)
        audio = np.concatenate([np.tile(np.array([20000, -20000], '<i2'), 28000), np.zeros(4800, '<i2'),
                                np.rint(rng.normal(0, 6000, 64000)).clip(-32768, 32767).astype('<i2')])
        write_wav(source / 'alt.wav', audio.tobytes())
        manifest = self.base / 'manifest-alt.json'
        manifest.write_text(json.dumps({'files': [{'path': 'alt.wav', 'speaker_id': 'spk_alt', 'label': 'bonafide',
                                                   'sha256': hashlib.sha256((source / 'alt.wav').read_bytes())
                                                   .hexdigest()}]}))
        prepared = self.base / 'prepared-alt'
        process_manifest(manifest, source, prepared, condition=True)
        self.assertEqual(len(json.loads((prepared / 'report.json').read_text(encoding='utf-8'))['clips']), 2)
        blur_path = self.base / 'blur.wav'
        write_wav(blur_path, blur)
        out = self.base / 'aug-refused'
        summary = augment.augment_prepared(prepared, out, chain, rir_paths=[blur_path])
        self.assertEqual((summary['clips'], summary['errors'], summary['reverb_clipping_refusals']), (1, 1, 1))
        report = json.loads((out / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual((report['errors'][0]['refusal'], report['errors'][0]['rir_sha256']),
                         ('reverb_clipping', blur_sha))
        self.assertEqual(summary['rir_counts'], {blur_sha: 1})  # refused copies are not counted
        self.assertEqual(augment.verify_augmented(prepared, out, rir_paths=[blur_path]), [])

    def test_unusable_rir_stops_the_run_before_anything_is_written(self):
        chain_for = lambda raw: spec(reverb={'rirs': [entry(raw, 'bad')]})  # noqa: E731
        bad = {'silent': pcm([0] * 100), 'late': pcm([0] * 8001 + [1000]),
               'long': pcm([1000] + [1] * 32000)}
        clip_pcm = pcm_of(self.prepared / self.report['clips'][0]['path'])
        for name, raw in bad.items():
            path = self.base / f'{name}.wav'
            write_wav(path, raw)
            target = self.base / f'never-{name}'
            with self.assertRaisesRegex(ValueError, 'not usable', msg=name):
                augment.augment_prepared(self.prepared, target, chain_for(raw), rir_paths=[path])
            self.assertFalse(target.exists())
            with self.assertRaisesRegex(ValueError, 'not usable', msg=name):
                augment.augment_clip(clip_pcm, 'c', chain_for(raw), rirs={hashlib.sha256(raw).hexdigest(): raw})

    def test_command_line(self):
        spec_path = self.base / 'spec.json'
        spec_path.write_text(json.dumps(spec(reverb=self.reverb)), encoding='utf-8')
        out = self.base / 'aug-cli'
        args = ['--prepared-root', str(self.prepared), '--spec', str(spec_path)]
        rir_args = [a for path in self.rir_paths for a in ('--rir', str(path))]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(augment.main(args + ['--output-root', str(out)] + rir_args), 0)
            self.assertEqual(augment.main(args + ['--verify', str(out)] + rir_args), 0)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(augment.main(args + ['--verify', str(out)] + rir_args[:2]), 1)
            self.assertEqual(augment.main(args + ['--output-root', str(self.base / 'aug-cli-2')]), 1)
        self.assertIn('--rir', err.getvalue())

    def test_reports_without_reverb_are_unchanged(self):
        # The pre-reverb spec hash (pure JSON, the same on every platform) and exact key sets.
        self.assertEqual(augment.spec_sha256(spec()),
                         '38ace4515827ffc1a0947d1c006b1a2a3b0e3a3590dac120ab9992a8bbdaad2c')
        out = self.base / 'aug-plain'
        summary = augment.augment_prepared(self.prepared, out, spec())
        self.assertEqual(set(summary), {'parent_clips', 'clips', 'errors', 'noise_clipping_refusals',
                                        'review_flagged_clips'})
        report = json.loads((out / 'report.json').read_text(encoding='utf-8'))
        chain = report['clips'][0]['chain']
        self.assertEqual(set(chain), {'chain_version', 'chain_spec_sha256', 'derivation_clip_id', 'input_pcm_sha256',
                                      'input_sample_count', 'output_pcm_sha256', 'output_sample_count',
                                      'post_chain_normalization', 'steps'})
        self.assertEqual([s['step'] for s in chain['steps']], ['telephone_roundtrip', 'return_to_model_grid'])


if __name__ == '__main__':
    unittest.main()
