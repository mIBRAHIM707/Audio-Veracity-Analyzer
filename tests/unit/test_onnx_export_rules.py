"""FR-M7 quantization decision rule and ONNX I/O contract check (backlog 10.1).

Claude-authored test cases, 14 November 2026. Pure functions only (no torch, no
onnxruntime): fake session objects stand in for onnxruntime's metadata.
"""
import math
import unittest
from types import SimpleNamespace

from models.export.onnx_export import INT8_MAX_EER_LOSS, check_session, choose_quantization


class ChooseQuantizationTests(unittest.TestCase):
    def test_limit_is_three_tenths_of_a_point(self):
        self.assertEqual(INT8_MAX_EER_LOSS, 0.003)

    def test_dynamic_within_limit(self):
        for fp32, int8 in ((0.05, 0.05), (0.05, 0.052), (0.05, 0.053), (0.05, 0.04), (0.0, 0.003)):
            with self.subTest(fp32=fp32, int8=int8):
                result = choose_quantization(fp32, int8)
                self.assertEqual(result['decision'], 'int8_dynamic')
                self.assertAlmostEqual(result['int8_dynamic_loss'], int8 - fp32)

    def test_dynamic_over_limit_needs_static(self):
        result = choose_quantization(0.05, 0.0531)
        self.assertEqual(result['decision'], 'needs_static')
        self.assertNotIn('int8_static_loss', result)

    def test_static_then_fp32(self):
        self.assertEqual(choose_quantization(0.05, 0.06, 0.053)['decision'], 'int8_static')
        self.assertEqual(choose_quantization(0.05, 0.06, 0.045)['decision'], 'int8_static')
        result = choose_quantization(0.05, 0.06, 0.0531)
        self.assertEqual(result['decision'], 'fp32')
        self.assertIn('re-benchmark', result['reason'])
        self.assertAlmostEqual(result['int8_static_loss'], 0.0031)

    def test_static_ignored_when_dynamic_passes(self):
        result = choose_quantization(0.05, 0.051, 0.9)
        self.assertEqual(result['decision'], 'int8_dynamic')
        self.assertNotIn('int8_static_loss', result)

    def test_custom_limit(self):
        self.assertEqual(choose_quantization(0.1, 0.11, max_loss=0.01)['decision'], 'int8_dynamic')
        self.assertEqual(choose_quantization(0.1, 0.1, max_loss=0)['decision'], 'int8_dynamic')
        self.assertEqual(choose_quantization(0.1, 0.1001, max_loss=0)['decision'], 'needs_static')

    def test_invalid_inputs_refused(self):
        bad = (True, math.nan, math.inf, -0.01, 1.01, '0.05', None)
        for value in bad:
            with self.subTest(fp32=value):
                with self.assertRaises(ValueError):
                    choose_quantization(value, 0.05)
            if value is not None:
                with self.subTest(dynamic=value):
                    with self.assertRaises(ValueError):
                        choose_quantization(0.05, value)
                with self.subTest(static=value):
                    with self.assertRaises(ValueError):
                        choose_quantization(0.05, 0.06, value)
        for limit in (True, -0.001, 1, math.nan, '0.003'):
            with self.subTest(max_loss=limit):
                with self.assertRaises(ValueError):
                    choose_quantization(0.05, 0.05, max_loss=limit)


def _spec(name, shape, type_='tensor(float)'):
    return SimpleNamespace(name=name, shape=shape, type=type_)


class FakeSession:
    def __init__(self, inputs, outputs):
        self._inputs, self._outputs = inputs, outputs

    def get_inputs(self):
        return self._inputs

    def get_outputs(self):
        return self._outputs


GOOD_OUTPUTS = [_spec('logits', ['batch', 2]), _spec('embedding', ['batch', 256])]


class CheckSessionTests(unittest.TestCase):
    def test_contract_holds(self):
        session = FakeSession([_spec('waveform', ['batch', 64000])], GOOD_OUTPUTS)
        self.assertEqual(check_session(session), [])
        # onnxruntime reports an unnamed dynamic dimension as None.
        session = FakeSession([_spec('waveform', [None, 64000])], GOOD_OUTPUTS)
        self.assertEqual(check_session(session), [])

    def test_each_violation_reported(self):
        cases = {
            'static batch': ([_spec('waveform', [1, 64000])], GOOD_OUTPUTS, 'input shape'),
            'dynamic length': ([_spec('waveform', ['batch', 'n'])], GOOD_OUTPUTS, 'input shape'),
            'other length': ([_spec('waveform', ['batch', 32000])], GOOD_OUTPUTS, 'input shape'),
            'rank 3': ([_spec('waveform', ['batch', 1, 64000])], GOOD_OUTPUTS, 'input shape'),
            'name': ([_spec('audio', ['batch', 64000])], GOOD_OUTPUTS, 'input name'),
            'double': ([_spec('waveform', ['batch', 64000], 'tensor(double)')], GOOD_OUTPUTS, 'input type'),
            'two inputs': ([_spec('waveform', ['batch', 64000])] * 2, GOOD_OUTPUTS, 'one input'),
            'order': ([_spec('waveform', ['batch', 64000])], GOOD_OUTPUTS[::-1], 'outputs'),
            'missing': ([_spec('waveform', ['batch', 64000])], GOOD_OUTPUTS[:1], 'outputs'),
            'output batch': ([_spec('waveform', ['batch', 64000])],
                             [_spec('logits', [2, 2]), GOOD_OUTPUTS[1]], 'dynamic batch'),
            'output type': ([_spec('waveform', ['batch', 64000])],
                            [_spec('logits', ['batch', 2], 'tensor(float16)'), GOOD_OUTPUTS[1]],
                            'output logits type'),
        }
        for label, (inputs, outputs, fragment) in cases.items():
            with self.subTest(label):
                problems = check_session(FakeSession(inputs, outputs))
                self.assertTrue(any(fragment in problem for problem in problems), problems)


if __name__ == '__main__':
    unittest.main()
