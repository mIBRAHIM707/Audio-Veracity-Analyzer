"""Read-only verification of a published data/prepare/prepare_audio.py output root.

Independently of prepare_audio.py (which it does not import) this re-reads every
source named by the input manifest, re-derives the conditioned 16 kHz PCM, the
quality metrics and flags, the pause boundaries and the retained-sample optimum,
rebuilds every clip byte-for-byte, and checks lineage, provenance, summary totals,
root contents, original-audio (e.g. FLAC) hashes and that no transcript text or
Arabic-script code point appears anywhere in report.json. Prints aggregates only.

Segmentation is checked without depending on tie-breaking: claimed clips must be
consecutive, ordered, in-bounds spans between independently recomputed pause
boundaries, and their total must equal the independently computed optimum.
Metric sums are exact: squared PCM16 values divided by 2**30 are dyadic rationals
whose float64 partial sums stay exact while the integer sum of squares is below
2**53, so metrics are compared for equality, not within a tolerance; a source
outside that range is reported as unverifiable rather than compared approximately.

Infrastructure evidence only: not listening QA, word-boundary, dialect, identity,
consent or training-eligibility evidence.
"""
import argparse
from collections import Counter
import hashlib
import io
from itertools import groupby
import json
import math
from pathlib import Path
import re
import unicodedata
import wave

import numpy as np

RATE, MIN_SAMPLES, MAX_SAMPLES, WINDOW, MIN_PAUSE = 16000, 48000, 96000, 320, 1920
TARGET_PEAK = round(32768 * 10 ** (-3 / 20))
EXACT_SUM_LIMIT = 1 << 53  # integer sum of squared PCM16 values; float64 sums are exact below it
MAX_SOURCE_BYTES, MAX_CLIP_BYTES, MAX_JSON_BYTES, MAX_TRANSCRIPT_BYTES = 10_000_000, 1_000_000, 32_000_000, 1_000_000
MAX_SOURCES = 10_000
OUTCOMES = ('all_quiet', 'too_short', 'no_valid_pause_segments', 'partial_coverage', 'full_coverage')
PROVENANCE = ('source_id', 'source_version', 'source_recording_id', 'language', 'label', 'label_provenance',
              'licence_ref', 'use_status', 'use_scope', 'cloning_status', 'split', 'listening_status',
              'generator_family', 'generator_version', 'source_partition', 'speaker_identity_status',
              'target_reference_status', 'training_eligible', 'benchmark_only', 'original_audio_sha256',
              'original_audio_path', 'source_shard', 'source_shard_sha256', 'source_row_index',
              'source_language', 'publisher_split', 'dialect', 'source_prompt_id', 'source_segment_id',
              'source_speaker_id', 'source_transcript_path', 'source_glottocode', 'original_split',
              'consent_status', 'policy_version')
CONDITIONING_FIELDS = ('input_rate', 'input_channels', 'input_sample_count', 'output_sample_count', 'output_rate',
                       'resampler', 'downmix', 'quantization', 'saturated_samples',
                       'native_near_full_scale_fraction', 'canonical_pcm_sha256')
POLICY_COMMON = {'sample_rate': RATE, 'min_seconds': 3, 'max_seconds': 6, 'quiet_dbfs': -40, 'window_ms': 20,
                 'min_pause_ms': 120, 'low_rms_dbfs': -35, 'mostly_quiet_fraction': .5,
                 'near_full_scale_amplitude': .999, 'clipping_fraction': .001,
                 'acceptance': 'candidates only; energy pauses are not verified word boundaries'}
POLICY = {'condition': {**POLICY_COMMON, 'normalization': 'per-clip peak -3 dBFS after segmentation',
                        'conditioning': 'native PCM16 -> mono mean -> soxr HQ 16k -> pause segments -> peak normalize'},
          'exact': {**POLICY_COMMON, 'normalization': 'none; exact source samples', 'conditioning': 'none'}}
ARABIC_SCRIPT = re.compile('[؀-ۿݐ-ݿࡰ-ࣿﭐ-﷿ﹰ-﻾'
                           '\U00010e60-\U00010e7f\U00010ec0-\U00010eff\U0001ee00-\U0001eeff]')
TRANSCRIPT_KEYS = ('raw_text', 'prompt', 'text', 'transcript', 'sentence')
REPORT_KEYS = {'schema_version', 'source_revision', 'source_manifest_sha256', 'policy', 'summary', 'sources', 'clips'}
SUMMARY_KEYS = {'sources', 'clips', 'errors', 'review_flagged_clips', 'retained_seconds', 'uncovered_seconds',
                'segmentation_outcomes'}
