"""Model windows from a published prepared or augmented root (backlog 9.1).

Reads ``report.json`` written by ``data/prepare/prepare_audio.py --condition`` or
``data/prepare/augment.py`` and returns one 64,000-sample window per clip (FR-M11):
shorter clips are zero-padded at the end; longer clips keep their first 64,000
samples (``dropped_samples`` records the rest; the PRD does not say how a 4-6 s clip
maps to one window, so this is an interpretation to confirm).

Selection is by the row's ``split`` field. ``purpose='train'`` reads only split
``train`` and only rows that are explicitly trainable (``training_eligible`` true and
no benchmark scope; the rule in ``data/manifest/validate_splits.py``); the others are
counted as exclusions, never silently mixed in, and a speaker or source-audio hash
shared with another split refuses the load. Only conditioned roots are accepted
(train/serve parity). Any unreadable, altered, duplicate or malformed selected clip,
and any quarantined source or refused copy (unless explicitly allowed), refuses the
whole load. Audio is held as int16 and converted per batch.
"""
import array
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import random
import stat
import sys
import wave

from data.manifest.validate_splits import is_trainable
from models.ava.model import SAMPLE_RATE, WINDOW_SAMPLES
from models.ava.sls import CLASS_ORDER

CLASS_INDEX = {name: index for index, name in enumerate(CLASS_ORDER)}
MIN_SAMPLES, MAX_SAMPLES = 3 * SAMPLE_RATE, 6 * SAMPLE_RATE
MAX_REPORT_BYTES = 64_000_000
MAX_CLIP_BYTES = 2 * MAX_SAMPLES + 4096
PURPOSES = ('train', 'evaluate')
TRAIN_SPLIT = 'train'
LINEAGE_KEYS = ('speaker_id', 'source_recording_id', 'source_sha256', 'original_audio_sha256', 'canonical_pcm_sha256')
MAX_PROBLEMS = 20
# String provenance carried on each window for evaluation subsets (FR-M5, FR-M10).
REPORT_PROVENANCE = ('language', 'dialect', 'source_id', 'generator_family', 'generator_version',
                     'source_partition')


def _reject_constant(value):
    raise ValueError(f'Non-finite JSON constant is unsupported: {value}')


def _finite(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('JSON number exceeds the finite range')
    return number


def _read_regular(path, limit):
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('expected a regular file')
    with path.open('rb') as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise ValueError('file exceeds size limit')
    return payload


def _clip_path(root, name):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name or '\x00' in name
            or PurePosixPath(name).is_absolute()
            or any(part in ('', '.', '..') for part in name.split('/'))):
        raise ValueError('unsafe clip path')
    path = root / name
    current = path
    # Path.is_junction exists from Python 3.12; on 3.11 the resolve() check below
    # is what blocks junction escapes.
    while current != root:
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise ValueError('symlinked clip path')
        current = current.parent
    if not path.resolve().is_relative_to(root):
        raise ValueError('clip path escapes the root')
    return path


def _decode(payload):
    with wave.open(io.BytesIO(payload), 'rb') as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getcomptype()) != (
                SAMPLE_RATE, 1, 2, 'NONE'):
            raise ValueError('expected 16 kHz mono PCM16 WAV')
        count = audio.getnframes()
        raw = audio.readframes(count)
    if len(raw) != 2 * count:
        raise ValueError('truncated WAV')
    if not MIN_SAMPLES <= count <= MAX_SAMPLES:
        raise ValueError('expected a 3-6 s clip')
    samples = array.array('h', raw)
    if sys.byteorder != 'little':
        samples.byteswap()
    return samples


def _load_report(root):
    if not root.is_dir() or (root / 'INCOMPLETE').exists():
        raise ValueError(f'Root {root.name!r} is missing or its publication is incomplete')
    payload = _read_regular(root / 'report.json', MAX_REPORT_BYTES)
    try:
        report = json.loads(payload, parse_constant=_reject_constant, parse_float=_finite)
    except RecursionError as error:
        raise ValueError('Report nesting is too deep') from error
    if not isinstance(report, dict) or not isinstance(report.get('clips'), list):
        raise ValueError('Report has no clips list')
    return report, payload


def _require_conditioned(report):
    """Only ``prepare_audio.py --condition`` output (16 kHz, peak-normalized) is served."""
    policy = report.get('policy')
    if (not isinstance(policy, dict) or policy.get('sample_rate') != SAMPLE_RATE
            or policy.get('conditioning') in (None, 'none')
            or not str(policy.get('normalization', '')).startswith('per-clip peak')):
        raise ValueError('Report is not a conditioned 16 kHz prepared-output report '
                         '(prepare_audio.py --condition)')


