"""FR-M7 export and parity on the real AntiDeepfake MMS-300M backbone (backlog 10.1).

Loads the hash-verified local checkpoint through ``models.smoke.antideepfake`` (no
download), truncates the backbone to ``--kept-layers`` (architecture §4.1), adds a
seeded, untrained SLS head (no AVA-trained head exists yet, so this checks the graph's
numerics, not a detector), exports FP32 ONNX outside the repository and runs the
FR-M7 parity harness on seeded synthetic noise windows. Optionally makes the dynamic
INT8 model and reports its drift. Writes ``report.json`` into a new output directory.

Infrastructure evidence only: no accuracy, calibration or latency claim.

    uv run --offline --no-project --python 3.11 --extra-index-url https://download.pytorch.org/whl/cpu
        --with-requirements requirements-model.txt --with-requirements requirements-export.txt
        python -m models.export.checkpoint_parity --model-root D:/AVA-Data/models/antideepfake-mms-300m
        --output-dir D:/AVA-Data/exports/<new> --kept-layers 8 --threads 4
"""
import argparse
import importlib.metadata
import json
import math
import platform
import time
from pathlib import Path

import numpy as np

from models.export import onnx_export as ox

_REPO = Path(__file__).resolve().parents[2]


def noise_windows(count, seed):
    """Seeded float32 noise windows at gains from 0.01 to 1 (the model layer-normalizes)."""
    rng = np.random.default_rng(seed)
    gains = rng.uniform(0.01, 1.0, size=(count, 1))
    return (rng.standard_normal((count, ox.WINDOW_SAMPLES)) * gains).astype(np.float32)


def load_verified(model_root):
    """(full torchaudio backbone, provenance) from the verified local checkpoint."""
    from models.smoke import antideepfake
    metadata, metadata_hash = antideepfake._verify_model(Path(model_root))
    detector = antideepfake.CpuDetector(Path(model_root) / 'model.safetensors')
    return detector.backbone, {'checkpoint_sha256': antideepfake.MODEL_SHA256,
                               'model_source_sha256': metadata_hash,
                               'repo': metadata.get('repo'), 'revision': metadata.get('revision')}


def run(model_root, output_dir, *, kept_layers, threads=4, seed=0, windows=ox.PARITY_MIN_WINDOWS,
        batch_size=8, int8=False, int8_static=False, calibration_windows=20, load=load_verified, base=None):
    """Export and check parity; returns the report (also written as report.json)."""
    import torch
    from models.ava.backbone import BACKBONE_CONFIG, backbone_config
    if type(threads) is not int or threads < 1:
        raise ValueError('threads must be a positive integer')
    if type(seed) is not int or seed < 0:
        raise ValueError('seed must be a non-negative integer')
    if type(windows) is not int or not ox.PARITY_MIN_WINDOWS <= windows <= ox.PARITY_MAX_WINDOWS:
        raise ValueError(f'windows must be an integer from {ox.PARITY_MIN_WINDOWS} to {ox.PARITY_MAX_WINDOWS}')
    if type(batch_size) is not int or not 1 <= batch_size <= ox.PARITY_MAX_BATCH:
        raise ValueError(f'batch_size must be an integer from 1 to {ox.PARITY_MAX_BATCH}')
    if (type(calibration_windows) is not int
            or not ox.CALIBRATION_MIN_WINDOWS <= calibration_windows <= ox.CALIBRATION_MAX_WINDOWS):
        raise ValueError(f'calibration_windows must be an integer from {ox.CALIBRATION_MIN_WINDOWS} '
                         f'to {ox.CALIBRATION_MAX_WINDOWS}')
    base = BACKBONE_CONFIG if base is None else base
    backbone_config(kept_layers, base)  # refuse a bad layer count before touching disk
    output_dir = Path(output_dir).resolve()
    if output_dir.is_relative_to(_REPO):
        raise ValueError('Exports and reports must be written outside the repository')
    output_dir.mkdir(parents=True, exist_ok=False)  # a new directory per run; never reuse
    # As in the smoke runner: INCOMPLETE stays if anything fails; consume only without it.
    marker = output_dir / 'INCOMPLETE'
    marker.write_text('export/parity run in progress or failed\n', encoding='utf-8')
    previous_threads = torch.get_num_threads()
    torch.set_num_threads(threads)
    try:
        report = _run(model_root, output_dir, kept_layers, threads, seed, windows, batch_size, int8,
                      int8_static, calibration_windows, load, base)
    finally:
        torch.set_num_threads(previous_threads)
    marker.unlink()
    return report


