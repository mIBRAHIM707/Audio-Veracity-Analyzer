"""Reproducible read-only verification of a published Punjabi (pnb_Arab) pilot import.

Independently of data/collect/meta_pnb_pilot.py this recomputes the archive SHA-256
and size, re-reads every metadata row, recomputes the deterministic per-speaker
selection sha256(f"{revision}/{shard}#{row_index}"), re-reads only the selected
embedded FLAC payloads, decodes them with soundfile and compares bytes, hashes,
WAV frames, durations, transcripts, lineage and policy fields with the published
raw root and manifests. Prints aggregates only. Not listening QA, dialect,
identity, consent or training-eligibility evidence.
"""
import argparse
from collections import Counter
import hashlib
from importlib import metadata
import io
import json
from pathlib import Path
import re
import wave

import pyarrow.parquet as pq
import soundfile as sf

MAX_AUDIO_BYTES, MAX_JSON_BYTES, MAX_TRANSCRIPT_BYTES, MAX_ROWS = 10_000_000, 8_000_000, 1_000_000, 10_000
META = ('speaker_id', 'prompt_id', 'prompt', 'segment_id', 'raw_text', 'duration',
        'language', 'iso_639_3', 'iso_15924', 'glottocode')
POLICY = {'training_eligible': False, 'cloning_status': 'not_authorized',
          'consent_status': 'not_individually_verified', 'dialect': 'unknown',
          'split': 'unassigned', 'publisher_split': 'train', 'original_split': 'train',
          'source_partition': 'train', 'label': 'genuine', 'label_provenance': 'publisher',
          'listening_status': 'not_individually_reviewed', 'validation_status': 'verified',
          'language': 'pnb', 'source_language': 'pnb_Arab', 'use_scope': 'noncommercial_detection_research'}
ROW_CHECKS = 17


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def read_bounded(path, limit):
    with path.open('rb') as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise ValueError(f'{path.name} exceeds {limit} bytes')
    return payload


def clip_id(revision, shard, index):
    return 'meta_pnb_' + hashlib.sha256(f'{revision}/{shard}#{index}'.encode()).hexdigest()


def expected_selection(meta, revision, per_speaker):
    """Pure: meta maps (shard, index) -> record; returns (selected keys, speaker count)."""
    groups = {}
    for key, record in meta.items():
        groups.setdefault(record['speaker_id'], []).append(key)
    selected = set()
    for candidates in groups.values():
        ranked = sorted(candidates, key=lambda k: hashlib.sha256(f'{revision}/{k[0]}#{k[1]}'.encode()).hexdigest())
        selected.update(ranked[:per_speaker])
    return selected, len(groups)


def decode_flac(payload):
    with sf.SoundFile(io.BytesIO(payload)) as audio:
        if audio.format != 'FLAC' or audio.subtype != 'PCM_16':
            raise ValueError('embedded audio is not FLAC PCM_16')
        rate, channels, frames = audio.samplerate, audio.channels, audio.frames
        samples = audio.read(frames, dtype='int16', always_2d=True)
    if frames <= 0 or len(samples) != frames:
        raise ValueError('empty or truncated FLAC decode')
    return rate, channels, frames, samples.astype('<i2', copy=False).tobytes()


def parse_wav(payload):
    with wave.open(io.BytesIO(payload), 'rb') as audio:
        params = (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes())
        return params, audio.readframes(params[3])


def compare_row(row, record, shard, index, revision, archive_sha, payload, decoded, stored):
    """Pure comparison of one pilot row with the shard record and the stored raw files."""
    failures = []
    check = lambda name, condition: None if condition else failures.append(name)
    identity, recording = clip_id(revision, shard, index), f'{shard}#{index}'
    rate, channels, frames, pcm = decoded
    wav_params, wav_pcm = parse_wav(stored['wav'])
    seconds, text = frames / rate, stored['transcript'] if isinstance(stored['transcript'], dict) else {}
    number = lambda value: isinstance(value, (int, float))
    check('flac_bytes_differ', stored['flac'] == payload)
    check('flac_sha256', hashlib.sha256(payload).hexdigest() == row.get('original_audio_sha256'))
    check('wav_sha256', hashlib.sha256(stored['wav']).hexdigest() == row.get('sha256'))
    check('wav_params', wav_params == (rate, channels, 2, frames))
    check('wav_frames_differ', wav_pcm == pcm)
    check('row_audio_fields', (row.get('sample_rate'), row.get('channels'), row.get('pcm_bits')) == (rate, channels, 16))
    check('declared_duration', number(record['duration']) and abs(seconds - record['duration']) <= .1)
    check('row_duration', all(number(row.get(k)) and abs(row[k] - seconds) < 1e-9 for k in ('duration_s', 'duration_seconds')))
    check('transcript_text', (text.get('raw_text'), text.get('prompt')) == (record['raw_text'], record['prompt']))
    check('transcript_alignment', (text.get('alignment'), text.get('source_recording_id'), text.get('source_version'))
          == ('source_recording_only', recording, revision))
    check('speaker_namespace', (row.get('source_speaker_id'), row.get('speaker_id')) == (record['speaker_id'], 'meta_pnb_' + record['speaker_id']))
    check('recording_and_clip_id', (row.get('source_recording_id'), row.get('clip_id'), row.get('source_shard')) == (recording, identity, shard))
    check('row_paths', (row.get('path'), row.get('original_audio_path'), row.get('source_transcript_path'))
          == (f'wav/{identity}.wav', f'flac/{identity}.flac', f'transcripts/{identity}.json'))
    check('source_metadata_fields', (row.get('source_prompt_id'), row.get('source_segment_id'), row.get('source_glottocode'))
          == (record['prompt_id'], record['segment_id'], record['glottocode']))
    check('shard_language', (record['language'], record['iso_639_3'], record['iso_15924']) == ('pnb_Arab', 'pnb', 'Arab'))
    check('archive_lineage', (row.get('source_shard_sha256'), row.get('source_version')) == (archive_sha, revision))
    check('policy_fields', all(row.get(k) == v and type(row.get(k)) is type(v) for k, v in POLICY.items()))
    return failures, (rate, channels, seconds)


