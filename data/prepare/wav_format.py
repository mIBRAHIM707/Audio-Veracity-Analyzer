"""Dependency-free WAV format-tag check shared by preparation, augmentation and the
analysis service (backlog 3.QA L1). Standard library only, so exact-mode preparation
does not need numpy/soxr.

Shared intake rule: exactly one `fmt ` chunk before the `data` chunk, with plain
WAVE_FORMAT_PCM (tag 1). CPython 3.11 `wave` refuses every other tag, but 3.12+ also
reads WAVE_FORMAT_EXTENSIBLE (0xFFFE), so the tag is checked explicitly to keep intake
identical across Python versions. A second `fmt ` chunk is refused because `wave` uses
the last one while a first-chunk check would see only the first.
"""

WAVE_FORMAT_PCM = 1
SCAN_LIMIT = 1 << 20  # chunk headers are only searched in the first 1 MB


def pcm_format_problem(payload):
    """None when the bytes declare plain PCM as above; otherwise a short reason."""
    data = bytes(payload[:SCAN_LIMIT])
    if len(data) < 12 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        return 'not a RIFF/WAVE file'
    tags, offset = [], 12
    while offset + 8 <= len(data):
        name = data[offset:offset + 4]
        size = int.from_bytes(data[offset + 4:offset + 8], 'little')
        if name == b'data':
            break
        if name == b'fmt ':
            if size < 2 or offset + 10 > len(data):
                return 'fmt chunk is truncated'
            tags.append(int.from_bytes(data[offset + 8:offset + 10], 'little'))
        offset += 8 + size + (size & 1)  # RIFF chunks are padded to an even size
    else:
        if not tags:
            return 'no fmt chunk found in the first 1 MB'
    if not tags:
        return 'no fmt chunk before the data chunk'
    if len(tags) > 1:
        return 'more than one fmt chunk'
    if tags[0] != WAVE_FORMAT_PCM:
        return f'format tag {tags[0]:#06x} is not plain PCM (format tag 1)'
    return None


def is_plain_pcm(payload):
    return pcm_format_problem(payload) is None
