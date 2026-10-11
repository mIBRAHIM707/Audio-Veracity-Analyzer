"""Import publisher-labelled CSALT synthetic audio for a separate benchmark only."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import wave

if __name__ == '__main__' and not __package__:
    from csalt_pilot import bounded_read, inspect_audio, safe_path, fetch_remote
else:
    from data.collect.csalt_pilot import bounded_read, inspect_audio, safe_path, fetch_remote

FAMILIES = {'Spoofed_TTS': 'vits', 'Spoofed_Tacotron': 'tacotron'}
MAX_BYTES = 10_000_000
FIELDS = (
    'clip_id', 'source_id', 'source_version', 'speaker_id', 'source_recording_id',
    'path', 'language', 'label', 'label_provenance', 'licence_ref', 'use_status',
    'use_scope', 'training_eligible', 'benchmark_only', 'split', 'listening_status',
    'cloning_status', 'generator_family', 'generator_version', 'source_partition',
    'speaker_identity_status', 'target_reference_status', 'validation_status',
    'sha256', 'duration_s', 'sample_rate', 'channels', 'pcm_bits', 'review_flags', 'error',
)


def validate_catalog(catalog):
    if (not isinstance(catalog, dict) or not isinstance(catalog.get('revision'), str)
            or not re.fullmatch('[0-9a-f]{40}', catalog['revision'])):
        raise ValueError('Catalog requires a full pinned revision')
    entries = catalog.get('files')
    if not isinstance(entries, list) or not 1 <= len(entries) <= 10000:
        raise ValueError('Catalog requires 1–10000 files')
    seen = set()
    speakers = {family: set() for family in FAMILIES}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('Catalog entries must be objects')
        path = entry.get('path')
        if (not isinstance(path, str) or not re.fullmatch(
                r'(Spoofed_TTS|Spoofed_Tacotron)/Speaker_[0-9]{2}/[A-Za-z0-9_ .\-/]+\.wav', path)):
            raise ValueError('Only synthetic CSALT speaker WAV paths are supported')
        if any(part in ('', '.', '..') or part != part.strip(' .') or len(part) > 120
               for part in path.split('/')):
            raise ValueError('Unsafe catalog path')
        if path.casefold() in seen:
            raise ValueError('Duplicate catalog path')
        seen.add(path.casefold())
        family, speaker = path.split('/')[:2]
        speakers[family].add(speaker)
        if type(entry.get('size')) is not int or not 0 < entry['size'] <= MAX_BYTES:
            raise ValueError('Invalid published size')
        lfs = entry.get('lfs')
        if (not isinstance(lfs, dict) or not isinstance(lfs.get('oid'), str)
                or not re.fullmatch('[0-9a-f]{64}', lfs['oid'])):
            raise ValueError('Missing publisher SHA256')
    inventories = list(speakers.values())
    if not all(inventories) or inventories[0] != inventories[1]:
        raise ValueError('Both generator families require the same speaker inventory')
    return entries


def select_entries(catalog, per_group=2):
    entries = validate_catalog(catalog)
    if type(per_group) is not int or not 1 <= per_group <= 10:
        raise ValueError('per_group must be an integer in 1..10')
    groups = {}
    for entry in entries:
        groups.setdefault('/'.join(entry['path'].split('/')[:2]), []).append(entry)
    selected = []
    for group, candidates in sorted(groups.items()):
        if len(candidates) < per_group:
            raise ValueError(f'{group} has fewer than requested clips')
        selected.extend(sorted(candidates, key=lambda entry: (
            hashlib.sha256((catalog['revision'] + entry['path']).encode()).hexdigest(),
            entry['path']))[:per_group])
    return selected


def run_benchmark(catalog_path, source_root, output_root, per_group=2,
                  fetch_bytes=None, expected_speakers=None):
    catalog_payload = bounded_read(Path(catalog_path))
    catalog = json.loads(catalog_payload)
    entries = validate_catalog(catalog)
    speakers = {entry['path'].split('/')[1] for entry in entries}
    expected = set(expected_speakers) if expected_speakers is not None else None
    if expected is not None and speakers != expected:
        raise ValueError('Catalog does not match expected speaker inventory')
    chosen = select_entries(catalog, per_group)
    source_root, output_root = Path(source_root).resolve(), Path(output_root).resolve()
    repo = Path(__file__).resolve().parents[2]
    if source_root.is_relative_to(repo) or output_root.is_relative_to(repo):
        raise ValueError('Audio and real manifests must stay outside Git workspace')
    if (source_root.is_relative_to(output_root) or output_root.is_relative_to(source_root)
            or output_root.exists()):
        raise ValueError('Output must be new and disjoint from sources')
    paths = {entry['path']: safe_path(source_root, entry['path']) for entry in chosen}
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True)
    marker = output_root / 'INCOMPLETE'
    marker.write_text('Do not consume until this marker is removed.', encoding='utf-8')
    fetch_bytes = fetch_bytes or fetch_remote

    def process(entry):
        relative = entry['path']
        partition, speaker = relative.split('/')[:2]
        path = paths[relative]
        row = {field: '' for field in FIELDS}
        row.update(
            clip_id='csalt_' + hashlib.sha256(relative.encode()).hexdigest(),
            source_id='csalt_urdu', source_version=catalog['revision'], speaker_id=speaker,
            source_recording_id=relative, path=relative, language='ur', label='synthetic',
            label_provenance='publisher',
            licence_ref='CC-BY-NC-4.0; https://huggingface.co/datasets/CSALT/deepfake_detection_dataset_urdu',
            use_status='pending', use_scope='noncommercial_benchmark_only',
            training_eligible=False, benchmark_only=True, split='unassigned',
            listening_status='not_individually_reviewed', cloning_status='not_authorized',
            generator_family=FAMILIES[partition], generator_version='unknown',
            source_partition=partition, speaker_identity_status='publisher_folder_only',
            target_reference_status='unknown', validation_status='failed')
        try:
            downloaded = False
            if path.exists():
                info = inspect_audio(bounded_read(path), entry)
            else:
                payload = fetch_bytes(entry, catalog['revision'])
                info = inspect_audio(payload, entry)
                temp_path = None
                try:
                    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.ava-csalt-',
                                                     suffix='.part', delete=False) as stream:
                        temp_path = Path(stream.name)
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if hashlib.sha256(bounded_read(temp_path)).hexdigest() != entry['lfs']['oid']:
                        raise ValueError('Staged download hash mismatch')
                    os.link(temp_path, path)
                finally:
                    if temp_path is not None:
                        temp_path.unlink(missing_ok=True)
                downloaded = True
            row.update(info, use_status='approved', validation_status='verified')
            return row, downloaded
        except (OSError, ValueError, wave.Error, EOFError) as error:
            row['error'] = str(error)
            return row, False

    rows, downloaded = [], 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks = [pool.submit(process, entry) for entry in chosen]
        for task in as_completed(tasks):
            row, new = task.result()
            rows.append(row)
            downloaded += new
    rows.sort(key=lambda row: row['path'])
    verified = [row for row in rows if row['validation_status'] == 'verified']
    counts = {f'{family}/{speaker}': sum(
        row['source_partition'] == family and row['speaker_id'] == speaker for row in verified)
        for family in sorted(FAMILIES) for speaker in sorted(speakers)}
    errors = [{'path': row['path'], 'error': row['error']} for row in rows
              if row['validation_status'] == 'failed']
    formats = {}
    for row in verified:
        key = f'{row["sample_rate"]}Hz/{row["channels"]}ch/{row["pcm_bits"]}bit'
        formats[key] = formats.get(key, 0) + 1
    summary = {
        'sources': len(rows), 'verified_files': len(verified), 'selected_files': len(verified),
        'downloaded_files': downloaded, 'errors': len(errors),
        'benchmark_complete': all(count == per_group for count in counts.values()),
        'per_group': counts, 'selected_minutes': sum(row['duration_s'] for row in verified) / 60,
        'selected_native_formats': dict(sorted(formats.items())),
    }
    with (output_root / 'inventory.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row, benchmark_only='true', training_eligible='false'))
    if summary['benchmark_complete']:
        (output_root / 'benchmark.json').write_text(json.dumps({
            'revision': catalog['revision'], 'benchmark_complete': True,
            'benchmark_only': True, 'training_eligible': False,
            'files': [dict(row, duration_seconds=row['duration_s']) for row in verified],
        }, indent=2), encoding='utf-8')
    (output_root / 'report.json').write_text(json.dumps({
        'summary': summary, 'errors': errors,
        'catalog_sha256': hashlib.sha256(catalog_payload).hexdigest(),
        'expected_speakers': sorted(expected) if expected is not None else None,
        'policy': 'Publisher labels; noncommercial benchmark only, not training eligible. '
                  'Speaker folders are unverified identity groupings. Generator versions and '
                  'target references unknown. No individual listening or cloning approval.',
    }, indent=2), encoding='utf-8')
    marker.unlink()
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', required=True, type=Path)
    parser.add_argument('--source-root', required=True, type=Path)
    parser.add_argument('--output-root', required=True, type=Path)
    parser.add_argument('--per-group', default=2, type=int)
    args = parser.parse_args()
    try:
        summary = run_benchmark(args.catalog, args.source_root, args.output_root, args.per_group,
                                expected_speakers={f'Speaker_{i:02d}' for i in range(1, 18)})
    except (OSError, ValueError) as error:
        print(f'Benchmark intake failed: {error}', file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0 if summary['benchmark_complete'] and not summary['errors'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
