"""GSM 06.10, AMR-NB and Opus file codec roundtrip through a pinned ffmpeg (backlog
3.3/3.4; PRD FR-D6, FR-D13; architecture §2.1/§2.2: GSM via an ffmpeg pipe, AMR-NB via
opencore-amr). CPU only; ffmpeg runs as a local subprocess over pipes, no files, no
network. The executable is a local tool, never downloaded at run time.

Codecs (PRD FR-D6): `gsm` (GSM 06.10 full rate, libgsm encode and decode, raw 33-byte
frames), `amr_nb_4.75`, `amr_nb_7.4`, `amr_nb_12.2` (OpenCORE AMR-NB encode and
decode, RFC 4867 storage format, no DTX) and the VoIP path `opus_8`, `opus_12`,
`opus_16` (libopus CBR 8/12/16 kbps, VoIP application, Ogg Opus; G.729 has no encoder
in ffmpeg, and the PRD accepts Opus or G.729). Every codec works on 20 ms frames of 160
samples at 8 kHz; the encoder zero-pads the last partial frame and the decoded output
has the measured codec delay (`CODEC_DELAY_SAMPLES`: 0 for GSM, 40 samples = 5 ms for
AMR-NB; `OPUS_DELAY_48K` for Opus, removed at its 48 kHz decode rate before the soxr
step to 8 kHz) removed from its start and is then cut back to the input length, so
decoded sample i lines up with input sample i and labels stay aligned. A live AMR-NB
decoder (no ffmpeg encoder padding) would differ; that parity is 6.x.

`telephone_codec_roundtrip` mirrors `codecs.telephone_roundtrip`: canonical PCM16 at
`rate` -> soxr HQ 8 kHz -> encode -> decode -> 8 kHz PCM16 plus a lineage record; bring
it back to the model grid with `conditioning.canonicalize(decoded, 8000, 1)`. AMR-NB
frame loss (`drop_amr_frames`, `loss_plan=`) replaces lost frames with NO_DATA frames
for the decoder's own concealment; GSM frame loss (`drop_gsm_frames`) substitutes frames
by the architecture §2.2 repeat_then_silence rule; Opus packet loss replaces lost
packets by TOC-only packets, so libopus conceals them (its PLC). Late packets (jitter
plans) join the same mask in `Ffmpeg.roundtrip`.
"""
import hashlib
import os
from pathlib import Path
import struct
import subprocess

import numpy as np

if __package__:
    from .codecs import REPEAT_LIMIT, _indices_sha256, _late_mask, _loss_mask, derive_jitter, derive_loss
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

# Opus VoIP path (PRD FR-D6 "Opus at 8 to 16 kbps"; backlog 3.4). libopus encode and
# decode, CBR, 20 ms frames, VoIP application, Ogg Opus (RFC 7845) over the pipe with
# bit-exact muxing (otherwise the Ogg serial number is random). The decoder runs at
# 48 kHz; `roundtrip` removes OPUS_DELAY_48K and brings it to 8 kHz with the shared soxr
# HQ step. Measured 15 Nov 2026 on ffmpeg 9.0.2 (band-limited seeded noise, 8/12/16/64
# kbps, two seeds: best alignment at 4 samples of 48 kHz in every case).
OPUS_FRAME_BYTES = {8000: 20, 12000: 30, 16000: 40}  # CBR: bit_rate * 20 ms / 8
OPUS_DECODE_RATE = 48000
OPUS_PRE_SKIP = 312  # OpusHead pre-skip ffmpeg 9.0.2 writes for 8 kHz libopus input
OPUS_DELAY_48K = 4
OPUS_INPUT_PADDING = FRAME_SAMPLES  # zeros appended so the aligned output covers the input
OPUS_CONCEALMENT = 'opus_decoder_native_plc'

