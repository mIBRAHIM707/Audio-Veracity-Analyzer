"""Recorded telephone/noise augmentation of prepared clips (backlog 3.3/3.4 wiring;
PRD FR-D6 chain recorded per clip, FR-D7 noise, FR-D13 lineage). CPU, no network.

A prepared output root (from `prepare_audio.py --condition`) is never modified. Each of
its 16 kHz clips is passed through one declared chain and written as an *augmented copy*
to a new output root with a report that binds every copy to its parent clip, the chain
specification and the full per-step records:

    optional additive noise at 16 kHz (noise.mix_planned, before the channel: the
    environment is heard by the handset) -> G.711 telephone roundtrip with per-clip
    loss and jitter plans (codecs.telephone_roundtrip) -> back to the 16 kHz model grid
    through the shared conditioning.canonicalize.

No peak normalization follows the chain (the analysis service applies none either).
The concealment rule has no default: the specification must name it, so a chain meant
to match the serving buffer says 'repeat_then_silence' explicitly. The derivation clip
ID is `<source_sha256>:<start_sample>:<end_sample>` of the parent row (content-based). Copies are stored but not counted toward the FR-D6 clip total.
A copy's `start_sample`/`end_sample` name the parent's range on the source grid; the
16 -> 8 -> 16 kHz return can make an odd-length copy one sample shorter or longer, so its
own length is `chain.output_sample_count` (and `duration_seconds`), not end - start.
Copies start `review_status: unreviewed`; the parent's review and listening statuses are
kept as `parent_review_status`/`parent_listening_status` and are not inherited.
A noise mix that would clip is refused per clip with its drawn SNR recorded, so the
refusal rate per SNR band can be audited (it can bias the FR-D7 0-20 dB spread).
The channel may instead be GSM 06.10 or AMR-NB 4.75/7.4/12.2 (`ffmpeg_codecs`, a pinned
local ffmpeg passed as `ffmpeg`/`--ffmpeg`); both may take seeded frame loss and
jitter (late frames) concealed once (AMR-NB: decoder-native NO_DATA, concealment null;
GSM: repeat_then_silence substitution, which the spec must name). A codec-tool failure aborts
the run before anything is written. Opus/G.729 and room impulse responses are not
implemented.
"""
import argparse
import array
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import wave

if __package__:
    from .codecs import CONCEALMENTS, LAWS, derive_jitter, derive_loss, telephone_roundtrip
    from .conditioning import RATE, canonicalize
    from .ffmpeg_codecs import CODECS as FFMPEG_CODECS, CodecToolError, Ffmpeg, telephone_codec_roundtrip
    from .noise import derive_noise, mix_planned
    from .prepare_audio import PROVENANCE_FIELDS, measures
    from .wav_format import pcm_format_problem
else:  # a script-directory 'codecs' import would shadow the standard library module
    raise SystemExit('Run as a module: python -m data.prepare.augment ...')

CHAIN_VERSION = 'ava-telephone-chain-v1'
G711_CODECS = tuple(f'g711_{law}' for law in LAWS)


def needs_ffmpeg(spec):
    return isinstance(spec, dict) and isinstance(spec.get('codec'), str) and spec['codec'] in FFMPEG_CODECS


def recorded_tool_hash(report):
    """The single codec-tool (ffmpeg) SHA-256 recorded in an augmented report's copies,
    None when no copy records one (G.711), or 'mixed' when copies disagree. The chain
    spec hash does not name the tool, and GSM/AMR output depends on it."""
    hashes = set()
    for row in report.get('clips') or []:
        steps = row.get('chain', {}).get('steps', []) if isinstance(row, dict) else []
        for step in steps if isinstance(steps, list) else []:
            if isinstance(step, dict) and step.get('ffmpeg_sha256') is not None:
                hashes.add(step['ffmpeg_sha256'])
    return None if not hashes else hashes.pop() if len(hashes) == 1 else 'mixed'
SPEC_KEYS = {'chain_version', 'master_seed', 'codec', 'concealment', 'packet_loss',
             'jitter', 'noise'}
