"""Bounded offline intake of verified Meta Punjabi train shards; CPU only."""
import argparse
import hashlib
import io
import json
import math
from pathlib import Path
import re
import stat
import sys
import tempfile
import wave

import pyarrow.parquet as pq
import soundfile as sf

if __name__ == '__main__' and not __package__:
    from csalt_pilot import inspect_audio
else:
    from data.collect.csalt_pilot import inspect_audio

REPO = 'facebook/omnilingual-asr-corpus'
REVISION = '8648ba8946377697b427ae952076e49fc0e5e44d'
LANGUAGE = 'pnb_Arab'
SHARDS = {f'data/pnb_Arab/train-{i:05d}-of-00002.parquet' for i in range(2)}
MAX_ARCHIVE_BYTES = 500_000_000
MAX_AUDIO_BYTES = 10_000_000
MAX_METADATA_BYTES = 8_000_000
METADATA_COLUMNS = ('language', 'speaker_id', 'prompt_id', 'prompt', 'segment_id',
                    'raw_text', 'duration', 'iso_639_3', 'glottocode', 'iso_15924')


def _digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _regular(path):
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('Inputs must be regular files, not symlinks')


def _no_links(path):
    for part in (path.absolute(), *path.absolute().parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
            raise ValueError('Symlink or junction paths are unsupported')


def _load_manifest(path):
    _regular(path)
    with path.open('rb') as stream:
        payload = stream.read(MAX_METADATA_BYTES + 1)
    if len(payload) > MAX_METADATA_BYTES:
        raise ValueError('Source manifest exceeds metadata limit')
    document = json.loads(payload)
    if not isinstance(document, dict) or any(document.get(k) != v for k, v in
            (('repo', REPO), ('revision', REVISION), ('language', LANGUAGE),
             ('licence', 'CC-BY-4.0'))):
        raise ValueError('Expected pinned official Punjabi source manifest')
    files = document.get('files')
    if not isinstance(files, list) or not 1 <= len(files) <= 2:
        raise ValueError('Expected one or two train shards')
    seen = set()
    for entry in files:
        if not isinstance(entry, dict) or entry.get('path') not in SHARDS:
            raise ValueError('Only the pinned Punjabi train shard paths are supported')
        if entry['path'] in seen:
            raise ValueError('Duplicate shard')
        seen.add(entry['path'])
        if entry.get('local_name') != entry['path'].split('/')[-1]:
            raise ValueError('Unsafe archive local_name')
        if type(entry.get('size')) is not int or not 0 < entry['size'] <= MAX_ARCHIVE_BYTES:
            raise ValueError('Invalid archive size')
        digest = entry.get('sha256')
        if (not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)
                or entry.get('publisher_lfs_sha256') != digest):
            raise ValueError('Archive hash must match the publisher LFS SHA256')
    return document, hashlib.sha256(payload).hexdigest()


def _metadata(row):
    if any(not isinstance(row.get(key), str) for key in METADATA_COLUMNS if key != 'duration'):
        raise ValueError('Source metadata requires string fields')
    if any(len(row[key]) > 100_000 for key in METADATA_COLUMNS if key != 'duration'):
        raise ValueError('Source metadata string exceeds limit')
    if (row['language'] != LANGUAGE or row['iso_639_3'] != 'pnb'
            or row['iso_15924'] != 'Arab'):
        raise ValueError('Unexpected source language/script metadata')
    # The stable meta_pnb_ prefix must fit preparation's 80-character identifier.
    if not re.fullmatch('[A-Za-z0-9_-]{1,71}', row['speaker_id']):
        raise ValueError('Invalid publisher speaker_id')
    duration = row.get('duration')
    if (type(duration) not in (int, float) or not math.isfinite(duration)
            or not 0 < duration <= 625):
        raise ValueError('Invalid declared duration')
    return row


