"""Receive-side jitter buffer and loss monitor for tapped RTP (backlog 6.3 partial; PRD
FR-I2 "maintain a jitter buffer and reassemble audio in order; tolerate up to 5% packet
loss with concealment"; architecture §2.2 step 1: 60 ms depth, sequence-number reorder,
repeat the last 20 ms frame, zero-fill after 3 consecutive losses).

`JitterBuffer` is pure and clock-explicit (arrival times are passed in). Frames are
indexed by the extended (unwrapped) RTP sequence number and must be 20 ms (the
receiver must refuse other packet times). The first packet anchors the playout clock:
frame k is due at `anchor + (k - first) * frame_s + depth_s`. `pop_due(now)` returns
the frames due by `now` (at most `limit` per call), in order, each with the number of
concealed samples: a received frame has 0; a missing frame repeats the last frame
played for up to `repeat_limit` consecutive losses, then is digital silence; both
count as concealed. A packet arriving a little after its frame was played is `late`
and dropped; a second copy is `duplicate`. Before playout starts, an earlier sequence
number moves the start back (start-up reordering). The clock is re-anchored
(`resync`) when a packet is more than 1 s behind the playout point or 3 packets in a
row are late (the sender paused with contiguous sequence numbers, or reset them), or a
packet is at least `max_ahead` frames ahead (a sequence jump); frames still waiting
are played out first. A discontinuity so costs a bounded gap, not the rest of the call. Slow clock drift between sender and receiver is absorbed by moving the
anchor one frame when the smoothed slack leaves [0.25, 1.75] x depth. `flush()` plays
everything received so far, concealing the holes.

This is the training-side G.711 `repeat_then_silence` rule (data/prepare/codecs.py)
for mu-law, whose silence code decodes to 0; for A-law the training silence code
decodes to +8 while this buffer inserts 0.

`LossMonitor` keeps the concealed fraction over the last `window_frames` played frames
(default 500 = 10 s) and reports a change when it rises above `threshold` (5 %,
FR-I2) and when it falls back to or below it, so a caller can emit
`system.health reason=media_quality` once per episode. The 10 s span is a choice to
confirm in the PRD ("sustained" is not defined).
"""
from collections import deque

FRAME_SAMPLES = 160
FRAME_S = 0.020
DEFAULT_DEPTH_S = 0.060
REPEAT_LIMIT = 3
SILENCE = bytes(2 * FRAME_SAMPLES)
DRIFT_ALPHA = 0.02  # smoothing of the playout slack (about 50 packets = 1 s)
# A packet more than 1 s behind the playout point, or 3 late packets in a row, means the
# sender paused (contiguous sequence numbers) or reset its sequence, not network delay.
RESYNC_BEHIND_FRAMES = 50
RESYNC_LATE_RUN = 3


def _check_time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or abs(value) == float('inf'):
        raise ValueError('times must be finite numbers of seconds')