LIBRARY_FIELDS = ('numpy_version', 'soxr_version', 'libsoxr_version')
SPEAKER = re.compile(r'[A-Za-z0-9_-]{1,80}')
HEX64 = re.compile(r'[0-9a-f]{64}')
MALFORMED = (TypeError, KeyError, AttributeError, IndexError, RecursionError)


class SourceRejected(ValueError):
    """The source should have been quarantined by preparation."""


def sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def read_bounded(path, limit):
    with path.open('rb') as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise ValueError(f'input exceeds {limit} bytes')
    return payload


def db(value):
    return 20 * math.log10(max(value, 1e-12))


def measure(samples):
    """Pure: int16 array -> (metrics, quiet windows). Mirrors the documented policy, not the code."""
    if not len(samples) or int(np.sum(samples.astype(np.int64) ** 2)) >= EXACT_SUM_LIMIT:
        raise ValueError('empty signal or energy outside the exact-metric range')
    squares = (samples.astype(np.float64) / 32768) ** 2
    rms = db(math.sqrt(float(squares.sum()) / len(samples)))
    starts = np.arange(0, len(samples), WINDOW)
    sums = np.add.reduceat(squares, starts)
    lengths = np.minimum(starts + WINDOW, len(samples)) - starts
    quiet = [db(math.sqrt(float(total) / int(length))) < -40 for total, length in zip(sums, lengths)]
    fraction = sum(quiet) / len(quiet)
    near_full = int(np.count_nonzero(np.abs(samples.astype(np.float64)) / 32768 >= .999)) / len(samples)
    flags = [name for name, raised in (('low_rms', rms < -35), ('mostly_quiet', fraction > .5),
                                       ('possible_digital_clipping', near_full > .001)) if raised]
    return {'rms_dbfs': round(rms, 3), 'quiet_fraction': round(fraction, 5),
            'near_full_scale_fraction': round(near_full, 5), 'review_flags': flags}, quiet


