"""Claude-authored tests (14 November 2026): per-clip codec mixing in
data/prepare/augment.py (backlog 3.3 "per-clip codec mix across copies"; PRD FR-D6 every
clip through G.711/GSM/AMR-NB with the chain recorded per clip, FR-D13 lineage).

Authored by Claude (Claude Code), not GPT-6 Astra. A fake codec tool stands in for
ffmpeg so the cases run in CI; fictional generated PCM in temporary directories.
Plumbing evidence only, no audio-quality or model claim.
"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from data.prepare import augment
from data.prepare.prepare_audio import process_manifest
from tests.unit.test_augment_chain import RATE, speechlike, spec, tree_hashes, write_wav
from tests.unit.test_augment_ffmpeg_chain import FakeTool


def channel(codec='g711_ulaw', concealment='repeat_then_silence', packet_loss=True, jitter=True, weight=1):
    return {'codec': codec, 'concealment': concealment, 'packet_loss': packet_loss, 'jitter': jitter,
            'weight': weight}


GSM = channel('gsm', None, False, False)
AMR = channel('amr_nb_7.4', None, True, False)


def mix(*channels, **overrides):
    value = {'chain_version': augment.CHAIN_VERSION, 'master_seed': 2026,
             'codec_mix': list(channels) or [channel(), GSM], 'noise': None}
    value.update(overrides)
    return value


class SpecTests(unittest.TestCase):
    def test_valid_mixes_and_single_specs_unchanged(self):
        for value in (mix(), mix(channel(), GSM, AMR), mix(channel(weight=3), channel('g711_alaw', weight=1000)),
                      mix(channel(), channel(packet_loss=False))):  # same codec, other impairment
            self.assertIs(augment.validate_spec(value), value)
        self.assertTrue(augment.needs_ffmpeg(mix()))
        self.assertFalse(augment.needs_ffmpeg(mix(channel(), channel('g711_alaw'))))
        self.assertEqual(augment.describe_codec(mix()), 'mix')
        # A single spec still validates, and its channel is itself with no choice record.
        self.assertEqual(augment.derive_channel(spec(), 'id'), (spec(), None))

    def test_invalid_mixes_name_each_problem(self):
        cases = [
            (mix(channel()), 'list of 2 to 8'),
            (mix(*[channel(weight=w) for w in range(1, 10)]), 'list of 2 to 8'),
            (mix(codec_mix='g711_ulaw'), 'list of 2 to 8'),
            (mix(channel(), channel()), 'repeats an earlier channel'),
            (mix(channel(), channel(weight=0)), r'codec_mix\[1\].weight'),
            (mix(channel(), channel(weight=True)), r'codec_mix\[1\].weight'),
            (mix(channel(), channel(weight=1001)), r'codec_mix\[1\].weight'),
            (mix(channel(), channel(weight=1.0)), r'codec_mix\[1\].weight'),
            (mix(channel(), {**GSM, 'concealment': 'repeat'}), r'codec_mix\[1\]: concealment'),
            (mix(channel(), channel('g729')), r'codec_mix\[1\]: codec must be one of'),
            (mix(channel(), {k: v for k, v in GSM.items() if k != 'weight'}), r'codec_mix\[1\] must be an object'),
            (mix(channel(), {**GSM, 'extra': 1}), r'codec_mix\[1\] must be an object'),
            (mix(channel(), 'gsm'), r'codec_mix\[1\] must be an object'),
            (mix(codec='gsm'), 'unknown keys'),  # channel keys belong in the channels
            (mix(master_seed=-1), 'master_seed'),
            (mix(chain_version='v0'), 'chain_version'),
            (mix(noise={'sha256': 'x'}), 'noise must be'),
            ({k: v for k, v in mix().items() if k != 'noise'}, 'missing keys'),
        ]
        for value, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                augment.validate_spec(value)
        # Malformed channel values are listed problems (all of them, in one message), not crashes.
        with self.assertRaises(ValueError) as caught:
            augment.validate_spec(mix(channel(codec=[]), {**GSM, 'concealment': {}},
                                      channel(packet_loss='true'), channel(jitter=None), noise=[]))
        message = str(caught.exception)
        for part in (r'codec_mix[0]: codec must be one of', 'codec_mix[1]: concealment',
                     'codec_mix[2]: packet_loss', 'codec_mix[3]: jitter', 'noise must be'):
            self.assertIn(part, message)
        # Shared-key problems are reported once, not once per channel.
        with self.assertRaises(ValueError) as caught:
            augment.validate_spec(mix(channel(), GSM, AMR, master_seed='x'))
        self.assertEqual(str(caught.exception).count('master_seed'), 1)


class DrawTests(unittest.TestCase):
    def test_choice_is_deterministic_seeded_and_recorded(self):
        value = mix(channel(), GSM, AMR)
        single, choice = augment.derive_channel(value, 'abc:0:16000')
        self.assertEqual(augment.derive_channel(copy.deepcopy(value), 'abc:0:16000'), (single, choice))
        index = choice['channel_index']
        picked = value['codec_mix'][index]
        self.assertEqual(single, {'chain_version': value['chain_version'], 'master_seed': 2026, 'noise': None,
                                  **{k: picked[k] for k in augment.CHANNEL_KEYS}})
        augment.validate_spec(single)
        self.assertEqual(choice['codec'], picked['codec'])
        self.assertEqual(choice['channel_spec_sha256'], augment.spec_sha256(single))
        self.assertEqual((choice['total_weight'], choice['derivation']), (3, augment.MIX_DERIVATION))
        # Independent recomputation of the draw rule.
        digest = hashlib.sha256(f'{augment.MIX_DERIVATION}\0{2026}\0abc:0:16000'.encode()).digest()
        draw = int.from_bytes(digest[:8], 'big') % 3
        self.assertEqual((choice['draw'], index), (draw, draw))
        for bad in ('', None, 5):
            with self.assertRaises(ValueError):
                augment.derive_channel(value, bad)

    def test_weights_shape_the_choice_and_seeds_change_it(self):
        value = mix(channel(weight=1), {**GSM, 'weight': 3})
        ids = [f'{i:064x}:0:16000' for i in range(4000)]
        picks = [augment.derive_channel(value, i)[1]['channel_index'] for i in ids]
        share = picks.count(1) / len(picks)
        self.assertGreater(share, 0.71)  # expected 0.75; 4000 draws give sd about 0.007
        self.assertLess(share, 0.79)
        other = [augment.derive_channel(mix(channel(weight=1), {**GSM, 'weight': 3}, master_seed=7), i)[1]
                 ['channel_index'] for i in ids]
        self.assertNotEqual(picks, other)
        # Boundary: every draw value maps to the channel whose cumulative range holds it.
        uneven = mix(channel(weight=2), {**GSM, 'weight': 1}, {**AMR, 'weight': 4})
        for clip_id in ids[:300]:
            choice = augment.derive_channel(uneven, clip_id)[1]
            want = 0 if choice['draw'] < 2 else 1 if choice['draw'] < 3 else 2
            self.assertEqual(choice['channel_index'], want)

    def test_mix_copy_equals_the_chosen_single_chain_except_lineage(self):
        clip = speechlike(1.0, 9).tobytes()
        for clip_id in ('a' * 64 + ':0:16000', 'b' * 64 + ':0:16000', 'c' * 64 + ':5:16005'):
            single, choice = augment.derive_channel(mix(), clip_id)
            out, record = augment.augment_clip(clip, clip_id, mix(), ffmpeg=FakeTool())
            alone, alone_record = augment.augment_clip(clip, clip_id, single, ffmpeg=FakeTool())
            self.assertEqual(out, alone)
            self.assertEqual(record, {**alone_record, 'chain_spec_sha256': augment.spec_sha256(mix()),
                                      'codec_choice': choice})
        with self.assertRaisesRegex(ValueError, 'codec mix needs an ffmpeg'):
            augment.augment_clip(clip, 'id', mix())

    def test_single_spec_output_is_pinned_to_the_pre_mix_code(self):
        # Pins computed 14 Nov 2026 from the committed augment.py before codec_mix
        # (HEAD 3272696), which gave byte-identical output and records for both chains:
        # previously written single-codec roots must keep verifying.
        clip, clip_id = speechlike(1.0, 9).tobytes(), 'a' * 64 + ':0:16000'
        gsm = spec(codec='gsm', concealment='repeat_then_silence')  # with loss and jitter
        for chain, tool, spec_pin, out_pin in (
                (spec(), None, '38ace4515827ffc1a0947d1c006b1a2a3b0e3a3590dac120ab9992a8bbdaad2c',
                 'a4c161d80cd37f04886248500f11860db4fc15093a7422f13b703908196aba3a'),
                (gsm, FakeTool(), 'c088c6b9c92800844d5f0966acd771e70991171eeafaf52610b407f368c896bc',
                 '64f29119cfe3c591466132ad403831bfc6a8c6662b64ba988c71cf1d51ddbac5')):
            out, record = augment.augment_clip(clip, clip_id, chain, ffmpeg=tool)
            self.assertEqual(augment.spec_sha256(chain), spec_pin)
            self.assertEqual(hashlib.sha256(out).hexdigest(), out_pin)
            self.assertEqual(set(record), {'chain_version', 'chain_spec_sha256', 'derivation_clip_id',
                                           'input_pcm_sha256', 'input_sample_count', 'output_pcm_sha256',
                                           'output_sample_count', 'post_chain_normalization', 'steps'})
            self.assertEqual(record['chain_spec_sha256'], spec_pin)


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        source = self.base / 'raw'
        source.mkdir()
        rows = []
        for index in range(4):
            audio = np.concatenate([speechlike(3.5, 10 + index), np.zeros(int(0.3 * RATE), '<i2'),
                                    speechlike(4.0, 20 + index)]).tobytes()
            write_wav(source / f'f{index}.wav', audio)
            rows.append({'path': f'f{index}.wav', 'speaker_id': f'spk_{index}', 'label': 'bonafide',
                         'sha256': hashlib.sha256((source / f'f{index}.wav').read_bytes()).hexdigest()})
        manifest = self.base / 'manifest.json'
        manifest.write_text(json.dumps({'files': rows}))
        self.prepared = self.base / 'prepared'
        process_manifest(manifest, source, self.prepared, condition=True)
        parents = json.loads((self.prepared / 'report.json').read_text(encoding='utf-8'))['clips']
        self.assertEqual(len(parents), 8)
        # Pick a seed whose draws use both channels on these clips (deterministic search).
        ids = [augment.derivation_id(row) for row in parents]
        for seed in range(50):
            codecs = {augment.derive_channel(mix(master_seed=seed), i)[1]['codec'] for i in ids}
            if codecs == {'g711_ulaw', 'gsm'}:
                break
        self.spec = mix(master_seed=seed)
        self.parents = parents

    def test_writer_records_each_choice_and_verifier_passes(self):
        out = self.base / 'aug'
        before = tree_hashes(self.prepared)
        summary = augment.augment_prepared(self.prepared, out, self.spec, ffmpeg=FakeTool())
        self.assertEqual(tree_hashes(self.prepared), before)
        report = json.loads((out / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['chain_spec'], self.spec)
        self.assertEqual(report['chain_spec_sha256'], augment.spec_sha256(self.spec))
        seen = {'g711_ulaw': 0, 'gsm': 0}
        for parent, row in zip(self.parents, report['clips']):
            single, choice = augment.derive_channel(self.spec, augment.derivation_id(parent))
            self.assertEqual(row['chain']['codec_choice'], choice)
            self.assertEqual(row['chain']['chain_spec_sha256'], augment.spec_sha256(self.spec))
            self.assertEqual(row['chain']['steps'][-2]['codec'], single['codec'])
            self.assertTrue(row['path'].endswith(f'_{single["codec"]}.wav'))
            self.assertEqual(augment.row_codec(self.spec, row), single['codec'])
            self.assertEqual(row['parent_clip_path'], parent['path'])
            seen[single['codec']] += 1
            if single['codec'] == 'g711_ulaw':  # G.711 keeps its own loss/jitter plan records
                self.assertIsNotNone(row['chain']['steps'][-2]['packet_loss'])
            else:
                self.assertEqual(row['chain']['steps'][-2]['codec_implementation'], 'fake')
        self.assertTrue(all(seen.values()), seen)
        self.assertEqual(summary['codec_counts'], seen)
        self.assertEqual(summary['channel_counts'], [seen['g711_ulaw'], seen['gsm']])
        self.assertEqual((summary['clips'], summary['errors']), (8, 0))
        self.assertEqual(augment.recorded_tool_hash(report), FakeTool.SHA256)
        self.assertEqual(augment.verify_augmented(self.prepared, out, expected_spec=self.spec, ffmpeg=FakeTool()), [])
        self.assertEqual(augment.verify_augmented(self.prepared, out),
                         ['codec mix needs the ffmpeg codec tool (--ffmpeg) to rebuild'])
        other = augment.verify_augmented(self.prepared, out, expected_spec=mix(master_seed=self.spec['master_seed'] + 1),
                                         ffmpeg=FakeTool())
        self.assertIn('recorded chain_spec differs from the expected specification', other)

    def test_verifier_detects_a_tampered_choice_and_reweighted_spec(self):
        out = self.base / 'aug'
        augment.augment_prepared(self.prepared, out, self.spec, ffmpeg=FakeTool())
        path = out / 'report.json'
        original = path.read_text(encoding='utf-8')
        report = json.loads(original)
        report['clips'][0]['chain']['codec_choice']['codec'] = 'other'
        path.write_text(json.dumps(report), encoding='utf-8')
        problems = augment.verify_augmented(self.prepared, out, ffmpeg=FakeTool())
        self.assertTrue(any('copy row 0 differs' in p and "'chain'" in p for p in problems), problems)
        for row in (report['clips'][0], {'chain': None}, {'chain': []}, {'chain': {'codec_choice': 'gsm'}}, None):
            with self.assertRaisesRegex(ValueError, 'lacks a valid chain.codec_choice'):
                augment.row_codec(self.spec, row)
        with self.assertRaisesRegex(ValueError, 'must be an object'):
            augment.row_codec(None, report['clips'][0])
        # A reweighted spec in the report no longer rebuilds the recorded copies.
        report = json.loads(original)
        report['chain_spec']['codec_mix'][0]['weight'] = 1000
        path.write_text(json.dumps(report), encoding='utf-8')
        self.assertNotEqual(augment.verify_augmented(self.prepared, out, ffmpeg=FakeTool()), [])

    def test_g711_only_mix_needs_no_tool_and_single_spec_reports_have_no_mix_keys(self):
        g711 = mix(channel(), channel('g711_alaw', packet_loss=False, jitter=False), master_seed=3)
        out = self.base / 'aug-g711'
        summary = augment.augment_prepared(self.prepared, out, g711)
        self.assertEqual(sum(summary['codec_counts'].values()), 8)
        self.assertEqual(augment.verify_augmented(self.prepared, out), [])
        self.assertIsNone(augment.recorded_tool_hash(json.loads((out / 'report.json').read_text(encoding='utf-8'))))
        plain = self.base / 'aug-plain'
        summary = augment.augment_prepared(self.prepared, plain, spec())
        report = json.loads((plain / 'report.json').read_text(encoding='utf-8'))
        self.assertNotIn('codec_counts', summary)
        self.assertTrue(all('codec_choice' not in row['chain'] for row in report['clips']))

    def test_tool_failure_in_a_mix_aborts_before_writing(self):
        from data.prepare import ffmpeg_codecs
        out = self.base / 'aug-fail'
        with self.assertRaises(ffmpeg_codecs.CodecToolError):
            augment.augment_prepared(self.prepared, out, self.spec, ffmpeg=FakeTool(fail_on=1))
        self.assertFalse(out.exists())
        with self.assertRaisesRegex(ValueError, 'needs an ffmpeg'):
            augment.augment_prepared(self.prepared, self.base / 'aug-none', self.spec)


if __name__ == '__main__':
    unittest.main()
