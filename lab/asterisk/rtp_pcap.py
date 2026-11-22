"""Build an RTP capture that SIPp plays into a lab call (backlog 6.1; PRD FR-T1 "play
genuine and synthetic audio files into calls, with codec forcing for G.711 and GSM").

`rtp_payloads(pcm, codec)` cuts 8 kHz mono PCM16 into 20 ms RTP payloads (G.711 mu-law
/A-law with the project's own encoder, GSM 06.10 through the pinned ffmpeg as raw
33-byte frames, RFC 3551); `write_pcap` writes them as a classic libpcap file
(Ethernet/IPv4/UDP/RTP, 20 ms apart) for SIPp's `play_pcap_audio`, which re-addresses
the packets to the call's negotiated media port. `reference_decode` is what an ideal
receiver reconstructs, for comparison with what Asterisk recorded. The last frame is
zero-padded. Pure apart from GSM, which needs an `ffmpeg_codecs.Ffmpeg`.
"""
import struct

from data.prepare import codecs as g711

RATE = 8000
FRAME_SAMPLES = 160
GSM_FRAME_BYTES = 33
MAX_SECONDS = 600
# codec -> (static RTP payload type, SDP rtpmap encoding name), RFC 3551 table 4
PAYLOAD_TYPES = {'ulaw': (0, 'PCMU'), 'alaw': (8, 'PCMA'), 'gsm': (3, 'GSM')}
SSRC = 0x41564131  # 'AVA1'


def _frames(pcm):
    if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
        raise ValueError('Expected nonempty mono PCM16 bytes at 8 kHz')
    if len(pcm) // 2 > MAX_SECONDS * RATE:
        raise ValueError(f'Input longer than {MAX_SECONDS} s')
    padded = pcm + bytes(-len(pcm) % (2 * FRAME_SAMPLES))
    return [padded[i:i + 2 * FRAME_SAMPLES] for i in range(0, len(padded), 2 * FRAME_SAMPLES)]


def rtp_payloads(pcm, codec, ffmpeg=None):
    """8 kHz PCM16 -> one payload per 20 ms frame."""
    if codec not in PAYLOAD_TYPES:
        raise ValueError(f'codec must be one of {sorted(PAYLOAD_TYPES)}')
    frames = _frames(pcm)
    if codec in g711.LAWS:
        return [g711.encode(frame, codec) for frame in frames]
    if ffmpeg is None:
        raise ValueError('GSM payloads need an ffmpeg_codecs.Ffmpeg')
    encoded = ffmpeg.encode(b''.join(frames), 'gsm')
    payloads = [encoded[i:i + GSM_FRAME_BYTES] for i in range(0, len(encoded), GSM_FRAME_BYTES)]
    if len(payloads) != len(frames) or any(len(p) != GSM_FRAME_BYTES for p in payloads):
        raise ValueError('GSM encoder returned an unexpected frame layout')
    return payloads


def reference_decode(payloads, codec, ffmpeg=None):
    """What a receiver decoding every payload in order reconstructs (8 kHz PCM16)."""
    if codec in g711.LAWS:
        return g711.decode(b''.join(payloads), codec)
    if codec != 'gsm' or ffmpeg is None:
        raise ValueError('reference_decode needs a known codec (and ffmpeg for GSM)')
    return ffmpeg.decode(b''.join(payloads), 'gsm')


def _ipv4_checksum(header):
    total = sum(struct.unpack('!10H', header))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def rtp_packet(payload, payload_type, sequence, timestamp, ssrc=SSRC, marker=False):
    """RFC 3550 fixed header (V=2, no padding/extension/CSRC) plus payload."""
    if not 0 <= payload_type <= 127:
        raise ValueError('payload type must be 0..127')
    return struct.pack('!BBHII', 0x80, (0x80 if marker else 0) | payload_type, sequence & 0xFFFF,
                       timestamp & 0xFFFFFFFF, ssrc) + payload


