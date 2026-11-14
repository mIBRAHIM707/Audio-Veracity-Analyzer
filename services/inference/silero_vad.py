"""Silero VAD (ONNX, CPU) frame adapter for `services.scorer.speech_windows`
(backlog 3.2; PRD FR-I3; architecture §2.2 step 3, ADR-12).

`SileroVad(model_path)` loads a local `silero_vad.onnx` (no runtime download), checks
its SHA-256 against the pinned model, and is a stateful callable on float32 [512]
frames at 16 kHz returning the speech probability. It follows the reference
`silero_vad.utils_vad.OnnxWrapper` for 16 kHz: input = the previous 64 samples of
context + the 512-sample frame ([1, 576]), recurrent `state` [2, 1, 128], `sr` = 16000;
after each frame the state and the last 64 input samples are kept. `reset()` clears
both. Use one instance per call direction. Pinned model: silero-vad 6.2.3 package file
`data/silero_vad.onnx` (MIT), stored outside Git, e.g. under
`D:/AVA-Data/models/silero-vad/6.2.3/`. Runtime: `requirements-vad.txt`.
"""
import hashlib
import math
from pathlib import Path

import numpy as np

RATE = 16000
FRAME_SAMPLES = 512
CONTEXT_SAMPLES = 64
STATE_SHAPE = (2, 1, 128)
SILERO_VERSION = '6.2.3'
SILERO_SHA256 = '1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3'
MAX_MODEL_BYTES = 16_000_000


def _cpu_session(model_bytes, threads):
    import onnxruntime
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    return onnxruntime.InferenceSession(model_bytes, sess_options=options,
                                        providers=['CPUExecutionProvider'])


class SileroVad:
    def __init__(self, model_path, *, expected_sha256=SILERO_SHA256, threads=1,
                 session_factory=_cpu_session):
        if type(threads) is not int or not 1 <= threads <= 8:
            raise ValueError('threads must be an integer from 1 to 8')
        path = Path(model_path)
        if not path.is_file():
            raise ValueError(f'Silero model not found: {path} (provision it locally; no download at runtime)')
        with open(path, 'rb') as handle:
            model = handle.read(MAX_MODEL_BYTES + 1)
        if len(model) > MAX_MODEL_BYTES:
            raise ValueError('Silero model file is larger than expected')
        digest = hashlib.sha256(model).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError(f'Silero model SHA-256 {digest} does not match the pinned model')
        self.model_sha256 = digest
        self._session = session_factory(model, threads)
        names = sorted(i.name for i in self._session.get_inputs())
        if names != ['input', 'sr', 'state']:
            raise ValueError(f'unexpected Silero model inputs {names}')
        self._sr = np.array(RATE, dtype=np.int64)
        self.reset()

    def reset(self):
        self._state = np.zeros(STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)

    def __call__(self, frame):
        frame = np.asarray(frame)
        if frame.shape != (FRAME_SAMPLES,) or frame.dtype != np.float32 or not np.isfinite(frame).all():
            raise ValueError(f'frame must be finite float32 of shape ({FRAME_SAMPLES},)')
        x = np.concatenate([self._context, frame])[None, :]
        output, state = self._session.run(None, {'input': x, 'state': self._state, 'sr': self._sr})
        probability = float(np.asarray(output).reshape(-1)[0])
        state = np.asarray(state, dtype=np.float32)
        if state.shape != STATE_SHAPE or not np.isfinite(state).all() \
                or not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError('Silero returned an invalid state or probability')
        self._state, self._context = state, x[0, -CONTEXT_SAMPLES:].copy()  # only after success
        return probability
