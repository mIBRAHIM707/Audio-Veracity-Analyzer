"""GSM 06.10 and AMR-NB file codec roundtrip through a pinned ffmpeg (backlog 3.3;
PRD FR-D6, FR-D13; architecture §2.1/§2.2: GSM via an ffmpeg pipe, AMR-NB via
opencore-amr). CPU only; ffmpeg runs as a local subprocess over pipes, no files, no
network. The executable is a local tool, never downloaded at run time.

Codecs (PRD FR-D6): `gsm` (GSM 06.10 full rate, libgsm encode and decode, raw 33-byte
frames) and `amr_nb_4.75`, `amr_nb_7.4`, `amr_nb_12.2` (OpenCORE AMR-NB encode and
decode, RFC 4867 storage format, no DTX). Every codec works on 20 ms frames of 160
samples at 8 kHz; the encoder zero-pads the last partial frame and the decoded output
has the measured codec delay (`CODEC_DELAY_SAMPLES`: 0 for GSM, 40 samples = 5 ms for
AMR-NB) removed from its start and is then cut back to the input length, so decoded
sample i lines up with input sample i and labels stay aligned. A live AMR-NB decoder
(no ffmpeg encoder padding) would differ; that parity is 6.x.

`telephone_codec_roundtrip` mirrors `codecs.telephone_roundtrip`: canonical PCM16 at
`rate` -> soxr HQ 8 kHz -> encode -> decode -> 8 kHz PCM16 plus a lineage record; bring
it back to the model grid with `conditioning.canonicalize(decoded, 8000, 1)`. AMR-NB
frame loss (`drop_amr_frames`, `loss_plan=`) replaces lost frames with NO_DATA frames
for the decoder's own concealment; GSM frame loss (`drop_gsm_frames`) substitutes frames
by the architecture §2.2 repeat_then_silence rule. Jitter is not implemented here.
"""
import hashlib
import os
from pathlib import Path
import subprocess

import numpy as np

if __package__:
    from .codecs import REPEAT_LIMIT, _loss_mask, derive_loss
    from .conditioning import library_versions, resample_to_pcm16
else:  # a script-directory 'codecs' import would shadow the standard library module
    raise SystemExit('Import as data.prepare.ffmpeg_codecs')

TELEPHONE_RATE = 8000
FRAME_SAMPLES = 160
PINNED_VERSION = '9.0.2'  # Gyan.FFmpeg 9.0.2 full build (GPL-3.0), see data/prepare/README.md
# ffmpeg.exe of that Windows build; pass as Ffmpeg(expected_sha256=...) for manifest runs.
PINNED_WINDOWS_SHA256 = '589e50b766d251afdf181dd664d40bd94407e200b019989fd7468c7d118a28d0'
MAX_SECONDS = 600  # PRD FR-A1 bound; also bounds memory and subprocess time
TIMEOUT_S = 120
AMR_MAGIC = b'#!AMR\n'
# Bytes per 20 ms frame including the one-byte frame header (RFC 4867 §5.3 storage format).
AMR_FRAME_BYTES = {4750: 13, 7400: 20, 12200: 32}
GSM_FRAME_BYTES = 33
AMR_INITIAL_PADDING = 50  # samples ffmpeg's libopencore_amrnb wrapper prepends (frame count rule)
# End-to-end delay of decoded vs input samples, measured 14 Nov 2026 with seeded noise
# (lag search 0-80 ms, every AMR mode agreed) on ffmpeg 9.0.2; removed by `roundtrip`
# so decoded sample i lines up with input sample i. Tests re-measure it.
CODEC_DELAY_SAMPLES = {'gsm': 0, 'amr_nb_4.75': 40, 'amr_nb_7.4': 40, 'amr_nb_12.2': 40}

CODECS = {
    'gsm': {'encoder': 'libgsm', 'decoder': 'libgsm', 'format': 'gsm', 'bit_rate': 13000,
            'standard': 'GSM 06.10 full rate'},
    'amr_nb_4.75': {'encoder': 'libopencore_amrnb', 'decoder': 'libopencore_amrnb', 'format': 'amr',
                    'bit_rate': 4750, 'standard': 'AMR-NB MR475, RFC 4867 storage, DTX off'},
    'amr_nb_7.4': {'encoder': 'libopencore_amrnb', 'decoder': 'libopencore_amrnb', 'format': 'amr',
                   'bit_rate': 7400, 'standard': 'AMR-NB MR74, RFC 4867 storage, DTX off'},
    'amr_nb_12.2': {'encoder': 'libopencore_amrnb', 'decoder': 'libopencore_amrnb', 'format': 'amr',
                    'bit_rate': 12200, 'standard': 'AMR-NB MR122, RFC 4867 storage, DTX off'},
}


