"""ONNX window detector adapter with a fake session (backlog 8.1/10.1).

Claude-authored test cases, 14 November 2026. No onnxruntime: a fake session records
its calls and returns scripted logits, so batching and validation are exact.
"""
import unittest
from types import SimpleNamespace

import numpy as np

from services.inference.analysis import WINDOW_SAMPLES, p_synthetic
from services.inference.onnx_detector import OnnxBatchDetector

INPUTS = [SimpleNamespace(name='waveform', shape=['batch', WINDOW_SAMPLES], type='tensor(float)')]
OUTPUTS = [SimpleNamespace(name='logits'), SimpleNamespace(name='embedding')]


class FakeSession:
    """Logits row i = [window mean * 10, 0] so each row is traceable to its window."""

    def __init__(self, inputs=INPUTS, outputs=OUTPUTS, result=None):
        self.inputs, self.outputs, self.result, self.calls = inputs, outputs, result, []

    def get_inputs(self):
        return self.inputs

    def get_outputs(self):
        return self.outputs

    def run(self, names, feeds):
        part = feeds['waveform']
        self.calls.append((tuple(names), part.shape, part.dtype.name))
        if self.result is not None:
            return [self.result(part)]
        means = part.mean(axis=1, dtype=np.float64)
        return [np.stack([means * 10, np.zeros_like(means)], axis=1).astype(np.float32)]


def batch(*values):
    return np.stack([np.full(WINDOW_SAMPLES, v, dtype=np.float32) for v in values])


class OnnxDetectorTests(unittest.TestCase):
    def test_whole_batch_and_logit_order(self):
        session = FakeSession()
        detector = OnnxBatchDetector(session, per_window=False)
        logits = detector(batch(0.1, -0.2, 0.3))
        self.assertEqual(session.calls, [(('logits',), (3, WINDOW_SAMPLES), 'float32')])
        self.assertEqual(logits.dtype, np.float64)
        np.testing.assert_allclose(logits[:, 0], [1.0, -2.0, 3.0], rtol=1e-6)
        # Class 0 is synthetic: a higher first logit means a higher p_synthetic.
        scores = [p_synthetic([float(v) for v in row]) for row in logits]
        self.assertGreater(scores[2], scores[0])
        self.assertGreater(scores[0], scores[1])

    def test_per_window_runs_one_window_per_call(self):
        session = FakeSession()
        logits = OnnxBatchDetector(session, per_window=True)(batch(0.1, 0.2))
        self.assertEqual([shape for _, shape, _ in session.calls], [(1, WINDOW_SAMPLES)] * 2)
        np.testing.assert_allclose(logits[:, 0], [1.0, 2.0], rtol=1e-6)

    def test_invalid_batches_refused_before_the_session(self):
        session = FakeSession()
        detector = OnnxBatchDetector(session, per_window=False, max_batch=2)
        nan = batch(0.1)
        nan[0, 5] = np.nan
        for label, value in {'float64': batch(0.1).astype(np.float64), 'list': [[0.0] * WINDOW_SAMPLES],
                             '1-D': np.zeros(WINDOW_SAMPLES, np.float32),
                             'short': np.zeros((1, 32000), np.float32), 'empty': np.zeros((0, WINDOW_SAMPLES), np.float32),
                             'over max_batch': batch(0.1, 0.2, 0.3), 'nan': nan}.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    detector(value)
        self.assertEqual(session.calls, [])

    def test_invalid_outputs_refused(self):
        cases = {'nan': lambda p: np.full((p.shape[0], 2), np.nan, np.float32),
                 'three classes': lambda p: np.zeros((p.shape[0], 3), np.float32),
                 'short': lambda p: np.zeros((p.shape[0] - 1, 2), np.float32)}
        for label, result in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    OnnxBatchDetector(FakeSession(result=result), per_window=False)(batch(0.1, 0.2))

    def test_contract_checked_at_construction(self):
        bad = {'name': [SimpleNamespace(name='audio', shape=['batch', WINDOW_SAMPLES], type='tensor(float)')],
               'static batch': [SimpleNamespace(name='waveform', shape=[1, WINDOW_SAMPLES], type='tensor(float)')],
               'length': [SimpleNamespace(name='waveform', shape=['batch', 32000], type='tensor(float)')],
               'type': [SimpleNamespace(name='waveform', shape=['batch', WINDOW_SAMPLES], type='tensor(double)')]}
        for label, inputs in bad.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    OnnxBatchDetector(FakeSession(inputs=inputs), per_window=False)
        with self.assertRaises(ValueError):
            OnnxBatchDetector(FakeSession(outputs=[SimpleNamespace(name='embedding')]), per_window=False)
        for kwargs in ({'per_window': 1}, {'per_window': None}, {'per_window': False, 'max_batch': 0},
                       {'per_window': False, 'max_batch': 65}, {'per_window': False, 'max_batch': True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    OnnxBatchDetector(FakeSession(), **kwargs)

    def test_batching_mode_must_be_chosen(self):
        # No default: the batch-coupled mode is never selected by omission.
        with self.assertRaises(TypeError):
            OnnxBatchDetector(FakeSession())


if __name__ == '__main__':
    unittest.main()
