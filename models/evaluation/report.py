"""Evaluation report generation from scored windows (backlog 9.3; PRD FR-M5, FR-M6,
FR-M10, FR-M11).

``score_windows`` runs a detector over ``models/ava/windows.py`` windows in eval and
inference mode and returns one row per window with the binary logit
``z = logit_synthetic - logit_genuine`` and ``p_synthetic = sigmoid(z / T)`` (the
softmax of the two logits divided by T). ``build_report`` joins those rows with each
window's label, speaker, codec and provenance and calls the tested metric functions:
the FR-M5 subset table, FR-M6 ECE and reliability bins, and, when a deployed threshold
and dialect list are given, FR-M10 dialect FAR on genuine windows. ``write_report``
publishes a new JSON file outside the repository.

The minDCF costs are required and recorded with a status string, because the
ASVspoof 5 constants are unconfirmed (see README). Every report carries the window
summary (report hashes, exclusions) and limitations; no figure here is a model result
unless real AVA scores were used.
"""
import json
import math
from pathlib import Path

import numpy as np

from models.evaluation import metrics

SUBSET_FIELDS = ('language', 'codec', 'dialect', 'generator_family', 'source_partition', 'source_id')
LIMITATIONS = ('Clip-level percentile bootstrap intervals (speaker clustering open, FR-M5); '
               'EER by linear interpolation at the FAR/FRR crossing; one window per clip (FR-M11). '
               'ECE is measured at the stated temperature on this set; see temperature_status for '
               'where T came from. FR-M5 subsets: (a) full, (b) language, (c) codec are reported; '
               '(d) held-out generator partition is approximated by generator_family/source_partition '
               'without a held-out marker; (e) code-switched, (g) real phone-call set have no manifest '
               'field yet and are not reported; (f) dialect rows here are not dialect-held-out sets.')