NOISE_KEYS = {'sha256', 'label', 'source_id', 'licence_ref'}
CLIP_PATH = re.compile(r'clips/[A-Za-z0-9_-][A-Za-z0-9_.-]*\.wav')
MAX_REPORT_BYTES = 64_000_000
MAX_CLIP_BYTES = 1_000_000
MAX_NOISE_BYTES = 50_000_000
# Listening/review QA of a parent does not carry over to a processed copy (FR-D13):
# copies start 'unreviewed' and keep the parent's values under parent_* names.
REVIEW_FIELDS = ('review_status', 'listening_status')
PARENT_FIELDS = ('speaker_id', 'source_path', 'source_sha256', 'start_sample',
                 'end_sample', 'source_clip_id') + tuple(f for f in PROVENANCE_FIELDS
                                                         if f not in REVIEW_FIELDS)


class NoiseClipped(ValueError):
    """The planned noise mix would clip; the refusal keeps the drawn SNR so refusal
    rates per SNR band (FR-D7 0-20 dB spread) can be audited."""

    def __init__(self, snr_db, clipped):
        super().__init__(f'noise mix at {snr_db} dB SNR clipped {clipped} samples; refused')
        self.snr_db, self.clipped = snr_db, clipped


def derivation_id(row):
    """Content-based derivation clip ID from a parent clip row: the source audio hash and
    the half-open canonical sample range, so identically named files in different corpora
    do not share draws and a rerun of the same source segment gets the same draws."""
    source, start, end = row.get('source_sha256'), row.get('start_sample'), row.get('end_sample')
    if not isinstance(source, str) or not re.fullmatch(r'[0-9a-f]{64}', source) \
            or type(start) is not int or type(end) is not int or not 0 <= start < end:
        raise ValueError('Parent clip row needs source_sha256 and an integer sample range')
    return f'{source}:{start}:{end}'