class JitterBuffer:
    def __init__(self, *, depth_s=DEFAULT_DEPTH_S, repeat_limit=REPEAT_LIMIT, max_ahead=250):
        if not 0 <= depth_s <= 1.0:
            raise ValueError('depth_s must be from 0 to 1 s')
        if type(repeat_limit) is not int or repeat_limit < 0:
            raise ValueError('repeat_limit must be a non-negative integer')
        if type(max_ahead) is not int or max_ahead < 1:
            raise ValueError('max_ahead must be a positive integer')
        self.depth_s, self.repeat_limit, self.max_ahead = depth_s, repeat_limit, max_ahead
        self._frames = {}  # extended sequence -> PCM16 bytes
        self._pending_out = []  # frames played out at a resync, handed over by pop_due
        self._late_run = 0
        self._first = self._first_arrival = None
        self._next = None  # next extended sequence to play
        self._highest = None  # highest extended sequence received
        self._slack = None
        self._last_played = SILENCE
        self._run = 0  # consecutive concealed frames
        self.stats = {'received': 0, 'played': 0, 'concealed_frames': 0, 'repeated_frames': 0,
                      'silence_frames': 0, 'late': 0, 'duplicate': 0, 'reordered': 0, 'resyncs': 0,
                      'drift_adjustments': 0}

    def _extend(self, sequence):
        """16-bit RTP sequence -> extended sequence nearest the highest seen so far."""
        if self._highest is None:
            return sequence
        candidate = (self._highest & ~0xFFFF) | sequence
        if candidate - self._highest > 0x8000:
            candidate -= 0x10000
        elif self._highest - candidate > 0x8000:
            candidate += 0x10000
        return candidate

    def push(self, sequence, pcm, arrival):
        """Offer one decoded 20 ms frame. Returns 'accepted', 'late', 'duplicate' or
        'resync' (accepted after re-anchoring the playout clock)."""
        if type(sequence) is not int or not 0 <= sequence <= 0xFFFF:
            raise ValueError('sequence must be a 16-bit integer')
        if not isinstance(pcm, bytes) or len(pcm) != 2 * FRAME_SAMPLES:
            raise ValueError('pcm must be one 20 ms frame of 8 kHz PCM16 (320 bytes); '
                             'other packet times must be refused by the receiver')
        _check_time(arrival)
        extended = self._extend(sequence)
        if self._first is None:
            self._anchor(extended, arrival)
        if extended < self._next:
            behind = self._next - extended
            if self.stats['played'] == 0 and behind < self.max_ahead:
                self._first, self._next = extended, extended  # reordered before playout began
            else:
                self._late_run += 1
                if behind > RESYNC_BEHIND_FRAMES or self._late_run >= RESYNC_LATE_RUN:
                    self._resync(extended, arrival)  # sender pause or sequence reset
                    return self._store(extended, pcm, arrival, 'resync')
                self.stats['late'] += 1  # also an extra copy of a frame already played
                return 'late'
        self._late_run = 0
        if extended in self._frames:
            self.stats['duplicate'] += 1
            return 'duplicate'
        if extended - self._next >= self.max_ahead:
            self._resync(extended, arrival)  # sequence jump forward
            return self._store(extended, pcm, arrival, 'resync')
        if self._highest is not None and extended < self._highest:
            self.stats['reordered'] += 1
        return self._store(extended, pcm, arrival, 'accepted')

    def _anchor(self, extended, arrival):
        self._first, self._first_arrival, self._next = extended, arrival, extended
        self._slack = None

    def _resync(self, extended, arrival):
        """Re-anchor on this packet. Frames still waiting are played out first (handed
        to the next `pop_due`), not discarded."""
        self.stats['resyncs'] += 1
        self._pending_out.extend(self.flush())
        self._frames.clear()
        self._highest = None
        self._late_run = 0
        self._anchor(extended, arrival)

    def _store(self, extended, pcm, arrival, outcome):
        self._frames[extended] = pcm
        self._highest = extended if self._highest is None else max(self._highest, extended)
        self.stats['received'] += 1
        self._track_drift(extended, arrival)
        return outcome

    def _track_drift(self, extended, arrival):
        """Keep the playout delay near `depth_s` when sender and receiver clocks drift:
        a smoothed slack (due time minus arrival) below a quarter of the depth moves the
        playout clock one frame later; above 1.75 x depth, one frame earlier. Neither
        drops nor repeats audio by itself; it only shifts when frames become due."""
        if self.depth_s <= 0:
            return
        slack = self._first_arrival + (extended - self._first) * FRAME_S + self.depth_s - arrival
        self._slack = slack if self._slack is None else self._slack + DRIFT_ALPHA * (slack - self._slack)
        if self._slack < 0.25 * self.depth_s:
            self._first_arrival += FRAME_S
            self._slack += FRAME_S
            self.stats['drift_adjustments'] += 1
        elif self._slack > 1.75 * self.depth_s:
            self._first_arrival -= FRAME_S
            self._slack -= FRAME_S
            self.stats['drift_adjustments'] += 1

    def _due_index(self, now):
        """The highest frame index whose playout time is <= now."""
        if self._first is None:
            return None
        elapsed = now - self._first_arrival - self.depth_s
        if elapsed < -1e-9:
            return self._first - 1
        return self._first + int(elapsed / FRAME_S + 1e-9)

    def pop_due(self, now, limit=500):
        """[(pcm16_bytes, concealed_samples), ...] for frames due by `now`, at most
        `limit` per call (call again for the rest)."""
        _check_time(now)
        out = self._pending_out[:limit]
        del self._pending_out[:limit]
        last = self._due_index(now)
        while self._next is not None and self._next <= last and len(out) < limit:
            out.append(self._play())
        return out

    def flush(self):
        """Play every frame up to the highest received one (end of call)."""
        out = []
        while self._highest is not None and self._next <= self._highest:
            out.append(self._play())
        return out

    def _play(self):
        pcm = self._frames.pop(self._next, None)
        self._next += 1
        self.stats['played'] += 1
        if pcm is not None:
            self._last_played, self._run = pcm, 0
            return pcm, 0
        self._run += 1
        self.stats['concealed_frames'] += 1
        if self._run <= self.repeat_limit:
            self.stats['repeated_frames'] += 1
            return self._last_played, FRAME_SAMPLES
        self.stats['silence_frames'] += 1
        self._last_played = SILENCE
        return SILENCE, FRAME_SAMPLES

    @property
    def buffered(self):
        return len(self._frames)


class LossMonitor:
    def __init__(self, *, window_frames=500, threshold=0.05):
        if type(window_frames) is not int or window_frames < 1:
            raise ValueError('window_frames must be a positive integer')
        if not 0 < threshold < 1:
            raise ValueError('threshold must be between 0 and 1')
        self.window_frames, self.threshold = window_frames, threshold
        self._flags = deque(maxlen=window_frames)
        self._concealed = 0
        self.degraded = False

    def add(self, concealed):
        """Record one played frame; returns 'degraded', 'recovered' or None. Only a full
        window can declare degradation (a short call start is not 'sustained')."""
        if len(self._flags) == self.window_frames:
            self._concealed -= self._flags[0]
        flag = 1 if concealed else 0
        self._flags.append(flag)
        self._concealed += flag
        fraction = self._concealed / len(self._flags)
        if not self.degraded and len(self._flags) == self.window_frames and fraction > self.threshold:
            self.degraded = True
            return 'degraded'
        if self.degraded and fraction <= self.threshold:
            self.degraded = False
            return 'recovered'
        return None

    @property
    def fraction(self):
        return self._concealed / len(self._flags) if self._flags else 0.0
