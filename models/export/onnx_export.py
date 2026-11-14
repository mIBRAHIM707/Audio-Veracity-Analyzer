"""ONNX export, PyTorch parity and the FR-M7 quantization rule (backlog 10.1).

PRD FR-M7: export to ONNX opset 17 with a static 64,000-sample input and a dynamic
batch; PyTorch parity as a maximum absolute logit difference below 1e-3 on 100
windows; INT8 dynamic quantization; if INT8 loses more than 0.3 EER points try static
quantization, then ship FP32. Architecture §2.3: serving loads the ONNX model on CPU
only, with no runtime download.

``export_onnx`` writes a new FP32 graph outside the repository (weights never enter
Git) and never overwrites. ``open_session`` builds a CPU-only onnxruntime session,
optionally pinned by SHA-256, and ``check_session`` verifies the I/O contract.
``parity`` compares the session with the PyTorch model on the same float32 windows.
``choose_quantization`` is the pure FR-M7 decision rule over measured EERs; it does not
measure anything itself. INT8 quantization is not implemented yet.
"""
import hashlib
import math
import numbers
import os
from pathlib import Path

import numpy as np

# Equal to models.ava.model.WINDOW_SAMPLES (pinned by a model test); not imported so the
# pure rules here load without torch.
WINDOW_SAMPLES = 64_000
OPSET = 17
INPUT_NAME = 'waveform'
OUTPUT_NAMES = ('logits', 'embedding')
BATCH_AXIS = 'batch'
PARITY_MAX_ABS_LOGIT_DIFF = 1e-3  # FR-M7, strict: a difference of exactly 1e-3 fails
PARITY_MIN_WINDOWS = 100          # FR-M7
PARITY_MAX_WINDOWS = 2_000        # local bound: outputs are kept for the whole call
PARITY_MAX_BATCH = 64             # local bound on one model call
INT8_MAX_EER_LOSS = 0.003         # FR-M7 "more than 0.3 EER points", EER as a fraction

_REPO = Path(__file__).resolve().parents[2]


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def export_onnx(model, path, *, opset=OPSET, seed=0):
    """Export ``model`` (``float32[B, 64000] -> logits, embedding``) to a new file.

    Returns ``{'path', 'sha256', 'bytes', 'opset', 'torch_version', 'onnx_version'}``
    (opset read back from the file). Needs a filesystem with hard links. The model's
    training mode is restored. Raises ValueError for a path inside the repository or an
    existing path; a failed export leaves no file at ``path``.
    """
    import onnx
    import torch
    if type(opset) is not int or opset != OPSET:
        raise ValueError(f'opset must be {OPSET} (FR-M7)')
    path = Path(path).resolve()
    if path.is_relative_to(_REPO):
        raise ValueError('ONNX models hold weights and must be written outside the repository')
    if path.suffix.lower() != '.onnx':
        raise ValueError('Export path must end in .onnx')
    if path.exists():
        raise ValueError('Export path already exists; models are never overwritten')
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + '.partial')
    if partial.exists():
        raise ValueError(f'A partial export is in the way: {partial.name}')
    example = torch.randn(2, WINDOW_SAMPLES, generator=torch.Generator().manual_seed(seed))
    axes = {name: {0: BATCH_AXIS} for name in (INPUT_NAME, *OUTPUT_NAMES)}
    was_training = model.training
    model.eval()
    try:
        with torch.no_grad():
            torch.onnx.export(model, (example,), str(partial), input_names=[INPUT_NAME],
                              output_names=list(OUTPUT_NAMES), dynamic_axes=axes,
                              opset_version=opset, do_constant_folding=True, dynamo=False)
        graph = onnx.load(str(partial), load_external_data=False)
        written = {item.domain: item.version for item in graph.opset_import}.get('', None)
        if written != opset:
            raise ValueError(f'Exported graph declares opset {written}, expected {opset}')
        # A hard link fails atomically if the target exists (POSIX and NTFS); a rename
        # would silently replace it on POSIX.
        try:
            os.link(partial, path)
        except FileExistsError:
            raise ValueError('Export path already exists; models are never overwritten') from None
    finally:
        model.train(was_training)
        if partial.exists():
            partial.unlink()
    return {'path': str(path), 'sha256': _sha256(path), 'bytes': path.stat().st_size,
            'opset': written, 'torch_version': torch.__version__, 'onnx_version': onnx.__version__}