def select_rows(rows, revision, per_speaker=2, max_sources=30):
    """Deterministic equal allocation without silently dropping speaker groups."""
    if type(per_speaker) is not int or not 1 <= per_speaker <= 30:
        raise ValueError('per_speaker must be an integer in 1..30')
    if type(max_sources) is not int or not 1 <= max_sources <= 30:
        raise ValueError('max_sources must be an integer in 1..30')
    groups = {}
    for row in rows:
        groups.setdefault(row['speaker_id'], []).append(row)
    if not groups or len(groups) * per_speaker > max_sources:
        raise ValueError('Requested equal speaker allocation exceeds source cap or is empty')
    selected = []
    for speaker, candidates in sorted(groups.items()):
        if len(candidates) < per_speaker:
            raise ValueError(f'{speaker} has fewer than requested sources')
        def key(row):
            identity = f"{revision}/{row['source_shard']}#{row['source_row_index']}"
            return hashlib.sha256(identity.encode()).hexdigest()
        selected.extend(sorted(candidates, key=key)[:per_speaker])
    return selected


def _decode(payload, declared_duration):
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_AUDIO_BYTES:
        raise ValueError('Missing or oversized embedded FLAC bytes')
    with sf.SoundFile(io.BytesIO(payload)) as audio:
        rate, channels, frames = audio.samplerate, audio.channels, audio.frames
        if (audio.format != 'FLAC' or audio.subtype != 'PCM_16'
                or not 8000 <= rate <= 192000 or channels not in (1, 2)):
            raise ValueError('Expected native FLAC PCM16, 8–192 kHz mono/stereo')
        if frames <= 0 or frames * channels * 2 + 44 > MAX_AUDIO_BYTES:
            raise ValueError('Decoded WAV exceeds 10 MB limit or is empty')
        if abs(frames / rate - declared_duration) > .1:
            raise ValueError('Declared and decoded durations disagree')
        samples = audio.read(frames, dtype='int16', always_2d=True)
        if len(samples) != frames:
            raise ValueError('Truncated decoded audio')
    result = io.BytesIO()
    with wave.open(result, 'wb') as audio:
        audio.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
        audio.writeframes(samples.astype('<i2', copy=False).tobytes())
    return result.getvalue()


def _row(record, revision, archive_hash):
    recording = f"{record['source_shard']}#{record['source_row_index']}"
    clip_id = 'meta_pnb_' + hashlib.sha256((revision + '/' + recording).encode()).hexdigest()
    return {
        'clip_id': clip_id, 'path': f'wav/{clip_id}.wav',
        'source_id': 'meta_omnilingual_pnb', 'source_version': revision,
        'speaker_id': 'meta_pnb_' + record['speaker_id'],
        'source_speaker_id': record['speaker_id'],
        'source_recording_id': recording, 'source_shard': record['source_shard'],
        'source_shard_sha256': archive_hash, 'source_row_index': record['source_row_index'],
        'original_audio_path': f'flac/{clip_id}.flac',
        'source_prompt_id': record['prompt_id'], 'source_segment_id': record['segment_id'],
        'source_transcript_path': f'transcripts/{clip_id}.json',
        'source_language': LANGUAGE, 'language': 'pnb', 'source_glottocode': record['glottocode'],
        'original_split': 'train', 'publisher_split': 'train',
        'source_partition': 'train', 'dialect': 'unknown',
        'label': 'genuine', 'label_provenance': 'publisher',
        'licence_ref': f'CC-BY-4.0; https://huggingface.co/datasets/{REPO}',
        'use_scope': 'noncommercial_detection_research', 'use_status': 'approved',
        'cloning_status': 'not_authorized', 'consent_status': 'not_individually_verified',
        'speaker_identity_status': 'publisher_id_only', 'split': 'unassigned',
        'listening_status': 'not_individually_reviewed', 'training_eligible': False,
        'validation_status': 'failed',
    }


def _write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False), encoding='utf-8')