def pause_boundaries(quiet, total):
    """Pure: 0, centres of interior quiet runs of at least 120 ms, total."""
    result, position = [0], 0
    for is_quiet, run in groupby(quiet):
        length = len(list(run))
        if is_quiet and position > 0 and position + length < len(quiet) and length * WINDOW >= MIN_PAUSE:
            result.append((2 * position + length) * WINDOW // 2)
        position += length
    return result + [total]


def best_retained(boundaries):
    """Pure: maximum total length of disjoint consecutive-boundary spans of 3-6 s."""
    best = [0] * len(boundaries)
    for j in range(1, len(boundaries)):
        best[j] = best[j - 1]
        for i in range(j - 1, -1, -1):
            length = boundaries[j] - boundaries[i]
            if length > MAX_SAMPLES:
                break  # boundaries increase, so every earlier start is longer still
            if length >= MIN_SAMPLES:
                best[j] = max(best[j], best[i] + length)
    return best[-1]


def spans_valid(spans, boundaries):
    allowed, cursor = set(boundaries), 0
    for start, end in spans:
        if start < cursor or start not in allowed or end not in allowed or not MIN_SAMPLES <= end - start <= MAX_SAMPLES:
            return False
        cursor = end
    return True


def complement(total, spans):
    gaps, cursor = [], 0
    for start, end in spans + [(total, total)]:
        if start > cursor:
            gaps.append({'start_sample': cursor, 'end_sample': start})
        cursor = max(cursor, end)
    return gaps


def encode(values):
    return np.clip(np.rint(values), -32768, 32767).astype('<i2')


def canonical_pcm(payload, mode):
    """Pure decode + conditioning of one source WAV. Raises SourceRejected like preparation would."""
    try:
        with wave.open(io.BytesIO(payload), 'rb') as audio:
            rate, channels, width, frames = (audio.getframerate(), audio.getnchannels(),
                                             audio.getsampwidth(), audio.getnframes())
            if mode == 'condition' and (width != 2 or channels not in (1, 2) or not 8000 <= rate <= 192000):
                raise SourceRejected('unsupported conditioning format')
            if mode == 'exact' and (rate, channels, width) != (RATE, 1, 2):
                raise SourceRejected('exact mode needs 16 kHz mono PCM16')
            raw = audio.readframes(frames)
    except (wave.Error, EOFError) as error:
        raise SourceRejected(type(error).__name__) from error
    if not frames or len(raw) != frames * channels * width:
        raise SourceRejected('empty or truncated WAV')
    native = np.frombuffer(raw, dtype='<i2').reshape(-1, channels).astype(np.float64)
    if mode == 'exact':
        return native[:, 0].astype('<i2'), None
    import soxr
    mixed = native.mean(axis=1)
    resampled = mixed if rate == RATE else soxr.resample(mixed, rate, RATE, quality='HQ')
    if not len(resampled) or not np.isfinite(resampled).all():
        raise SourceRejected('empty or non-finite resampling result')
    samples = encode(resampled)
    return samples, {'input_rate': rate, 'input_channels': channels, 'input_sample_count': len(mixed),
                     'output_sample_count': len(samples), 'output_rate': RATE,
                     'resampler': 'bypass' if rate == RATE else 'soxr_hq',
                     'downmix': 'arithmetic_mean' if channels == 2 else 'bypass',
                     'quantization': 'round_nearest_even_clip_pcm16',
                     'saturated_samples': int(np.count_nonzero((resampled < -32768) | (resampled > 32767))),
                     'native_near_full_scale_fraction': float(np.max(np.mean(np.abs(native) / 32768 >= .999, axis=0))),
                     'canonical_pcm_sha256': sha256(samples.tobytes())}


def normalized(samples):
    peak = float(np.max(np.abs(samples.astype(np.float64))))
    gain = TARGET_PEAK / peak if peak else 1.0
    return encode(samples.astype(np.float64) * gain), {
        'target_peak_dbfs': -3.0, 'input_peak_dbfs': 20 * math.log10(peak / 32768) if peak else None,
        'output_peak_dbfs': 20 * math.log10(TARGET_PEAK / 32768) if peak else None, 'gain_db': 20 * math.log10(gain)}


def wav_bytes(samples):
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as audio:
        audio.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        audio.writeframes(samples.astype('<i2').tobytes())
    return stream.getvalue()


def expected_clips(row, samples, conditioning, spans, mode):
    """Pure: the clip rows and WAV bytes preparation must have produced for one source."""
    provenance = {key: row[key] for key in PROVENANCE if key in row}
    if 'clip_id' in row:
        provenance['source_clip_id'] = row['clip_id']
    key, result = sha256(row['path'].encode())[:16], []
    for start, end in spans:
        piece, extra = samples[start:end], dict(provenance)
        if mode == 'condition':
            piece, normalization = normalized(piece)
            pre = measure(samples[start:end])[0]
            extra.update(sample_coordinate_rate=RATE, canonical_pcm_sha256=conditioning['canonical_pcm_sha256'],
                         normalization=normalization, pre_normalization_metrics=pre)
        payload = wav_bytes(piece)
        metrics = measure(piece)[0]
        if mode == 'condition':
            flags = set(metrics['review_flags']) | set(pre['review_flags'])
            if conditioning['saturated_samples']:
                flags.add('resampling_saturation')
            if conditioning['native_near_full_scale_fraction'] > .001:
                flags.add('native_possible_digital_clipping')
            metrics['review_flags'] = sorted(flags)
        result.append(({'path': f'clips/{key}_{start}_{end}.wav', 'speaker_id': row['speaker_id'],
                        'source_path': row['path'], 'source_sha256': row['sha256'], 'start_sample': start,
                        'end_sample': end, 'duration_seconds': (end - start) / RATE, 'sha256': sha256(payload),
                        'review_status': 'unreviewed' if mode == 'condition' else 'pending_listening',
                        **extra, **metrics}, payload))
    return result


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def relative_ok(relative):
    return (isinstance(relative, str) and bool(relative) and not any(c in relative for c in '\\:\x00')
            and not relative.startswith('/') and all(p not in ('', '.', '..') for p in relative.split('/')))


def safe_relative(root, relative):
    """Like preparation: no traversal, no link or junction component, no INCOMPLETE parent."""
    if not relative_ok(relative):
        raise ValueError('unsafe relative path')
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise ValueError('path escapes root')
    current = path
    while current != root:
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise ValueError('link or junction component')
        if (current.parent / 'INCOMPLETE').exists():
            raise ValueError('incomplete publication')
        current = current.parent
    return path


def strict_equal(left, right):
    """Equality that also requires identical types at every depth (True is not 1)."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(strict_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(strict_equal(a, b) for a, b in zip(left, right))
    return left == right


def rows_valid(rows):
    """The manifest must be one preparation's preflight would have accepted."""
    seen = set()
    for row in rows:
        if not (isinstance(row, dict) and relative_ok(row.get('path')) and isinstance(row.get('speaker_id'), str)
                and SPEAKER.fullmatch(row['speaker_id']) and isinstance(row.get('sha256'), str)
                and HEX64.fullmatch(row['sha256']) and row['path'].casefold() not in seen):
            return False
        seen.add(row['path'].casefold())
    return True


def normal_text(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def transcript_strings(value, keyed=False):
    """Strings stored under transcript keys at any depth of a transcript document."""
    if isinstance(value, str):
        if keyed and len(value.strip()) >= 2:
            yield normal_text(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from transcript_strings(item, keyed or key in TRANSCRIPT_KEYS)
    elif isinstance(value, list):
        for item in value:
            yield from transcript_strings(item, keyed)


def verify_source(row, report_source, report_clips, source_root, output_root, mode, tally, seconds):
    """One source: returns failure names. tally and seconds collect aggregates in report order."""
    failures = []
    check = lambda name, condition: None if condition else failures.append(name)
    provenance = {key: row[key] for key in PROVENANCE if key in row}
    lineage = {'path': row['path'], 'speaker_id': row['speaker_id'], 'source_sha256': row['sha256'], **provenance}
    if 'clip_id' in row:
        lineage['source_clip_id'] = row['clip_id']
    if mode == 'condition':
        lineage['sample_coordinate_rate'] = RATE
    check('source_lineage_and_provenance', all(k in report_source and strict_equal(report_source[k], v) for k, v in lineage.items()))
    try:
        try:
            payload = read_bounded(safe_relative(source_root, row['path']), MAX_SOURCE_BYTES)
        except (OSError, ValueError) as error:
            raise SourceRejected('source missing, unsafe or oversized') from error
        if sha256(payload) != row['sha256']:
            raise SourceRejected('source hash differs from manifest')
        samples, conditioning = canonical_pcm(payload, mode)
    except SourceRejected:
        error = report_source.get('error')
        check('source_rejected_but_not_quarantined', report_source.get('status') == 'quarantined' and not report_clips)
        check('quarantined_source_record', isinstance(error, str) and bool(error)
              and set(report_source) == set(lineage) | {'status', 'error'})
        tally['quarantined'] += 1
        return failures
    check('source_processed', report_source.get('status') == 'processed')
    computed = {'status', 'sample_count', 'metrics', 'uncovered_ranges', 'segmentation_outcome'}
    check('source_has_no_unexpected_fields', set(report_source) == set(lineage) | computed | ({'conditioning'} if mode == 'condition' else set()))
    metrics, quiet = measure(samples)
    if mode == 'condition':
        import soxr
        if conditioning['native_near_full_scale_fraction'] > .001:
            metrics['review_flags'].append('native_possible_digital_clipping')
        stored = report_source.get('conditioning')
        stored = stored if isinstance(stored, dict) else {}
        check('source_conditioning', set(stored) == set(CONDITIONING_FIELDS) | set(LIBRARY_FIELDS)
              and all(strict_equal(stored[k], conditioning[k]) for k in CONDITIONING_FIELDS))
        check('conditioning_library_versions', [stored.get(k) for k in LIBRARY_FIELDS]
              == [np.__version__, soxr.__version__, soxr.__libsoxr_version__])
    check('source_metrics', strict_equal(report_source.get('metrics'), metrics))
    check('source_sample_count', strict_equal(report_source.get('sample_count'), len(samples)))
    boundaries = pause_boundaries(quiet, len(samples))
    optimum = 0 if all(quiet) else best_retained(boundaries)
    spans = [(c.get('start_sample'), c.get('end_sample')) for c in report_clips]
    integral = all(type(s) is int and type(e) is int for s, e in spans)
    check('segmentation_spans_on_pause_boundaries', integral and spans_valid(spans, boundaries) and not (all(quiet) and spans))
    check('segmentation_retains_optimum', integral and sum(e - s for s, e in spans) == optimum)
    if not integral:
        return failures
    gaps = complement(len(samples), spans)
    check('uncovered_ranges', strict_equal(report_source.get('uncovered_ranges'), gaps))
    outcome = ('all_quiet' if all(quiet) else 'too_short' if len(samples) < MIN_SAMPLES else
               'no_valid_pause_segments' if not spans else 'partial_coverage' if gaps else 'full_coverage')
    check('segmentation_outcome', report_source.get('segmentation_outcome') == outcome)
    tally['outcome:' + outcome] += 1
    for (expected, payload), clip in zip(expected_clips(row, samples, conditioning, spans, mode), report_clips):
        stored = read_bounded(safe_relative(output_root, expected['path']), MAX_CLIP_BYTES) if clip.get('path') == expected['path'] else b''
        check('clip_path', clip.get('path') == expected['path'])
        check('clip_bytes_rebuilt', stored == payload)
        check('clip_sha256', clip.get('sha256') == expected['sha256'] == sha256(stored))
        check('clip_metrics_and_flags', all(strict_equal(clip.get(k), expected[k]) for k in ('rms_dbfs', 'quiet_fraction', 'near_full_scale_fraction', 'review_flags')))
        if mode == 'condition':
            check('clip_pre_normalization_metrics', strict_equal(clip.get('pre_normalization_metrics'), expected['pre_normalization_metrics']))
            check('clip_normalization', strict_equal(clip.get('normalization'), expected['normalization']))
        check('clip_lineage_and_provenance', all(k in clip and strict_equal(clip[k], v) for k, v in expected.items()))
        check('clip_has_no_unexpected_fields', set(clip) == set(expected))
        tally['clips'] += 1
        tally['flagged'] += bool(expected['review_flags'])
        seconds['retained'].append(expected['duration_seconds'])
    seconds['uncovered'].extend((g['end_sample'] - g['start_sample']) / RATE for g in gaps)
    tally['processed'] += 1
    return failures


def verify(args):
    """Returns (failure names, aggregate summary)."""
    failures, names = [], []
    check = lambda name, condition: (names.append(name), None if condition else failures.append(name))
    source_root, output_root = Path(args.source_root).resolve(), Path(args.output_root).resolve()
    check('no_incomplete_markers', not any((root / 'INCOMPLETE').exists() for root in (source_root, output_root, Path(args.manifest).resolve().parent)))
    report_path = output_root / 'report.json'
    check('report_is_regular_file', report_path.is_file() and not report_path.is_symlink())
    manifest_bytes = read_bounded(Path(args.manifest), MAX_JSON_BYTES)
    manifest, report = json.loads(manifest_bytes), json.loads(read_bounded(report_path, MAX_JSON_BYTES))
    if not (isinstance(manifest, dict) and isinstance(report, dict)):
        return failures + ['manifest_or_report_unreadable'], None
    rows, sources, clips = manifest.get('files'), report.get('sources'), report.get('clips')
    if not (isinstance(rows, list) and isinstance(sources, list) and isinstance(clips, list) and 0 < len(rows) <= MAX_SOURCES
            and all(isinstance(x, dict) for x in sources + clips) and all(isinstance(c.get('source_path'), str) for c in clips)):
        return failures + ['manifest_or_report_unreadable'], None
    if not rows_valid(rows):
        return failures + ['manifest_rows_not_preparable'], None
    check('report_has_no_unexpected_fields', set(report) == REPORT_KEYS)
    check('report_schema_version', strict_equal(report.get('schema_version'), 1))
    check('report_policy_matches_mode', strict_equal(report.get('policy'), POLICY[args.mode]))
    check('manifest_sha256', report.get('source_manifest_sha256') == sha256(manifest_bytes))
    check('manifest_revision', report.get('source_revision') == manifest.get('revision'))
    check('source_rows_in_manifest_order', [s.get('path') for s in sources] == [r['path'] for r in rows])
    by_source = {}
    for clip in clips:
        by_source.setdefault(clip.get('source_path'), []).append(clip)
    check('clips_reference_manifest_sources', set(by_source) <= {r['path'] for r in rows})
    check('clips_in_source_order', [c.get('source_path') for c in clips] == [r['path'] for r in rows for _ in by_source.get(r['path'], ())])
    tally, seconds, transcripts, originals = Counter(), {'retained': [], 'uncovered': []}, set(), 0
    for row, source in zip(rows, sources):
        try:
            failures.extend(verify_source(row, source, by_source.get(row['path'], []), source_root, output_root,
                                          args.mode, tally, seconds))
        except (OSError, ValueError, *MALFORMED) as error:
            failures.append(f'source_unverifiable:{type(error).__name__}')
        try:
            if 'original_audio_sha256' in row and row.get('original_audio_path') == row['path']:
                check('original_audio_sha256', row['original_audio_sha256'] == row['sha256'])
            elif 'original_audio_sha256' in row or 'original_audio_path' in row:
                original = read_bounded(safe_relative(source_root, row.get('original_audio_path')), MAX_SOURCE_BYTES)
                originals += 1
                check('original_audio_sha256', sha256(original) == row.get('original_audio_sha256'))
            if 'source_transcript_path' in row:
                document = json.loads(read_bounded(safe_relative(source_root, row['source_transcript_path']), MAX_TRANSCRIPT_BYTES))
                transcripts.update(transcript_strings(document))
        except (OSError, ValueError, *MALFORMED) as error:
            failures.append(f'lineage_file_unreadable:{type(error).__name__}')
    values = list(strings(report))
    haystack = '\x00'.join(normal_text(value) for value in values)
    check('no_transcript_text_in_report', not any(text in haystack for text in transcripts))
    check('no_arabic_script_in_report', not any(ARABIC_SCRIPT.search(value) for value in values))
    summary = report.get('summary')
    summary = summary if isinstance(summary, dict) else {}
    # Same float operations in the same order as preparation, so totals compare exactly.
    retained, uncovered = sum(seconds['retained']), sum(seconds['uncovered'])
    check('summary_has_no_unexpected_fields', set(summary) == SUMMARY_KEYS)
    check('summary_totals', all(strict_equal(summary.get(k), v) for k, v in (
        ('sources', len(rows)), ('clips', tally['clips']), ('errors', tally['quarantined']),
        ('review_flagged_clips', tally['flagged']))) and tally['clips'] == len(clips))
    check('summary_seconds', all(type(summary.get(k)) in (int, float) and summary[k] == v
                                 for k, v in (('retained_seconds', retained), ('uncovered_seconds', uncovered))))
    check('summary_outcomes', strict_equal(summary.get('segmentation_outcomes'), {o: tally['outcome:' + o] for o in OUTCOMES}))
    allowed = {'report.json', 'clips', *args.allow_extra}
    entries = {p.name for p in output_root.iterdir()}
    check('output_root_contents', {'report.json', 'clips'} <= entries <= allowed)
    clip_dir = output_root / 'clips'
    check('clips_directory_contents', clip_dir.is_dir() and not clip_dir.is_symlink()
          and all(p.is_file() and not p.is_symlink() for p in clip_dir.iterdir())
          and sorted('clips/' + p.name for p in clip_dir.iterdir()) == sorted(str(c.get('path')) for c in clips))
    aggregate = {'mode': args.mode, 'sources': len(rows), 'processed': tally['processed'], 'quarantined': tally['quarantined'],
                 'clips': tally['clips'], 'flagged': tally['flagged'], 'retained_seconds': round(retained, 3),
                 'uncovered_seconds': round(uncovered, 3), 'originals_hashed': originals,
                 'transcripts_scanned': len(transcripts), 'aggregate_checks': len(set(names))}
    return failures, aggregate


def main(argv=None):
    if not __debug__:
        print('FAIL refusing to run under python -O: optimized mode is not a verified configuration')
        return 2
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--manifest', required=True, type=Path, help='input manifest given to prepare_audio.py')
    parser.add_argument('--source-root', required=True, type=Path)
    parser.add_argument('--output-root', required=True, type=Path, help='published preparation output root')
    parser.add_argument('--mode', required=True, choices=sorted(POLICY), help='condition = --condition run; exact = legacy run')
    parser.add_argument('--allow-extra', action='append', default=[], metavar='NAME',
                        help='permit one named extra entry in the output root, e.g. an earlier verification.json')
    args = parser.parse_args(argv)
    if any(Path(name).name != name or name in ('', '.', '..') for name in args.allow_extra):
        parser.error('--allow-extra takes a plain entry name')
    try:
        failures, aggregate = verify(args)
    except (OSError, ValueError, *MALFORMED) as error:
        failures, aggregate = [f'setup:{type(error).__name__}'], None
    if failures:
        print('FAIL ' + '; '.join(f'{name} x{count}' for name, count in sorted(Counter(failures).items())))
        return 1
    print('PASS ' + ' '.join(f'{key}={value}' for key, value in aggregate.items()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