def write_pcap(payloads, payload_type, path, *, src=('10.0.0.1', 40000), dst=('10.0.0.2', 6000),
               start_sequence=1, start_timestamp=0, start_time=1_700_000_000.0):
    """Write payloads as a libpcap file of RTP packets, one per 20 ms. Addresses are
    placeholders: SIPp's play_pcap_audio rewrites them for the live call."""
    if not payloads or not all(isinstance(p, bytes) and 0 < len(p) <= 1400 for p in payloads):
        raise ValueError('payloads must be 1..1400-byte bytes objects')
    if not 0 <= payload_type <= 127:
        raise ValueError('payload type must be 0..127')
    for address in (src, dst):
        if not 0 < address[1] < 65536 or len(address[0].split('.')) != 4:
            raise ValueError('addresses must be (dotted IPv4, port 1..65535)')
    src_ip, dst_ip = (bytes(int(part) for part in address.split('.')) for address in (src[0], dst[0]))
    with open(path, 'wb') as handle:
        handle.write(struct.pack('<IHHiIII', 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))  # Ethernet link type
        for index, payload in enumerate(payloads):
            rtp = rtp_packet(payload, payload_type, start_sequence + index,
                             start_timestamp + index * FRAME_SAMPLES, marker=index == 0)
            udp = struct.pack('!HHHH', src[1], dst[1], 8 + len(rtp), 0) + rtp  # checksum 0: unused (IPv4)
            header = bytearray(struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(udp), index & 0xFFFF, 0, 64, 17, 0,
                                           src_ip, dst_ip))
            header[10:12] = struct.pack('!H', _ipv4_checksum(bytes(header)))
            frame = b'\x02\x00\x00\x00\x00\x02' + b'\x02\x00\x00\x00\x00\x01' + b'\x08\x00' + bytes(header) + udp
            when = start_time + index * FRAME_SAMPLES / RATE
            seconds = int(when)
            micros = min(round((when - seconds) * 1e6), 999_999)
            handle.write(struct.pack('<IIII', seconds, micros, len(frame), len(frame)))
            handle.write(frame)


def wav_to_pcm8k(path):
    """A plain-PCM16 WAV file (mono or stereo, 8-192 kHz) -> 8 kHz mono PCM16 bytes
    through the shared conditioning steps (arithmetic-mean downmix, soxr HQ, round and
    clip). No gain change. Raises ValueError for anything else."""
    import wave

    import numpy as np

    from data.prepare.conditioning import resample_to_pcm16
    from data.prepare.wav_format import pcm_format_problem
    with open(path, 'rb') as handle:
        head = handle.read(64 * 1024)
    problem = pcm_format_problem(head)
    if problem:
        raise ValueError(f'{path}: {problem}')
    with wave.open(str(path), 'rb') as handle:
        channels, width, rate, frames = (handle.getnchannels(), handle.getsampwidth(), handle.getframerate(),
                                         handle.getnframes())
        if width != 2 or channels not in (1, 2) or not 8000 <= rate <= 192000:
            raise ValueError('expected 16-bit mono or stereo PCM at 8-192 kHz')
        if frames > MAX_SECONDS * rate:
            raise ValueError(f'longer than {MAX_SECONDS} s')
        raw = handle.readframes(frames)
    samples = np.frombuffer(raw, dtype='<i2').astype(np.float64).reshape(-1, channels).mean(axis=1)
    if not len(samples):
        raise ValueError('no audio frames')
    pcm, _ = resample_to_pcm16(samples, rate, RATE)
    return pcm


def main(argv=None):
    """Command line: WAV file -> RTP capture for the lab call generator."""
    import argparse
    parser = argparse.ArgumentParser(description='WAV -> RTP pcap for lab/asterisk/sipp/uac_inject.xml')
    parser.add_argument('--wav', required=True)
    parser.add_argument('--codec', required=True, choices=sorted(PAYLOAD_TYPES))
    parser.add_argument('--out', required=True, help='pcap path, outside the repository (e.g. the lab I/O folder)')
    parser.add_argument('--ffmpeg', help='pinned ffmpeg for GSM (else AVA_FFMPEG)')
    args = parser.parse_args(argv)
    ffmpeg = None
    if args.codec == 'gsm':
        from data.prepare.ffmpeg_codecs import Ffmpeg
        ffmpeg = Ffmpeg(args.ffmpeg)
    payloads = rtp_payloads(wav_to_pcm8k(args.wav), args.codec, ffmpeg)
    payload_type, name = PAYLOAD_TYPES[args.codec]
    write_pcap(payloads, payload_type, args.out)
    print(f'{len(payloads)} packets ({len(payloads) * 20} ms), payload type {payload_type} ({name}) -> {args.out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
