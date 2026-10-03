"""Prepare immutable PCM WAV sources into review-only, pause-bounded candidates."""
import argparse
import array
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import sys
import stat
import tempfile
import wave

RATE = 16000
MIN_SAMPLES = 3 * RATE
MAX_SAMPLES = 6 * RATE
WINDOW = 320
MAX_SOURCE_BYTES = 10_000_000
MAX_MANIFEST_BYTES = 8_000_000


def preflight(manifest_path, source_root, output_root):
    source_root = Path(source_root).resolve()
    output_root = Path(output_root).resolve()
    if not source_root.is_dir():
        raise ValueError('Source root must be an existing directory')
    if output_root.exists() or output_root.is_relative_to(source_root) or source_root.is_relative_to(output_root):
        raise ValueError('Output must be new and disjoint from source root')
    repo = Path(__file__).resolve().parents[2]
    if output_root.is_relative_to(repo):
        raise ValueError('Real preparation output must be outside repository')
    manifest_path = Path(manifest_path).resolve()
    if manifest_path.is_relative_to(output_root):
        raise ValueError('Output overlaps input manifest')
    if not stat.S_ISREG(manifest_path.stat().st_mode):
        raise ValueError('Manifest must be a regular file')
    with manifest_path.open('rb') as stream:
        document = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(document) > MAX_MANIFEST_BYTES:
        raise ValueError('Manifest exceeds 8 MB pilot limit')
    manifest = json.loads(document)
    if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), list) or not manifest['files']:
        raise ValueError('Manifest requires a nonempty files list')
    if len(manifest['files']) > 10000:
        raise ValueError('Pilot manifest exceeds 10000 sources')
    seen = set()
    for row in manifest['files']:
        if not isinstance(row, dict):
            raise ValueError('Each source must be an object')
        path = row.get('path')
        if not isinstance(path, str) or not path or '\\' in path or ':' in path or '\x00' in path:
            raise ValueError('Source paths must be safe relative POSIX paths')
        parts = path.split('/')
        if PurePosixPath(path).is_absolute() or any(p in ('', '.', '..') for p in parts):
            raise ValueError('Source path traversal or empty component')
        if path.casefold() in seen:
            raise ValueError('Duplicate source path')
        seen.add(path.casefold())
        if not isinstance(row.get('speaker_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', row['speaker_id']):
            raise ValueError('Invalid speaker_id')
        if not isinstance(row.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', row['sha256']):
            raise ValueError('Invalid source SHA256')
    return manifest, source_root, output_root


def measures(samples):
    def db(value):
        return 20 * math.log10(max(value, 1e-12))
    rms = db(math.sqrt(sum((x / 32768) ** 2 for x in samples) / len(samples)))
    quiet = []
    for start in range(0, len(samples), WINDOW):
        block = samples[start:start + WINDOW]
        quiet.append(db(math.sqrt(sum((x / 32768) ** 2 for x in block) / len(block))) < -40)
    fraction = sum(quiet) / len(quiet)
    near_full = sum(abs(x) / 32768 >= .999 for x in samples) / len(samples)
    flags = []
    if rms < -35:
        flags.append('low_rms')
    if fraction > .5:
        flags.append('mostly_quiet')
    if near_full > .001:
        flags.append('possible_digital_clipping')
    return {'rms_dbfs': round(rms, 3), 'quiet_fraction': round(fraction, 5),
            'near_full_scale_fraction': round(near_full, 5), 'review_flags': flags}, quiet


def segment_ranges(samples, quiet):
    """Internal boundaries use centers of quiet runs >=120ms; never forced cuts."""
    if all(quiet):
        return []
    boundaries = [0]
    i = 0
    while i < len(quiet):
        if not quiet[i]:
            i += 1
            continue
        start = i
        while i < len(quiet) and quiet[i]:
            i += 1
        # Ignore leading/trailing quiet runs: preserve the full recording edges.
        if start and i < len(quiet) and (i-start)*WINDOW >= 1920:
            boundaries.append((start+i)*WINDOW//2)
    boundaries.append(len(samples))
    # Dynamic program maximizes retained samples over consecutive pause boundaries.
    best = [(0, []) for _ in boundaries]
    for end in range(1, len(boundaries)):
        best[end] = best[end-1]
        for start in range(end-1, -1, -1):
            length = boundaries[end]-boundaries[start]
            if length > MAX_SAMPLES:
                break
            if length >= MIN_SAMPLES:
                score = best[start][0]+length
                if score > best[end][0]:
                    best[end] = (score, best[start][1]+[(boundaries[start], boundaries[end])])
    return best[-1][1]


def uncovered(total, ranges):
    result, cursor = [], 0
    for start, end in ranges:
        if start > cursor:
            result.append({'start_sample': cursor, 'end_sample': start})
        cursor = end
    if cursor < total:
        result.append({'start_sample': cursor, 'end_sample': total})
    return result


def process_manifest(manifest_path, source_root, output_root):
    manifest, source_root, output_root = preflight(manifest_path, source_root, output_root)
    output_root.parent.mkdir(parents=True, exist_ok=True)
    sources, clips = [], []
    # A fresh staging directory is published only once its complete report exists.
    with tempfile.TemporaryDirectory(prefix='ava-prep-', dir=output_root.parent) as temp:
        staging = Path(temp) / 'result'
        (staging / 'clips').mkdir(parents=True)
        for row in manifest['files']:
            source = {'path': row['path'], 'speaker_id': row['speaker_id'],
                      'source_sha256': row['sha256'], 'status': 'quarantined'}
            sources.append(source)
            try:
                path = source_root / row['path']
                if not path.resolve().is_relative_to(source_root):
                    raise ValueError('Source resolves outside source root')
                current = path
                while current != source_root:
                    if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
                        raise ValueError('Symlink or junction sources are unsupported')
                    current = current.parent
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('Source must be a regular file')
                if info.st_size > MAX_SOURCE_BYTES:
                    raise ValueError('Source exceeds 10 MB pilot limit')
                with path.open('rb') as stream:
                    payload = stream.read(MAX_SOURCE_BYTES + 1)
                if len(payload) > MAX_SOURCE_BYTES:
                    raise ValueError('Source exceeds 10 MB pilot limit')
                if hashlib.sha256(payload).hexdigest() != row['sha256']:
                    raise ValueError('Source hash mismatch')
                import io
                with wave.open(io.BytesIO(payload), 'rb') as audio:
                    if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (RATE, 1, 2):
                        raise ValueError('Expected 16kHz mono PCM16 WAV')
                    count = audio.getnframes()
                    raw = audio.readframes(count)
                if not count or len(raw) != count*2:
                    raise ValueError('Empty or truncated WAV')
                samples = array.array('h', raw)
                if sys.byteorder != 'little':
                    samples.byteswap()
                metrics, quiet = measures(samples)
                ranges = segment_ranges(samples, quiet)
            except (OSError, ValueError, wave.Error, EOFError) as error:
                source['error'] = str(error)
                continue
            source.update(status='processed', sample_count=count, metrics=metrics,
                          uncovered_ranges=uncovered(count, ranges))
            source['segmentation_outcome'] = ('all_quiet' if all(quiet) else
                'too_short' if count < MIN_SAMPLES else
                'no_valid_pause_segments' if not ranges else
                'partial_coverage' if source['uncovered_ranges'] else 'full_coverage')
            for start, end in ranges:
                key = hashlib.sha256(row['path'].encode()).hexdigest()[:16]
                relative = f'clips/{key}_{start}_{end}.wav'
                out = staging / relative
                with wave.open(str(out), 'wb') as audio:
                    audio.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
                    audio.writeframes(raw[start*2:end*2])
                metrics, _ = measures(samples[start:end])
                clips.append({'path': relative, 'speaker_id': row['speaker_id'],
                              'source_path': row['path'], 'source_sha256': row['sha256'],
                              'start_sample': start, 'end_sample': end,
                              'duration_seconds': (end-start)/RATE,
                              'sha256': hashlib.sha256(out.read_bytes()).hexdigest(),
                              'review_status': 'pending_listening', **metrics})
        summary = {'sources': len(sources), 'clips': len(clips),
                   'errors': sum(s['status'] == 'quarantined' for s in sources),
                   'review_flagged_clips': sum(bool(c['review_flags']) for c in clips),
                   'retained_seconds': sum(c['duration_seconds'] for c in clips),
                   'uncovered_seconds': sum((r['end_sample']-r['start_sample'])/RATE for s in sources for r in s.get('uncovered_ranges', []))}
        summary['segmentation_outcomes'] = {reason: sum(s.get('segmentation_outcome') == reason for s in sources)
            for reason in ('all_quiet', 'too_short', 'no_valid_pause_segments', 'partial_coverage', 'full_coverage')}
        report = {'schema_version': 1, 'source_revision': manifest.get('revision'),
                  'policy': {'sample_rate': RATE, 'min_seconds': 3, 'max_seconds': 6,
                             'quiet_dbfs': -40, 'window_ms': 20, 'min_pause_ms': 120,
                             'low_rms_dbfs': -35, 'mostly_quiet_fraction': .5,
                             'near_full_scale_amplitude': .999, 'clipping_fraction': .001,
                             'normalization': 'none; exact source samples',
                             'acceptance': 'candidates only; energy pauses are not verified word boundaries'},
                  'summary': summary, 'sources': sources, 'clips': clips}
        (staging / 'report.json').write_text(json.dumps(report, indent=2, sort_keys=True), encoding='utf-8')
        # Atomic reservation prevents overwriting a concurrent destination.
        output_root.mkdir()
        marker = output_root / 'INCOMPLETE'
        marker.write_text('Publication incomplete; do not consume this directory.', encoding='utf-8')
        (staging / 'clips').rename(output_root / 'clips')
        (staging / 'report.json').rename(output_root / 'report.json')
        marker.unlink()
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--source-root', required=True, type=Path)
    parser.add_argument('--output-root', required=True, type=Path)
    args = parser.parse_args()
    try:
        summary = process_manifest(args.manifest, args.source_root, args.output_root)
    except (OSError, ValueError) as error:
        print(f'Preparation failed: {error}', file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 1 if summary['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
