"""Recorded augmentation chain over a prepared output root (backlog 3.3/3.4 manifest
wiring; PRD FR-D6 chain recorded per clip, FR-D7 noise, FR-D13 lineage).

Claude-authored test cases, 11 November 2026. Fictional generated PCM in temporary
directories; infrastructure evidence only, nothing about detector accuracy.
"""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import wave

import numpy as np

from data.prepare import augment
from data.prepare.codecs import derive_jitter, derive_loss, telephone_roundtrip
from data.prepare.conditioning import canonicalize
from data.prepare.noise import derive_noise, mix_planned
from data.prepare.prepare_audio import process_manifest

RATE = 16000


def write_wav(path, raw):
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        audio.writeframes(raw)


def pcm_of(path):
    with wave.open(str(path), 'rb') as audio:
        return audio.readframes(audio.getnframes())


def speechlike(seconds, seed, amplitude=6000):
    rng = np.random.default_rng(seed)
    return np.rint(rng.normal(0, amplitude, int(seconds * RATE))).clip(-32768, 32767).astype('<i2')


def spec(**overrides):
    base = {'chain_version': augment.CHAIN_VERSION, 'master_seed': 2026, 'codec': 'g711_ulaw',
            'concealment': 'repeat_then_silence', 'packet_loss': True, 'jitter': True, 'noise': None}
    base.update(overrides)
    return base


def tree_hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(root).rglob('*')) if p.is_file()}


class AugmentChainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        source = base / 'raw'
        source.mkdir()
        # 3.5 s speech-like noise, 0.3 s digital silence, 4 s: two pause-bounded candidates.
        audio = np.concatenate([speechlike(3.5, 1), np.zeros(int(0.3 * RATE), '<i2'),
                                speechlike(4.0, 2)]).tobytes()
        write_wav(source / 'a.wav', audio)
        sha = hashlib.sha256((source / 'a.wav').read_bytes()).hexdigest()
        manifest = base / 'manifest.json'
        manifest.write_text(json.dumps({'files': [{'path': 'a.wav', 'speaker_id': 'spk_1',
                                                   'sha256': sha, 'label': 'bonafide'}]}))
        self.prepared = base / 'prepared'
        process_manifest(manifest, source, self.prepared, condition=True)
        self.report = json.loads((self.prepared / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(len(self.report['clips']), 2, 'fixture must give two parent clips')
        self.base = base
        self.noise_pcm = speechlike(2.0, 3, amplitude=3000).tobytes()
        self.noise_path = base / 'noise.wav'
        write_wav(self.noise_path, self.noise_pcm)
        self.noise_spec = {'sha256': hashlib.sha256(self.noise_pcm).hexdigest(), 'label': 'babble',
                           'source_id': 'fixture-noise', 'licence_ref': 'generated'}

    def run_chain(self, chain, name='aug', noise=None):
        out = self.base / name
        summary = augment.augment_prepared(self.prepared, out, chain, noise)
        return out, summary, json.loads((out / 'report.json').read_text(encoding='utf-8'))

    def test_copies_match_an_independent_composition_of_the_steps(self):
        out, summary, report = self.run_chain(spec())
        self.assertEqual({k: summary[k] for k in ('parent_clips', 'clips', 'errors', 'noise_clipping_refusals')},
                         {'parent_clips': 2, 'clips': 2, 'errors': 0, 'noise_clipping_refusals': 0})
        self.assertEqual(summary['review_flagged_clips'], sum(bool(c['review_flags']) for c in report['clips']))
        for parent, copy in zip(self.report['clips'], report['clips']):
            clip = pcm_of(self.prepared / parent['path'])
            cid = f"{parent['source_sha256']}:{parent['start_sample']}:{parent['end_sample']}"
            self.assertEqual(augment.derivation_id(parent), cid)
            narrow, _ = telephone_roundtrip(clip, 'ulaw', concealment='repeat_then_silence',
                                            loss_plan=derive_loss(2026, cid),
                                            jitter_plan=derive_jitter(2026, cid))
            expected, _ = canonicalize(narrow, 8000, 1)
            self.assertEqual(pcm_of(out / copy['path']), expected)
            self.assertEqual(len(expected), len(clip), 'returned to the 16 kHz grid at the same length')
            self.assertNotEqual(expected, clip)
            self.assertEqual(copy['parent_clip_sha256'], parent['sha256'])
            self.assertEqual((copy['augmented_copy'], copy['counts_toward_clip_total']), (True, False))
            self.assertEqual((copy['speaker_id'], copy['label'], copy['start_sample']),
                             ('spk_1', 'bonafide', parent['start_sample']))
            steps = copy['chain']['steps']
            self.assertEqual([s['step'] for s in steps], ['telephone_roundtrip', 'return_to_model_grid'])
            self.assertEqual(steps[0]['packet_loss']['concealment'], 'repeat_then_silence')
            self.assertEqual(steps[0]['jitter']['clip_id'], cid)
            self.assertEqual(copy['chain']['output_pcm_sha256'], hashlib.sha256(expected).hexdigest())
            self.assertEqual(copy['sha256'], hashlib.sha256((out / copy['path']).read_bytes()).hexdigest())
        self.assertEqual(report['chain_spec_sha256'], augment.spec_sha256(spec()))
        self.assertEqual(report['parent_report_sha256'],
                         hashlib.sha256((self.prepared / 'report.json').read_bytes()).hexdigest())
        self.assertNotEqual(*(c['chain']['steps'][0]['packet_loss']['seed'] for c in report['clips']),
                            'each clip has its own loss draw')
        self.assertEqual(augment.verify_augmented(self.prepared, out), [])
        self.assertFalse((out / 'INCOMPLETE').exists())

    def test_noise_is_mixed_before_the_codec_and_order_matters(self):
        out, _, report = self.run_chain(spec(noise=self.noise_spec, codec='g711_alaw'),
                                        noise=self.noise_path)
        self.assertEqual(report['summary']['clips'], 2, report['errors'])
        parent, copy = self.report['clips'][0], report['clips'][0]
        clip = pcm_of(self.prepared / parent['path'])
        cid = augment.derivation_id(parent)
        noisy, _ = mix_planned(clip, self.noise_pcm, derive_noise(2026, cid))
        narrow, _ = telephone_roundtrip(noisy, 'alaw', concealment='repeat_then_silence',
                                        loss_plan=derive_loss(2026, cid),
                                        jitter_plan=derive_jitter(2026, cid))
        expected, _ = canonicalize(narrow, 8000, 1)
        self.assertEqual(pcm_of(out / copy['path']), expected)
        clean_narrow, _ = telephone_roundtrip(clip, 'alaw', concealment='repeat_then_silence',
                                              loss_plan=derive_loss(2026, cid),
                                              jitter_plan=derive_jitter(2026, cid))
        codec_first, _ = mix_planned(canonicalize(clean_narrow, 8000, 1)[0], self.noise_pcm,
                                     derive_noise(2026, cid))
        self.assertNotEqual(expected, codec_first)
        self.assertEqual([s['step'] for s in copy['chain']['steps']],
                         ['additive_noise', 'telephone_roundtrip', 'return_to_model_grid'])
        noise_step = copy['chain']['steps'][0]
        self.assertEqual((noise_step['noise_label'], noise_step['noise_source_id'],
                          noise_step['noise_licence_ref'], noise_step['snr_db']),
                         ('babble', 'fixture-noise', 'generated', derive_noise(2026, cid)['snr_db']))
        self.assertTrue(0 <= noise_step['snr_db'] <= 20)
        self.assertEqual(augment.verify_augmented(self.prepared, out, self.noise_path), [])
        with self.assertRaisesRegex(ValueError, 'give the noise WAV'):
            augment.verify_augmented(self.prepared, out, None)

    def test_specification_must_name_concealment_and_lists_every_problem(self):
        incomplete = spec()
        del incomplete['concealment']
        with self.assertRaisesRegex(ValueError, 'missing keys'):
            augment.validate_spec(incomplete)
        bad = spec(codec='gsm', packet_loss=1, extra=True, master_seed=-1, chain_version='v0',
                   noise={'sha256': 'ABC', 'label': 'Bad Label', 'source_id': '', 'licence_ref': 3},
                   concealment='fade')
        with self.assertRaises(ValueError) as caught:
            augment.validate_spec(bad)
        for fragment in ('unknown keys', 'chain_version', 'master_seed', 'codec', 'concealment',
                         'packet_loss', 'noise.sha256', 'noise.label', 'noise.source_id',
                         'noise.licence_ref'):
            self.assertIn(fragment, str(caught.exception))
        for value in ([], 'x', {'sha256': 'a' * 64, 'label': 'babble'}):
            with self.assertRaises(ValueError):
                augment.validate_spec(spec(noise=value))
        with self.assertRaises(ValueError):
            augment.validate_spec([spec()])

    def test_rerun_is_byte_identical_and_parent_is_unchanged(self):
        before = tree_hashes(self.prepared)
        first, _, report_one = self.run_chain(spec(), 'one')
        second, _, report_two = self.run_chain(spec(), 'two')
        self.assertEqual(tree_hashes(first), tree_hashes(second))
        self.assertEqual(report_one, report_two)
        self.assertEqual(tree_hashes(self.prepared), before)
        other, _, report_three = self.run_chain(spec(master_seed=7), 'three')
        self.assertNotEqual(report_one['clips'][0]['sha256'], report_three['clips'][0]['sha256'])

    def test_no_loss_no_jitter_still_records_a_plain_codec_roundtrip(self):
        _, _, report = self.run_chain(spec(packet_loss=False, jitter=False, concealment='silence'))
        step = report['clips'][0]['chain']['steps'][0]
        self.assertEqual((step['packet_loss'], step['jitter'], step['packet_concealment']),
                         (None, None, None))

    def test_verify_detects_tampering_and_unlisted_files(self):
        out, _, report = self.run_chain(spec())
        clip_path = out / report['clips'][0]['path']
        payload = bytearray(clip_path.read_bytes())
        payload[-1] ^= 1
        clip_path.write_bytes(bytes(payload))
        (out / 'clips' / 'stray.wav').write_bytes(b'x')
        report['clips'][1]['chain']['steps'][0]['codec'] = 'g711_alaw'
        (out / 'report.json').write_text(json.dumps(report), encoding='utf-8')
        problems = augment.verify_augmented(self.prepared, out)
        self.assertEqual(len(problems), 3, problems)
        self.assertTrue(any('does not rebuild byte-for-byte' in p for p in problems), problems)
        self.assertTrue(any('stray.wav' in p for p in problems), problems)
        self.assertTrue(any("differs from the rebuild in ['chain']" in p for p in problems), problems)
        report['chain_spec']['master_seed'] = 1
        (out / 'report.json').write_text(json.dumps(report), encoding='utf-8')
        self.assertIn("report field 'chain_spec_sha256' differs from the rebuild",
                      augment.verify_augmented(self.prepared, out))
        report['chain_spec'] = {'codec': 'g711_ulaw'}
        (out / 'report.json').write_text(json.dumps(report), encoding='utf-8')
        self.assertTrue(augment.verify_augmented(self.prepared, out)[0].startswith('recorded chain_spec is invalid'))

    def test_verify_detects_label_speaker_and_count_tampering(self):
        out, _, report = self.run_chain(spec())
        report['clips'][0]['label'] = 'spoof'
        report['clips'][1]['speaker_id'] = 'spk_2'
        report['clips'][1]['counts_toward_clip_total'] = True
        (out / 'report.json').write_text(json.dumps(report), encoding='utf-8')
        problems = augment.verify_augmented(self.prepared, out)
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("['label']", problems[0])
        self.assertIn("['counts_toward_clip_total', 'speaker_id']", problems[1])

    def test_verify_detects_orphan_and_missing_rows_and_summary_edits(self):
        out, _, report = self.run_chain(spec())
        original = json.loads(json.dumps(report))
        report['clips'].append(dict(report['clips'][0], parent_clip_path='clips/nonexistent.wav'))
        (out / 'report.json').write_text(json.dumps(report), encoding='utf-8')
        self.assertIn('report lists 3 copies; the rebuild gives 2', augment.verify_augmented(self.prepared, out))
        del original['clips'][0]
        original['summary']['clips'] = 5
        (out / 'report.json').write_text(json.dumps(original), encoding='utf-8')
        problems = augment.verify_augmented(self.prepared, out)
        self.assertIn("report field 'summary' differs from the rebuild", problems)
        self.assertIn('report lists 1 copies; the rebuild gives 2', problems)

    def test_verify_detects_a_changed_parent_report(self):
        out, _, _ = self.run_chain(spec())
        parent = json.loads((self.prepared / 'report.json').read_text(encoding='utf-8'))
        parent['note'] = 'edited'
        (self.prepared / 'report.json').write_text(json.dumps(parent), encoding='utf-8')
        self.assertEqual(augment.verify_augmented(self.prepared, out),
                         ["report field 'parent_report_sha256' differs from the rebuild"])

    def test_noise_clipping_refusal_records_the_drawn_snr(self):
        # Sparse impulse noise: matched to the clip's power its spikes clip at low SNR only.
        impulses = np.zeros(2 * RATE, '<i2')
        impulses[::400] = 30000
        noise_pcm = impulses.tobytes()
        noise_path = self.base / 'impulses.wav'
        write_wav(noise_path, noise_pcm)
        noise = dict(self.noise_spec, sha256=hashlib.sha256(noise_pcm).hexdigest(), label='clicks')
        ids = [augment.derivation_id(row) for row in self.report['clips']]
        seed = next(s for s in range(5000) if derive_noise(s, ids[0])['snr_db'] < 2
                    and derive_noise(s, ids[1])['snr_db'] > 18)
        out, summary, report = self.run_chain(spec(master_seed=seed, noise=noise), noise=noise_path)
        self.assertEqual((summary['clips'], summary['noise_clipping_refusals']), (1, 1))
        error = report['errors'][0]
        self.assertEqual((error['parent_clip_path'], error['refusal'], error['snr_db']),
                         (self.report['clips'][0]['path'], 'noise_clipping',
                          derive_noise(seed, ids[0])['snr_db']))
        self.assertEqual(report['clips'][0]['parent_clip_path'], self.report['clips'][1]['path'])
        self.assertEqual(augment.verify_augmented(self.prepared, out, noise_path), [])

    def test_corrupt_parent_clip_is_listed_and_others_proceed(self):
        bad = self.prepared / self.report['clips'][1]['path']
        bad.write_bytes(bad.read_bytes()[:-2] + b'\x00\x01')
        out = self.base / 'aug'
        with contextlib.redirect_stdout(io.StringIO()):
            code = augment.main(['--prepared-root', str(self.prepared), '--spec',
                                 str(self.write_spec(spec())), '--output-root', str(out)])
        self.assertEqual(code, 1)
        report = json.loads((out / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual((len(report['clips']), report['errors']),
                         (1, [{'parent_clip_path': self.report['clips'][1]['path'],
                               'error': 'Parent clip hash mismatch'}]))
        self.assertEqual(augment.verify_augmented(self.prepared, out), [])

    def test_noise_clipping_and_silent_clip_are_refused_per_clip(self):
        loud = np.where(np.arange(4 * RATE) % 40 < 20, 32767, -32768).astype('<i2').tobytes()
        with self.assertRaisesRegex(ValueError, 'clipped'):
            augment.augment_clip(loud, 'clips/x.wav', spec(noise=self.noise_spec), self.noise_pcm)
        with self.assertRaisesRegex(ValueError, 'silence'):
            augment.augment_clip(bytes(8000), 'clips/x.wav', spec(noise=self.noise_spec), self.noise_pcm)
        with self.assertRaisesRegex(ValueError, 'hash'):
            augment.augment_clip(loud, 'clips/x.wav', spec(noise=self.noise_spec), self.noise_pcm + b'\0\0')
        with self.assertRaisesRegex(ValueError, 'exactly when'):
            augment.augment_clip(loud, 'clips/x.wav', spec(), self.noise_pcm)
        for clip in (b'', b'\x00', bytearray(4)):
            with self.assertRaises(ValueError):
                augment.augment_clip(clip, 'clips/x.wav', spec())

    def test_root_and_noise_refusals_create_nothing(self):
        (self.base / 'exists').mkdir()
        repo_out = Path(augment.__file__).resolve().parents[2] / 'never-created-aug-output'
        wrong_rate = self.base / 'noise8k.wav'
        with wave.open(str(wrong_rate), 'wb') as audio:
            audio.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            audio.writeframes(self.noise_pcm)
        cases = [
            (self.base / 'exists', spec(), None, 'new and disjoint'),
            (self.prepared / 'inside', spec(), None, 'new and disjoint'),
            (repo_out, spec(), None, 'outside the repository'),
            (self.base / 'n1', spec(), self.noise_path, 'names no noise'),
            (self.base / 'n2', spec(noise=self.noise_spec), None, 'give the noise WAV'),
            (self.base / 'n3', spec(noise=dict(self.noise_spec, sha256='a' * 64)), self.noise_path,
             'does not match'),
            (self.base / 'n4', spec(noise=self.noise_spec), wrong_rate, '16 kHz mono'),
        ]
        for out, chain, noise, message in cases:
            with self.subTest(message=message, out=out.name):
                with self.assertRaisesRegex(ValueError, message):
                    augment.augment_prepared(self.prepared, out, chain, noise)
                if out.name != 'exists':
                    self.assertFalse(out.exists())
        (self.prepared / 'INCOMPLETE').write_text('x')
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            augment.augment_prepared(self.prepared, self.base / 'n5', spec())
        (self.prepared / 'INCOMPLETE').unlink()

    def test_augmented_root_cannot_be_stacked(self):
        out, _, _ = self.run_chain(spec())
        with self.assertRaisesRegex(ValueError, 'not stacked'):
            augment.augment_prepared(out, self.base / 'again', spec())

    def test_cli_verify_pass_and_fail(self):
        spec_path = self.write_spec(spec())
        out, _, _ = self.run_chain(spec())
        args = ['--prepared-root', str(self.prepared), '--spec', str(spec_path), '--verify', str(out)]
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(augment.main(args), 0)
        self.assertEqual(stdout.getvalue().strip(), 'PASS')
        other = ['--prepared-root', str(self.prepared), '--spec',
                 str(self.write_spec(spec(master_seed=5))), '--verify', str(out)]
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(augment.main(other), 1)
        self.assertIn('differs from the expected specification', stderr.getvalue())
        self.write_spec(spec())
        (out / 'clips' / 'stray.wav').write_bytes(b'x')
        with contextlib.redirect_stdout(io.StringIO()) as stdout, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(augment.main(args), 1)
        self.assertEqual(stdout.getvalue().strip(), 'FAIL')
        self.assertIn('stray.wav', stderr.getvalue())

    def write_spec(self, chain):
        path = self.base / 'spec.json'
        path.write_text(json.dumps(chain), encoding='utf-8')
        return path


if __name__ == '__main__':
    unittest.main()