def _publish(staged_raw, staged_output, raw_root, output_root, complete):
    """Reserve both new destinations; any partial publication retains markers."""
    raw_root.mkdir()
    (raw_root / 'INCOMPLETE').write_text('Do not consume: publication incomplete.', encoding='utf-8')
    output_root.mkdir()
    (output_root / 'INCOMPLETE').write_text('Do not consume: publication incomplete.', encoding='utf-8')
    for child in staged_raw.iterdir():
        child.rename(raw_root / child.name)
    for child in staged_output.iterdir():
        child.rename(output_root / child.name)
    if complete:
        (raw_root / 'INCOMPLETE').unlink()
        (output_root / 'INCOMPLETE').unlink()


def run_pilot(source_manifest, archive_root, raw_root, output_root, per_speaker=2, max_sources=30):
    source_manifest = Path(source_manifest)
    for path in (source_manifest, Path(archive_root), Path(raw_root), Path(output_root)):
        _no_links(path)
    document, manifest_hash = _load_manifest(source_manifest)
    archive_input = Path(archive_root)
    if archive_input.is_symlink() or not archive_input.is_dir():
        raise ValueError('Archive root must be a regular directory')
    archive_root = archive_input.resolve()
    raw_root, output_root = Path(raw_root).resolve(), Path(output_root).resolve()
    repo = Path(__file__).resolve().parents[2]
    roots = (archive_root, raw_root, output_root)
    if any(root.is_relative_to(repo) for root in (raw_root, output_root)):
        raise ValueError('Raw audio and output must stay outside repository')
    if any(a.is_relative_to(b) or b.is_relative_to(a) for i, a in enumerate(roots) for b in roots[i+1:]):
        raise ValueError('Archive, raw and output roots must be disjoint')
    if raw_root.exists() or output_root.exists():
        raise ValueError('Raw and output roots must be new')
    if any(source_manifest.resolve().is_relative_to(root) for root in (raw_root, output_root)):
        raise ValueError('Outputs overlap source manifest')
    archives = {}
    # Validate every complete archive before parsing metadata or creating outputs.
    for entry in document['files']:
        path = archive_root / entry['local_name']
        _regular(path)
        if path.stat().st_size != entry['size'] or _digest(path) != entry['sha256']:
            raise ValueError('Archive size or publisher SHA256 mismatch')
        archives[entry['path']] = (path, entry['sha256'])
    rows, metadata_bytes = [], 0
    for shard, (path, _) in sorted(archives.items()):
        with pq.ParquetFile(path) as parquet:
            if parquet.metadata.num_rows > 10000 or not set(METADATA_COLUMNS + ('audio',)).issubset(parquet.schema_arrow.names):
                raise ValueError('Unsupported Parquet schema or row count')
            index = 0
            for batch in parquet.iter_batches(batch_size=128, columns=list(METADATA_COLUMNS)):
                for record in batch.to_pylist():
                    _metadata(record)
                    metadata_bytes += sum(len(v.encode('utf-8')) for v in record.values() if isinstance(v, str))
                    if metadata_bytes > MAX_METADATA_BYTES or len(rows) >= 10000:
                        raise ValueError('Pilot metadata exceeds bounded inventory')
                    rows.append(dict(record, source_shard=shard, source_row_index=index))
                    index += 1
    selected = select_rows(rows, document['revision'], per_speaker, max_sources)
    chosen = {(r['source_shard'], r['source_row_index']): r for r in selected}
    raw_root.parent.mkdir(parents=True, exist_ok=True)
    output_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ava-pnb-', dir=raw_root.parent) as raw_temp, \
            tempfile.TemporaryDirectory(prefix='ava-pnb-', dir=output_root.parent) as output_temp:
        staged_raw, staged_output = Path(raw_temp), Path(output_temp)
        for directory in ('flac', 'wav', 'transcripts'):
            (staged_raw / directory).mkdir()
        inventory = []
        for shard, (path, archive_hash) in sorted(archives.items()):
            with pq.ParquetFile(path) as parquet:
                index = 0
                for batch in parquet.iter_batches(batch_size=8, columns=['audio']):
                    for offset in range(batch.num_rows):
                        record = chosen.get((shard, index + offset))
                        if record is None:
                            continue
                        row = _row(record, document['revision'], archive_hash)
                        inventory.append(row)
                        try:
                            value = batch.column(0)[offset].as_py()
                            if not isinstance(value, dict):
                                raise ValueError('Audio must contain embedded bytes')
                            payload = value.get('bytes')
                            decoded = _decode(payload, record['duration'])
                            digest = hashlib.sha256(decoded).hexdigest()
                            metrics = inspect_audio(decoded, {'size': len(decoded), 'lfs': {'oid': digest}})
                            (staged_raw / row['original_audio_path']).write_bytes(payload)
                            (staged_raw / row['path']).write_bytes(decoded)
                            _write_json(staged_raw / row['source_transcript_path'], {
                                'source_recording_id': row['source_recording_id'],
                                'source_version': document['revision'], 'raw_text': record['raw_text'],
                                'prompt': record['prompt'], 'alignment': 'source_recording_only',
                            })
                            row.update(metrics, original_audio_sha256=hashlib.sha256(payload).hexdigest(),
                                       duration_seconds=metrics['duration_s'], validation_status='verified')
                        except (OSError, ValueError, RuntimeError, wave.Error, EOFError) as error:
                            row['error'] = str(error)
                    index += batch.num_rows
        inventory.sort(key=lambda row: row['source_recording_id'])
        verified = [r for r in inventory if r['validation_status'] == 'verified']
        errors = [{'source_recording_id': r['source_recording_id'], 'error': r['error']}
                  for r in inventory if r['validation_status'] != 'verified']
        complete = len(verified) == len(selected) and not errors
        counts = {speaker: sum(r['source_speaker_id'] == speaker for r in verified)
                  for speaker in sorted({r['speaker_id'] for r in selected})}
        summary = {'pilot_complete': complete, 'sources': len(selected), 'selected_files': len(selected),
                   'verified_files': len(verified), 'errors': len(errors), 'speakers': len(counts),
                   'per_speaker': counts, 'selected_minutes': sum(r['duration_s'] for r in verified) / 60,
                   'metadata_rows': len(rows)}
        _write_json(staged_output / 'inventory.json', {'files': inventory})
        _write_json(staged_output / 'report.json', {'summary': summary, 'errors': errors,
                    'source_manifest_sha256': manifest_hash, 'revision': document['revision'],
                    'raw_root': str(raw_root), 'publisher_split': 'train',
                    'policy': 'Publisher-labelled genuine Punjabi; dialect/identity unverified; '
                              'no cloning consent or final training eligibility; source transcripts unaligned to cuts.'})
        if complete:
            _write_json(staged_output / 'pilot.json', {'revision': document['revision'],
                        'pilot_complete': True, 'source_manifest_sha256': manifest_hash, 'files': verified})
        for entry in document['files']:
            path = archives[entry['path']][0]
            if path.stat().st_size != entry['size'] or _digest(path) != entry['sha256']:
                raise ValueError('Archive changed during extraction; refusing publication')
        _publish(staged_raw, staged_output, raw_root, output_root, complete)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source-manifest', 'archive-root', 'raw-root', 'output-root'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--per-speaker', type=int, default=2)
    parser.add_argument('--max-sources', type=int, default=30)
    args = parser.parse_args()
    try:
        summary = run_pilot(args.source_manifest, args.archive_root, args.raw_root,
                            args.output_root, args.per_speaker, args.max_sources)
    except (OSError, ValueError, RuntimeError) as error:
        print(f'Punjabi intake failed: {error}', file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0 if summary['pilot_complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
