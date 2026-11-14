"""Real-checkpoint parity runner mechanics with an injected tiny backbone (backlog 10.1).

Claude-authored test cases, 14 November 2026. The loader is replaced by a seeded tiny
torchaudio wav2vec2 model, so these check the runner's flow, truncation wiring,
report and refusals; the real checkpoint run is recorded separately.
"""
import json
import tempfile
import unittest
from pathlib import Path

import torch
import torchaudio

from models.export import checkpoint_parity as cp
from tests.model.test_ava_sls import TINY


def _load(model_root):
    torch.manual_seed(3)
    return torchaudio.models.wav2vec2_model(**TINY).eval(), {'fake': True, 'root': str(model_root)}


class CheckpointParityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_report_and_files(self):
        out = self.dir / 'run'
        report = cp.run('unused-root', out, kept_layers=2, threads=1, load=_load, base=TINY, int8=True)
        self.assertTrue(report['parity']['passed'], report['parity'])
        self.assertEqual(report['kept_layers'], 2)
        self.assertEqual(report['provenance'], {'fake': True, 'root': 'unused-root'})
        self.assertEqual(report['windows']['count'], 100)
        self.assertEqual(report['parity']['windows'], 100)
        self.assertGreater(report['int8_dynamic']['export']['quantized_nodes'], 0)
        self.assertTrue(report['int8_dynamic']['drift']['finite'])
        self.assertIn('no accuracy', report['limitations'])
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         ['ava-l2-fp32.onnx', 'ava-l2-int8-dynamic.onnx', 'report.json'])
        written = json.loads((out / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(written['export']['sha256'], report['export']['sha256'])
        self.assertEqual(written['parity']['max_abs_logit_diff'], report['parity']['max_abs_logit_diff'])

    def test_truncation_uses_the_loaded_weights(self):
        # The exported graph must equal the loaded backbone's first layers, not a fresh
        # random one: two runs with different loader weights give different exports.
        first = cp.run('r', self.dir / 'a', kept_layers=2, threads=1, load=_load, base=TINY)

        def other(model_root):
            torch.manual_seed(4)
            return torchaudio.models.wav2vec2_model(**TINY).eval(), {}

        second = cp.run('r', self.dir / 'b', kept_layers=2, threads=1, load=other, base=TINY)
        self.assertNotEqual(first['export']['sha256'], second['export']['sha256'])
        again = cp.run('r', self.dir / 'c', kept_layers=2, threads=1, load=_load, base=TINY)
        self.assertEqual(first['export']['sha256'], again['export']['sha256'])  # reproducible

    def test_refusals(self):
        existing = self.dir / 'exists'
        existing.mkdir()
        with self.assertRaises(FileExistsError):
            cp.run('r', existing, kept_layers=2, threads=1, load=_load, base=TINY)
        with self.assertRaises(ValueError):
            cp.run('r', Path(cp.__file__).resolve().parent / 'out', kept_layers=2, load=_load, base=TINY)
        self.assertFalse((Path(cp.__file__).resolve().parent / 'out').exists())
        for kwargs in ({'threads': 0}, {'threads': True}, {'seed': -1}, {'windows': 99},
                       {'windows': 2001}, {'batch_size': 0}, {'batch_size': 65}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    cp.run('r', self.dir / 'x', kept_layers=2, load=_load, base=TINY, **kwargs)
        for layers in (0, 5, True):  # TINY has 4 layers
            with self.subTest(kept_layers=layers):
                with self.assertRaises(ValueError):
                    cp.run('r', self.dir / 'y', kept_layers=layers, threads=1, load=_load, base=TINY)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ['exists'])  # nothing created

    def test_failure_leaves_incomplete_marker(self):
        def broken(model_root):
            raise RuntimeError('checkpoint unreadable')

        out = self.dir / 'failed'
        with self.assertRaises(RuntimeError):
            cp.run('r', out, kept_layers=2, threads=1, load=broken, base=TINY)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ['INCOMPLETE'])

    def test_non_finite_parity_is_reported_not_lost(self):
        from unittest import mock
        from models.export import onnx_export as ox
        real_run = ox.open_session

        class NanSession:
            def __init__(self, session):
                self._session = session

            def get_inputs(self):
                return self._session.get_inputs()

            def get_outputs(self):
                return self._session.get_outputs()

            def run(self, names, feeds):
                logits, embedding = self._session.run(names, feeds)
                logits[:] = float('nan')
                return logits, embedding

        out = self.dir / 'nan'
        with mock.patch.object(ox, 'open_session', lambda *a, **k: NanSession(real_run(*a, **k))):
            report = cp.run('r', out, kept_layers=2, threads=1, load=_load, base=TINY)
        written = json.loads((out / 'report.json').read_text(encoding='utf-8'))
        self.assertFalse(written['parity']['passed'])
        self.assertFalse(written['parity']['finite'])
        self.assertIsNone(written['parity']['max_abs_logit_diff'])
        self.assertEqual(report['parity'], written['parity'])
        self.assertFalse((out / 'INCOMPLETE').exists())

    def test_int8_failure_keeps_fp32_evidence(self):
        from unittest import mock
        from models.export import onnx_export as ox

        def fails(*args, **kwargs):
            raise RuntimeError('quantizer unavailable')

        out = self.dir / 'int8fail'
        with mock.patch.object(ox, 'quantize_int8_dynamic', fails):
            report = cp.run('r', out, kept_layers=2, threads=1, load=_load, base=TINY, int8=True)
        self.assertTrue(report['parity']['passed'])
        self.assertEqual(report['int8_dynamic'], {'error': 'RuntimeError: quantizer unavailable'})
        self.assertTrue((out / 'report.json').exists())
        self.assertFalse((out / 'INCOMPLETE').exists())

    def test_static_int8_option(self):
        out = self.dir / 'static'
        report = cp.run('r', out, kept_layers=2, threads=1, load=_load, base=TINY, int8_static=True,
                        calibration_windows=10)
        static = report['int8_static']
        self.assertGreater(static['export']['quantized_nodes'], 0)
        self.assertEqual(static['export']['calibration_windows'], 10)
        self.assertEqual(static['calibration_seed'], report['windows']['seed'] + 1)  # not the parity seed
        self.assertTrue(static['drift']['finite'])
        self.assertNotIn('int8_dynamic', report)
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         ['ava-l2-fp32.onnx', 'ava-l2-int8-static.onnx', 'report.json'])
        for count in (9, True):
            with self.subTest(calibration_windows=count):
                with self.assertRaises(ValueError):
                    cp.run('r', self.dir / f'bad{count}', kept_layers=2, threads=1, load=_load, base=TINY,
                           int8_static=True, calibration_windows=count)
                self.assertFalse((self.dir / f'bad{count}').exists())

    def test_static_failure_keeps_fp32_evidence_and_exit_code(self):
        from unittest import mock
        from models.export import onnx_export as ox

        def fails(*args, **kwargs):
            raise RuntimeError('calibration failed')

        out = self.dir / 'staticfail'
        with mock.patch.object(ox, 'quantize_int8_static', fails):
            report = cp.run('r', out, kept_layers=2, threads=1, load=_load, base=TINY, int8_static=True,
                            calibration_windows=10)
        self.assertTrue(report['parity']['passed'])
        self.assertEqual(report['int8_static'], {'error': 'RuntimeError: calibration failed'})
        self.assertFalse((out / 'INCOMPLETE').exists())

    def test_main_exit_code_two_on_int8_failure(self):
        from unittest import mock
        real_run = cp.run

        def run_tiny(*args, **kwargs):
            kwargs.update(load=_load, base=TINY, threads=1)
            return real_run(*args, **kwargs)

        def fails(*args, **kwargs):
            raise RuntimeError('calibration failed')

        from models.export import onnx_export as ox
        with mock.patch.object(cp, 'run', run_tiny), mock.patch.object(ox, 'quantize_int8_static', fails):
            code = cp.main(['--model-root', 'r', '--output-dir', str(self.dir / 'm'), '--kept-layers', '2',
                            '--int8-static', '--calibration-windows', '10'])
        self.assertEqual(code, 2)

    def test_thread_setting_restored(self):
        before = torch.get_num_threads()
        cp.run('r', self.dir / 't', kept_layers=1, threads=1 if before != 1 else 2, load=_load, base=TINY)
        self.assertEqual(torch.get_num_threads(), before)

    def test_noise_windows(self):
        a, b = cp.noise_windows(4, 1), cp.noise_windows(4, 1)
        self.assertEqual(a.dtype.name, 'float32')
        self.assertEqual(a.shape, (4, 64000))
        self.assertTrue((a == b).all())
        self.assertFalse((a == cp.noise_windows(4, 2)).all())


if __name__ == '__main__':
    unittest.main()
