"""Balanced, publisher-verified CSALT genuine research intake; no cloning or splits."""
import argparse
import array
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from urllib.parse import quote
from urllib.request import urlopen
import wave

REPO = 'CSALT/deepfake_detection_dataset_urdu'
MAX_BYTES = 10_000_000
FIELDS = ('clip_id','source_id','source_version','speaker_id','source_recording_id',
          'language','label','sha256','duration_s','licence_ref','use_status','split',
          'path','sample_rate','channels','pcm_bits','selected_for_pilot',
          'validation_status','review_flags','label_provenance','listening_status',
          'cloning_status','use_scope','error')


def validate_catalog(catalog):
    if not isinstance(catalog, dict) or not isinstance(catalog.get('revision'),str) or not re.fullmatch('[0-9a-f]{40}',catalog['revision']):
        raise ValueError('Catalog requires a full pinned revision')
    files = catalog.get('files')
    if not isinstance(files,list) or not 1 <= len(files) <= 10000:
        raise ValueError('Catalog requires 1–10000 files')
    seen=set()
    for entry in files:
        if not isinstance(entry,dict):
            raise ValueError('Catalog entries must be objects')
        path=entry.get('path')
        if not isinstance(path,str) or not re.fullmatch(r'Bonafide/Speaker_[0-9]{2}/[A-Za-z0-9_ .\-/]+\.wav',path):
            raise ValueError('Only genuine CSALT speaker WAV paths are supported')
        if any(part in ('','.','..') or part != part.strip(' .') or len(part)>120 for part in path.split('/')):
            raise ValueError('Unsafe catalog path')
        if path.casefold() in seen:
            raise ValueError('Duplicate catalog path')
        seen.add(path.casefold())
        if type(entry.get('size')) is not int or not 0 < entry['size'] <= MAX_BYTES:
            raise ValueError('Invalid published size')
        lfs=entry.get('lfs')
        if not isinstance(lfs,dict) or not isinstance(lfs.get('oid'),str) or not re.fullmatch('[0-9a-f]{64}',lfs['oid']):
            raise ValueError('Missing publisher SHA256')
    return files


def select_entries(catalog, existing_paths, per_speaker):
    files=validate_catalog(catalog)
    if type(per_speaker) is not int or not 1 <= per_speaker <= 100:
        raise ValueError('per_speaker must be an integer in 1..100')
    groups={}
    for entry in files:
        groups.setdefault(entry['path'].split('/')[1],[]).append(entry)
    result=[]
    for speaker, entries in sorted(groups.items()):
        if len(entries)<per_speaker:
            raise ValueError(f'{speaker} has fewer than requested clips')
        entries.sort(key=lambda e:(e['path'] not in existing_paths,
            hashlib.sha256((catalog['revision']+e['path']).encode()).hexdigest(),e['path']))
        result.extend(entries[:per_speaker])
    return result


def bounded_read(path):
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('Input must be a regular file')
    with path.open('rb') as stream:
        payload=stream.read(MAX_BYTES+1)
    if len(payload)>MAX_BYTES:
        raise ValueError('Input exceeds 10 MB limit')
    return payload


def fetch_remote(entry, revision):
    url=f'https://huggingface.co/datasets/{REPO}/resolve/{revision}/'+quote(entry['path'],safe='/')
    with urlopen(url,timeout=90) as response:
        payload=response.read(MAX_BYTES+1)
    return payload