def _run(model_root, output_dir, kept_layers, threads, seed, windows, batch_size, int8,
         int8_static, calibration_windows, load, base):
    import torch
    from models.ava.backbone import truncate_backbone
    from models.ava.model import AvaDetector
    from models.ava.sls import SLSHead
    started = time.perf_counter()
    full, provenance = load(model_root)
    backbone = truncate_backbone(full, kept_layers, base)
    del full
    torch.manual_seed(seed)
    model = AvaDetector(backbone, SLSHead(backbone.kept_layers, backbone.feature_dim)).eval()
    loaded = time.perf_counter()
    record = ox.export_onnx(model, output_dir / f'ava-l{kept_layers}-fp32.onnx', seed=seed)
    exported = time.perf_counter()
    session = ox.open_session(record['path'], threads=threads, expected_sha256=record['sha256'])
    data = noise_windows(windows, seed + 1)
    result = ox.parity(model, session, data, batch_size=batch_size)
    checked = time.perf_counter()
    report = {'schema_version': 1, 'kept_layers': kept_layers, 'head': 'SLS, seeded untrained',
              'head_seed': seed, 'provenance': provenance, 'export': record,
              'windows': {'kind': 'seeded synthetic noise, gains 0.01-1', 'count': windows,
                          'seed': seed + 1},
              'parity': result, 'threads': threads,
              'seconds': {'load_and_truncate': loaded - started, 'export': exported - loaded,
                          'parity': checked - exported},
              'versions': {name: importlib.metadata.version(name)
                           for name in ('torch', 'torchaudio', 'onnx', 'onnxruntime', 'numpy')},
              'platform': platform.platform(),
              'limitations': 'Untrained head and noise windows: graph numerics only; no '
                             'accuracy, calibration or latency claim (FR-M7 dev EER not measured).'}
    if int8:
        try:
            quantized = ox.quantize_int8_dynamic(record['path'], output_dir / f'ava-l{kept_layers}-int8-dynamic.onnx',
                                                 source_sha256=record['sha256'])
            candidate = ox.open_session(quantized['path'], threads=threads, expected_sha256=quantized['sha256'])
            report['int8_dynamic'] = {'export': quantized,
                                      'drift': ox.compare_sessions(session, candidate, data, batch_size=1)}
            del candidate
        except Exception as error:  # keep the FP32 evidence; the INT8 failure is recorded
            report['int8_dynamic'] = {'error': f'{type(error).__name__}: {error}'}
    if int8_static:
        # Calibration noise uses its own seed, disjoint from the parity windows.
        calibration_seed = seed + 2
        try:
            quantized = ox.quantize_int8_static(record['path'], output_dir / f'ava-l{kept_layers}-int8-static.onnx',
                                                noise_windows(calibration_windows, calibration_seed),
                                                source_sha256=record['sha256'])
            candidate = ox.open_session(quantized['path'], threads=threads, expected_sha256=quantized['sha256'])
            report['int8_static'] = {'export': quantized, 'calibration_seed': calibration_seed,
                                     'calibration_kind': 'seeded synthetic noise (timing/numerics only)',
                                     'drift': ox.compare_sessions(session, candidate, data, batch_size=1)}
            del candidate
        except Exception as error:
            report['int8_static'] = {'error': f'{type(error).__name__}: {error}'}
    del session
    report = _json_safe(report)
    with (output_dir / 'report.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
    return report


def _json_safe(value):
    """Non-finite floats become None (the results keep their own `finite` flags)."""
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--model-root', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--kept-layers', type=int, required=True)
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--int8', action='store_true')
    parser.add_argument('--int8-static', action='store_true')
    parser.add_argument('--calibration-windows', type=int, default=20)
    args = parser.parse_args(argv)
    report = run(args.model_root, args.output_dir, kept_layers=args.kept_layers, threads=args.threads,
                 seed=args.seed, int8=args.int8, int8_static=args.int8_static,
                 calibration_windows=args.calibration_windows)
    parity = report['parity']
    errors = {name: report[name]['error'] for name in ('int8_dynamic', 'int8_static')
              if 'error' in report.get(name, {})}
    int8_error = errors or None
    print(json.dumps({'passed': parity['passed'], 'max_abs_logit_diff': parity.get('max_abs_logit_diff'),
                      'export_sha256': report['export']['sha256'], 'int8_error': int8_error,
                      'report': str(Path(args.output_dir).resolve() / 'report.json')}))
    if not parity['passed']:
        return 1
    return 2 if int8_error else 0


if __name__ == '__main__':
    raise SystemExit(main())
