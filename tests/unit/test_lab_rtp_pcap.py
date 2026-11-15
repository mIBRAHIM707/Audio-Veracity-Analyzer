"""Claude-authored tests (15 November 2026) for lab/asterisk/rtp_pcap.py (backlog 6.1;
PRD FR-T1). Authored by Claude (Claude Code), not by GPT-6 Astra. Pure: no Docker,
no ffmpeg; the Docker lab cases are in tests/lab/test_asterisk_lab.py.
"""
from pathlib import Path
import struct
import tempfile
import unittest

import numpy as np

from data.prepare import codecs as g711
from lab.asterisk import rtp_pcap


def speechlike(seconds, seed, rate=8000):
    """Band-limited noise with a syllable-rate envelope (aperiodic, so alignment is unique)."""
    rng = np.random.default_rng(seed)
    n = round(seconds * rate)
    spectrum = np.fft.rfft(rng.normal(0, 1, n))
    freqs = np.fft.rfftfreq(n, 1 / rate)
    spectrum[(freqs < 250) | (freqs > 3300)] = 0
    signal = np.fft.irfft(spectrum, n)
    envelope = 0.55 + 0.45 * np.sin(2 * np.pi * 4.0 * np.arange(n) / rate)
    signal = signal * envelope
    return np.rint(signal / np.abs(signal).max() * 9000).astype('<i2').tobytes()


def read_pcap(path):
    """Independent reader of the classic libpcap layout."""
    data = Path(path).read_bytes()
    magic, major, minor, _, _, _, link = struct.unpack('<IHHiIII', data[:24])
    packets, offset = [], 24
    while offset < len(data):
        sec, usec, incl, orig = struct.unpack('<IIII', data[offset:offset + 16])
        assert incl == orig and usec < 1_000_000
        packets.append((sec + usec / 1e6, data[offset + 16:offset + 16 + incl]))
        offset += 16 + incl
    return (magic, major, minor, link), packets


class RtpPcapTests(unittest.TestCase):
    def test_g711_payloads_and_capture_layout(self):
        pcm = speechlike(1.013, 1)  # 50 full frames and a partial one
        payloads = rtp_pcap.rtp_payloads(pcm, 'ulaw')
        self.assertEqual(len(payloads), 51)
        self.assertTrue(all(len(p) == 160 for p in payloads))
        self.assertEqual(b''.join(payloads)[:len(pcm) // 2], g711.encode(pcm, 'ulaw'))
        self.assertEqual(b''.join(payloads)[len(pcm) // 2:], g711.encode(bytes(len(b''.join(payloads)) * 2 - len(pcm)),
                                                                         'ulaw'))  # zero padding
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'x.pcap')
            rtp_pcap.write_pcap(payloads, 0, path)
            header, packets = read_pcap(path)
        self.assertEqual(header, (0xA1B2C3D4, 2, 4, 1))
        self.assertEqual(len(packets), 51)
        times = [t for t, _ in packets]
        self.assertTrue(np.allclose(np.diff(times), 0.02, atol=2e-6))
        for index, (_, frame) in enumerate(packets):
            self.assertEqual(frame[12:14], b'\x08\x00')
            ip = frame[14:34]
            self.assertEqual(rtp_pcap._ipv4_checksum(ip), 0, 'a valid header sums to zero')
            self.assertEqual((ip[9], struct.unpack('!H', ip[2:4])[0]), (17, len(frame) - 14))
            udp_length = struct.unpack('!H', frame[38:40])[0]
            self.assertEqual(udp_length, 8 + 12 + 160)
            version, marker_pt, seq, ts, ssrc = struct.unpack('!BBHII', frame[42:54])
            self.assertEqual((version, marker_pt & 0x7F, seq, ts, ssrc),
                             (0x80, 0, 1 + index, 160 * index, rtp_pcap.SSRC))
            self.assertEqual(bool(marker_pt & 0x80), index == 0)
            self.assertEqual(frame[54:], payloads[index])

    def test_wav_command_line_and_refused_inputs_leave_no_file(self):
        import wave
        from data.prepare.conditioning import resample_to_pcm16
        stereo16k = np.repeat(np.frombuffer(speechlike(1.0, 6, rate=16000), dtype='<i2'), 2)
        with tempfile.TemporaryDirectory() as folder:
            wav, out = Path(folder, 'in.wav'), Path(folder, 'out.pcap')
            with wave.open(str(wav), 'wb') as handle:
                handle.setnchannels(2)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(stereo16k.tobytes())
            expected, _ = resample_to_pcm16(stereo16k.reshape(-1, 2).astype(np.float64).mean(axis=1), 16000, 8000)
            self.assertEqual(rtp_pcap.wav_to_pcm8k(wav), expected)
            self.assertEqual(rtp_pcap.main(['--wav', str(wav), '--codec', 'alaw', '--out', str(out)]), 0)
            _, packets = read_pcap(out)
            self.assertEqual(len(packets), 50)
            self.assertEqual(b''.join(frame[54:] for _, frame in packets), g711.encode(expected, 'alaw'))
            Path(folder, 'bad.wav').write_bytes(b'RIFF\x00\x00\x00\x00WAVEjunk')
            with self.assertRaises(ValueError):
                rtp_pcap.wav_to_pcm8k(Path(folder, 'bad.wav'))
            for kwargs in ({'payload_type': 128}, {'src': ('10.0.0', 1)}, {'dst': ('10.0.0.2', 0)}):
                target = Path(folder, f'refused-{len(kwargs)}-{list(kwargs)[0]}.pcap')
                options = {'payload_type': 0, **kwargs}
                payload_type = options.pop('payload_type')
                with self.assertRaises(ValueError):
                    rtp_pcap.write_pcap([b'\xff' * 160], payload_type, target, **options)
                self.assertFalse(target.exists(), 'validated before the file is opened')
            with self.assertRaises(ValueError):
                rtp_pcap.write_pcap([b''], 0, Path(folder, 'empty-payload.pcap'))

    def test_sequence_and_timestamp_wrap(self):
        packet = rtp_pcap.rtp_packet(b'x', 8, 0x1FFFF, 0x1_0000_00A0)
        self.assertEqual(struct.unpack('!BBHI', packet[:8]), (0x80, 8, 0xFFFF, 0xA0))

    def test_reference_decode_and_refusals(self):
        pcm = speechlike(0.2, 2)
        for law in ('ulaw', 'alaw'):
            payloads = rtp_pcap.rtp_payloads(pcm, law)
            self.assertEqual(rtp_pcap.reference_decode(payloads, law), g711.decode(g711.encode(pcm, law), law))
        for bad in (b'', b'\x00', 'pcm', bytes(2 * (rtp_pcap.MAX_SECONDS * 8000 + 1))):
            with self.assertRaises(ValueError):
                rtp_pcap.rtp_payloads(bad, 'ulaw')
        with self.assertRaises(ValueError):
            rtp_pcap.rtp_payloads(pcm, 'g729')
        with self.assertRaises(ValueError):
            rtp_pcap.rtp_payloads(pcm, 'gsm')  # no ffmpeg
        with self.assertRaises(ValueError):
            rtp_pcap.reference_decode([b'x'], 'gsm')
        with self.assertRaises(ValueError):
            rtp_pcap.write_pcap([], 0, 'unused.pcap')
        with self.assertRaises(ValueError):
            rtp_pcap.rtp_packet(b'', 128, 1, 0)


if __name__ == '__main__':
    unittest.main()