def inspect_audio(payload, entry):
    digest=hashlib.sha256(payload).hexdigest()
    if len(payload)!=entry['size'] or digest!=entry['lfs']['oid']:
        raise ValueError('Publisher size or SHA256 mismatch')
    with wave.open(io.BytesIO(payload),'rb') as audio:
        rate,channels,width,count=audio.getframerate(),audio.getnchannels(),audio.getsampwidth(),audio.getnframes()
        if not 8000<=rate<=192000 or channels not in (1,2) or width!=2:
            raise ValueError('Pilot supports 8–192 kHz mono/stereo PCM16 originals')
        raw=audio.readframes(count)
    if not count or len(raw)!=count*channels*2:
        raise ValueError('Empty or truncated WAV')
    values=array.array('h',raw)
    if sys.byteorder!='little':
        values.byteswap()
    rms=math.sqrt(sum((x/32768)**2 for x in values)/len(values))
    rms_db=20*math.log10(max(rms,1e-12))
    quiet=[]
    window=max(1,round(rate*.02))*channels
    for start in range(0,len(values),window):
        block=values[start:start+window]
        quiet.append(math.sqrt(sum((x/32768)**2 for x in block)/len(block))<.01)
    flags=[]
    if (rate,channels)!=(16000,1): flags.append('requires_format_processing')
    if rms_db < -35: flags.append('low_rms')
    if sum(quiet)/len(quiet)>.5: flags.append('mostly_quiet')
    if sum(abs(x)/32768>=.999 for x in values)/len(values)>.001: flags.append('possible_digital_clipping')
    if not 3<=count/rate<=6: flags.append('requires_duration_processing')
    return {'sha256':digest,'duration_s':count/rate,'sample_rate':rate,'channels':channels,'pcm_bits':width*8,'review_flags':json.dumps(flags)}


def safe_path(root, relative):
    path=root/relative
    if not path.resolve().is_relative_to(root):
        raise ValueError('Source resolves outside source root')
    current=path
    while current!=root:
        if current.is_symlink():
            raise ValueError('Symlink sources unsupported')
        current=current.parent
    return path


