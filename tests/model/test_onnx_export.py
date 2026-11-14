"""ONNX export and PyTorch parity on a tiny random AVA detector (backlog 10.1; FR-M7).

Claude-authored test cases, 14 November 2026. Infrastructure evidence only: a tiny
randomly initialized model, synthetic noise windows. It shows that the export keeps the
I/O contract and the numbers, not anything about the real checkpoint or accuracy.
Needs requirements-model.txt and requirements-vad.txt (onnxruntime).
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import torch

from models.ava.model import WINDOW_SAMPLES, build_detector
from models.export import onnx_export as ox
from tests.model.test_ava_sls import TINY


def _model(seed):
    torch.manual_seed(seed)
    return build_detector(2, TINY)


def _windows(count, seed=1):
    rng = np.random.default_rng(seed)
    gains = rng.uniform(0.01, 1.0, size=(count, 1))  # quiet to loud; layer norm inside the model
    return (rng.standard_normal((count, WINDOW_SAMPLES)) * gains).astype(np.float32)


class ExportParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._tmp.name)
        cls.model = _model(0)
        cls.model.train()
        cls.record = ox.export_onnx(cls.model, cls.dir / 'tiny.onnx')
        cls.session = ox.open_session(cls.record['path'], threads=1, expected_sha256=cls.record['sha256'])

    @classmethod
    def tearDownClass(cls):
        del cls.session
        cls._tmp.cleanup()

    def test_window_constant_matches_the_model(self):
        self.assertEqual(ox.WINDOW_SAMPLES, WINDOW_SAMPLES)

    def test_record_and_files(self):
        path = Path(self.record['path'])
        self.assertTrue(path.is_file())
        self.assertEqual(self.record['bytes'], path.stat().st_size)
        self.assertEqual(self.record['sha256'], ox._sha256(path))
        self.assertEqual(self.record['opset'], 17)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ['tiny.onnx'])  # no partial left
        self.assertTrue(self.model.training)  # mode restored

    def test_io_contract(self):
        self.assertEqual(ox.check_session(self.session), [])
        self.assertEqual(self.session.get_providers(), ['CPUExecutionProvider'])

    def test_parity_on_100_windows_with_other_batch_sizes(self):
        windows = _windows(100)
        for batch_size in (1, 7, 16):  # export example had batch 2; 7 leaves a remainder of 2
            with self.subTest(batch_size=batch_size):
                result = ox.parity(self.model, self.session, windows, batch_size=batch_size)
                self.assertTrue(result['passed'], result)
                self.assertEqual(result['windows'], 100)
                self.assertLess(result['max_abs_logit_diff'], 1e-3)
                self.assertTrue(np.isfinite(result['max_abs_embedding_diff']))
                self.assertEqual(result['class_disagreements'], 0)
        self.assertTrue(self.model.training)

    def test_parity_detects_a_different_model(self):
        other = _model(1)
        result = ox.parity(other, self.session, _windows(100))
        self.assertFalse(result['passed'])
        self.assertGreater(result['max_abs_logit_diff'], 1e-3)

    def test_tolerance_is_strict(self):
        result = ox.parity(self.model, self.session, _windows(100))
        diff = result['max_abs_logit_diff']
        if diff == 0:
            self.skipTest('graph is bit-exact here; the strict boundary cannot be shown')
        at = ox.parity(self.model, self.session, _windows(100), tolerance=diff)
        self.assertFalse(at['passed'])
        above = ox.parity(self.model, self.session, _windows(100), tolerance=diff + 1e-9)
        self.assertTrue(above['passed'])

    def test_parity_input_refusals(self):
        bad = {'99 windows': _windows(99), 'float64': _windows(100).astype(np.float64),
               'short': _windows(100)[:, :32000], 'rank 1': _windows(1)[0]}
        nan = _windows(100)
        nan[5, 10] = np.nan
        bad['nan'] = nan
        for label, windows in bad.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    ox.parity(self.model, self.session, windows)
        for kwargs in ({'batch_size': 0}, {'batch_size': True}, {'batch_size': 65}, {'tolerance': 0},
                       {'tolerance': float('nan')}, {'min_windows': 0}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    ox.parity(self.model, self.session, _windows(100), **kwargs)
        with mock.patch.object(ox, 'PARITY_MAX_WINDOWS', 100):
            with self.assertRaises(ValueError):
                ox.parity(self.model, self.session, _windows(101))

    def test_parity_reports_contract_violation(self):
        class Renamed:
            def __init__(self, session):
                self._session = session

            def get_inputs(self):
                spec = self._session.get_inputs()[0]
                return [type('Spec', (), {'name': 'audio', 'shape': spec.shape, 'type': spec.type})()]

            def get_outputs(self):
                return self._session.get_outputs()

        result = ox.parity(self.model, Renamed(self.session), _windows(100))
        self.assertFalse(result['passed'])
        self.assertTrue(result['problems'])

    def test_export_refusals_leave_nothing(self):
        repo = Path(ox.__file__).resolve().parents[2]
        cases = {'inside repo': repo / 'models' / 'x.onnx', 'existing': Path(self.record['path']),
                 'suffix': self.dir / 'x.bin'}
        for label, path in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    ox.export_onnx(self.model, path)
        self.assertFalse((repo / 'models' / 'x.onnx').exists())
        for opset in (16, 18, True, 17.0):
            with self.subTest(opset=opset):
                with self.assertRaises(ValueError):
                    ox.export_onnx(self.model, self.dir / 'opset.onnx', opset=opset)
        self.assertFalse((self.dir / 'opset.onnx').exists())

    def test_failed_export_leaves_no_file(self):
        class Broken(torch.nn.Module):
            def forward(self, waveform):
                raise RuntimeError('broken model')

        target = self.dir / 'broken.onnx'
        with self.assertRaises(RuntimeError):
            ox.export_onnx(Broken(), target)
        self.assertFalse(target.exists())
        self.assertFalse(target.with_name('broken.onnx.partial').exists())

    def test_partial_written_then_failure_is_removed(self):
        target = self.dir / 'late.onnx'

        def writes_then_fails(model, args, f, **kwargs):
            Path(f).write_bytes(b'half a model')
            raise RuntimeError('export failed after writing')

        with mock.patch.object(torch.onnx, 'export', writes_then_fails):
            with self.assertRaises(RuntimeError):
                ox.export_onnx(self.model, target)
        self.assertFalse(target.exists())
        self.assertFalse(target.with_name('late.onnx.partial').exists())

    def test_target_created_during_export_is_not_replaced(self):
        target = self.dir / 'race.onnx'
        real_export = torch.onnx.export

        def export_then_race(model, args, f, **kwargs):
            real_export(model, args, f, **kwargs)
            target.write_bytes(b'someone else')

        with mock.patch.object(torch.onnx, 'export', export_then_race):
            with self.assertRaises(ValueError):
                ox.export_onnx(self.model, target)
        self.assertEqual(target.read_bytes(), b'someone else')
        self.assertFalse(target.with_name('race.onnx.partial').exists())

    def test_written_opset_is_read_back(self):
        target = self.dir / 'opset18.onnx'
        real_export = torch.onnx.export

        def exports_other_opset(model, args, f, **kwargs):
            real_export(model, args, f, **dict(kwargs, opset_version=18))

        with mock.patch.object(torch.onnx, 'export', exports_other_opset):
            with self.assertRaises(ValueError):
                ox.export_onnx(self.model, target)
        self.assertFalse(target.exists())
        self.assertFalse(target.with_name('opset18.onnx.partial').exists())
        self.assertEqual(self.record['opset'], 17)  # read back from the real export

    def test_open_session_refusals(self):
        with self.assertRaises(ValueError):
            ox.open_session(self.record['path'], expected_sha256='0' * 64)
        with self.assertRaises(ValueError):
            ox.open_session(self.dir / 'missing.onnx')
        for threads in (0, True, 1.0):
            with self.subTest(threads=threads):
                with self.assertRaises(ValueError):
                    ox.open_session(self.record['path'], threads=threads)


if __name__ == '__main__':
    unittest.main()