def _temperature(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError('temperature must be a finite number > 0')
    return float(value)


def score_windows(model, windows, *, temperature=1.0, batch_size=8):
    """Score windows in report order; returns rows with path, sha256, label, speaker,
    codec, provenance, ``logit`` (z) and ``p_synthetic``."""
    import torch
    from models.ava.windows import batches
    temperature = _temperature(temperature)
    if not windows:
        raise ValueError('No windows to score')
    was_training = model.training
    model.eval()
    logits = []
    try:
        with torch.inference_mode():
            for waveform, _ in batches(windows, batch_size):
                out, _ = model(waveform)
                if (not isinstance(out, torch.Tensor) or out.ndim != 2 or out.shape[1] != 2
                        or not torch.isfinite(out).all()):
                    raise ValueError('Model must return finite logits shaped [B, 2]')
                out = out.double()
                logits.extend((out[:, 0] - out[:, 1]).tolist())
    finally:
        model.train(was_training)
    rows = []
    for window, z in zip(windows, logits, strict=True):
        rows.append({'path': window['path'], 'sha256': window['sha256'], 'label': window['label'],
                     'speaker_id': window.get('speaker_id'), 'codec': window.get('codec'),
                     'provenance': dict(window.get('provenance') or {}),
                     'logit': z, 'temperature': temperature, 'p_synthetic': _sigmoid(z / temperature)})
    return rows


def _sigmoid(x):
    """Overflow-free logistic function."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def build_report(scored, *, costs, costs_status, temperature_status, model_id, windows_summary,
                 dialects=None, threshold=None, seed=0, n_resamples=1000, min_clips_per_class=10):
    """Assemble the report dictionary (see module docstring). The temperature is taken
    from the scored rows (all must agree); ``temperature_status`` says where it came
    from (e.g. 'uncalibrated' or 'fit on dev <report sha>')."""
    if not isinstance(costs_status, str) or not costs_status:
        raise ValueError('costs_status must say where the minDCF costs come from')
    if not isinstance(temperature_status, str) or not temperature_status:
        raise ValueError('temperature_status must say where the temperature came from')
    if not isinstance(model_id, str) or not model_id:
        raise ValueError('model_id must identify the scored model or checkpoint')
    if not isinstance(windows_summary, dict) or 'report_sha256' not in windows_summary:
        raise ValueError('windows_summary must be the load_windows summary')
    if (dialects is None) != (threshold is None):
        raise ValueError('dialects and threshold must be given together (FR-M10)')
    if not scored:
        raise ValueError('No scored windows')
    temperatures = {row.get('temperature') for row in scored}
    if len(temperatures) != 1:
        raise ValueError('Scored rows disagree on (or lack) their temperature')
    temperature = _temperature(temperatures.pop())
    for index, row in enumerate(scored):
        z, p = row.get('logit'), row.get('p_synthetic')
        if (isinstance(z, bool) or not isinstance(z, (int, float)) or not math.isfinite(z)
                or not math.isclose(_sigmoid(z / temperature), p, rel_tol=1e-9, abs_tol=1e-12)):
            raise ValueError(f'scored row {index}: p_synthetic does not match its logit and temperature')
    records = []
    for index, row in enumerate(scored):
        if row.get('label') not in metrics.LABELS:
            raise ValueError(f'scored row {index} has no genuine/synthetic label')
        record = {'label': row['label'], 'score': row['p_synthetic'], 'codec': row.get('codec')}
        for field in SUBSET_FIELDS:
            if field != 'codec':
                record[field] = (row.get('provenance') or {}).get(field)
        records.append(record)
    subsets = metrics.subset_report(records, list(SUBSET_FIELDS), costs=costs, seed=seed,
                                    n_resamples=n_resamples, min_clips_per_class=min_clips_per_class)
    p = [r['score'] for r in records]
    y = [r['label'] == 'synthetic' for r in records]
    calibration = {'temperature': temperature, 'temperature_status': temperature_status, 'n_bins': 15}
    if all(y) or not any(y):
        calibration['status'] = 'single class: ECE not reported'
    else:
        calibration.update(status='ok', ece=metrics.ece(p, y), reliability=metrics.reliability_bins(p, y))
    report = {'schema_version': 1, 'model_id': model_id, 'windows': len(records),
              'windows_summary': windows_summary, 'costs': dict(costs), 'costs_status': costs_status,
              'subsets': subsets, 'calibration': calibration, 'limitations': LIMITATIONS}
    if dialects is not None:
        listed, unlisted, no_speaker = [], {}, 0
        for row in scored:
            if row['label'] != 'genuine':
                continue
            dialect = (row.get('provenance') or {}).get('dialect')
            if dialect not in dialects:
                # Dialect names are not identities; per-name counts expose spelling drift.
                key = dialect if isinstance(dialect, str) else '(missing)'
                unlisted[key] = unlisted.get(key, 0) + 1
            elif not isinstance(row.get('speaker_id'), str) or not row['speaker_id']:
                no_speaker += 1
            else:
                listed.append({'dialect': dialect, 'speaker': row['speaker_id'],
                               'score': row['p_synthetic'], 'label': 'genuine'})
        report['dialect_far'] = {'threshold': threshold, 'unlisted_genuine_by_dialect': unlisted,
                                 'listed_genuine_without_speaker': no_speaker,
                                 'rows': metrics.dialect_far(listed, dialects=list(dialects), threshold=threshold,
                                                             seed=seed, n_resamples=n_resamples)}
    return report


def _json_safe(value):
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.ndarray):
        return _json_safe(value.tolist())
    if isinstance(value, np.generic):
        return value.item()
    return value


def write_report(path, report):
    """Write a new JSON report outside the repository; never overwrite."""
    path = Path(path).resolve()
    repo = Path(__file__).resolve().parents[2]
    if path.is_relative_to(repo):
        raise ValueError('Evaluation reports must be written outside the repository')
    if path.exists():
        raise ValueError('Report path already exists; reports are never overwritten')
    try:
        text = json.dumps(_json_safe(report), indent=2, sort_keys=True, allow_nan=False)
    except TypeError as error:
        raise ValueError(f'Report is not JSON-serializable: {error}') from error
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        stream.write(text)
    return path