CODECS = {
    'gsm': {'encoder': 'libgsm', 'decoder': 'libgsm', 'format': 'gsm', 'bit_rate': 13000,
            'standard': 'GSM 06.10 full rate'},
    **{f'opus_{rate // 1000}': {
        'encoder': 'libopus', 'decoder': 'libopus', 'format': 'ogg', 'bit_rate': rate,
        'standard': f'Opus (RFC 6716) {rate // 1000} kbps CBR, 20 ms, VoIP application, Ogg (RFC 7845)'}
       for rate in OPUS_FRAME_BYTES},
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
        """8 kHz PCM16 bytes -> encoded stream (raw GSM frames, an RFC 4867 .amr file or
        an Ogg Opus stream)."""
        _spec(codec)
        _check_pcm(narrow)
        return self._encode_checked(narrow, codec)

    def _encode_checked(self, narrow, codec):
        encoded = self._call(_encode_args(CODECS[codec]), narrow)
        _check_stream(encoded, codec, encoded_frames(codec, len(narrow) // 2))
        return encoded

    def decode(self, encoded, codec, allow_lost_frames=False):
        """Encoded stream -> PCM16 bytes, 8 kHz for GSM/AMR-NB and 48 kHz for Opus (whole
        frames; no delay removal or length correction). The stream is checked first
        (ffmpeg itself decodes e.g. an AMR-WB header as AMR). NO_DATA (lost) AMR frames
        and TOC-only (lost) Opus packets are accepted only with `allow_lost_frames`, which
        `roundtrip` sets after its own seeded loss; a plain decode refuses them."""
        spec = _spec(codec)
        if not isinstance(encoded, bytes) or not encoded:
            raise ValueError('Expected nonempty encoded bytes')
        try:
            if spec['format'] == 'amr' and allow_lost_frames:
                frames = _amr_frames(encoded, codec)  # this mode's frames, or NO_DATA (lost) frames
                if all(frame[0] == AMR_NO_DATA for frame in frames):
                    raise CodecToolError('AMR stream has no speech frame')
            elif spec['format'] == 'ogg':
                packets = _opus_packets(_ogg_pages(encoded), codec, allow_lost=allow_lost_frames)
                if all(len(packet) == 1 for packet in packets):
                    raise CodecToolError('Opus stream has no received packet')
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

    def roundtrip(self, narrow, codec, loss_plan=None, jitter_plan=None):
        """8 kHz PCM16 -> encode -> optional seeded frame loss (`loss_plan` from
        codecs.derive_loss) and/or late frames (`jitter_plan` from codecs.derive_jitter:
        exponential extra delay behind a fixed playout buffer, as for G.711) -> one
        lost-or-late mask concealed once (AMR-NB NO_DATA frames, GSM repeat_then_silence
        substitution) -> decode -> remove the codec delay -> 8 kHz PCM16 of the input
        length. Returns (decoded bytes, record). Raises CodecToolError if the decoder
        returned too few samples to cover the input after the delay."""
        spec = _spec(codec)
        _check_pcm(narrow)
        if loss_plan is not None and (not isinstance(loss_plan, dict) or loss_plan != derive_loss(
                loss_plan.get('master_seed'), loss_plan.get('clip_id'))):
            raise ValueError('loss_plan must come unchanged from codecs.derive_loss')
        if jitter_plan is not None:
            if not isinstance(jitter_plan, dict) or jitter_plan != derive_jitter(
                    jitter_plan.get('master_seed'), jitter_plan.get('clip_id')):
                raise ValueError('jitter_plan must come unchanged from codecs.derive_jitter')
            if loss_plan is not None and (loss_plan['clip_id'], loss_plan['master_seed']) != (
                    jitter_plan['clip_id'], jitter_plan['master_seed']):
                raise ValueError('loss_plan and jitter_plan name different clips or master seeds')
        amr, opus = spec['format'] == 'amr', spec['format'] == 'ogg'
        # Opus: zeros appended so that, after the decoder delay is removed, the output
        # still covers every input sample; they are cut away again below.
        source = narrow + bytes(2 * OPUS_INPUT_PADDING) if opus else narrow
        encoded = self._encode_checked(source, codec)
        if amr:
            frames = _amr_frames(encoded, codec)
            rule = {'unit': 'amr_frame_20ms', 'concealment': FRAME_LOSS_CONCEALMENT,
                    'applied_via': 'amr_no_data_frames',
                    'mask_covers': 'every encoded frame incl. ffmpeg padding and the flush frame'}
        elif opus:
            pages = _ogg_pages(encoded)
            frames = _opus_packets(pages, codec)
            rule = {'unit': 'opus_packet_20ms', 'concealment': OPUS_CONCEALMENT,
                    'applied_via': 'opus_toc_only_packets',
                    'mask_covers': 'every audio packet incl. the encoder pre-skip and the input padding'}
        else:
            frames = [encoded[i:i + GSM_FRAME_BYTES] for i in range(0, len(encoded), GSM_FRAME_BYTES)]
            rule = {'unit': 'gsm_frame_20ms', 'concealment': GSM_CONCEALMENT, 'repeat_limit': REPEAT_LIMIT,
                    'applied_via': 'gsm_frame_substitution', 'mask_covers': 'every encoded frame',
                    'silence_frame_sha256': hashlib.sha256(self.gsm_silence_frame()).hexdigest()}
        missing = np.zeros(len(frames), dtype=bool)
        frame_loss = jitter = concealment = None
        if loss_plan is not None:
            lost, frame_loss = _loss_mask(len(frames), loss_plan['loss_rate'], loss_plan['seed'])
            frame_loss.update(rule, **{key: loss_plan[key] for key in ('derivation', 'master_seed', 'clip_id')})
            missing |= lost
        if jitter_plan is not None:
            late, jitter = _late_mask(len(frames), jitter_plan['jitter_ms'], jitter_plan['buffer_ms'],
                                      jitter_plan['seed'])
            jitter.update(rule, **{key: jitter_plan[key] for key in ('derivation', 'master_seed', 'clip_id')})
            missing |= late
        received = encoded
        if loss_plan is not None or jitter_plan is not None:
            if amr:
                received = AMR_MAGIC + b''.join(bytes([AMR_NO_DATA]) if gone else f for f, gone in zip(frames, missing))
            elif opus:
                # A TOC-only code-0 packet (same configuration, code bits cleared; CBR
                # packets use code 3) carries one zero-length frame; libopus treats it
                # as lost and conceals it (measured on ffmpeg 9.0.2). The Ogg demuxer
                # refuses truly empty packets. Granule positions stay unchanged.
                received = _ogg_replace_packets(pages, [bytes([f[0] & 0xFC]) if gone else f
                                                        for f, gone in zip(frames, missing)])
            else:
                received = b''.join(substitute_frames(frames, list(missing), self.gsm_silence_frame()))
            both = loss_plan is not None and jitter_plan is not None
            concealment = {'mask': 'lost_or_late', **rule, 'frames': len(frames),
                           'concealed_frames': int(missing.sum()),
                           'concealed_fraction': float(missing.sum()) / len(frames),
                           'lost_and_late_frames': int((lost & late).sum()) if both else None,
                           'concealed_indices_sha256': _indices_sha256(missing)}
        raw = self.decode(received, codec, allow_lost_frames=bool(missing.any()))
        target = len(narrow)
        extra = {}
        if opus:
            delay = 2 * OPUS_DELAY_48K
            wide_target = 2 * (target // 2) * (OPUS_DECODE_RATE // TELEPHONE_RATE)
            if len(raw) < delay + wide_target:
                raise CodecToolError(f'decoder returned {len(raw) // 2} samples at 48 kHz, '
                                     f'need {(delay + wide_target) // 2}')
            wide = np.frombuffer(raw[delay:], dtype='<i2').astype(np.float64)
            narrowed, saturated = resample_to_pcm16(wide, OPUS_DECODE_RATE, TELEPHONE_RATE)
            if len(narrowed) < target:
                raise CodecToolError(f'resampled decoder output has {len(narrowed) // 2} samples, need {target // 2}')
            decoded = narrowed[:target]
            extra = {'decode_rate': OPUS_DECODE_RATE, 'delay_rate': OPUS_DECODE_RATE,
                     'input_padding_samples': OPUS_INPUT_PADDING, 'opus_pre_skip': OPUS_PRE_SKIP,
                     'decode_resampler': 'soxr_hq', 'decode_resample_saturated_samples': saturated}
        else:
            delay = 2 * CODEC_DELAY_SAMPLES[codec]
            if len(raw) < delay + target:
                raise CodecToolError(f'decoder returned {len(raw) // 2} samples, need {(delay + target) // 2}')
            decoded = raw[delay:delay + target]
        spec = CODECS[codec]
        return decoded, {**extra,
            'codec': codec, 'codec_standard': spec['standard'], 'bit_rate': spec['bit_rate'],
            'encoder': spec['encoder'], 'decoder': spec['decoder'], 'container': spec['format'],
            'codec_implementation': f'ffmpeg {self.version} subprocess pipe',
            'ffmpeg_sha256': self.sha256, 'codec_rate': TELEPHONE_RATE,
            'ffmpeg_encode_args': _encode_args(spec), 'ffmpeg_decode_args': _decode_args(spec),
            'frames': -(-len(narrow) // 2 // FRAME_SAMPLES),
            'encoded_frames': encoded_frames(codec, len(source) // 2),
            'decoded_samples_before_alignment': len(raw) // 2,
            'delay_compensated_samples': OPUS_DELAY_48K if opus else CODEC_DELAY_SAMPLES[codec],
            'length_policy': 'drop_codec_delay_then_cut_to_input_length',
            'encoded_sha256': hashlib.sha256(encoded).hexdigest(), 'encoded_bytes': len(encoded),
            'frame_loss': frame_loss, 'frame_jitter': jitter, 'frame_concealment': concealment,
            'received_sha256': hashlib.sha256(received).hexdigest(),
            'decoded_pcm_sha256': hashlib.sha256(decoded).hexdigest(),
        }


def _encode_args(spec):
    """Exact ffmpeg arguments (no paths), recorded in the lineage."""
    args = ['-hide_banner', '-loglevel', 'error', '-f', 's16le', '-ar', str(TELEPHONE_RATE), '-ac', '1',
            '-i', 'pipe:0', '-map_metadata', '-1', '-c:a', spec['encoder']]
    if spec['format'] == 'amr':
        args += ['-b:a', str(spec['bit_rate']), '-dtx', '0']
    elif spec['format'] == 'ogg':
        # Bit-exact muxing fixes the Ogg serial number (random otherwise).
        args += ['-b:a', str(spec['bit_rate']), '-vbr', 'off', '-application', 'voip', '-frame_duration', '20',
                 '-fflags', '+bitexact', '-flags:a', '+bitexact']
    return args + ['-f', spec['format'], 'pipe:1']


def _decode_args(spec):
    args = ['-hide_banner', '-loglevel', 'error', '-f', spec['format']]
    if spec['format'] == 'gsm':
        args += ['-ar', str(TELEPHONE_RATE)]
    # Opus decodes at 48 kHz; roundtrip resamples with the shared soxr step, not ffmpeg's.
    rate = OPUS_DECODE_RATE if spec['format'] == 'ogg' else TELEPHONE_RATE
    return args + ['-c:a', spec['decoder'], '-i', 'pipe:0', '-map_metadata', '-1', '-f', 's16le',
                   '-ar', str(rate), '-ac', '1', 'pipe:1']


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
    if CODECS[codec]['format'] == 'ogg':
        # Audio packets cover the pre-skip plus the input at 48 kHz: ceil((6n + 312) / 960).
        ratio = OPUS_DECODE_RATE // TELEPHONE_RATE
        return -(-(samples * ratio + OPUS_PRE_SKIP) // (FRAME_SAMPLES * ratio))
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


def _crc_table():
    table = []
    for byte in range(256):
        value = byte << 24
        for _ in range(8):
            value = ((value << 1) ^ 0x04C11DB7) if value & 0x80000000 else value << 1
        table.append(value & 0xFFFFFFFF)
    return tuple(table)


_OGG_CRC_TABLE = _crc_table()


def ogg_crc(page):
    """Ogg page checksum (RFC 3533: CRC-32, polynomial 0x04C11DB7, initial 0, no
    reflection, no final xor), over the page with its checksum field zeroed."""
    value = 0
    for byte in page:
        value = ((value << 8) & 0xFFFFFFFF) ^ _OGG_CRC_TABLE[((value >> 24) ^ byte) & 0xFF]
    return value


def _ogg_pages(data):
    """Split an Ogg stream into pages, checking capture pattern, version, checksum, one
    logical stream with consecutive page numbers, BOS first and EOS last. Packets that
    span pages or need a 255 lacing value never occur for these small CBR packets and
    are refused. Returns a list of (27-byte header, [packets])."""
    pages, offset, serial = [], 0, None
    while offset < len(data):
        if len(data) - offset < 27 or data[offset:offset + 4] != b'OggS' or data[offset + 4] != 0:
            raise CodecToolError(f'Ogg page {len(pages)} lacks the OggS version-0 header or is truncated')
        count = data[offset + 26]
        lacing = data[offset + 27:offset + 27 + count]
        body = offset + 27 + count
        end = body + sum(lacing)
        if len(lacing) != count or end > len(data):
            raise CodecToolError(f'Ogg page {len(pages)} is truncated')
        header = data[offset:offset + 27]
        page_type, (page_serial, sequence, checksum) = header[5], struct.unpack('<III', header[14:26])
        page = data[offset:end]
        if ogg_crc(page[:22] + b'\0\0\0\0' + page[26:]) != checksum:
            raise CodecToolError(f'Ogg page {len(pages)} has a wrong checksum')
        if 255 in lacing or not count or page_type & 1:
            raise CodecToolError(f'Ogg page {len(pages)} has continued or empty packet layout')
        serial = page_serial if serial is None else serial
        if page_serial != serial or sequence != len(pages):
            raise CodecToolError(f'Ogg page {len(pages)} belongs to another stream or is out of order')
        if bool(page_type & 2) != (not pages) or page_type & ~0x06:
            raise CodecToolError(f'Ogg page {len(pages)} has header type {page_type:#04x}')
        packets, position = [], body
        for size in lacing:
            packets.append(data[position:position + size])
            position += size
        pages.append((header, packets))
        offset = end
    if not pages or not pages[-1][0][5] & 4 or any(header[5] & 4 for header, _ in pages[:-1]):
        raise CodecToolError('Ogg stream lacks a single final end-of-stream page')
    return pages


def _opus_toc_problem(packet):
    """None for a mono Opus packet holding exactly one 20 ms frame (RFC 6716 §3.1-3.2):
    code 0, or code 3 with a frame count of 1 (libopus CBR pads that way). A lost
    packet is a TOC-only code-0 packet."""
    toc = packet[0]
    config, code = toc >> 3, toc & 0x03
    duration = ((10, 20, 40, 60)[config % 4] if config < 12 else (10, 20)[config % 2] if config < 16
                else (2.5, 5, 10, 20)[config % 4])
    single = code == 0 or (code == 3 and len(packet) >= 2 and packet[1] & 0x3F == 1)
    if toc & 0x04 or not single or duration != 20:
        return f'TOC {toc:#04x} is not a mono single-frame 20 ms packet'
    return None


def _opus_packets(pages, codec, allow_lost=False):
    """Check the Ogg Opus headers this pipeline writes (RFC 7845: OpusHead v1, mono,
    pre-skip OPUS_PRE_SKIP, input rate 8000, gain 0, mapping 0; one OpusTags) and the
    audio packets (CBR size of `codec`, TOC rule). With `allow_lost`, TOC-only packets
    (lost packets) are accepted. Returns the audio packets."""
    if len(pages) < 3 or len(pages[0][1]) != 1 or len(pages[1][1]) != 1:
        raise CodecToolError('Ogg Opus stream lacks its OpusHead/OpusTags header pages or audio')
    head, tags = pages[0][1][0], pages[1][1][0]
    if len(head) != 19 or head[:8] != b'OpusHead' or struct.unpack('<BBHIhB', head[8:]) != (
            1, 1, OPUS_PRE_SKIP, TELEPHONE_RATE, 0, 0):
        raise CodecToolError('OpusHead is not v1 mono, 8 kHz input, the pinned pre-skip, gain 0, mapping 0')
    if not tags.startswith(b'OpusTags'):
        raise CodecToolError('Second Ogg Opus packet is not OpusTags')
    size = OPUS_FRAME_BYTES[CODECS[codec]['bit_rate']]
    packets = [packet for _, page_packets in pages[2:] for packet in page_packets]
    for index, packet in enumerate(packets):
        if len(packet) != size and not (allow_lost and len(packet) == 1 and packet[0] & 0x03 == 0):
            raise CodecToolError(f'Opus packet {index} is {len(packet)} bytes, expected {size} for {codec}')
        problem = _opus_toc_problem(packet)
        if problem:
            raise CodecToolError(f'Opus packet {index}: {problem}')
    return packets


def _ogg_replace_packets(pages, audio_packets):
    """Rebuild the stream with new audio packets (same count per page, same granule
    positions), recomputing lacing values and checksums."""
    output, queue = [], list(audio_packets)
    for index, (header, packets) in enumerate(pages):
        if index >= 2:
            if len(queue) < len(packets):
                raise ValueError('fewer audio packets than the stream holds')
            packets, queue = queue[:len(packets)], queue[len(packets):]
        page = bytearray(header[:22] + b'\0\0\0\0' + bytes([len(packets)]) + bytes(len(p) for p in packets)
                         + b''.join(packets))
        page[22:26] = struct.pack('<I', ogg_crc(page))
        output.append(bytes(page))
    if queue:
        raise ValueError('more audio packets than the stream holds')
    return b''.join(output)


def _check_stream(encoded, codec, frames=None):
    """Known-answer framing check: exact frame size, mode and (when `frames` is given)
    count for the requested codec, so a different mode, codec or truncated stream is
    refused before or after ffmpeg sees it."""
    spec = CODECS[codec]
    if spec['format'] == 'ogg':
        packets = _opus_packets(_ogg_pages(encoded), codec)
        if frames is not None and len(packets) != frames:
            raise CodecToolError(f'Opus stream has {len(packets)} packets, expected {frames}')
        return
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


def _as_packet_concealment(frames):
    """The frame_concealment record under the G.711 `packet_concealment` key names."""
    if frames is None:
        return None
    rename = {'frames': 'packets', 'concealed_frames': 'concealed_packets',
              'lost_and_late_frames': 'lost_and_late_packets'}
    return {rename.get(key, key): value for key, value in frames.items()}


def telephone_codec_roundtrip(canonical, codec, ffmpeg, *, rate=16000, loss_plan=None, jitter_plan=None):
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
    # Plans are validated by Ffmpeg.roundtrip; passed only when given.
    plans = {key: value for key, value in (('loss_plan', loss_plan), ('jitter_plan', jitter_plan))
             if value is not None}
    decoded, record = ffmpeg.roundtrip(narrow, codec, **plans)
    return decoded, {
        **record, 'input_rate': rate, 'input_sample_count': len(samples),
        'input_sha256': hashlib.sha256(canonical).hexdigest(),
        'resampler': 'bypass' if rate == TELEPHONE_RATE else 'soxr_hq',
        'quantization': 'round_nearest_even_clip_pcm16', 'saturated_samples': saturated,
        # Same key as the G.711 record, so FR-D6 impairment counts see AMR frame loss.
        'sample_count': len(decoded) // 2, 'packet_loss': record.get('frame_loss'),
        'jitter': record.get('frame_jitter'),
        # G.711 key names (codecs.telephone_roundtrip), so FR-D6 union totals read one schema.
        'packet_concealment': _as_packet_concealment(record.get('frame_concealment')),
        **library_versions(),
    }