def verify(args):
    """Returns (failure names, aggregate summary). Only I/O orchestration lives here."""
    failures, names = [], []
    check = lambda name, condition: (names.append(name), None if condition else failures.append(name))
    raw_root, manifest_root, archive_root = (Path(p).resolve() for p in (args.raw_root, args.manifest_root, args.archive_root))
    check('no_incomplete_markers', not any((root / 'INCOMPLETE').exists() for root in (raw_root, manifest_root)))
    source_bytes = read_bounded(Path(args.source_manifest), MAX_JSON_BYTES)
    pilot, inventory, report = (json.loads(read_bounded(manifest_root / name, MAX_JSON_BYTES))
                                for name in ('pilot.json', 'inventory.json', 'report.json'))
    source = json.loads(source_bytes)
    revision, files = source.get('revision'), source.get('files')
    if not (isinstance(revision, str) and isinstance(files, list) and len(files) == 1 and isinstance(files[0], dict)):
        return failures + ['single_archive_source_manifest_required'], None
    entry, rows, summary = files[0], pilot.get('files'), report.get('summary') or {}
    shard, archive = str(entry.get('path')), archive_root / Path(str(entry.get('local_name'))).name
    archive_sha = sha256_file(archive)
    check('archive_sha256_matches_expected', archive_sha == args.expected_archive_sha256)
    check('archive_size_and_manifest_hash', archive.stat().st_size == entry.get('size')
          and entry.get('sha256') == entry.get('publisher_lfs_sha256') == archive_sha)
    check('revision_consistent', pilot.get('revision') == revision == report.get('revision'))
    manifest_sha = hashlib.sha256(source_bytes).hexdigest()
    check('source_manifest_sha256', pilot.get('source_manifest_sha256') == manifest_sha == report.get('source_manifest_sha256'))
    check('pilot_complete', pilot.get('pilot_complete') is True and isinstance(rows, list) and bool(rows))
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return failures + ['pilot_rows_unreadable'], None
    check('counts_consistent', len(rows) == len(inventory.get('files') or ()) == summary.get('verified_files') and summary.get('errors') == 0)
    prior = json.loads(read_bounded(manifest_root / 'verification.json', MAX_JSON_BYTES)) if (manifest_root / 'verification.json').is_file() else {}
    prior_hashes = prior.get('transcript_sha256') if isinstance(prior, dict) and isinstance(prior.get('transcript_sha256'), dict) else None
    meta, seen, seconds, native, transcript_hashes = {}, 0, 0., Counter(), {}
    with pq.ParquetFile(archive) as parquet:
        if parquet.metadata.num_rows > MAX_ROWS or not set(META + ('audio',)).issubset(parquet.schema_arrow.names):
            return failures + ['unsupported_parquet_schema_or_row_count'], None
        for batch in parquet.iter_batches(batch_size=128, columns=list(META)):
            for record in batch.to_pylist():
                meta[(shard, len(meta))] = record
        check('metadata_rows_match_report', len(meta) == summary.get('metadata_rows'))
        expected, speakers = expected_selection(meta, revision, args.per_speaker)
        by_index = {r['source_row_index']: r for r in rows if type(r.get('source_row_index')) is int}
        check('selection_recomputed', {(shard, i) for i in by_index} == expected and len(by_index) == len(rows))
        check('speaker_count', args.expected_speakers in (None, speakers) and len(rows) == speakers * args.per_speaker)
        check('per_speaker_allocation', Counter(r.get('source_speaker_id') for r in rows) == {m['speaker_id']: args.per_speaker for m in meta.values()})
        index = 0
        for batch in parquet.iter_batches(batch_size=8, columns=['audio']):
            for offset in range(batch.num_rows):
                row, position = by_index.get(index + offset), index + offset
                if row is None:
                    continue
                seen += 1
                try:
                    value = batch.column(0)[offset].as_py()
                    payload = value.get('bytes') if isinstance(value, dict) else None
                    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_AUDIO_BYTES:
                        raise ValueError('missing or oversized embedded FLAC bytes')
                    identity = clip_id(revision, shard, position)
                    transcript_bytes = read_bounded(raw_root / 'transcripts' / f'{identity}.json', MAX_TRANSCRIPT_BYTES)
                    transcript_hashes[f'transcripts/{identity}.json'] = hashlib.sha256(transcript_bytes).hexdigest()
                    stored = {'flac': read_bounded(raw_root / 'flac' / f'{identity}.flac', MAX_AUDIO_BYTES),
                              'wav': read_bounded(raw_root / 'wav' / f'{identity}.wav', MAX_AUDIO_BYTES),
                              'transcript': json.loads(transcript_bytes)}
                    row_failures, (rate, channels, length) = compare_row(
                        row, meta[(shard, position)], shard, position, revision, archive_sha, payload, decode_flac(payload), stored)
                except (OSError, ValueError, RuntimeError, wave.Error, EOFError, KeyError, TypeError) as error:
                    row_failures, (rate, channels, length) = [f'row_unreadable:{type(error).__name__}'], (None, None, 0.)
                failures.extend(row_failures)
                native[(rate, channels)] += 1
                seconds += length
            index += batch.num_rows
    check('all_selected_rows_reread', seen == len(rows))
    minutes = summary.get('selected_minutes')
    check('minutes_match_report', isinstance(minutes, (int, float)) and abs(seconds / 60 - minutes) < 1e-9)
    check('raw_file_counts', all((raw_root / name).is_dir() and sum(1 for _ in (raw_root / name).iterdir()) == len(rows)
                                 for name in ('flac', 'wav', 'transcripts')))
    check('archive_unchanged_after_reread', sha256_file(archive) == archive_sha)
    if prior_hashes is not None:
        check('transcript_hashes_match_prior_verification', prior_hashes == transcript_hashes)
    aggregate = {'rows': len(rows), 'speakers': speakers, 'per_speaker': args.per_speaker, 'minutes': round(seconds / 60, 4),
                 'native_formats': {f'{r}Hz/{c}ch': n for (r, c), n in sorted(native.items(), key=str)},
                 'transcript_hashes': 'recorded' if prior_hashes is None else 'matched_prior',
                 'aggregate_checks': len(names), 'row_checks_per_row': ROW_CHECKS,
                 'archive': {'path': shard, 'size': entry.get('size'), 'sha256': archive_sha}}
    return failures, {'summary': aggregate, 'transcript_sha256': dict(sorted(transcript_hashes.items()))}


