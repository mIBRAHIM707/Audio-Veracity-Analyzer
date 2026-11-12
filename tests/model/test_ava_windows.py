"""Backlog 9.1 window loader from prepared/augmented roots (FR-M11, split and
training-eligibility rules).

Claude-authored test design, 12 November 2026. Fictional generated PCM written
through the real ``prepare_audio.process_manifest`` and ``augment.augment_prepared``
into temporary directories; infrastructure evidence only.
"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import wave

import numpy as np
import torch

from data.prepare import augment
from data.prepare.prepare_audio import process_manifest
from models.ava import train, windows
from models.ava.model import WINDOW_SAMPLES, build_detector
from tests.model.test_ava_sls import TINY

RATE = 16000


def write_wav(path, pcm):
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        audio.writeframes(pcm.tobytes())


def pcm_of(path):
    with wave.open(str(path), 'rb') as audio:
        return np.frombuffer(audio.readframes(audio.getnframes()), dtype='<i2')


class WindowLoaderTests(unittest.TestCase):
    SOURCES = (  # name, seconds, label, split, extra provenance
        ('a', 3.5, 'genuine', 'train', {'training_eligible': True}),
        ('b', 5.0, 'synthetic', 'train', {'training_eligible': True}),
        ('c', 3.5, 'synthetic', 'train', {'training_eligible': False, 'benchmark_only': True,
                                          'use_scope': 'benchmark'}),
        ('d', 3.5, 'genuine', 'dev', {'training_eligible': True}),
    )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        source = self.base / 'raw'
        source.mkdir()
        rows = []
        for seed, (name, seconds, label, split, extra) in enumerate(self.SOURCES):
            pcm = np.rint(np.random.default_rng(seed).normal(0, 6000, int(seconds * RATE)))
            write_wav(source / f'{name}.wav', pcm.clip(-32768, 32767).astype('<i2'))
            rows.append({'path': f'{name}.wav', 'speaker_id': f'spk_{name}', 'label': label, 'split': split,
                         'language': 'urd' if name in 'ab' else 'pnb', 'dialect': 'Majhi',
                         'sha256': hashlib.sha256((source / f'{name}.wav').read_bytes()).hexdigest(), **extra})
        manifest = self.base / 'manifest.json'
        manifest.write_text(json.dumps({'files': rows}))
        self.prepared = self.base / 'prepared'
        process_manifest(manifest, source, self.prepared, condition=True)
        self.report = json.loads((self.prepared / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(len(self.report['clips']), 4, 'fixture must give one clip per source')
        self.clips = {row['source_path'][0]: row for row in self.report['clips']}

    def rewrite_report(self, change):
        report = json.loads((self.prepared / 'report.json').read_text(encoding='utf-8'))
        change(report)
        (self.prepared / 'report.json').write_text(json.dumps(report), encoding='utf-8')

    def test_training_selection_uses_only_trainable_rows_of_the_split(self):
        loaded = windows.load_windows(self.prepared, 'train', 'train')
        by_path = {w['path']: w for w in loaded['windows']}
        self.assertEqual(set(by_path), {self.clips['a']['path'], self.clips['b']['path']})
        summary = loaded['summary']
        self.assertEqual(summary['excluded'], {'other_split': 1, 'not_trainable': 1})
        self.assertEqual(summary['by_label'], {'synthetic': 1, 'genuine': 1})
        self.assertEqual(summary['padded_by_label'], {'synthetic': 0, 'genuine': 1})
        self.assertEqual(summary['truncated_by_label'], {'synthetic': 1, 'genuine': 0})
        self.assertEqual(summary['mean_padded_samples_by_label']['synthetic'], 0)
        self.assertEqual(summary['source_errors_allowed'], 0)
        self.assertEqual(summary['report_sha256'],
                         hashlib.sha256((self.prepared / 'report.json').read_bytes()).hexdigest())
        short, long = by_path[self.clips['a']['path']], by_path[self.clips['b']['path']]
        self.assertEqual((short['target'], long['target']), (1, 0))
        self.assertEqual(short['codec'], 'none')
        self.assertEqual(short['provenance'], {'language': 'urd', 'dialect': 'Majhi'})
        for window in (short, long):
            self.assertEqual(len(window['samples']), WINDOW_SAMPLES)
        clip_a = pcm_of(self.prepared / short['path'])
        self.assertEqual(short['padded_samples'], WINDOW_SAMPLES - len(clip_a))
        self.assertTrue(np.array_equal(np.frombuffer(short['samples'], np.int16)[:len(clip_a)], clip_a))
        self.assertFalse(np.frombuffer(short['samples'], np.int16)[len(clip_a):].any())
        clip_b = pcm_of(self.prepared / long['path'])
        self.assertEqual((long['dropped_samples'], long['padded_samples']), (len(clip_b) - WINDOW_SAMPLES, 0))
        self.assertTrue(np.array_equal(np.frombuffer(long['samples'], np.int16), clip_b[:WINDOW_SAMPLES]))

    def test_evaluation_keeps_benchmark_rows_and_splits_stay_separate(self):
        evaluation = windows.load_windows(self.prepared, 'train', 'evaluate')
        self.assertEqual(evaluation['summary']['windows'], 3)
        self.assertEqual(evaluation['summary']['excluded'], {'other_split': 1, 'not_trainable': 0})
        dev = windows.load_windows(self.prepared, 'dev', 'evaluate')
        self.assertEqual([w['speaker_id'] for w in dev['windows']], ['spk_d'])
        with self.assertRaisesRegex(ValueError, "only reads split 'train'"):
            windows.load_windows(self.prepared, 'dev', 'train')

    def test_training_refuses_lineage_shared_with_another_split(self):
        self.rewrite_report(lambda r: [row.update(speaker_id='spk_a') for row in r['clips']
                                       if row['source_path'] == 'd.wav'])
        with self.assertRaisesRegex(ValueError, r'1 training lineage value.*speaker_id x1') as caught:
            windows.load_windows(self.prepared, 'train', 'train')
        self.assertNotIn('spk_a', str(caught.exception))
        self.assertEqual(windows.load_windows(self.prepared, 'train', 'evaluate')['summary']['windows'], 3)

    def test_unconditioned_roots_are_refused(self):
        plain = self.base / 'plain'
        process_manifest(self.base / 'manifest.json', self.base / 'raw', plain, condition=False)
        with self.assertRaisesRegex(ValueError, 'conditioned'):
            windows.load_windows(plain, 'train', 'evaluate')

    def test_quarantined_sources_need_an_explicit_opt_in(self):
        manifest = json.loads((self.base / 'manifest.json').read_text())
        manifest['files'].append(dict(manifest['files'][0], path='e.wav', speaker_id='spk_e', sha256='0' * 64))
        (self.base / 'raw' / 'e.wav').write_bytes((self.base / 'raw' / 'a.wav').read_bytes())
        (self.base / 'manifest2.json').write_text(json.dumps(manifest))
        partial = self.base / 'partial'
        process_manifest(self.base / 'manifest2.json', self.base / 'raw', partial, condition=True)
        with self.assertRaisesRegex(ValueError, '1 quarantined'):
            windows.load_windows(partial, 'train', 'train')
        loaded = windows.load_windows(partial, 'train', 'train', allow_source_errors=True)
        self.assertEqual((loaded['summary']['windows'], loaded['summary']['source_errors_allowed']), (2, 1))
        with self.assertRaises(ValueError):
            windows.load_windows(partial, 'train', 'train', allow_source_errors=1)
        # The parent's quarantine is not visible in the copies but still needs the opt-in.
        spec = {'chain_version': augment.CHAIN_VERSION, 'master_seed': 1, 'codec': 'g711_ulaw',
                'concealment': 'repeat_then_silence', 'packet_loss': False, 'jitter': False, 'noise': None}
        copies = self.base / 'partial_aug'
        augment.augment_prepared(partial, copies, spec)
        with self.assertRaisesRegex(ValueError, '1 quarantined'):
            windows.load_windows(copies, 'train', 'train', parent_root=partial)
        loaded = windows.load_windows(copies, 'train', 'train', parent_root=partial, allow_source_errors=True)
        self.assertEqual(loaded['summary']['source_errors_allowed'], 1)

    def test_missing_or_non_boolean_eligibility_is_not_trainable(self):
        self.rewrite_report(lambda r: [row.update(training_eligible='true') for row in r['clips']
                                       if row['source_path'] == 'a.wav'])
        loaded = windows.load_windows(self.prepared, 'train', 'train')
        self.assertEqual(loaded['summary']['excluded']['not_trainable'], 2)

    def test_batches_scale_pcm_and_shuffle_reproducibly(self):
        loaded = windows.load_windows(self.prepared, 'train', 'evaluate')['windows']
        ordered = list(windows.batches(loaded, 2))
        self.assertEqual([tuple(x.shape) for x, _ in ordered], [(2, WINDOW_SAMPLES), (1, WINDOW_SAMPLES)])
        self.assertEqual(ordered[0][0].dtype, torch.float32)
        expected = np.frombuffer(loaded[0]['samples'], np.int16).astype(np.float32) / 32768
        self.assertTrue(torch.equal(ordered[0][0][0], torch.from_numpy(expected)))
        self.assertEqual(ordered[0][1].tolist(), [loaded[0]['target'], loaded[1]['target']])
        rows = [torch.from_numpy(np.frombuffer(w['samples'], np.int16).astype(np.float32) / 32768)
                for w in loaded]

        def order(seed):
            out = []
            for x, y in windows.batches(loaded, 1, seed=seed):
                index = next(i for i, row in enumerate(rows) if torch.equal(row, x[0]))
                self.assertEqual(y.item(), loaded[index]['target'])
                out.append(index)
            return out

        self.assertEqual(order(None), [0, 1, 2])
        orders = {seed: order(seed) for seed in range(8)}
        self.assertEqual(order(3), orders[3])
        for seen in orders.values():
            self.assertEqual(sorted(seen), [0, 1, 2])
        self.assertGreater(len({tuple(o) for o in orders.values()}), 1)
        self.assertTrue(any(o != [0, 1, 2] for o in orders.values()))
        for bad in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                list(windows.batches(loaded, bad))

    def test_loaded_windows_drive_a_training_step(self):
        loaded = windows.load_windows(self.prepared, 'train', 'train')['windows']
        torch.manual_seed(0)
        model = build_detector(2, TINY)
        config = train.TrainConfig(freeze_feature_encoder_epochs=0)
        result = train.train_epoch(model, train.build_optimizer(model, config),
                                   windows.batches(loaded, 2, seed=1), config, 0)
        self.assertEqual(result['steps'], 1)
        self.assertTrue(np.isfinite(result['mean_loss']))

    def test_augmented_copy_roots_load_with_inherited_provenance(self):
        spec = {'chain_version': augment.CHAIN_VERSION, 'master_seed': 7, 'codec': 'g711_alaw',
                'concealment': 'repeat_then_silence', 'packet_loss': True, 'jitter': False, 'noise': None}
        augmented = self.base / 'augmented'
        augment.augment_prepared(self.prepared, augmented, spec)
        loaded = windows.load_windows(augmented, 'train', 'train', parent_root=self.prepared)
        self.assertEqual(loaded['summary']['windows'], 2)
        self.assertEqual(loaded['summary']['augmented_copies'], 2)
        self.assertEqual(loaded['summary']['excluded'], {'other_split': 1, 'not_trainable': 1})
        self.assertEqual(loaded['summary']['chain_spec_sha256'], augment.spec_sha256(spec))
        self.assertEqual({w['codec'] for w in loaded['windows']}, {'g711_alaw'})
        self.assertEqual({w['provenance']['language'] for w in loaded['windows']}, {'urd'})
        self.assertEqual(loaded['summary']['parent_report_sha256'],
                         hashlib.sha256((self.prepared / 'report.json').read_bytes()).hexdigest())
        with self.assertRaisesRegex(ValueError, 'parent_root'):
            windows.load_windows(augmented, 'train', 'train')
        with self.assertRaisesRegex(ValueError, 'only to augmented'):
            windows.load_windows(self.prepared, 'train', 'train', parent_root=self.prepared)
        # A re-hashed tampered copy passes the loader's own hash check but not verification.
        report = json.loads((augmented / 'report.json').read_text(encoding='utf-8'))
        target = augmented / report['clips'][0]['path']
        target.write_bytes(target.read_bytes()[:-2] + b'\x00\x00')
        report['clips'][0]['sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
        (augmented / 'report.json').write_text(json.dumps(report), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'failed verification'):
            windows.load_windows(augmented, 'train', 'train', parent_root=self.prepared)

    def test_altered_or_malformed_selected_clips_refuse_the_whole_load(self):
        path = self.prepared / self.clips['a']['path']
        original = path.read_bytes()
        path.write_bytes(original[:-2] + b'\x01\x00')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            windows.load_windows(self.prepared, 'train', 'train')
        path.write_bytes(original)
        # An unselected (dev) clip being altered does not block a train load.
        (self.prepared / self.clips['d']['path']).write_bytes(b'junk')
        self.assertEqual(windows.load_windows(self.prepared, 'train', 'train')['summary']['windows'], 2)
        copy = self.prepared / 'clips' / 'copy_of_a.wav'
        copy.write_bytes(original)
        cases = [
            ('label', lambda row: row.update(label='bonafide'), 'clip 1: label'),
            ('traversal', lambda row: row.update(path='../raw/a.wav'), 'clip 1: unsafe'),
            ('backslash', lambda row: row.update(path='clips\\x.wav'), 'clip 1: unsafe'),
            ('absolute', lambda row: row.update(path='/clips/x.wav'), 'clip 1: unsafe'),
            ('missing', lambda row: row.update(path='clips/absent.wav'), r'clip 1: \[(WinError|Errno)'),
            ('duplicate path', lambda row: row.update(path=self.clips['a']['path']), 'clip 1: duplicate clip path'),
            ('duplicate audio', lambda row: row.update(path='clips/copy_of_a.wav', sha256=self.clips['a']['sha256']),
             'clip 1: duplicate clip audio'),
        ]
        for name, change, message in cases:
            with self.subTest(name):
                saved = (self.prepared / 'report.json').read_text(encoding='utf-8')
                self.rewrite_report(lambda r: [change(row) for row in r['clips'] if row['source_path'] == 'b.wav'])
                with self.assertRaisesRegex(ValueError, message):
                    windows.load_windows(self.prepared, 'train', 'train')
                (self.prepared / 'report.json').write_text(saved, encoding='utf-8')

    def test_short_clip_and_wrong_format_are_refused(self):
        row = self.clips['a']
        path = self.prepared / row['path']
        for seconds, rate, message in ((2, RATE, 'expected a 3-6 s'), (7, RATE, 'file exceeds size limit'),
                                       (4, 8000, 'expected 16 kHz')):
            with self.subTest(seconds=seconds, rate=rate):
                with wave.open(str(path), 'wb') as audio:
                    audio.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
                    audio.writeframes(np.ones(seconds * rate, '<i2').tobytes())
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                self.rewrite_report(lambda r: [c.update(sha256=digest) for c in r['clips']
                                               if c['path'] == row['path']])
                with self.assertRaisesRegex(ValueError, 'clip 0: ' + message):
                    windows.load_windows(self.prepared, 'train', 'train')

    def test_root_and_argument_refusals(self):
        with self.assertRaisesRegex(ValueError, "No evaluate windows for split 'test'"):
            windows.load_windows(self.prepared, 'test', 'evaluate')
        with self.assertRaisesRegex(ValueError, 'max_windows'):
            windows.load_windows(self.prepared, 'train', 'train', max_windows=1)
        for args in (('train', 'fit'), ('', 'train'), (None, 'train')):
            with self.assertRaises(ValueError):
                windows.load_windows(self.prepared, *args)
        with self.assertRaises(ValueError):
            windows.load_windows(self.prepared, 'train', 'train', max_windows=0)
        self.rewrite_report(lambda r: r['policy'].update(sample_rate=8000))
        with self.assertRaisesRegex(ValueError, '16 kHz'):
            windows.load_windows(self.prepared, 'train', 'train')
        (self.prepared / 'report.json').write_text('[' * 100_000 + ']' * 100_000)
        with self.assertRaisesRegex(ValueError, 'too deep'):
            windows.load_windows(self.prepared, 'train', 'train')
        (self.prepared / 'report.json').write_text('{"policy": {"sample_rate": NaN}, "clips": []}')
        with self.assertRaisesRegex(ValueError, 'Non-finite'):
            windows.load_windows(self.prepared, 'train', 'train')
        (self.prepared / 'INCOMPLETE').write_text('x')
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            windows.load_windows(self.prepared, 'train', 'train')
        with self.assertRaisesRegex(ValueError, 'missing'):
            windows.load_windows(self.base / 'absent', 'train', 'train')


if __name__ == '__main__':
    unittest.main()
