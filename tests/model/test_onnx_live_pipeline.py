"""ONNX detector inside the real live pipeline (backlog 8.1/10.1; FR-I9, FR-M7).

Claude-authored test cases, 14 November 2026. A tiny random detector is exported to
ONNX (FP32 and dynamic INT8); real LiveCalls, CallGate, EventHub, MicroBatcher and
LivePipeline publish the scores. Infrastructure evidence only (random model).
"""
import tempfile
import unittest
from pathlib import Path

from services.inference.analysis import p_synthetic
from services.inference.onnx_detector import OnnxBatchDetector
from services.scorer.pipeline import LivePipeline
from models.export import onnx_export as ox
from tests.model.test_onnx_export import _model, _windows
from tests.unit.test_scorer_pipeline import CONFIG, Configs, events

from services.events.fanout import EventHub
from services.inference.capacity import CallGate, MicroBatcher
from services.scorer.live import LiveCalls


def _alone(session, window):
    (logits,) = session.run(['logits'], {'waveform': window[None, :]})
    return p_synthetic([float(v) for v in logits[0]])


class OnnxLivePipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls.fp32 = ox.export_onnx(_model(0), d / 'fp32.onnx')
        cls.int8 = ox.quantize_int8_dynamic(cls.fp32['path'], d / 'int8.onnx', source_sha256=cls.fp32['sha256'])

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _run_two_calls(self, detector, windows):
        hub = EventHub()
        sub = hub.subscribe(buffer=1024)
        live = LiveCalls(Configs(), hub, CallGate(8), new_alert_id=iter(f'a{i}' for i in range(99)).__next__)
        batcher = MicroBatcher(capacity_windows_per_s=10.0, collect_s=0.1, max_live=4)
        pipe = LivePipeline(live, batcher, detector, CONFIG)
        for call_id, window in zip(('a', 'b'), windows):
            live.start(call_id)
            self.assertTrue(pipe.offer(call_id, 'caller', 0, window, 0.0)['accepted'])
        self.assertEqual(pipe.schedule(0.0), 2)
        self.assertEqual(pipe.step(0.1), 2)  # both calls in one batch
        self.assertEqual(pipe.counts['batches'], 1)
        published = [e for e in events(sub) if e.type == 'score.updated']
        self.assertEqual(len(published), 2)
        return {e.call_id: e.data['score'] for e in published}

    def test_fp32_scores_equal_each_window_alone(self):
        session = ox.open_session(self.fp32['path'], expected_sha256=self.fp32['sha256'])
        windows = _windows(2, seed=11)
        scores = self._run_two_calls(OnnxBatchDetector(session, per_window=False), windows)
        self.assertEqual(sorted(scores), ['a', 'b'])
        for call_id, window in zip(('a', 'b'), windows):
            self.assertAlmostEqual(scores[call_id], _alone(session, window), places=6)

    def test_int8_per_window_scores_are_exactly_each_window_alone(self):
        session = ox.open_session(self.int8['path'], expected_sha256=self.int8['sha256'])
        windows = _windows(2, seed=11)
        scores = self._run_two_calls(OnnxBatchDetector(session, per_window=True), windows)
        for call_id, window in zip(('a', 'b'), windows):
            self.assertEqual(scores[call_id], _alone(session, window))
        # Contrast: whole-batch INT8 lets call b's audio move call a's score (the
        # dynamic-scale finding), which is what per_window prevents. The margin is far
        # above the FP32 batch-path noise (about 1e-8).
        batched = self._run_two_calls(OnnxBatchDetector(session, per_window=False), windows)
        moved = max(abs(batched[c] - _alone(session, w)) for c, w in zip(('a', 'b'), windows))
        self.assertGreater(moved, 1e-6)

    def test_serving_copies_match_the_export_contract(self):
        from services.inference import onnx_detector as od
        self.assertEqual(od.MAX_BATCH, ox.PARITY_MAX_BATCH)
        self.assertEqual(od.INPUT_NAME, ox.INPUT_NAME)
        self.assertEqual(od.LOGITS_NAME, ox.OUTPUT_NAMES[0])
        for record in (self.fp32, self.int8):
            session = ox.open_session(record['path'])
            self.assertEqual(ox.check_session(session), [])
            self.assertEqual(od._contract_problems(session), [])


if __name__ == '__main__':
    unittest.main()
