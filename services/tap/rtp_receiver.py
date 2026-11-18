"""Per-call RTP receiver for the passive tap (backlog 6.2 partial; PRD FR-I1, FR-T2;
architecture §2.1 "RTP receiver": ExternalMedia sends plain RTP to a host and port
owned by the receiver).

`parse_rtp(datagram)` validates an RFC 3550 packet (version 2; CSRC list, header
extension and padding handled; bounded size) and returns its fields. `RtpSession`
owns one UDP socket per tapped call: `poll(timeout)` reads what has arrived, accepts
only the negotiated payload type, only from `allowed_source` (the PBX address) when
configured, and locks onto the first sender's address (other senders and malformed
datagrams are counted and dropped; a new SSRC from the locked address is accepted and
counted as `ssrc_changes`, its sequence tracking restarting); it decodes G.711 to 8 kHz PCM16 in
arrival order, counting sequence gaps, duplicates and late packets. `take()` hands the
new audio over and keeps none, so memory stays bounded; if the consumer stops taking,
audio beyond `max_seconds` (default 10 s) pending is dropped and counted.
`seconds_since_packet` supports the FR-T2 30 s silence teardown. Reordering and
concealment are `jitter.py` (6.3), not wired here yet. GSM payloads are not decoded:
on the ARI path Asterisk transcodes the tap to mu-law.
"""
import socket
import struct
import time

from data.prepare import codecs as g711

MAX_DATAGRAM = 1500
PAYLOADS = {0: 'ulaw', 8: 'alaw'}  # static RTP payload types decoded here (RFC 3551)
FRAME_BYTES_LIMIT = 960  # 120 ms of G.711, the largest payload accepted
JITTER_FRAME_BYTES = 160  # 20 ms of G.711


class RtpError(ValueError):
    """A datagram that is not a usable RTP packet."""


def parse_rtp(datagram):
    """RFC 3550 §5.1 -> dict(version, marker, payload_type, sequence, timestamp, ssrc,
    payload). Raises RtpError on anything malformed."""
    if not isinstance(datagram, (bytes, bytearray)) or not 12 <= len(datagram) <= MAX_DATAGRAM:
        raise RtpError('datagram size outside 12..1500 bytes')
    first, second, sequence, timestamp, ssrc = struct.unpack('!BBHII', datagram[:12])
    if first >> 6 != 2:
        raise RtpError('not RTP version 2')
    offset = 12 + 4 * (first & 0x0F)
    if first & 0x10:  # header extension: 16-bit profile, 16-bit length in words
        if len(datagram) < offset + 4:
            raise RtpError('truncated header extension')
        offset += 4 + 4 * struct.unpack('!H', datagram[offset + 2:offset + 4])[0]
    end = len(datagram)
    if first & 0x20:  # padding: last byte counts the padding bytes
        end -= datagram[-1]
        if datagram[-1] == 0:
            raise RtpError('zero padding count')
    if offset > end:
        raise RtpError('header longer than the packet')
    return {'version': 2, 'marker': bool(second & 0x80), 'payload_type': second & 0x7F, 'sequence': sequence,
            'timestamp': timestamp, 'ssrc': ssrc, 'payload': bytes(datagram[offset:end])}


