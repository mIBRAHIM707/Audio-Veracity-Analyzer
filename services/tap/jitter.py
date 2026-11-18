"""Receive-side jitter buffer and loss monitor for tapped RTP (backlog 6.3 partial; PRD
FR-I2 "maintain a jitter buffer and reassemble audio in order; tolerate up to 5% packet
loss with concealment"; architecture §2.2 step 1: 60 ms depth, sequence-number reorder,
repeat the last 20 ms frame, zero-fill after 3 consecutive losses).

`JitterBuffer` is pure and clock-explicit (arrival times are passed in). Frames are
indexed by the extended (unwrapped) RTP sequence number and must be 20 ms (the
receiver must refuse other packet times). The first packet anchors the playout clock:
frame k is due at `anchor + (k - first) * frame_s + depth_s`. `pop_due(now)` returns
the frames due by `now` (at most `limit` per call), in order, each with the number of
concealed samples, but **never plays past the highest frame received**: a missing
frame is concealed only when a later packet has arrived (a real hole), repeating the
last frame played for up to `repeat_limit` consecutive losses, then digital silence;
both count as concealed. A stream that stops (end of call, a pause) so produces no
made-up audio and no loss. When packets resume into a buffer that has played
everything and the packet is already past its slot, a new talkspurt starts on it (the
clock re-anchors; nothing dropped). A packet arriving after its frame was played while
later frames are still buffered is `late` and dropped; a second copy is `duplicate`.
Right after any anchoring, an earlier sequence number moves the start back
(reordering at a talkspurt start). The clock is re-anchored (`resync`) when a packet
is more than 1 s behind the playout point or 3 packets in a row are late, or a packet
is at least `max_ahead` frames ahead (a sequence jump); `restart()` does the same for
a new SSRC. Frames still waiting are handed over first (at most 60 s, excess counted). Slow clock drift between sender and receiver is absorbed by moving the
anchor one frame when the smoothed slack leaves [0.25, 1.75] x depth. `flush()` plays
everything received so far, concealing the holes. Concealment in hand-overs (resync,
restart, flush) spends a real-time budget (16 Nov): at most `HANDOVER_BURST_FRAMES`
(1 s) at once, refilled one frame per 20 ms; holes beyond it are skipped
(`dropped_handover`; later holes conceal with silence). Received frames are always
played. Output is so bounded by what the sender sends plus time: at most about
2 x (packets accepted + elapsed frames) + the burst (`pop_due` advances with time and
with one drift step per packet). A sender that keeps jumping its sequence can no longer
multiply made-up audio. Skipped holes are not reported to `LossMonitor` or the sink
(the timeline closes over them); whether they count as FR-I2 loss is a PRD question.

Hold time (18 Nov, PRD FR-O6 "latency per stage"; architecture §3 latency budget "RTP
arrival -> frame available", 60 ms at p95): each received frame keeps the time it was pushed, and
when `pop_due(now)` or `flush(now)` hands it out its hold `now - arrival` is counted in
the integer stats `hold_le_<ms>ms` (cumulative, HOLD_BUCKETS_MS), `hold_frames` and
`hold_us_sum` (microseconds), so they add up across calls like the other counts. Only
received frames are timed (concealed ones never arrived). A frame handed out with no
time (`flush()` without `now`), with a negative hold (a clock that went back) or an
unrepresentable one counts as `hold_unmeasured`; received frames lost to the hand-over
bound are never handed out and not timed (`dropped_pending_received`, also part of
`dropped_pending`). So once everything is handed out, `received == hold_frames +
hold_unmeasured + dropped_pending_received`. In the receiver, "arrival" is when the
service loop read the packet from the socket, once per loop turn, and the hand-out is
also once per turn: the hold is the buffer's delay plus loop-turn quantisation, not the
network arrival-to-frame time (socket-buffer wait is measured by nothing).

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
import math

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
MAX_PENDING_FRAMES = 3000  # frames handed over at resyncs/restarts (60 s); excess dropped, counted
# Hand-overs (resync, restart, flush) play every frame up to the highest received, holes
# concealed, without waiting for their playout time. A sender that keeps jumping its
# sequence number could so make the buffer emit far more made-up audio than real time
# (measured 16 Nov: about 124x with alternating jumps of 249/250). Concealing a hole in
# a hand-over therefore spends a budget that refills at real-time rate (one frame per
# 20 ms of arrival/now time), at most this many; holes beyond it are skipped and counted
# (`dropped_handover`). Received frames never spend it and are never skipped.
HANDOVER_BURST_FRAMES = 50  # 1 s of concealment; a normal hand-over has a hole or two
# Upper bounds (ms) of the hold-time histogram; 60 ms is the depth and the §3 budget.
HOLD_BUCKETS_MS = (20, 40, 60, 80, 100, 150, 200, 300, 500, 1000, 2000, 5000)
HOLD_KEYS = tuple(f'hold_le_{ms}ms' for ms in HOLD_BUCKETS_MS)


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
        self._played_since_anchor = 0
        self._last_played = SILENCE
        self._run = 0  # consecutive concealed frames
        self._handover_budget = float(HANDOVER_BURST_FRAMES)
        self._budget_time = None
        self.stats = {'received': 0, 'played': 0, 'concealed_frames': 0, 'repeated_frames': 0,
                      'silence_frames': 0, 'late': 0, 'duplicate': 0, 'reordered': 0, 'resyncs': 0,
                      'talkspurts': 0, 'restarts': 0, 'drift_adjustments': 0, 'dropped_pending': 0,
                      'dropped_handover': 0, 'dropped_pending_received': 0, **{key: 0 for key in HOLD_KEYS}, 'hold_frames': 0,
                      'hold_us_sum': 0, 'hold_unmeasured': 0}

    def _deliver(self, entries, now):
        """(pcm, concealed, arrival) -> (pcm, concealed), timing each received frame's
        hold up to `now` (see the module docstring)."""
        for _, _, arrival in entries:
            if arrival is None:
                continue  # concealed: never arrived
            held = None if now is None else (now - arrival) * 1e6
            held_us = round(held) if held is not None and math.isfinite(held) else None  # review L2: no overflow
            if held_us is None or held_us < 0:
                self.stats['hold_unmeasured'] += 1
                continue
            self.stats['hold_frames'] += 1
            self.stats['hold_us_sum'] += held_us
            for ms, key in zip(HOLD_BUCKETS_MS, HOLD_KEYS):
                if held_us <= ms * 1000:
                    self.stats[key] += 1
        return [(pcm, concealed) for pcm, concealed, _ in entries]

    def _refill(self, now):
        """Hand-over budget: one frame per 20 ms of (non-decreasing) time, capped."""
        if self._budget_time is not None and now > self._budget_time:
            self._handover_budget = min(float(HANDOVER_BURST_FRAMES),
                                        self._handover_budget + (now - self._budget_time) / FRAME_S)
        if self._budget_time is None or now > self._budget_time:
            self._budget_time = now

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
        self._refill(arrival)
        extended = self._extend(sequence)
        if self._first is None:
            self._anchor(extended, arrival)
        idle = self._highest is None or self._next > self._highest  # everything received was played
        if idle and extended >= self._next and self._due(extended) < arrival:
            # The stream resumes after a pause (no packets, contiguous sequence numbers or a
            # few lost at its end): start a new talkspurt on this packet. The pause is not
            # loss and nothing is dropped.
            self.stats['talkspurts'] += 1
            self._anchor(extended, arrival)
            self._late_run = 0
            return self._store(extended, pcm, arrival, 'accepted')
        if extended < self._next:
            behind = self._next - extended
            if self._played_since_anchor == 0 and behind < self.max_ahead:
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
        self._played_since_anchor = 0

    def _due(self, extended):
        return self._first_arrival + (extended - self._first) * FRAME_S + self.depth_s

    def _hand_over(self):
        """Play out what is buffered into the pending output (bounded; excess counted)."""
        frames = self._play_to_highest()
        room = max(0, MAX_PENDING_FRAMES - len(self._pending_out))
        self._pending_out.extend(frames[:room])
        self.stats['dropped_pending'] += len(frames) - min(len(frames), room)
        # Of those, frames that really arrived (review L3): never handed out, so never timed.
        self.stats['dropped_pending_received'] += sum(1 for _, _, arrival in frames[room:] if arrival is not None)

    def _resync(self, extended, arrival):
        """Re-anchor on this packet. Frames still waiting are played out first (handed
        to the next `pop_due`), not discarded."""
        self.stats['resyncs'] += 1
        self._hand_over()
        self._frames.clear()
        self._highest = None
        self._late_run = 0
        self._anchor(extended, arrival)

    def restart(self, now):
        """A new stream from the same sender (SSRC change): play out what is buffered
        and let the next packet anchor afresh (its sequence numbers are unrelated).
        `now` (the receiver's clock) refills the hand-over budget first."""
        _check_time(now)
        self._refill(now)
        self.stats['restarts'] += 1
        self._hand_over()
        self._frames.clear()
        self._first = self._first_arrival = self._next = self._highest = None
        self._late_run = 0

    def _store(self, extended, pcm, arrival, outcome):
        self._frames[extended] = (pcm, arrival)
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
        `limit` per call (call again for the rest). Playout never passes the highest
        frame received: only holes that a later packet closes are concealed, so a stream
        that stops (end of call, a pause) produces no made-up audio."""
        _check_time(now)
        self._refill(now)
        out = self._pending_out[:limit]
        del self._pending_out[:limit]
        if self._next is not None:
            last = min(self._due_index(now), self._highest if self._highest is not None else self._next - 1)
            while self._next <= last and len(out) < limit:
                out.append(self._play())
        return self._deliver(out, now)

    def flush(self, now=None):
        """Everything still pending, then every frame up to the highest received one
        (end of call). `now` (the receiver's clock) times the received frames' hold;
        without it they count as `hold_unmeasured`."""
        if now is not None:
            _check_time(now)
        out, self._pending_out = self._pending_out, []
        return self._deliver(out + self._play_to_highest(), now)

    def _play_to_highest(self):
        """Hand-over: every frame up to the highest received. Received frames are always
        played (each is a real packet, played once: no amplification, and a backlog after
        a loop stall is never lost); only concealment of holes spends the budget."""
        out = []
        while self._highest is not None and self._next is not None and self._next <= self._highest:
            if self._next in self._frames:
                out.append(self._play())
            elif self._handover_budget >= 1:
                self._handover_budget -= 1
                out.append(self._play())
            else:  # a hole over the real-time budget: skipped, not made up
                self._next += 1
                self.stats['dropped_handover'] += 1
                # The audio around the splice is unknown: later holes conceal with silence.
                self._run, self._last_played = self.repeat_limit, SILENCE
        return out

    def _play(self):
        """(pcm, concealed_samples, arrival or None for a concealed frame)."""
        entry = self._frames.pop(self._next, None)
        self._next += 1
        self.stats['played'] += 1
        self._played_since_anchor += 1
        if entry is not None:
            pcm, arrival = entry
            self._last_played, self._run = pcm, 0
            return pcm, 0, arrival
        self._run += 1
        self.stats['concealed_frames'] += 1
        if self._run <= self.repeat_limit:
            self.stats['repeated_frames'] += 1
            return self._last_played, FRAME_SAMPLES, None
        self.stats['silence_frames'] += 1
        self._last_played = SILENCE
        return SILENCE, FRAME_SAMPLES, None

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