def run_pilot(catalog_path, source_root, output_root, per_speaker=20, fetch_bytes=None, expected_speakers=None):
    catalog_payload=bounded_read(Path(catalog_path))
    catalog=json.loads(catalog_payload)
    entries=validate_catalog(catalog)
    speakers={e['path'].split('/')[1] for e in entries}
    if expected_speakers is not None and speakers != set(expected_speakers):
        raise ValueError('Catalog does not match expected speaker inventory')
    source_root,output_root=Path(source_root).resolve(),Path(output_root).resolve()
    repo=Path(__file__).resolve().parents[2]
    if source_root.is_relative_to(repo) or output_root.is_relative_to(repo):
        raise ValueError('Audio and real manifests must stay outside Git workspace')
    if source_root.is_relative_to(output_root) or output_root.is_relative_to(source_root) or output_root.exists():
        raise ValueError('Output must be new and disjoint from sources')
    existing={p.relative_to(source_root).as_posix() for p in source_root.rglob('*.wav')} if source_root.exists() else set()
    chosen=select_entries(catalog,existing,per_speaker)
    selected={entry['path'] for entry in chosen}
    catalog_paths={e['path'] for e in entries}
    work=[e for e in entries if e['path'] in selected or e['path'] in existing]
    # Validate and create destination directories serially before worker activity.
    paths={e['path']:safe_path(source_root,e['path']) for e in work}
    for path in paths.values():
        path.parent.mkdir(parents=True,exist_ok=True)
    output_root.mkdir(parents=True)
    (output_root/'INCOMPLETE').write_text('Do not consume until this marker is removed.',encoding='utf-8')
    fetch_bytes=fetch_bytes or fetch_remote
    def process(entry):
        relative=entry['path']; path=paths[relative]
        row={field:'' for field in FIELDS}
        row.update(clip_id='csalt_'+hashlib.sha256(relative.encode()).hexdigest(),source_id='csalt_urdu',
            source_version=catalog['revision'],speaker_id=relative.split('/')[1],source_recording_id=relative,
            path=relative,language='ur',label='genuine',label_provenance='publisher',
            licence_ref='CC-BY-NC-4.0; https://huggingface.co/datasets/CSALT/deepfake_detection_dataset_urdu',
            use_status='pending',split='unassigned',selected_for_pilot='false',validation_status='failed',
            listening_status='not_individually_reviewed',cloning_status='not_authorized',use_scope='noncommercial_detection_research')
        try:
            if path.exists():
                payload=bounded_read(path)
                info=inspect_audio(payload,entry)
                downloaded=False
            else:
                payload=fetch_bytes(entry,catalog['revision'])
                info=inspect_audio(payload,entry)
                # Stage completely, then atomically link without replacing a file.
                temp_path=None
                try:
                    with tempfile.NamedTemporaryFile(dir=path.parent,prefix='.ava-csalt-',suffix='.part',delete=False) as stream:
                        temp_path=Path(stream.name)
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if hashlib.sha256(bounded_read(temp_path)).hexdigest()!=entry['lfs']['oid']:
                        raise ValueError('Staged download hash mismatch')
                    os.link(temp_path,path)
                finally:
                    if temp_path is not None:
                        temp_path.unlink(missing_ok=True)
                downloaded=True
            row.update(info,use_status='approved',validation_status='verified',selected_for_pilot=str(relative in selected).lower())
            return row,downloaded
        except (OSError,ValueError,wave.Error,EOFError) as error:
            row['error']=str(error)
            return row,False
    rows=[]; downloaded=0
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks=[pool.submit(process,e) for e in work]
        for task in as_completed(tasks):
            row,new=task.result(); rows.append(row); downloaded+=new
    rows.sort(key=lambda r:r['path'])
    balanced=[r for r in rows if r['selected_for_pilot']=='true']
    counts={speaker:sum(r['speaker_id']==speaker for r in balanced) for speaker in sorted({e['path'].split('/')[1] for e in entries})}
    errors=[{'path':r['path'],'error':r['error']} for r in rows if r['validation_status']=='failed']
    summary={'inventory_files':len(rows),'verified_files':sum(r['validation_status']=='verified' for r in rows),
        'selected_files':len(balanced),'speakers':len(counts),'per_speaker':counts,'downloaded_files':downloaded,
        'errors':len(errors),'balanced_complete':all(n==per_speaker for n in counts.values()),
        'selected_minutes':sum(r['duration_s'] for r in balanced)/60,
        'selected_bytes':sum(e['size'] for e in entries if any(r['path']==e['path'] for r in balanced)),
        'review_flagged_selected':sum(json.loads(r['review_flags'])!=[] for r in balanced)}
    summary['selected_native_formats']={fmt:sum(f'{r["sample_rate"]}Hz/{r["channels"]}ch/{r["pcm_bits"]}bit'==fmt for r in balanced)
        for fmt in sorted({f'{r["sample_rate"]}Hz/{r["channels"]}ch/{r["pcm_bits"]}bit' for r in balanced})}
    with (output_root/'inventory.csv').open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=FIELDS); writer.writeheader(); writer.writerows(rows)
    if summary['balanced_complete']:
        (output_root/'balanced.json').write_text(json.dumps({'revision':catalog['revision'],
            'balanced_complete':True,'files':[dict(r,duration_seconds=r['duration_s']) for r in balanced]},indent=2),encoding='utf-8')
    (output_root/'report.json').write_text(json.dumps({'summary':summary,'errors':errors,
        'unregistered_local_paths':sorted(existing-catalog_paths),
        'policy':'Publisher labels retained; automated structural checks for all. Signal flags request targeted review, not individual listening approval. Use approved by project owner for noncommercial detection research; no cloning approval. Split unassigned.',
        'expected_speakers':sorted(expected_speakers) if expected_speakers is not None else None,
        'catalog_sha256':hashlib.sha256(catalog_payload).hexdigest()},indent=2),encoding='utf-8')
    (output_root/'INCOMPLETE').unlink()
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog',required=True,type=Path)
    parser.add_argument('--source-root',required=True,type=Path)
    parser.add_argument('--output-root',required=True,type=Path)
    parser.add_argument('--per-speaker',default=20,type=int)
    args=parser.parse_args()
    try:
        summary=run_pilot(args.catalog,args.source_root,args.output_root,args.per_speaker,
            expected_speakers={f'Speaker_{i:02d}' for i in range(1,18)})
    except (OSError,ValueError) as error:
        print(f'Intake failed: {error}',file=sys.stderr); return 1
    print(json.dumps(summary,indent=2))
    return 0 if summary['balanced_complete'] and not summary['errors'] else 1


if __name__=='__main__':
    raise SystemExit(main())