def _summary_errors(report):
    summary = report.get('summary')
    errors = summary.get('errors') if isinstance(summary, dict) else None
    if type(errors) is not int or errors < 0:
        raise ValueError('Prepared report summary lacks an error count')
    return errors


def _split_leaks(rows, split):
    """Lineage values of ``split`` rows that also occur in a row of another split.

    Checks speaker and source-audio keys within this report only; leakage across
    separately published roots needs ``data/manifest/validate_splits.py``.
    """
    seen = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in LINEAGE_KEYS:
            value = row.get(key)
            if isinstance(value, str) and value:
                seen.setdefault((key, value), set()).add(row.get('split'))
    # Key names only: diagnostics never print speaker identities.
    return [key for (key, _), splits in seen.items() if split in splits and len(splits) > 1]


def load_windows(prepared_root, split, purpose, *, parent_root=None, noise_path=None,
                 allow_source_errors=False, max_windows=10_000, ffmpeg=None):
    """Return ``{'windows': [...], 'summary': {...}}``; each window keeps its row metadata.

    ``window['samples']`` is ``array('h')`` of exactly WINDOW_SAMPLES values. An
    augmented-copy root needs its ``parent_root`` (and ``noise_path`` if the chain
    names noise): the parent must be a conditioned prepared root and
    ``data.prepare.augment.verify_augmented`` must pass before anything loads.
    Roots that quarantined sources or refused copies are refused unless
    ``allow_source_errors`` is true (the count is then reported). Holding the default
    10,000 windows takes about 1.3 GB of int16 audio.
    """
    if purpose not in PURPOSES:
        raise ValueError(f'purpose must be one of {PURPOSES}')
    if not isinstance(split, str) or not split:
        raise ValueError('split must be a nonempty string')
    if purpose == 'train' and split != TRAIN_SPLIT:
        raise ValueError(f"purpose 'train' only reads split {TRAIN_SPLIT!r}")
    if type(max_windows) is not int or max_windows < 1:
        raise ValueError('max_windows must be a positive integer')
    if type(allow_source_errors) is not bool:
        raise ValueError('allow_source_errors must be a boolean')
    root = Path(prepared_root).resolve()
    report, payload = _load_report(root)
    lineage = {}
    if report.get('kind') == 'augmented_copies':
        from data.prepare import augment
        if parent_root is None:
            raise ValueError('An augmented-copy root needs its parent_root for verification')
        parent, _ = _load_report(Path(parent_root).resolve())
        _require_conditioned(parent)
        try:
            # GSM/AMR-NB roots rebuild through the pinned ffmpeg (data/prepare/ffmpeg_codecs.Ffmpeg).
            problems = augment.verify_augmented(parent_root, root, noise_path, ffmpeg=ffmpeg)
        except RecursionError as error:
            raise ValueError('Report nesting is too deep') from error
        if problems:
            raise ValueError(f'Augmented root failed verification: {"; ".join(problems[:MAX_PROBLEMS])}')
        if _load_report(root)[1] != payload:
            raise ValueError('Augmented report changed during verification')
        # Parent quarantines are invisible in the copies, so they count too.
        source_errors = len(report.get('errors') or []) + _summary_errors(parent)
        lineage = {'parent_report_sha256': report.get('parent_report_sha256'),
                   'chain_spec_sha256': report.get('chain_spec_sha256'),
                   # The spec hash does not name the codec tool; GSM/AMR output depends on it.
                   'codec_tool_sha256': augment.recorded_tool_hash(report)}
        spec = report['chain_spec']  # verified above; a codec_mix names each row's codec

        def codec_of(row):
            return augment.row_codec(spec, row)
    else:
        if parent_root is not None or noise_path is not None or ffmpeg is not None:
            raise ValueError('parent_root, noise_path and ffmpeg apply only to augmented-copy roots')
        _require_conditioned(report)
        source_errors = _summary_errors(report)
        def codec_of(row):
            return 'none'  # conditioned 16 kHz clips without a telephone channel
    if source_errors and not allow_source_errors:
        raise ValueError(f'Root lists {source_errors} quarantined source(s) or refused copies; '
                         'pass allow_source_errors=True to load the remainder knowingly')
    leaks = _split_leaks(report['clips'], split) if purpose == 'train' else []
    if leaks:
        counts = ', '.join(f'{key} x{leaks.count(key)}' for key in sorted(set(leaks)))
        raise ValueError(f'{len(leaks)} training lineage value(s) also occur in other splits: {counts}')
    excluded = {'other_split': 0, 'not_trainable': 0}
    windows, problems, seen, hashes = [], [], set(), set()
    for index, row in enumerate(report['clips']):
        if not isinstance(row, dict):
            problems.append(f'clip {index}: not an object')
            continue
        if row.get('split') != split:
            excluded['other_split'] += 1
            continue
        if purpose == 'train' and not is_trainable(row):
            excluded['not_trainable'] += 1
            continue
        try:
            if row.get('label') not in CLASS_INDEX:
                raise ValueError(f'label must be one of {sorted(CLASS_INDEX)}')
            path = _clip_path(root, row.get('path'))
            if row['path'].casefold() in seen:
                raise ValueError('duplicate clip path')
            seen.add(row['path'].casefold())
            clip = _read_regular(path, MAX_CLIP_BYTES)
            if hashlib.sha256(clip).hexdigest() != row.get('sha256'):
                raise ValueError('clip hash mismatch')
            if row['sha256'] in hashes:
                raise ValueError('duplicate clip audio')
            hashes.add(row['sha256'])
            samples = _decode(clip)
            codec = codec_of(row)
        except (OSError, ValueError, wave.Error, EOFError) as error:
            problems.append(f'clip {index}: {error}')
            continue
        if len(windows) >= max_windows:
            raise ValueError(f'Selection exceeds max_windows={max_windows}')
        kept = samples[:WINDOW_SAMPLES]
        padded = WINDOW_SAMPLES - len(kept)
        kept.extend(array.array('h', bytes(2 * padded)))
        windows.append({'path': row['path'], 'sha256': row['sha256'], 'label': row['label'],
                        'target': CLASS_INDEX[row['label']], 'speaker_id': row.get('speaker_id'),
                        'codec': codec,
                        'provenance': {key: row[key] for key in REPORT_PROVENANCE if isinstance(row.get(key), str)},
                        'augmented_copy': row.get('augmented_copy') is True,
                        'review_flagged': bool(row.get('review_flags')),
                        'clip_samples': len(samples), 'padded_samples': padded,
                        'dropped_samples': max(0, len(samples) - WINDOW_SAMPLES), 'samples': kept})
    if problems:
        shown = '; '.join(problems[:MAX_PROBLEMS])
        more = f' (+{len(problems) - MAX_PROBLEMS} more)' if len(problems) > MAX_PROBLEMS else ''
        raise ValueError(f'{len(problems)} selected clip(s) refused: {shown}{more}')
    if not windows:
        raise ValueError(f'No {purpose} windows for split {split!r}')
    def per_label(test):
        return {name: sum(w['label'] == name and test(w) for w in windows) for name in CLASS_ORDER}

    # Per-label padding/truncation counts expose a length shortcut between classes.
    summary = {'split': split, 'purpose': purpose, 'windows': len(windows),
               'by_label': per_label(lambda w: True),
               'augmented_copies': sum(w['augmented_copy'] for w in windows),
               'review_flagged_windows': sum(w['review_flagged'] for w in windows),
               'padded_by_label': per_label(lambda w: w['padded_samples'] > 0),
               'truncated_by_label': per_label(lambda w: w['dropped_samples'] > 0),
               'mean_padded_samples_by_label': {
                   name: (sum(w['padded_samples'] for w in windows if w['label'] == name)
                          / max(1, sum(w['label'] == name for w in windows))) for name in CLASS_ORDER},
               'excluded': excluded, 'source_errors_allowed': source_errors,
               'report_sha256': hashlib.sha256(payload).hexdigest(), **lineage}
    return {'windows': windows, 'summary': summary}


def batches(windows, batch_size, seed=None):
    """Yield ``(float32[B, 64000], int64[B])``; ``seed`` gives a reproducible shuffle and
    ``seed=None`` keeps report order (use a seed for training).

    PCM16 is scaled by 1/32768, as in the inference service and the smoke runner.
    """
    import numpy as np
    import torch
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError('batch_size must be a positive integer')
    order = list(range(len(windows)))
    if seed is not None:
        random.Random(seed).shuffle(order)
    for start in range(0, len(order), batch_size):
        chosen = [windows[i] for i in order[start:start + batch_size]]
        pcm = np.stack([np.frombuffer(w['samples'], dtype=np.int16) for w in chosen])
        yield torch.from_numpy(pcm.astype(np.float32) / 32768.0), torch.tensor([w['target'] for w in chosen], dtype=torch.int64)
