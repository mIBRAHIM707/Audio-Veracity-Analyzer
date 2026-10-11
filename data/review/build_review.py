"""Build a self-contained offline listening-review page beside a preparation report."""
import argparse
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import stat
import sys

MAX_BYTES = 8_000_000


def bounded_read(path, limit):
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('Expected a regular file')
    with path.open('rb') as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise ValueError('File exceeds pilot size limit')
    return payload


def build_review(report_path):
    report_path = Path(report_path).resolve()
    root = report_path.parent
    if root.is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError('Real listening-review artifacts must stay outside Git workspace')
    if (root / 'INCOMPLETE').exists():
        raise ValueError('Preparation output is incomplete')
    output = root / 'review.html'
    if output.exists():
        raise ValueError('Review page already exists; refusing overwrite')
    payload = bounded_read(report_path, MAX_BYTES)
    report = json.loads(payload)
    if not isinstance(report, dict) or report.get('schema_version') != 1:
        raise ValueError('Expected preparation report schema_version 1')
    clips = report.get('clips')
    if not isinstance(clips, list) or not 1 <= len(clips) <= 200:
        raise ValueError('Pilot review requires 1–200 candidate clips per page')
    safe_clips, seen = [], set()
    for clip in clips:
        if not isinstance(clip, dict):
            raise ValueError('Candidate must be an object')
        name = clip.get('path')
        if (not isinstance(name, str) or len(name) > 1000 or not name.startswith('clips/')
            or not name.lower().endswith('.wav') or '\\' in name or ':' in name
            or '\x00' in name or PurePosixPath(name).is_absolute()
            or any(p in ('', '.', '..') for p in name.split('/'))):
            raise ValueError('Unsafe candidate path')
        if name.casefold() in seen:
            raise ValueError('Duplicate candidate path')
        seen.add(name.casefold())
        digest = clip.get('sha256')
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('Invalid candidate SHA256')
        duration = clip.get('duration_seconds')
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 3 <= duration <= 6:
            raise ValueError('Invalid candidate duration')
        for field in ('speaker_id', 'source_path'):
            if not isinstance(clip.get(field), str) or not clip[field] or len(clip[field]) > 1000:
                raise ValueError(f'Invalid {field}')
        flags = clip.get('review_flags')
        if not isinstance(flags, list) or len(flags) > 20 or any(not isinstance(f, str) or len(f) > 200 for f in flags):
            raise ValueError('Invalid review flags')
        start, end = clip.get('start_sample'), clip.get('end_sample')
        if type(start) is not int or type(end) is not int or not 0 <= start < end or abs((end-start)/16000-duration) > 1/16000:
            raise ValueError('Invalid source sample interval')
        path = root / name
        if not path.resolve().is_relative_to(root):
            raise ValueError('Candidate escapes preparation directory')
        current = path
        while current != root:
            if current.is_symlink():
                raise ValueError('Symlink candidate unsupported')
            current = current.parent
        if hashlib.sha256(bounded_read(path, 10_000_000)).hexdigest() != digest:
            raise ValueError('Candidate hash mismatch')
        safe_clips.append({key: clip[key] for key in ('path', 'speaker_id', 'duration_seconds', 'review_flags', 'sha256', 'source_path', 'start_sample', 'end_sample')})
    data = {'report_sha256': hashlib.sha256(payload).hexdigest(), 'clips': safe_clips}
    # JSON embedded in a script must not contain an HTML end-script sequence.
    encoded = json.dumps(data, ensure_ascii=True).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    template = Path(__file__).with_name('review_template.html').read_text(encoding='utf-8')
    if template.count('__AVA_REVIEW_DATA__') != 1:
        raise ValueError('Invalid review template')
    page = template.replace('__AVA_REVIEW_DATA__', encoded)
    # Exclusive creation protects a preexisting page; it is never silently refreshed.
    with output.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(page)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(build_review(args.report))
    except (OSError, ValueError) as error:
        print(f'Review page failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