def open_session(path, *, threads=1, expected_sha256=None):
    """CPU-only onnxruntime session with bounded threads; refuses a hash mismatch."""
    import onnxruntime as ort
    if type(threads) is not int or threads < 1:
        raise ValueError('threads must be a positive integer')
    path = Path(path)
    if not path.is_file():
        raise ValueError('ONNX model file not found')
    if expected_sha256 is not None and _sha256(path) != expected_sha256:
        raise ValueError('ONNX model SHA-256 does not match the pinned value')
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    return ort.InferenceSession(str(path), sess_options=options, providers=['CPUExecutionProvider'])


def check_session(session):
    """Problems with the FR-M7 I/O contract (empty list when it holds)."""
    problems = []
    inputs, outputs = session.get_inputs(), session.get_outputs()
    if len(inputs) != 1:
        problems.append(f'expected one input, found {len(inputs)}')
    else:
        spec = inputs[0]
        if spec.name != INPUT_NAME:
            problems.append(f'input name {spec.name!r}, expected {INPUT_NAME!r}')
        if spec.type != 'tensor(float)':
            problems.append(f'input type {spec.type}, expected tensor(float)')
        shape = list(spec.shape)
        if len(shape) != 2 or isinstance(shape[0], int) or shape[1] != WINDOW_SAMPLES:
            problems.append(f'input shape {shape}, expected [dynamic batch, {WINDOW_SAMPLES}]')
    names = [spec.name for spec in outputs]
    if names != list(OUTPUT_NAMES):
        problems.append(f'outputs {names}, expected {list(OUTPUT_NAMES)}')
    for spec in outputs:
        if spec.type != 'tensor(float)':
            problems.append(f'output {spec.name} type {spec.type}, expected tensor(float)')
        if not spec.shape or isinstance(spec.shape[0], int):
            problems.append(f'output {spec.name} shape {list(spec.shape)} lacks a dynamic batch')
    return problems


def _windows(windows, min_windows):
    array = np.asarray(windows)
    if array.dtype != np.float32:
        raise ValueError('windows must be float32 (the serving dtype)')
    if array.ndim != 2 or array.shape[1] != WINDOW_SAMPLES:
        raise ValueError(f'windows must be shaped [N, {WINDOW_SAMPLES}]')
    if array.shape[0] < min_windows:
        raise ValueError(f'parity needs at least {min_windows} windows (insufficient data)')
    if array.shape[0] > PARITY_MAX_WINDOWS:
        raise ValueError(f'parity takes at most {PARITY_MAX_WINDOWS} windows per call (memory bound)')
    if not np.isfinite(array).all():
        raise ValueError('windows must be finite')
    return np.ascontiguousarray(array)