class CodecToolError(RuntimeError):
    """ffmpeg is missing, the wrong version, lacks a codec, fails, or times out."""


def find_ffmpeg(explicit=None):
    """Explicit path, else the AVA_FFMPEG environment variable. No PATH search: on
    Windows that can pick up an ffmpeg.exe in the current folder. Use `expected_sha256`
    on `Ffmpeg` for manifest runs."""
    candidate = explicit or os.environ.get('AVA_FFMPEG')
    if not candidate or not Path(candidate).is_file():
        raise CodecToolError('ffmpeg not found: pass its path or set AVA_FFMPEG')
    return str(Path(candidate).resolve())


def _check_pcm(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) % 2:
        raise ValueError('Expected nonempty mono PCM16 bytes')
    if len(raw) // 2 > MAX_SECONDS * TELEPHONE_RATE:
        raise ValueError(f'Input longer than {MAX_SECONDS} s at 8 kHz')


class Ffmpeg:
    """A verified ffmpeg executable. `run` is injectable for failure-path tests."""

    def __init__(self, executable=None, *, expected_sha256=None, run=subprocess.run, timeout=TIMEOUT_S):
        self.executable = find_ffmpeg(executable)
        self._run, self._timeout = run, timeout
        self.sha256 = self._file_sha256()  # recorded in every lineage record
        if expected_sha256 is not None and self.sha256 != expected_sha256:
            raise CodecToolError('ffmpeg executable hash does not match the pinned hash')
        banner = self._call(['-hide_banner', '-version'], b'').decode('utf-8', 'replace').splitlines()
        words = banner[0].split() if banner else []
        # Exact version token: '9.0.2' or '9.0.2-<build>', never '9.0.20'.
        if len(words) < 3 or words[:2] != ['ffmpeg', 'version'] or words[2].split('-')[0] != PINNED_VERSION:
            raise CodecToolError(f'expected ffmpeg {PINNED_VERSION}, found {banner[0] if banner else "nothing"!r}')
        self.version = words[2]
        for kind, key in (('-encoders', 'encoder'), ('-decoders', 'decoder')):
            listed = self._call(['-hide_banner', kind], b'').decode('utf-8', 'replace')
            names = {line.split()[1] for line in listed.splitlines() if len(line.split()) > 1}
            needed = {spec[key] for spec in CODECS.values()}
            if not needed <= names:
                raise CodecToolError(f'ffmpeg lacks {key}s: {sorted(needed - names)}')

    def _file_sha256(self):
        digest = hashlib.sha256()
        with open(self.executable, 'rb') as handle:
            for block in iter(lambda: handle.read(1 << 20), b''):
                digest.update(block)
        return digest.hexdigest()

    def _call(self, args, data):
        # FFREPORT would make ffmpeg write a log file into the working directory.
        env = {key: value for key, value in os.environ.items() if key.upper() != 'FFREPORT'}
        try:
            done = self._run([self.executable, '-nostdin', *args], input=data, capture_output=True,
                             timeout=self._timeout, check=False, env=env)
        except subprocess.TimeoutExpired:
            raise CodecToolError(f'ffmpeg timed out after {self._timeout} s') from None
        except OSError as error:
            raise CodecToolError(f'ffmpeg could not run: {type(error).__name__}') from None
        if done.returncode != 0:
            tail = (done.stderr or b'').decode('utf-8', 'replace').strip().splitlines()[-1:]
            raise CodecToolError(f'ffmpeg failed ({done.returncode}): {tail[0] if tail else "no message"}')
        return done.stdout

    def encode(self, narrow, codec):
        """8 kHz PCM16 bytes -> encoded stream (raw GSM frames or an RFC 4867 .amr file)."""
        spec = _spec(codec)
        _check_pcm(narrow)
        encoded = self._call(_encode_args(spec), narrow)
        _check_stream(encoded, codec, encoded_frames(codec, len(narrow) // 2))
        return encoded

    def decode(self, encoded, codec, allow_lost_frames=False):
        """Encoded stream -> 8 kHz PCM16 bytes (whole frames; no length correction). The
        stream is checked first (ffmpeg itself decodes e.g. an AMR-WB header as AMR).
        NO_DATA (lost) AMR frames are accepted only with `allow_lost_frames`, which
        `roundtrip` sets after its own seeded loss; a plain decode refuses them."""
        spec = _spec(codec)
        if not isinstance(encoded, bytes) or not encoded:
            raise ValueError('Expected nonempty encoded bytes')
        try:
            if spec['format'] == 'amr' and allow_lost_frames:
                frames = _amr_frames(encoded, codec)  # this mode's frames, or NO_DATA (lost) frames
                if all(frame[0] == AMR_NO_DATA for frame in frames):
                    raise CodecToolError('AMR stream has no speech frame')
            else:
                _check_stream(encoded, codec)
        except CodecToolError as error:
            raise ValueError(f'Not a {codec} stream: {error}') from None
        decoded = self._call(_decode_args(spec), encoded)
        if not decoded or len(decoded) % 2:
            raise CodecToolError('ffmpeg returned no or odd-length PCM')
        return decoded

    def gsm_silence_frame(self):
        """One GSM 06.10 frame encoding 160 samples of digital silence (cached)."""
        if getattr(self, '_gsm_silence', None) is None:
            self._gsm_silence = self.encode(bytes(2 * FRAME_SAMPLES), 'gsm')
        return self._gsm_silence

    def roundtrip(self, narrow, codec, loss_plan=None):
        """8 kHz PCM16 -> encode -> optional seeded frame loss (`loss_plan` from
        codecs.derive_loss: AMR-NB NO_DATA frames, GSM repeat_then_silence substitution)
        -> decode -> remove the codec delay -> 8 kHz PCM16 of the input length. Returns
        (decoded bytes, record). Raises CodecToolError if the decoder returned too few
        samples to cover the input after the delay."""
        spec = _spec(codec)
        if loss_plan is not None and (not isinstance(loss_plan, dict) or loss_plan != derive_loss(
                loss_plan.get('master_seed'), loss_plan.get('clip_id'))):
            raise ValueError('loss_plan must come unchanged from codecs.derive_loss')
        encoded = self.encode(narrow, codec)
        received, frame_loss = encoded, None
        if loss_plan is not None:
            if spec['format'] == 'amr':
                received, frame_loss = drop_amr_frames(encoded, codec, loss_plan['loss_rate'], loss_plan['seed'])
                frame_loss.update(applied_via='amr_no_data_frames',
                                  mask_covers='every encoded frame incl. ffmpeg padding and the flush frame')
            else:
                received, frame_loss = drop_gsm_frames(encoded, loss_plan['loss_rate'], loss_plan['seed'],
                                                       self.gsm_silence_frame())
                frame_loss.update(mask_covers='every encoded frame')
            frame_loss.update({key: loss_plan[key] for key in ('derivation', 'master_seed', 'clip_id')})
        raw = self.decode(received, codec, allow_lost_frames=loss_plan is not None)
        delay, target = 2 * CODEC_DELAY_SAMPLES[codec], len(narrow)
        if len(raw) < delay + target:
            raise CodecToolError(f'decoder returned {len(raw) // 2} samples, need {(delay + target) // 2}')
        decoded = raw[delay:delay + target]
        spec = CODECS[codec]
        return decoded, {
            'codec': codec, 'codec_standard': spec['standard'], 'bit_rate': spec['bit_rate'],
            'encoder': spec['encoder'], 'decoder': spec['decoder'], 'container': spec['format'],
            'codec_implementation': f'ffmpeg {self.version} subprocess pipe',
            'ffmpeg_sha256': self.sha256, 'codec_rate': TELEPHONE_RATE,
            'ffmpeg_encode_args': _encode_args(spec), 'ffmpeg_decode_args': _decode_args(spec),
            'frames': -(-len(narrow) // 2 // FRAME_SAMPLES),
            'encoded_frames': encoded_frames(codec, len(narrow) // 2),
            'decoded_samples_before_alignment': len(raw) // 2,
            'delay_compensated_samples': CODEC_DELAY_SAMPLES[codec],
            'length_policy': 'drop_codec_delay_then_cut_to_input_length',
            'encoded_sha256': hashlib.sha256(encoded).hexdigest(), 'encoded_bytes': len(encoded),
            'frame_loss': frame_loss,
            'received_sha256': hashlib.sha256(received).hexdigest(),
            'decoded_pcm_sha256': hashlib.sha256(decoded).hexdigest(),
        }


def _encode_args(spec):
    """Exact ffmpeg arguments (no paths), recorded in the lineage."""
    args = ['-hide_banner', '-loglevel', 'error', '-f', 's16le', '-ar', str(TELEPHONE_RATE), '-ac', '1',
            '-i', 'pipe:0', '-map_metadata', '-1', '-c:a', spec['encoder']]
    if spec['format'] == 'amr':
        args += ['-b:a', str(spec['bit_rate']), '-dtx', '0']
    return args + ['-f', spec['format'], 'pipe:1']


def _decode_args(spec):
    args = ['-hide_banner', '-loglevel', 'error', '-f', spec['format']]
    if spec['format'] == 'gsm':
        args += ['-ar', str(TELEPHONE_RATE)]
    return args + ['-c:a', spec['decoder'], '-i', 'pipe:0', '-map_metadata', '-1', '-f', 's16le',
                   '-ar', str(TELEPHONE_RATE), '-ac', '1', 'pipe:1']


def _spec(codec):
    if codec not in CODECS:
        raise ValueError(f'Unknown codec {codec!r}; expected one of {sorted(CODECS)}')
    return CODECS[codec]


def encoded_frames(codec, samples):
    """Frames in the encoded stream for `samples` input samples. GSM: one per started
    160-sample frame. OpenCORE AMR-NB via ffmpeg 9.0.2: floor((samples + 50) / 160) + 1
    (ffmpeg's 50-sample initial padding plus a flush frame), measured 14 Nov 2026 on
    every remainder 0-159 at two lengths and pinned by the known-answer tests."""
    if CODECS[codec]['format'] == 'amr':
        return (samples + AMR_INITIAL_PADDING) // FRAME_SAMPLES + 1
    return -(-samples // FRAME_SAMPLES)


AMR_NO_DATA = 0x7C  # RFC 4867 frame header FT=15 (NO_DATA), Q=1: a one-byte frame
AMR_MODES = {4750: 0, 7400: 4, 12200: 7}
FRAME_LOSS_CONCEALMENT = 'amr_decoder_native_no_data'


def _amr_frames(encoded, codec):
    """Split an RFC 4867 storage stream of `codec` into frames, allowing NO_DATA frames
    (lost frames). Raises CodecToolError on any other header or a truncated frame."""
    spec = CODECS[codec]
    size, mode = AMR_FRAME_BYTES[spec['bit_rate']], AMR_MODES[spec['bit_rate']]
    if not encoded.startswith(AMR_MAGIC):
        raise CodecToolError('AMR stream lacks the #!AMR storage header')
    frames, offset = [], len(AMR_MAGIC)
    while offset < len(encoded):
        header = encoded[offset]
        length = 1 if header == AMR_NO_DATA else size if header == (mode << 3 | 0x04) else None
        if length is None or offset + length > len(encoded):
            raise CodecToolError(f'AMR frame {len(frames)} has header {header:#04x} or is truncated')
        frames.append(encoded[offset:offset + length])
        offset += length
    if not frames:
        raise CodecToolError('AMR stream has no frames')
    return frames


def drop_amr_frames(encoded, codec, loss_rate, seed):
    """Seeded Bernoulli loss of 20 ms AMR-NB frames (PRD FR-D6, 0-5 %): each lost frame is
    replaced by a NO_DATA frame, so the OpenCORE decoder applies its own error
    concealment and the frame count (timing) is kept. Same mask rule and record as the
    G.711 packet loss (`codecs._loss_mask`). Returns (stream, record)."""
    if CODECS.get(codec, {}).get('format') != 'amr':
        raise ValueError(f'frame loss is implemented for AMR-NB only, not {codec!r}')
    frames = _amr_frames(encoded, codec)
    if any(frame[0] == AMR_NO_DATA for frame in frames):
        raise ValueError('stream already contains NO_DATA frames')
    lost, record = _loss_mask(len(frames), loss_rate, seed)
    stream = AMR_MAGIC + b''.join(bytes([AMR_NO_DATA]) if gone else frame for frame, gone in zip(frames, lost))
    return stream, {**record, 'unit': 'amr_frame_20ms', 'concealment': FRAME_LOSS_CONCEALMENT}


GSM_CONCEALMENT = 'repeat_then_silence'


def substitute_frames(frames, lost, silence_frame):
    """Pure: the repeat_then_silence rule on encoded frames (see drop_gsm_frames)."""
    output, previous, run = [], None, 0
    for frame, gone in zip(frames, lost, strict=True):
        if not gone:
            output.append(frame)
            previous, run = frame, 0
            continue
        run += 1
        output.append(previous if previous is not None and run <= REPEAT_LIMIT else silence_frame)
    return output


def drop_gsm_frames(encoded, loss_rate, seed, silence_frame):
    """Seeded Bernoulli loss of 20 ms GSM 06.10 frames (PRD FR-D6, 0-5 %), concealed in
    the bitstream by the architecture §2.2 rule used for G.711: a lost frame repeats the
    previous received frame for the first REPEAT_LIMIT frames of a burst, then (or with
    no previous frame) becomes `silence_frame` (GSM-encoded digital silence); a hard
    cut, no fade. libgsm has no bad-frame input, so this substitution is the decoder
    side. Same mask rule and record as `codecs.drop_packets`. Returns (stream, record)."""
    _check_stream(encoded, 'gsm')
    if not isinstance(silence_frame, bytes) or len(silence_frame) != GSM_FRAME_BYTES:
        raise ValueError('silence_frame must be one 33-byte GSM frame')
    frames = [encoded[i:i + GSM_FRAME_BYTES] for i in range(0, len(encoded), GSM_FRAME_BYTES)]
    lost, record = _loss_mask(len(frames), loss_rate, seed)
    return b''.join(substitute_frames(frames, lost, silence_frame)), {**record, 'unit': 'gsm_frame_20ms', 'concealment': GSM_CONCEALMENT,
                              'repeat_limit': REPEAT_LIMIT, 'applied_via': 'gsm_frame_substitution',
                              'silence_frame_sha256': hashlib.sha256(silence_frame).hexdigest()}


def _check_stream(encoded, codec, frames=None):
    """Known-answer framing check: exact frame size, mode and (when `frames` is given)
    count for the requested codec, so a different mode, codec or truncated stream is
    refused before or after ffmpeg sees it."""
    spec = CODECS[codec]
    if spec['format'] == 'gsm':
        if frames is None:
            frames = len(encoded) // GSM_FRAME_BYTES if len(encoded) % GSM_FRAME_BYTES == 0 else -1
        expected = frames * GSM_FRAME_BYTES
        if len(encoded) != expected:
            raise CodecToolError(f'GSM stream is {len(encoded)} bytes, expected {expected}')
        return
    size = AMR_FRAME_BYTES[spec['bit_rate']]
    if frames is None:
        body_bytes = len(encoded) - len(AMR_MAGIC)
        frames = body_bytes // size if body_bytes > 0 and body_bytes % size == 0 else -1
    if frames < 1 or not encoded.startswith(AMR_MAGIC) or len(encoded) != len(AMR_MAGIC) + frames * size:
        raise CodecToolError(f'AMR stream is {len(encoded)} bytes, expected {len(AMR_MAGIC) + frames * size}')
    body = np.frombuffer(encoded, dtype=np.uint8, offset=len(AMR_MAGIC)).reshape(frames, size)
    mode = {4750: 0, 7400: 4, 12200: 7}[spec['bit_rate']]
    # Frame header: P FT(4) Q P P; padding bits 0 and Q (good frame) set.
    if np.any(((body[:, 0] >> 3) & 0x0F) != mode) or np.any((body[:, 0] & 0x87) != 0x04):
        raise CodecToolError('AMR stream contains frames of another mode, bad-quality frames or set padding bits')


def telephone_codec_roundtrip(canonical, codec, ffmpeg, *, rate=16000, loss_plan=None):
    """Canonical PCM16 at `rate` -> soxr HQ 8 kHz -> `codec` encode/decode via `ffmpeg`
    (an `Ffmpeg`) -> 8 kHz PCM16. Returns (decoded bytes, lineage record)."""
    _spec(codec)
    if type(rate) is not int or not TELEPHONE_RATE <= rate <= 192000:
        raise ValueError('Expected integer input rate from 8000 to 192000')
    if not isinstance(canonical, bytes) or not canonical or len(canonical) % 2:
        raise ValueError('Expected nonempty mono PCM16 bytes')
    if len(canonical) // 2 > MAX_SECONDS * rate:  # refuse before resampling
        raise ValueError(f'Input longer than {MAX_SECONDS} s')
    samples = np.frombuffer(canonical, dtype='<i2').astype(np.float64)
    narrow, saturated = resample_to_pcm16(samples, rate, TELEPHONE_RATE)
    # `loss_plan` is validated by Ffmpeg.roundtrip; passed only when given.
    decoded, record = (ffmpeg.roundtrip(narrow, codec) if loss_plan is None
                       else ffmpeg.roundtrip(narrow, codec, loss_plan=loss_plan))
    return decoded, {
        **record, 'input_rate': rate, 'input_sample_count': len(samples),
        'input_sha256': hashlib.sha256(canonical).hexdigest(),
        'resampler': 'bypass' if rate == TELEPHONE_RATE else 'soxr_hq',
        'quantization': 'round_nearest_even_clip_pcm16', 'saturated_samples': saturated,
        # Same key as the G.711 record, so FR-D6 impairment counts see AMR frame loss.
        'sample_count': len(decoded) // 2, 'packet_loss': record.get('frame_loss'), 'jitter': None,
        **library_versions(),
    }