def validate_spec(spec):
    """Return the specification unchanged or raise ValueError listing every problem."""
    if not isinstance(spec, dict):
        raise ValueError('chain specification must be an object')
    problems = []
    missing, unknown = SPEC_KEYS - spec.keys(), spec.keys() - SPEC_KEYS
    if missing:
        problems.append(f'missing keys {sorted(missing)}')
    if unknown:
        problems.append(f'unknown keys {sorted(unknown)}')
    if 'chain_version' in spec and spec['chain_version'] != CHAIN_VERSION:
        problems.append(f'chain_version must be {CHAIN_VERSION!r}')
    seed = spec.get('master_seed')
    if 'master_seed' in spec and (type(seed) is not int or seed < 0):
        problems.append('master_seed must be a non-negative integer')
    codec = spec.get('codec') if isinstance(spec.get('codec'), str) else None
    if 'codec' in spec and codec not in G711_CODECS + tuple(FFMPEG_CODECS):
        problems.append(f'codec must be one of {list(G711_CODECS + tuple(FFMPEG_CODECS))}')
    if codec in FFMPEG_CODECS:
        # GSM/AMR: AMR conceals natively (NO_DATA), so its spec names no concealment; a
        # GSM chain with loss or jitter must name the rule it gets ('repeat_then_silence',
        # as G.711 specs must), so the spec hash covers it.
        for key in ('packet_loss', 'jitter'):
            if type(spec.get(key)) is not bool:
                problems.append(f'{key} must be true or false')
        impaired = spec.get('packet_loss') is True or spec.get('jitter') is True
        wanted = 'repeat_then_silence' if codec == 'gsm' and impaired else None
        if spec.get('concealment') != wanted:
            problems.append(f'concealment must be {wanted!r} for {codec} with packet_loss '
                            f'{spec.get("packet_loss")!r} and jitter {spec.get("jitter")!r}')
    else:
        if 'concealment' in spec and spec['concealment'] not in CONCEALMENTS:
            problems.append(f'concealment must be one of {CONCEALMENTS}')
        for key in ('packet_loss', 'jitter'):
            if key in spec and type(spec[key]) is not bool:
                problems.append(f'{key} must be true or false')
    noise = spec.get('noise')
    if noise is not None:
        if not isinstance(noise, dict) or noise.keys() != NOISE_KEYS:
            problems.append(f'noise must be null or an object with keys {sorted(NOISE_KEYS)}')
        else:
            if not isinstance(noise['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', noise['sha256']):
                problems.append('noise.sha256 must be a lowercase SHA-256 hex digest')
            for key in ('label', 'source_id', 'licence_ref'):
                if not isinstance(noise[key], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', noise[key]):
                    problems.append(f'noise.{key} must be a short identifier')
    if problems:
        raise ValueError('invalid chain specification: ' + '; '.join(problems))
    return spec


def spec_sha256(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _check_ffmpeg(spec, ffmpeg):
    if needs_ffmpeg(spec) and ffmpeg is None:
        raise ValueError(f'codec {spec["codec"]} needs an ffmpeg (data/prepare/ffmpeg_codecs.Ffmpeg)')


def augment_clip(clip_pcm, clip_id, spec, noise_pcm=None, ffmpeg=None):
    """Apply the declared chain to one 16 kHz mono PCM16 clip. Returns (16 kHz PCM16
    bytes, record). `noise_pcm` must be 16 kHz mono PCM16 whose SHA-256 the spec names
    (exactly when the spec asks for noise). GSM/AMR-NB codecs need `ffmpeg` (an
    `ffmpeg_codecs.Ffmpeg`). Raises ValueError on invalid input and CodecToolError when
    the codec tool fails (a systemic fault: callers abort rather than list it per clip)."""
    validate_spec(spec)
    _check_ffmpeg(spec, ffmpeg)
    if not isinstance(clip_pcm, bytes) or not clip_pcm or len(clip_pcm) % 2:
        raise ValueError('clip must be nonempty mono PCM16 bytes')
    if (spec['noise'] is None) != (noise_pcm is None):
        raise ValueError('noise audio must be given exactly when the specification names noise')
    steps, current = [], clip_pcm
    if spec['noise'] is not None:
        if not isinstance(noise_pcm, bytes) or hashlib.sha256(noise_pcm).hexdigest() != spec['noise']['sha256']:
            raise ValueError('noise audio does not match the specification hash')
        current, record = mix_planned(current, noise_pcm, derive_noise(spec['master_seed'], clip_id))
        if record['clipped_samples']:
            raise NoiseClipped(record['snr_db'], record['clipped_samples'])
        steps.append({'step': 'additive_noise', **{f'noise_{k}': v for k, v in spec['noise'].items()
                                                   if k != 'sha256'}, **record})
    if needs_ffmpeg(spec):
        narrow, record = telephone_codec_roundtrip(
            current, spec['codec'], ffmpeg, rate=RATE,
            loss_plan=derive_loss(spec['master_seed'], clip_id) if spec['packet_loss'] else None,
            jitter_plan=derive_jitter(spec['master_seed'], clip_id) if spec['jitter'] else None)
    else:
        law = spec['codec'].removeprefix('g711_')
        narrow, record = telephone_roundtrip(
            current, law, rate=RATE, concealment=spec['concealment'],
            loss_plan=derive_loss(spec['master_seed'], clip_id) if spec['packet_loss'] else None,
            jitter_plan=derive_jitter(spec['master_seed'], clip_id) if spec['jitter'] else None)
    steps.append({'step': 'telephone_roundtrip', **record})
    wide, record = canonicalize(narrow, 8000, 1)
    steps.append({'step': 'return_to_model_grid', **record})
    return wide, {
        'chain_version': CHAIN_VERSION, 'chain_spec_sha256': spec_sha256(spec),
        'derivation_clip_id': clip_id, 'input_pcm_sha256': hashlib.sha256(clip_pcm).hexdigest(),
        'input_sample_count': len(clip_pcm) // 2, 'output_pcm_sha256': hashlib.sha256(wide).hexdigest(),
        'output_sample_count': len(wide) // 2, 'post_chain_normalization': 'none',
        'steps': steps,
    }


def _plain_dir(path, what):
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()) or not path.is_dir():
        raise ValueError(f'{what} must be a plain directory, not a symlink or junction')


def _read_regular(path, limit, what):
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise ValueError(f'{what} must not be a symlink or junction')
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f'{what} must be a regular file')
    with path.open('rb') as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise ValueError(f'{what} exceeds {limit} bytes')
    return payload


def read_pcm16_wav(payload, what):
    """16 kHz mono PCM16 WAV bytes -> raw PCM bytes."""
    problem = pcm_format_problem(payload)
    if problem:
        raise ValueError(f'{what} must be a plain PCM WAV: {problem}')
    try:
        with wave.open(io.BytesIO(payload), 'rb') as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (RATE, 1, 2):
                raise ValueError(f'{what} must be 16 kHz mono PCM16 WAV')
            count = audio.getnframes()
            raw = audio.readframes(count)
    except (wave.Error, EOFError) as error:
        raise ValueError(f'{what} is not a readable WAV: {str(error) or type(error).__name__}') from None
    if not count or len(raw) != count * 2:
        raise ValueError(f'{what} is empty or truncated')
    return raw


def _wav_bytes(raw):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as audio:
        audio.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        audio.writeframes(raw)
    return buffer.getvalue()


def _load_json(document, what):
    try:
        return json.loads(document)
    except RecursionError:
        raise ValueError(f'{what} JSON is nested too deeply') from None


def _load_parent(prepared_root):
    prepared_root = Path(prepared_root).resolve()
    if not prepared_root.is_dir():
        raise ValueError('Prepared root must be an existing directory')
    if (prepared_root / 'INCOMPLETE').exists():
        raise ValueError('Prepared output publication is incomplete')
    document = _read_regular(prepared_root / 'report.json', MAX_REPORT_BYTES, 'Prepared report')
    report = _load_json(document, 'Prepared report')
    if not isinstance(report, dict) or not isinstance(report.get('clips'), list):
        raise ValueError('Prepared report requires a clips list')
    if report.get('kind') == 'augmented_copies':
        raise ValueError('Prepared root is already an augmented-copy root; chains are not stacked')
    paths = set()
    for row in report['clips']:
        path = row.get('path') if isinstance(row, dict) else None
        if not isinstance(path, str) or not CLIP_PATH.fullmatch(path) or path.casefold() in paths:
            raise ValueError('Prepared report clip paths must be unique clips/<name>.wav entries')
        if not isinstance(row.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', row['sha256']):
            raise ValueError('Prepared report clip rows need a SHA-256')
        paths.add(path.casefold())
    if report['clips']:
        _plain_dir(prepared_root / 'clips', 'Prepared clips folder')
    return prepared_root, report, hashlib.sha256(document).hexdigest()


def _load_noise(spec, noise_path):
    if spec['noise'] is None:
        if noise_path is not None:
            raise ValueError('A noise file was given but the specification names no noise')
        return None
    if noise_path is None:
        raise ValueError('The specification names noise; give the noise WAV')
    payload = _read_regular(Path(noise_path), MAX_NOISE_BYTES, 'Noise file')
    raw = read_pcm16_wav(payload, 'Noise file')
    if hashlib.sha256(raw).hexdigest() != spec['noise']['sha256']:
        raise ValueError('Noise PCM SHA-256 does not match the specification')
    return raw


def _build(prepared_root, report, spec, noise, ffmpeg=None):
    """Pure-in-effect derivation of every copy: returns (clip rows, error rows, summary,
    {relative path: WAV bytes}). Used by the writer and, unchanged, by the verifier. A
    CodecToolError propagates (nothing has been written yet)."""
    clips, errors, files = [], [], {}
    for row in report['clips']:
        try:
            payload = _read_regular(prepared_root / row['path'], MAX_CLIP_BYTES, 'Parent clip')
            if hashlib.sha256(payload).hexdigest() != row['sha256']:
                raise ValueError('Parent clip hash mismatch')
            out, record = augment_clip(read_pcm16_wav(payload, 'Parent clip'), derivation_id(row),
                                       spec, noise, ffmpeg)
        except NoiseClipped as failure:
            errors.append({'parent_clip_path': row['path'], 'error': str(failure),
                           'refusal': 'noise_clipping', 'snr_db': failure.snr_db})
            continue
        except (OSError, ValueError) as failure:
            errors.append({'parent_clip_path': row['path'], 'error': str(failure)})
            continue
        wav = _wav_bytes(out)
        relative = f'clips/{PurePosixPath(row["path"]).stem}_{spec["codec"]}.wav'
        files[relative] = wav
        clips.append({
            'path': relative, 'sha256': hashlib.sha256(wav).hexdigest(),
            'parent_clip_path': row['path'], 'parent_clip_sha256': row['sha256'],
            'augmented_copy': True, 'counts_toward_clip_total': False,
            'duration_seconds': len(out) / 2 / RATE,
            **{key: row[key] for key in PARENT_FIELDS if key in row},
            'review_status': 'unreviewed',
            **{f'parent_{key}': row[key] for key in REVIEW_FIELDS if key in row},
            'chain': record, **measures(_samples(out))[0]})
    summary = {'parent_clips': len(report['clips']), 'clips': len(clips), 'errors': len(errors),
               'noise_clipping_refusals': sum(e.get('refusal') == 'noise_clipping' for e in errors),
               'review_flagged_clips': sum(bool(c['review_flags']) for c in clips)}
    return clips, errors, summary, files


def augment_prepared(prepared_root, output_root, spec, noise_path=None, ffmpeg=None):
    """Write augmented copies of every parent clip to a new output root; return the
    summary. Unreadable or refused clips are listed as errors while others proceed; a
    codec-tool failure aborts before anything is written."""
    validate_spec(spec)
    _check_ffmpeg(spec, ffmpeg)
    prepared_root, report, report_sha = _load_parent(prepared_root)
    output_root = Path(output_root).resolve()
    if output_root.exists() or output_root.is_relative_to(prepared_root) or prepared_root.is_relative_to(output_root):
        raise ValueError('Output must be new and disjoint from the prepared root')
    if output_root.is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError('Augmented output must be outside the repository')
    noise = _load_noise(spec, noise_path)
    clips, errors, summary, files = _build(prepared_root, report, spec, noise, ffmpeg)
    output_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ava-aug-', dir=output_root.parent) as temp:
        staging = Path(temp) / 'result'
        (staging / 'clips').mkdir(parents=True)
        for relative, payload in files.items():
            (staging / relative).write_bytes(payload)
        result = _report(report_sha, report, spec, summary, clips, errors)
        (staging / 'report.json').write_text(json.dumps(result, indent=2, sort_keys=True), encoding='utf-8')
        output_root.mkdir()
        marker = output_root / 'INCOMPLETE'
        marker.write_text('Publication incomplete; do not consume this directory.', encoding='utf-8')
        (staging / 'clips').rename(output_root / 'clips')
        (staging / 'report.json').rename(output_root / 'report.json')
        marker.unlink()
    return summary


def _report(report_sha, report, spec, summary, clips, errors):
    return {'schema_version': 1, 'kind': 'augmented_copies', 'parent_report_sha256': report_sha,
            'parent_source_manifest_sha256': report.get('source_manifest_sha256'),
            'chain_spec': spec, 'chain_spec_sha256': spec_sha256(spec), 'summary': summary,
            'clips': clips, 'errors': errors}


def _samples(raw):
    samples = array.array('h', raw)
    if sys.byteorder != 'little':
        samples.byteswap()
    return samples


def verify_augmented(prepared_root, augmented_root, noise_path=None, expected_spec=None, ffmpeg=None):
    """Read-only rebuild check: re-derive every augmented copy from its parent clip and the
    recorded specification and compare bytes and records. Returns a list of problems
    (empty means every listed copy rebuilds identically and nothing is unlisted). This
    reuses _build, so it detects drift and tampering, not a defect shared with the writer.
    Every report field is compared with the rebuild (labels, speaker IDs, provenance,
    flags, errors, summary), and files are read only at rebuilt paths."""
    augmented_root = Path(augmented_root).resolve()
    if (augmented_root / 'INCOMPLETE').exists():
        return ['augmented root publication is incomplete']
    result = _load_json(_read_regular(augmented_root / 'report.json', MAX_REPORT_BYTES, 'Augmented report'),
                        'Augmented report')
    if not isinstance(result, dict) or result.get('kind') != 'augmented_copies':
        return ['not an augmented-copy report']
    try:
        spec = validate_spec(result.get('chain_spec'))
    except ValueError as error:
        return [f'recorded chain_spec is invalid: {error}']
    problems = []
    if expected_spec is not None and spec_sha256(validate_spec(expected_spec)) != spec_sha256(spec):
        problems.append('recorded chain_spec differs from the expected specification')
    if needs_ffmpeg(spec):
        if ffmpeg is None:
            return problems + [f'codec {spec["codec"]} needs the ffmpeg codec tool (--ffmpeg) to rebuild']
        recorded, using = recorded_tool_hash(result), getattr(ffmpeg, 'sha256', None)
        if recorded is not None and recorded != using:
            return problems + [f'copies were made with ffmpeg sha256 {recorded}; verifying with {using}: '
                               'use the recorded binary']
    prepared_root, report, report_sha = _load_parent(prepared_root)
    noise = _load_noise(spec, noise_path)
    try:
        clips, errors, summary, files = _build(prepared_root, report, spec, noise, ffmpeg)
    except CodecToolError as error:
        return problems + [f'codec tool failed during rebuild: {error}']
    expected = json.loads(json.dumps(_report(report_sha, report, spec, summary, clips, errors)))
    for key in sorted(expected.keys() | result.keys()):
        if key == 'clips':
            continue
        if result.get(key) != expected.get(key):
            problems.append(f'report field {key!r} differs from the rebuild')
    recorded = result.get('clips') if isinstance(result.get('clips'), list) else []
    if len(recorded) != len(expected['clips']):
        problems.append(f'report lists {len(recorded)} copies; the rebuild gives {len(expected["clips"])}')
    for index, (row, want) in enumerate(zip(recorded, expected['clips'])):
        if row != want:
            fields = sorted(k for k in (row.keys() | want.keys()) if row.get(k) != want.get(k)) \
                if isinstance(row, dict) else ['<not an object>']
            problems.append(f'{want["path"]}: copy row {index} differs from the rebuild in {fields}')
    try:
        _plain_dir(augmented_root / 'clips', 'Augmented clips folder')
        present = {f'clips/{p.name}' for p in (augmented_root / 'clips').iterdir()}
    except (OSError, ValueError) as error:
        return problems + [str(error)]
    for extra in sorted(present - files.keys()):
        problems.append(f'{extra}: file not produced by the rebuild')
    for relative, wav in files.items():
        try:
            payload = _read_regular(augmented_root / relative, MAX_CLIP_BYTES, 'Augmented clip')
        except (OSError, ValueError) as error:
            problems.append(f'{relative}: {error}')
            continue
        if payload != wav:
            problems.append(f'{relative}: audio does not rebuild byte-for-byte')
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--prepared-root', required=True, type=Path)
    parser.add_argument('--spec', required=True, type=Path, help='chain specification JSON')
    parser.add_argument('--noise', type=Path, help='16 kHz mono PCM16 noise WAV named by the spec')
    parser.add_argument('--ffmpeg', type=Path, help='pinned ffmpeg 9.0.2 for GSM/AMR-NB chains '
                        '(else AVA_FFMPEG)')
    parser.add_argument('--ffmpeg-sha256', help='refuse an ffmpeg executable with another hash')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--output-root', type=Path)
    group.add_argument('--verify', type=Path, help='augmented root to rebuild read-only')
    args = parser.parse_args(argv)
    try:
        spec = validate_spec(_load_json(_read_regular(args.spec, 64_000, 'Specification'), 'Specification'))
        if not needs_ffmpeg(spec) and (args.ffmpeg or args.ffmpeg_sha256):
            raise ValueError(f'--ffmpeg/--ffmpeg-sha256 apply only to GSM/AMR-NB chains, not {spec["codec"]}')
        ffmpeg = Ffmpeg(args.ffmpeg, expected_sha256=args.ffmpeg_sha256) if needs_ffmpeg(spec) else None
        if args.verify is not None:
            problems = verify_augmented(args.prepared_root, args.verify, args.noise, spec, ffmpeg)
            for problem in problems:
                print(problem, file=sys.stderr)
            print('FAIL' if problems else 'PASS')
            return 1 if problems else 0
        summary = augment_prepared(args.prepared_root, args.output_root, spec, args.noise, ffmpeg)
    except (OSError, ValueError, CodecToolError) as error:
        print(f'Augmentation failed: {error}', file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 1 if summary['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