class RtpSession:
    """One tapped call direction. Owned by one thread; `close()` releases the socket."""

    def __init__(self, *, payload_type=0, host='0.0.0.0', port=0, max_seconds=10, clock=time.monotonic,
                 allowed_source=None, jitter=None):
        """`jitter`: a `jitter.JitterBuffer` (playout in sequence order with concealment;
        only 20 ms packets accepted, others counted as `other_ptime`), or None for the
        plain arrival-order mode."""
        self._jitter = jitter
        if payload_type not in PAYLOADS:
            raise ValueError(f'payload type must be one of {sorted(PAYLOADS)} (G.711)')
        if type(max_seconds) is not int or not 1 <= max_seconds <= 3600:
            raise ValueError('max_seconds must be an integer from 1 to 3600')
        self.payload_type, self._law, self._clock = payload_type, PAYLOADS[payload_type], clock
        self.allowed_source, self.source = allowed_source, None
        self._limit = max_seconds * 8000 * 2
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._socket.bind((host, port))
        except OSError:
            self._socket.close()
            raise
        self.port = self._socket.getsockname()[1]
        self._chunks, self._bytes = [], 0
        self.ssrc = None
        self._last_sequence = None
        self._last_packet = None
        self._opened = clock()
        self.stats = {'packets': 0, 'malformed': 0, 'other_payload': 0, 'other_source': 0, 'gaps': 0,
                      'missing': 0, 'late_or_duplicate': 0, 'over_limit': 0, 'ssrc_changes': 0,
                      'other_ptime': 0}

    def close(self):
        self._socket.close()

    @property
    def closed(self):
        return self._socket.fileno() == -1

    def seconds_since_packet(self):
        """Seconds since the last accepted packet (or since the session opened)."""
        return self._clock() - (self._last_packet if self._last_packet is not None else self._opened)

    def poll(self, timeout=0.0, limit=500):
        """Read up to `limit` datagrams, waiting at most `timeout` for the first.
        Returns the number of packets accepted."""
        accepted = 0
        self._socket.settimeout(timeout if timeout > 0 else 0.0)
        for _ in range(limit):
            try:
                datagram, address = self._socket.recvfrom(MAX_DATAGRAM + 1)
            except (BlockingIOError, socket.timeout):
                break
            except ConnectionResetError:  # Windows reports an ICMP port-unreachable this way
                continue
            self._socket.settimeout(0.0)
            accepted += self._accept(datagram, address)
        return accepted

    def _accept(self, datagram, address=None):
        if self.allowed_source is not None and (address is None or address[0] != self.allowed_source):
            self.stats['other_source'] += 1
            return 0
        try:
            packet = parse_rtp(datagram)
        except RtpError:
            self.stats['malformed'] += 1
            return 0
        if packet['payload_type'] != self.payload_type:
            self.stats['other_payload'] += 1
            return 0
        if not packet['payload'] or len(packet['payload']) > FRAME_BYTES_LIMIT:
            self.stats['malformed'] += 1
            return 0
        if self.ssrc is None:  # lock onto the first sender's address
            self.ssrc, self.source = packet['ssrc'], address
        elif address != self.source:
            self.stats['other_source'] += 1
            return 0
        elif packet['ssrc'] != self.ssrc:  # same sender, new stream (e.g. a bridge change)
            self.stats['ssrc_changes'] += 1
            self.ssrc, self._last_sequence = packet['ssrc'], None
            if self._jitter is not None:
                self._jitter.restart(self._clock())  # unrelated sequence numbers: anchor afresh
        if self._jitter is not None:
            if len(packet['payload']) != JITTER_FRAME_BYTES:
                self.stats['other_ptime'] += 1  # the buffer plays 20 ms frames only
                return 0
            outcome = self._jitter.push(packet['sequence'], g711.decode(packet['payload'], self._law), self._clock())
            if outcome in ('late', 'duplicate'):
                self.stats['late_or_duplicate'] += 1
                return 0
            self._last_packet = self._clock()
            self.stats['packets'] += 1
            return 1
        if self._last_sequence is not None:
            step = (packet['sequence'] - self._last_sequence) & 0xFFFF
            if step == 0 or step >= 0x8000:  # duplicate, or behind the newest (late)
                self.stats['late_or_duplicate'] += 1
                return 0
            if step > 1:
                self.stats['gaps'] += 1
                self.stats['missing'] += step - 1
        self._last_sequence = packet['sequence']
        self._last_packet = self._clock()
        self.stats['packets'] += 1
        pcm = g711.decode(packet['payload'], self._law)
        if self._bytes + len(pcm) > self._limit:
            self.stats['over_limit'] += 1  # the consumer is not taking audio: drop, counted
            return 1
        self._chunks.append(pcm)
        self._bytes += len(pcm)
        return 1

    def take(self):
        """Decoded 8 kHz PCM16 since the previous take (jitter mode: the frames due
        now, concealed ones included). The session keeps no audio after handing it over."""
        return b''.join(pcm for pcm, _ in self.take_frames())

    def receiver_stats(self):
        """The session's counts plus, in jitter mode, the buffer's (prefixed `jitter_`)."""
        stats = dict(self.stats)
        if self._jitter is not None:
            stats.update({f'jitter_{key}': value for key, value in self._jitter.stats.items()})
        return stats

    def take_frames(self, final=False):
        """[(pcm16_bytes, concealed_samples), ...]. Jitter mode: frames due now in
        sequence order (`final=True`: everything received, holes concealed); plain mode:
        one chunk in arrival order with 0 concealed."""
        if self._jitter is not None:
            # The clock times each received frame's hold in the buffer (FR-O6, 18 Nov).
            return self._jitter.flush(self._clock()) if final else self._jitter.pop_due(self._clock())
        chunk = b''.join(self._chunks)
        self._chunks, self._bytes = [], 0
        return [(chunk, 0)] if chunk else []
