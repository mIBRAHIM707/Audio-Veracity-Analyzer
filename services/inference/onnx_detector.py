"""ONNX window detector for the live pipeline (backlog 8.1/10.1; PRD FR-M7, FR-I9;
architecture §2.3).

``OnnxBatchDetector`` adapts an onnxruntime session of the exported AVA window model
(``waveform float32[batch, 64000] -> logits [batch, 2], embedding``) to the detector
contract of ``services.scorer.pipeline.LivePipeline``: called with a ``float32[B, 64000]``
batch, it returns ``float64[B, 2]`` logits in [synthetic, genuine] order, which
``analysis.p_synthetic`` turns into a probability.

The session is injected (the caller creates it CPU-only with bounded threads and a
pinned hash; ``models/export/onnx_export.open_session`` does that offline). The input
check traced into the graph is a constant, so this adapter validates every batch
itself (shape, dtype, finite samples, batch bound) and every output (shape, finite).

``per_window`` is a required keyword. ``True`` runs one window per session call. Use it for dynamic INT8 graphs,
whose activation scales are computed over the whole batch, so that a call's score never
depends on another call's audio (finding of 14 Nov, ``models/export/README.md``).
FP32 and static INT8 graphs are batch-independent and can run whole batches.
"""
import numpy as np

from services.inference.analysis import WINDOW_SAMPLES

# Same contract and batch bound as models/export/onnx_export.py (INPUT_NAME,
# check_session, PARITY_MAX_BATCH); copied so serving does not import export tooling,
# and pinned equal by tests/model/test_onnx_live_pipeline.py.
INPUT_NAME = 'waveform'
LOGITS_NAME = 'logits'
MAX_BATCH = 64


def _contract_problems(session):
    problems = []
    inputs = session.get_inputs()
    if len(inputs) != 1 or inputs[0].name != INPUT_NAME:
        problems.append(f'expected one input named {INPUT_NAME!r}')
    else:
        shape = list(inputs[0].shape)
        if len(shape) != 2 or isinstance(shape[0], int) or shape[1] != WINDOW_SAMPLES:
            problems.append(f'input shape {shape}, expected [dynamic batch, {WINDOW_SAMPLES}]')
        if inputs[0].type != 'tensor(float)':
            problems.append(f'input type {inputs[0].type}, expected tensor(float)')
    if LOGITS_NAME not in [spec.name for spec in session.get_outputs()]:
        problems.append(f'no {LOGITS_NAME!r} output')
    return problems


class OnnxBatchDetector:
    def __init__(self, session, *, per_window, max_batch=MAX_BATCH):
        # per_window has no default: the caller must decide it from the graph's
        # quantization (dynamic INT8 -> True), never get the batch-coupled mode silently.
        if not isinstance(per_window, bool):
            raise ValueError('per_window must be a bool')
        if type(max_batch) is not int or not 1 <= max_batch <= MAX_BATCH:
            raise ValueError(f'max_batch must be an integer from 1 to {MAX_BATCH}')
        problems = _contract_problems(session)
        if problems:
            raise ValueError(f'ONNX session breaks the window-model contract: {problems}')
        self.session, self.per_window, self.max_batch = session, per_window, max_batch

    def __call__(self, batch):
        if not isinstance(batch, np.ndarray) or batch.dtype != np.float32:
            raise ValueError('batch must be a float32 ndarray')
        if batch.ndim != 2 or batch.shape[1] != WINDOW_SAMPLES or not 1 <= batch.shape[0] <= self.max_batch:
            raise ValueError(f'batch must be shaped [1..{self.max_batch}, {WINDOW_SAMPLES}]')
        if not np.isfinite(batch).all():
            raise ValueError('batch contains non-finite samples')
        batch = np.ascontiguousarray(batch)
        if self.per_window:
            parts = [self._run(batch[i:i + 1]) for i in range(batch.shape[0])]
            logits = np.concatenate(parts)
        else:
            logits = self._run(batch)
        if logits.shape != (batch.shape[0], 2):
            raise ValueError(f'logits shaped {logits.shape}, expected ({batch.shape[0]}, 2)')
        if not np.isfinite(logits).all():
            raise ValueError('detector produced non-finite logits')
        return logits

    def _run(self, part):
        (logits,) = self.session.run([LOGITS_NAME], {INPUT_NAME: part})
        return np.asarray(logits, dtype=np.float64)