def hex_digest(value):
    if not re.fullmatch('[0-9a-fA-F]{64}', value):
        raise argparse.ArgumentTypeError('expected 64 hexadecimal characters')
    return value.lower()


def bounded_int(low, high):
    def parse(value):
        if not low <= int(value) <= high:
            raise argparse.ArgumentTypeError(f'expected an integer in {low}..{high}')
        return int(value)
    return parse


def main(argv=None):
    if not __debug__:
        print('FAIL refusing to run under python -O: optimized mode is not a verified configuration')
        return 2
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ('source-manifest', 'archive-root', 'raw-root', 'manifest-root'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--expected-archive-sha256', required=True, type=hex_digest,
                        help='publisher LFS SHA-256 from the preflight, so source.json cannot self-certify')
    parser.add_argument('--per-speaker', type=bounded_int(1, 30), default=2)
    parser.add_argument('--expected-speakers', type=bounded_int(1, 10_000), default=None)
    parser.add_argument('--write-verification', action='store_true', help='write verification.json into the manifest root; refuses if it exists')
    args = parser.parse_args(argv)
    target = Path(args.manifest_root) / 'verification.json'
    if args.write_verification and target.exists():
        print('FAIL verification.json already exists in the manifest root; refusing to overwrite prior evidence')
        return 1
    try:
        failures, record = verify(args)
    except (OSError, ValueError, RuntimeError) as error:
        failures, record = [f'setup:{type(error).__name__}: {error}'], None
    if failures:
        print('FAIL ' + '; '.join(f'{name} x{count}' for name, count in sorted(Counter(failures).items())))
        return 1
    aggregate = record['summary']
    formats = ','.join(f'{k}:{v}' for k, v in aggregate['native_formats'].items())
    line = ('PASS rows={rows} speakers={speakers} per_speaker={per_speaker} minutes={minutes} native_formats={formats} '
            'transcript_hashes={transcript_hashes} aggregate_checks={aggregate_checks}').format(formats=formats, **aggregate)
    if args.write_verification:
        environment = {'verifier_sha256': sha256_file(Path(__file__).resolve()),
                       **{name: metadata.version(name) for name in ('pyarrow', 'soundfile', 'numpy')}}
        with target.open('x', encoding='utf-8') as stream:
            json.dump({'result': 'PASS', 'scope': 'Independent recomputation of the pilot import; not listening QA, dialect, '
                       'identity, consent or training-eligibility evidence.', 'environment': environment, **record}, stream, indent=2)
    print(line)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
