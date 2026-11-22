"""Claude-authored tests (15 November 2026) for models/export/model_card.py (backlog
10.2; PRD FR-M12, FR-E5, FR-O4, FR-I4; architecture §2.3). Authored by Claude (Claude
Code), not by GPT-6 Astra. A small stand-in file plays the ONNX model (the card only
hashes it); no model is loaded.
"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from models.export import model_card as mc


def fields(**overrides):
    base = dict(version='ava-0.0-test', backbone='AntiDeepfake MMS-300M (truncated)', kept_layers=8,
                metrics={'dev_eer': mc.NOT_MEASURED, 'parity_max_logit_diff': 4.95e-6},
                training_data={'status': 'not trained on AVA data; untrained head'},
                failure_modes=['untrained head: scores carry no meaning'],
                licence_restriction='Research and non-commercial use only (CC BY-NC derived weights).')
    base.update(overrides)
    return base


class ModelCardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.onnx = self.dir / 'ava-test.onnx'
        self.onnx.write_bytes(b'not really onnx' * 100)

    def test_build_write_check_and_markdown(self):
        card = mc.build_card(self.onnx, **fields())
        self.assertEqual(card['onnx'], {'file': 'ava-test.onnx', 'bytes': 1500,
                                        'sha256': hashlib.sha256(self.onnx.read_bytes()).hexdigest()})
        self.assertEqual((card['window_samples'], card['sample_rate'], card['window_s']), (64000, 16000, 4.0))
        self.assertEqual((card['calibration_status'], card['calibration_temperature'], card['ece']),
                         ('uncalibrated', None, 'not_measured'))
        self.assertEqual(card['limitations'], list(mc.LIMITATIONS))
        path = mc.write_card(card, self.dir / 'model_card.json')
        with self.assertRaises(FileExistsError):
            mc.write_card(card, path)  # a published card is never overwritten
        self.assertEqual(mc.check_card(path, self.onnx, {}), card)  # defaults: 4 s windows
        self.assertEqual(mc.check_card(path, self.onnx, {'audio': {'window_s': 4.0, 'hop_s': 2.0}}), card)
        text = mc.render_markdown(card)
        for needle in ('UNCALIBRATED', 'Research and non-commercial', 'not a voice biometric', 'dev_eer: not_measured'):
            self.assertIn(needle, text)
        calibrated = mc.build_card(self.onnx, **fields(), calibration_temperature=1.7, ece=0.031)
        self.assertEqual(calibrated['calibration_status'], 'calibrated')
        self.assertIn('temperature 1.7', mc.render_markdown(calibrated))

    def test_start_up_check_refuses_every_mismatch(self):
        card = mc.build_card(self.onnx, **fields())
        path = mc.write_card(card, self.dir / 'model_card.json')
        cases = []
        other = self.dir / 'other.onnx'
        other.write_bytes(self.onnx.read_bytes())
        cases.append(('file name', path, other, {}))  # same bytes, not the file the card names
        swapped = self.dir / 'swapped' / 'ava-test.onnx'
        swapped.parent.mkdir()
        swapped.write_bytes(b'x' * 1500)  # same name and size, other content
        cases.append(('hash', path, swapped, {}))
        grown = self.dir / 'grown' / 'ava-test.onnx'
        grown.parent.mkdir()
        grown.write_bytes(self.onnx.read_bytes() + b'!')
        cases.append(('size', path, grown, {}))
        cases.append(('missing model', path, self.dir / 'absent.onnx', {}))
        cases.append(('missing card', self.dir / 'absent.json', self.onnx, {}))
        cases.append(('config window', path, self.onnx, {'audio': {'window_s': 3.0, 'hop_s': 1.0}}))
        tampered_contract = json.loads(json.dumps(card))
        tampered_contract['contract']['input']['name'] = 'audio'
        for key, value in (('window_samples', 48000), ('limitations', ['AVA is perfect.']),
                           ('calibration_temperature', 2.0), ('licence_restriction', ''),
                           ('metrics', {'dev_eer': 'great'}), ('failure_modes', []), ('schema', 'v0'),
                           ('contract', tampered_contract['contract']),
                           ('onnx', {**card['onnx'], 'sha256': card['onnx']['sha256'].upper()})):
            changed = self.dir / f'card-{key}.json'
            changed.write_text(json.dumps({**card, key: value}), encoding='utf-8')
            cases.append((key, changed, self.onnx, {}))
        nan_card = self.dir / 'card-nan.json'
        nan_card.write_text(json.dumps({**card, 'ece': 'X'}).replace('"X"', 'NaN'), encoding='utf-8')
        cases.append(('NaN', nan_card, self.onnx, {}))
        duplicate = self.dir / 'card-duplicate.json'
        duplicate.write_text(json.dumps(card)[:-1] + ', "version": "other"}', encoding='utf-8')
        cases.append(('duplicate key', duplicate, self.onnx, {}))
        for label, card_path, onnx_path, config in cases:
            with self.assertRaises(mc.ModelCardError, msg=label):
                mc.check_card(card_path, onnx_path, config)

    def test_contract_is_the_exported_graph(self):
        # Review B1: the card's names are the export's, so a card cannot describe an
        # input the ONNX file does not have.
        from models.export import onnx_export
        self.assertEqual(mc.CONTRACT['input']['name'], onnx_export.INPUT_NAME)
        self.assertEqual([o['name'] for o in mc.CONTRACT['outputs']], list(onnx_export.OUTPUT_NAMES))
        self.assertEqual(mc.CONTRACT['input']['shape'][1], onnx_export.WINDOW_SAMPLES)
        self.assertEqual(mc.CONTRACT['outputs'][1]['shape'][1], 256)

    def test_build_refuses_invented_or_missing_values(self):
        for overrides in ({'metrics': {}}, {'metrics': {'eer': None}}, {'kept_layers': 0},
                          {'failure_modes': ['']}, {'licence_restriction': '  '}, {'training_data': {}},
                          {'training_data': {'n': float('nan')}}, {'training_data': {'s': {1, 2}}},
                          {'calibration_temperature': -1.0}, {'ece': float('nan')},
                          {'calibration_temperature': 1.5}):  # calibrated without a measured ECE
            with self.assertRaises(mc.ModelCardError, msg=overrides):
                mc.build_card(self.onnx, **fields(**overrides))
        card = mc.build_card(self.onnx, **fields())
        card['training_data'] = {'n': float('nan')}
        with self.assertRaises(mc.ModelCardError):
            mc.write_card(card, self.dir / 'refused.json')
        self.assertFalse((self.dir / 'refused.json').exists(), 'no partial card is left behind')
        with self.assertRaises(mc.ModelCardError):
            mc.build_card(self.dir / 'absent.onnx', **fields())
        self.assertTrue(mc.validate([]))


if __name__ == '__main__':
    unittest.main()
