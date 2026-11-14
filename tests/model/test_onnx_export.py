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


class QuantizeTests(unittest.TestCase):
    """INT8 dynamic quantization of the tiny export (Claude-authored, 14 Nov 2026)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._tmp.name)
        cls.fp32 = ox.export_onnx(_model(0), cls.dir / 'fp32.onnx')
        cls.int8 = ox.quantize_int8_dynamic(cls.fp32['path'], cls.dir / 'int8.onnx',
                                            source_sha256=cls.fp32['sha256'])
        cls.fp32_session = ox.open_session(cls.fp32['path'])
        cls.int8_session = ox.open_session(cls.int8['path'], expected_sha256=cls.int8['sha256'])

    @classmethod
    def tearDownClass(cls):
        del cls.fp32_session, cls.int8_session
        cls._tmp.cleanup()

    def test_record_graph_and_contract(self):
        import onnx
        graph = onnx.load(self.int8['path'])
        types = [node.op_type for node in graph.graph.node]
        counted = sum(1 for op in types if op in ox.QUANTIZED_OP_TYPES)
        self.assertGreater(counted, 0)
        self.assertEqual(self.int8['quantized_nodes'], counted)
        self.assertEqual(self.int8['activation_quantizers'], types.count('DynamicQuantizeLinear'))
        # Architecture §4.3 step 4: MatMul/Linear only; the conv front end stays FP32.
        self.assertNotIn('ConvInteger', types)
        self.assertIn('Conv', types)
        self.assertEqual(self.int8['op_types_to_quantize'], ['MatMul', 'Gemm'])
        self.assertEqual(self.int8['source_sha256'], self.fp32['sha256'])
        self.assertNotEqual(self.int8['sha256'], self.fp32['sha256'])
        self.assertEqual(self.int8['sha256'], ox._sha256(self.int8['path']))
        self.assertEqual(self.int8['opset'], ox._opset(graph))
        self.assertEqual(ox._sha256(self.fp32['path']), self.fp32['sha256'])  # source untouched
        self.assertEqual(ox.check_session(self.int8_session), [])
        # No partial and no quantizer side files (e.g. -inferred/-opt) left behind.
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ['fp32.onnx', 'int8.onnx'])

    def test_drift_is_measured_not_judged(self):
        same = ox.compare_sessions(self.fp32_session, self.fp32_session, _windows(20))
        self.assertEqual((same['max_abs_logit_diff'], same['class_disagreements']), (0.0, 0))
        drift = ox.compare_sessions(self.fp32_session, self.int8_session, _windows(20), batch_size=1)
        self.assertTrue(drift['finite'])
        self.assertEqual(drift['problems'], [])
        self.assertGreater(drift['max_abs_logit_diff'], 0.0)  # INT8 really changes the numbers
        self.assertLessEqual(drift['mean_abs_logit_diff'], drift['max_abs_logit_diff'])
        self.assertNotIn('passed', drift)

    def test_int8_output_depends_on_batch_partners(self):
        # DynamicQuantizeLinear takes one scale over the whole activation tensor, batch
        # included, so a window's INT8 score moves with the other windows in its batch
        # (measured 14 Nov with onnxruntime 1.32.0 on this seed-0 tiny model: about 1e-3
        # to 3e-3 logits; FP32 about 3e-8; MMS-300M may differ). With the cross-call
        # micro-batching of architecture §2.3 / ADR-3 one call's score would depend on
        # another's audio. Characterization test of a recorded design question.
        windows = _windows(8)
        quiet_partners = windows.copy()
        quiet_partners[1:] = 0
        for session, bound, check in ((self.fp32_session, 1e-5, self.assertLess),
                                      (self.int8_session, 1e-5, self.assertGreater)):
            alone = session.run(None, {'waveform': windows[:1]})[0]
            for batch in (windows, quiet_partners):
                with self.subTest(session=session is self.int8_session, quiet=batch is quiet_partners):
                    batched = session.run(None, {'waveform': batch})[0][:1]
                    check(float(np.abs(alone - batched).max()), bound)

    def test_compare_refusals_and_contract(self):
        class Renamed:
            def __init__(self, session):
                self._session = session

            def get_inputs(self):
                spec = self._session.get_inputs()[0]
                return [type('Spec', (), {'name': 'audio', 'shape': spec.shape, 'type': spec.type})()]

            def get_outputs(self):
                return self._session.get_outputs()

        result = ox.compare_sessions(self.fp32_session, Renamed(self.int8_session), _windows(2))
        self.assertTrue(result['problems'][0].startswith('candidate:'))
        self.assertNotIn('max_abs_logit_diff', result)
        for kwargs in ({'batch_size': 0}, {'batch_size': 65}, {'min_windows': 0}, {'min_windows': 3}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    ox.compare_sessions(self.fp32_session, self.int8_session, _windows(2), **kwargs)

    def test_quantize_refusals_leave_nothing(self):
        cases = {'wrong hash': (self.fp32['path'], self.dir / 'a.onnx', '0' * 64),
                 'no hash': (self.fp32['path'], self.dir / 'b.onnx', None),
                 'missing source': (self.dir / 'missing.onnx', self.dir / 'c.onnx', self.fp32['sha256']),
                 'existing target': (self.fp32['path'], self.int8['path'], self.fp32['sha256'])}
        before = sorted(p.name for p in self.dir.iterdir())
        for label, (source, target, digest) in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    ox.quantize_int8_dynamic(source, target, source_sha256=digest)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), before)

    def test_quantizer_failure_and_no_op_leave_nothing(self):
        import shutil
        from onnxruntime import quantization

        def writes_then_fails(source, target, **kwargs):
            Path(target).write_bytes(b'half')
            raise RuntimeError('quantizer failed')

        def copies_unchanged(source, target, **kwargs):
            shutil.copyfile(source, target)

        before = sorted(p.name for p in self.dir.iterdir())
        with mock.patch.object(quantization, 'quantize_dynamic', writes_then_fails):
            with self.assertRaises(RuntimeError):
                ox.quantize_int8_dynamic(self.fp32['path'], self.dir / 'fail.onnx',
                                         source_sha256=self.fp32['sha256'])
        with mock.patch.object(quantization, 'quantize_dynamic', copies_unchanged):
            with self.assertRaisesRegex(ValueError, 'no MatMul/Gemm'):
                ox.quantize_int8_dynamic(self.fp32['path'], self.dir / 'noop.onnx',
                                         source_sha256=self.fp32['sha256'])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), before)

    def test_contract_breaking_quantization_leaves_nothing(self):
        import onnx
        from onnxruntime import quantization
        real = quantization.quantize_dynamic

        def renames_input(source, target, **kwargs):
            real(source, target, **kwargs)
            model = onnx.load(target)
            for node in model.graph.node:
                node.input[:] = ['audio' if name == 'waveform' else name for name in node.input]
            model.graph.input[0].name = 'audio'
            onnx.save(model, target)

        before = sorted(p.name for p in self.dir.iterdir())
        with mock.patch.object(quantization, 'quantize_dynamic', renames_input):
            with self.assertRaisesRegex(ValueError, 'I/O contract'):
                ox.quantize_int8_dynamic(self.fp32['path'], self.dir / 'renamed.onnx',
                                         source_sha256=self.fp32['sha256'])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), before)


class StaticQuantizeTests(unittest.TestCase):
    """Static INT8 with MinMax calibration (Claude-authored, 14 Nov 2026)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._tmp.name)
        cls.fp32 = ox.export_onnx(_model(0), cls.dir / 'fp32.onnx')
        cls.calibration = _windows(12, seed=7)
        cls.static = ox.quantize_int8_static(cls.fp32['path'], cls.dir / 'static.onnx', cls.calibration,
                                             source_sha256=cls.fp32['sha256'])
        cls.session = ox.open_session(cls.static['path'], expected_sha256=cls.static['sha256'])

    @classmethod
    def tearDownClass(cls):
        del cls.session
        cls._tmp.cleanup()

    def test_record_graph_and_contract(self):
        import hashlib
        import onnx
        types = [node.op_type for node in onnx.load(self.static['path']).graph.node]
        self.assertIn('QLinearMatMul', types)
        self.assertEqual(self.static['quantized_nodes'], sum(1 for op in types if op in ox.QUANTIZED_OP_TYPES))
        for absent in ('DynamicQuantizeLinear', 'ConvInteger', 'QLinearConv'):
            self.assertNotIn(absent, types)
        self.assertIn('Conv', types)  # conv front end stays FP32
        self.assertEqual(self.static['calibration_windows'], 12)
        self.assertEqual(self.static['calibration_sha256'], hashlib.sha256(self.calibration.tobytes()).hexdigest())
        self.assertEqual(ox.check_session(self.session), [])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ['fp32.onnx', 'static.onnx'])

    def test_output_does_not_depend_on_batch_partners(self):
        # The contrast with the dynamic INT8 characterization test: fixed scales.
        windows = _windows(8)
        quiet = windows.copy()
        quiet[1:] = 0
        alone = self.session.run(None, {'waveform': windows[:1]})[0]
        for batch in (windows, quiet):
            batched = self.session.run(None, {'waveform': batch})[0][:1]
            self.assertLess(float(np.abs(alone - batched).max()), 1e-5)

    def test_quantized_and_finite(self):
        fp32 = ox.open_session(self.fp32['path'])
        drift = ox.compare_sessions(fp32, self.session, _windows(20))
        self.assertTrue(drift['finite'])
        self.assertGreater(drift['max_abs_logit_diff'], 0.0)

    def test_calibration_set_changes_the_model(self):
        other = ox.quantize_int8_static(self.fp32['path'], self.dir / 'other.onnx', _windows(12, seed=8),
                                        source_sha256=self.fp32['sha256'])
        try:
            self.assertNotEqual(other['calibration_sha256'], self.static['calibration_sha256'])
            self.assertNotEqual(other['sha256'], self.static['sha256'])
        finally:
            Path(other['path']).unlink()

    def test_refusals_leave_nothing(self):
        good = self.calibration
        nan = good.copy()
        nan[0, 0] = np.nan
        cases = {'9 windows': (good[:9], self.fp32['sha256']), 'float64': (good.astype(np.float64), self.fp32['sha256']),
                 'short': (good[:, :32000], self.fp32['sha256']), 'nan': (nan, self.fp32['sha256']),
                 'wrong hash': (good, '0' * 64)}
        before = sorted(p.name for p in self.dir.iterdir())
        for label, (windows, digest) in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    ox.quantize_int8_static(self.fp32['path'], self.dir / 'bad.onnx', windows, source_sha256=digest)
        with mock.patch.object(ox, 'CALIBRATION_MAX_WINDOWS', 11):
            with self.assertRaises(ValueError):
                ox.quantize_int8_static(self.fp32['path'], self.dir / 'bad.onnx', good,
                                        source_sha256=self.fp32['sha256'])
        with self.assertRaises(ValueError):  # existing target
            ox.quantize_int8_static(self.fp32['path'], self.static['path'], good, source_sha256=self.fp32['sha256'])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), before)

    def test_quantizer_failure_leaves_nothing(self):
        from onnxruntime import quantization

        def writes_then_fails(source, target, reader, **kwargs):
            Path(target).write_bytes(b'half')
            raise RuntimeError('calibration failed')

        before = sorted(p.name for p in self.dir.iterdir())
        with mock.patch.object(quantization, 'quantize_static', writes_then_fails):
            with self.assertRaises(RuntimeError):
                ox.quantize_int8_static(self.fp32['path'], self.dir / 'fail.onnx', self.calibration,
                                        source_sha256=self.fp32['sha256'])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), before)

    def test_reader_yields_each_window_once(self):
        reader = ox._CalibrationReader(self.calibration[:3])
        seen = []
        while (item := reader.get_next()) is not None:
            seen.append(item['waveform'])
        self.assertEqual([w.shape for w in seen], [(1, WINDOW_SAMPLES)] * 3)
        np.testing.assert_array_equal(np.concatenate(seen), self.calibration[:3])
        reader.rewind()
        self.assertIsNotNone(reader.get_next())


if __name__ == '__main__':
    unittest.main()