def parity(model, session, windows, *, batch_size=8, tolerance=PARITY_MAX_ABS_LOGIT_DIFF,
           min_windows=PARITY_MIN_WINDOWS):
    """Compare ``session`` with ``model`` on the same windows (FR-M7).

    Returns windows, batch size, maximum absolute logit and embedding differences,
    the number of windows whose predicted class differs, the tolerance and ``passed``
    (all outputs finite, I/O contract holds and the maximum logit difference is below
    the tolerance). The model's training mode is restored.
    """
    import torch
    if type(batch_size) is not int or not 1 <= batch_size <= PARITY_MAX_BATCH:
        raise ValueError(f'batch_size must be an integer from 1 to {PARITY_MAX_BATCH}')
    if (isinstance(tolerance, bool) or not isinstance(tolerance, numbers.Real)
            or not math.isfinite(tolerance) or tolerance <= 0):
        raise ValueError('tolerance must be a finite number > 0')
    if type(min_windows) is not int or min_windows < 1:
        raise ValueError('min_windows must be a positive integer')
    array = _windows(windows, min_windows)
    problems = check_session(session)
    if problems:
        return {'windows': int(array.shape[0]), 'passed': False, 'problems': problems}
    torch_logits, torch_emb, onnx_logits, onnx_emb = [], [], [], []
    was_training = model.training
    model.eval()
    try:
        with torch.inference_mode():
            for start in range(0, array.shape[0], batch_size):
                chunk = array[start:start + batch_size]
                logits, embedding = model(torch.from_numpy(chunk.copy()))
                torch_logits.append(logits.double().numpy())
                torch_emb.append(embedding.double().numpy())
                out_logits, out_emb = session.run(list(OUTPUT_NAMES), {INPUT_NAME: chunk})
                onnx_logits.append(np.asarray(out_logits, dtype=np.float64))
                onnx_emb.append(np.asarray(out_emb, dtype=np.float64))
    finally:
        model.train(was_training)
    tl, te = np.concatenate(torch_logits), np.concatenate(torch_emb)
    ol, oe = np.concatenate(onnx_logits), np.concatenate(onnx_emb)
    if tl.shape != ol.shape or te.shape != oe.shape:
        return {'windows': int(array.shape[0]), 'passed': False,
                'problems': [f'output shapes differ: logits {tl.shape} vs {ol.shape}, '
                             f'embedding {te.shape} vs {oe.shape}']}
    finite = bool(np.isfinite(tl).all() and np.isfinite(ol).all() and np.isfinite(te).all()
                  and np.isfinite(oe).all())
    logit_diff = float(np.max(np.abs(tl - ol))) if finite else math.inf
    embedding_diff = float(np.max(np.abs(te - oe))) if finite else math.inf
    return {'windows': int(array.shape[0]), 'batch_size': batch_size,
            'max_abs_logit_diff': logit_diff, 'max_abs_embedding_diff': embedding_diff,
            'class_disagreements': int(np.sum(np.argmax(tl, axis=1) != np.argmax(ol, axis=1))),
            'tolerance': float(tolerance), 'finite': finite, 'problems': [],
            'passed': finite and logit_diff < tolerance}


def _eer(value, name):
    if (isinstance(value, bool) or not isinstance(value, numbers.Real)
            or not math.isfinite(value) or not 0 <= value <= 1):
        raise ValueError(f'{name} must be an EER fraction from 0 to 1')
    return float(value)


def choose_quantization(fp32_eer, int8_dynamic_eer, int8_static_eer=None, *, max_loss=INT8_MAX_EER_LOSS):
    """FR-M7 decision from dev EERs (fractions). Returns ``{'decision', 'reason', ...}``.

    'int8_dynamic' when dynamic INT8 loses at most ``max_loss``; otherwise
    'needs_static' until a static EER is measured; then 'int8_static' when it loses at
    most ``max_loss``, else 'fp32' (latency must then be re-benchmarked). A loss of
    exactly ``max_loss`` is allowed ("more than" fails); an improvement is a zero loss.
    """
    if (isinstance(max_loss, bool) or not isinstance(max_loss, numbers.Real)
            or not math.isfinite(max_loss) or not 0 <= max_loss < 1):
        raise ValueError('max_loss must be a fraction from 0 to below 1')
    fp32 = _eer(fp32_eer, 'fp32_eer')
    dynamic_loss = _eer(int8_dynamic_eer, 'int8_dynamic_eer') - fp32
    result = {'fp32_eer': fp32, 'int8_dynamic_loss': dynamic_loss, 'max_loss': float(max_loss)}
    # Small tolerance so 0.3 points computed from EERs such as 0.053 - 0.05 is not refused.
    slack = 1e-12
    if dynamic_loss <= max_loss + slack:
        return dict(result, decision='int8_dynamic', reason='dynamic INT8 within the EER loss limit')
    if int8_static_eer is None:
        return dict(result, decision='needs_static',
                    reason='dynamic INT8 loses too much; measure static INT8 next (FR-M7)')
    static_loss = _eer(int8_static_eer, 'int8_static_eer') - fp32
    result['int8_static_loss'] = static_loss
    if static_loss <= max_loss + slack:
        return dict(result, decision='int8_static', reason='static INT8 within the EER loss limit')
    return dict(result, decision='fp32',
                reason='both INT8 variants lose too much; ship FP32 and re-benchmark latency (FR-M7)')
