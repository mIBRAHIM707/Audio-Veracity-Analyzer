"""Passive Asterisk tap (backlog 6.2 partial; PRD FR-T2, FR-T3, FR-T4, FR-T5;
architecture §2.1, ADR-4).

Calls never enter Stasis. The tap watches every channel's events (the ARI WebSocket
subscribes to all, see `ari.py`) and taps a call channel once it is answered (state
`Up`), is a telephone channel (name prefix, `PJSIP/` by default), was marked by the
dialplan as the inbound caller leg (`Set(AVA_TAP=caller)` after `Answer()`, seen as a
`ChannelVarset` event; a plain variable, so an agent leg created by `Dial()` is never
tapped as the caller) and, when `tap_extensions` is set, its dialplan extension is one
of them. `resume()` after every (re)connect sweeps leftovers and taps marked calls that
were already answered (read through ARI's channel-variable call). Tapping means: open
an `RtpSession` (one UDP port per call), create a snoop channel on the call with
`spy=in` (the caller direction) in the tap's ARI app, an ExternalMedia channel sending
that audio as plain G.711 mu-law RTP to the session's port, and a mixing bridge
joining the two. The call itself is only ever spied on: a tap that is dead, slow or
disconnected cannot hold, answer or hang up a call (FR-T4), unlike a dialplan
`Stasis()` hand-off, which would leave a call waiting on the application. Audio before
the answer and the first frames after it are not tapped.

Any tap failure is contained: partial tap resources are released and the call is
reported `failed`. Call metadata (FR-T3): call ID (the call channel's ID), from
(caller number), to (dialled extension), creation time and the tap's format (the
call's negotiated codec is not in the ARI channel object). The call ends with
`reason=bye` on the call channel's (hard) hangup request or destruction, or its snoop
being destroyed; `timeout` when its RTP session received nothing for
`silence_timeout_s` (30 s, FR-T2); `monitor_lost` when the ExternalMedia channel is
lost, its socket fails, or on `close()` (such a call may be tapped again by `resume()`).
A soft hangup request (transfer, redirect, administrative) does not end the tap; the
destruction that follows does. On end the socket is drained for up to 40 ms, the
remaining audio delivered, and the tap's own bridge and channels are released with a
short timeout. One tap process per Asterisk and ARI app name: `sweep()` removes every
`ava-tap` bridge it does not own (only its snoop/ExternalMedia channels are hung up).

`TapApp.handle(event)` is dispatch on decoded ARI events (malformed events are
counted, never raised) and is tested with a fake client; `run_once(connection)` reads
one event and polls the RTP sessions. One thread owns the app. Each call's RTP goes
through a `jitter.JitterBuffer` (60 ms by default; `jitter_depth_s=None` for plain
arrival order) and a `LossMonitor`. The sink receives `started(call)`,
`audio(call_id, pcm16_8k, concealment)` (`concealment`: one `(samples,
concealed_samples)` pair per 20 ms frame in jitter mode, per chunk otherwise),
optionally `quality(call_id, change)` after that audio (`degraded` / `recovered`:
sustained loss above 5 %, FR-I2), `ended(call_id, reason, receiver_stats)` (receiver and
`jitter_` buffer counts) and `failed(call_id, error)`; sink faults are counted, never
raised.

For the live `/metrics` (FR-O6, 18 Nov): `receiver_totals` sums ended calls' receiver
and `jitter_` counts; `receiver_live` adds each open call's counts as re-read by `poll`
at most every RECEIVER_REFRESH_S (1 s), and `calls_degraded` is the number of open calls
in a sustained-loss episode. An ending call's final counts replace its last read ones in
one step, so `receiver_live` never decreases.
"""
from collections import deque
from dataclasses import dataclass
import json
import time

from .ari import AriError
from .jitter import DEFAULT_DEPTH_S, JitterBuffer, LossMonitor
from .rtp_receiver import RtpSession

TAP_TAG = 'ava-tap'
SILENCE_TIMEOUT_S = 30.0
OWN_PREFIXES = ('Snoop/', 'UnicastRTP/')  # snoop and ExternalMedia channels are never tapped
# The dialplan marks the inbound caller leg after answering: Set(AVA_TAP=caller). A plain
# (non-inherited) variable, so a leg created by Dial() to an agent is never marked.
MARK_VARIABLE, MARK_VALUE = 'AVA_TAP', 'caller'
END_DRAIN_S = 0.04
RECEIVER_REFRESH_S = 1.0  # how often open calls' RTP/jitter counts are re-read for /metrics


def _add_counts(totals, counts):
    """Adds the well-formed (str key, int >= 0) entries of `counts` into `totals`."""
    for key, value in (counts or {}).items():
        if isinstance(key, str) and type(value) is int and value >= 0:
            totals[key] = totals.get(key, 0) + value


@dataclass
class TappedCall:
    call_id: str
    caller: str
    dialled: str
    started: str
    codec: str = 'ulaw'  # the tap's ExternalMedia format, not the call's codec
    session: object = None
    monitor: object = None
    snoop_id: str = None
    media_id: str = None
    bridge_id: str = None


class TapApp:
    def __init__(self, ari, sink, *, receiver_host, bind_host='0.0.0.0', silence_timeout_s=SILENCE_TIMEOUT_S,
                 max_calls=64, clock=time.monotonic, session_factory=RtpSession, tap_prefixes=('PJSIP/',),
                 tap_extensions=None, rtp_source=None, jitter_depth_s=DEFAULT_DEPTH_S):
        if type(max_calls) is not int or max_calls < 1:
            raise ValueError('max_calls must be a positive integer')
        self.jitter_depth_s = jitter_depth_s  # None: arrival order, no concealment
        self.ari, self.sink = ari, sink
        self.receiver_host, self.bind_host, self.rtp_source = receiver_host, bind_host, rtp_source
        self.silence_timeout_s, self.max_calls, self._clock = silence_timeout_s, max_calls, clock
        self._session_factory = session_factory
        self.tap_prefixes = tuple(tap_prefixes)
        self.tap_extensions = None if tap_extensions is None else frozenset(tap_extensions)
        self.calls = {}
        self._ours = set()  # snoop and ExternalMedia channel IDs created by the tap
        self._done = deque(maxlen=4096)  # recently ended or refused call IDs: never tapped again
        self._done_set = set()
        self._marked = {}  # channel IDs marked as inbound caller legs (insertion-ordered)
        self.stats = {'events': 0, 'bad_events': 0, 'tapped': 0, 'tap_failures': 0, 'refused_capacity': 0,
                      'ended': 0, 'sink_errors': 0, 'cleanup_errors': 0, 'swept': 0}
        # Receiver and `jitter_` buffer counts summed over ended calls (FR-I2, FR-O6).
        self.receiver_totals = {}
        # The same plus each open call's counts as last read (at most RECEIVER_REFRESH_S
        # old), so loss shows while a call runs, not only after it ends (18 Nov). Both
        # dicts are replaced, never changed in place (read by the health thread).
        self.receiver_live = {}
        self.calls_degraded = 0  # open calls in a sustained-loss episode (LossMonitor), same refresh
        self._call_counts = {}  # call ID -> its counts at the last refresh (loop thread only)
        self._refreshed_at = None

    # -- event dispatch ----------------------------------------------------------------

    def handle(self, event):
        self.stats['events'] += 1
        try:
            self._dispatch(event)
        except (AttributeError, KeyError, TypeError, ValueError, IndexError):
            self.stats['bad_events'] += 1  # a malformed event never stops the loop

    def _dispatch(self, event):
        kind, channel = event.get('type'), event.get('channel')
        if not isinstance(channel, dict) or not isinstance(channel.get('id'), str):
            return
        channel_id = channel['id']
        if kind == 'ChannelVarset' and event.get('variable') == MARK_VARIABLE and event.get('value') == MARK_VALUE:
            self._mark(channel_id)
        if kind in ('ChannelHangupRequest', 'ChannelDestroyed'):
            if kind == 'ChannelHangupRequest' and event.get('soft'):
                return  # soft request (transfer, redirect, admin): teardown follows on destroy
            if channel_id in self.calls:
                self._end(channel_id, 'bye')
            elif channel_id in self._ours:
                for call in list(self.calls.values()):
                    if channel_id == call.snoop_id:  # a snoop ends with the call it spies on
                        self._end(call.call_id, 'bye')
                    elif channel_id == call.media_id:  # tap lost while the call goes on
                        self._end(call.call_id, 'monitor_lost')
            else:
                self._remember(channel_id)
            if kind == 'ChannelDestroyed':
                self._marked.pop(channel_id, None)
            return
        if channel.get('state') == 'Up' and self._wanted(channel_id, channel):
            self._start(channel_id, channel)

    def _mark(self, channel_id):
        if len(self._marked) >= 4 * self._done.maxlen:  # bounded: drop the oldest marks
            self._marked.pop(next(iter(self._marked)))
        self._marked[channel_id] = True

    def _wanted(self, channel_id, channel):
        name = channel.get('name')
        if channel_id not in self._marked:
            return False  # only the inbound caller leg the dialplan marked (never a Dial()ed leg)
        if channel_id in self.calls or channel_id in self._ours or channel_id in self._done_set:
            return False
        if not isinstance(name, str) or name.startswith(OWN_PREFIXES) or not name.startswith(self.tap_prefixes):
            return False
        exten = (channel.get('dialplan') or {}).get('exten')
        return self.tap_extensions is None or exten in self.tap_extensions

    def _remember(self, call_id):
        if call_id in self._done_set:
            return
        if len(self._done) == self._done.maxlen:
            self._done_set.discard(self._done[0])
        self._done.append(call_id)
        self._done_set.add(call_id)

    def _start(self, channel_id, channel):
        if len(self.calls) >= self.max_calls:
            self.stats['refused_capacity'] += 1
            self._remember(channel_id)
            self._emit('failed', channel_id, 'capacity')
            return
        caller, dialplan = channel.get('caller') or {}, channel.get('dialplan') or {}
        call = TappedCall(channel_id, str(caller.get('number', '')), str(dialplan.get('exten', '')),
                          str(channel.get('creationtime', '')))
        try:
            jitter = None if self.jitter_depth_s is None else JitterBuffer(depth_s=self.jitter_depth_s)
            call.monitor = LossMonitor()
            call.session = self._session_factory(payload_type=0, host=self.bind_host, clock=self._clock,
                                                 allowed_source=self.rtp_source, jitter=jitter)
            # Bridge first and each channel into it at once, so whatever a crash leaves
            # behind sits in an `ava-tap` bridge that `sweep()` finds.
            call.bridge_id = self.ari.create_bridge(name=TAP_TAG)['id']
            call.snoop_id = self.ari.snoop(channel_id, spy='in', app_args=TAP_TAG)['id']
            self._ours.add(call.snoop_id)
            self.ari.add_to_bridge(call.bridge_id, [call.snoop_id])
            call.media_id = self.ari.external_media(f'{self.receiver_host}:{call.session.port}', fmt='ulaw')['id']
            self._ours.add(call.media_id)
            self.ari.add_to_bridge(call.bridge_id, [call.media_id])
        except (AriError, OSError, KeyError, TypeError, ValueError) as error:
            self.stats['tap_failures'] += 1
            self._release(call)
            self._remember(channel_id)
            self._emit('failed', channel_id, f'{type(error).__name__}')
            return
        self.calls[channel_id] = call
        self.stats['tapped'] += 1
        self._emit('started', call)

    def _end(self, call_id, reason):
        call = self.calls.pop(call_id, None)
        if call is None:
            return
        if reason != 'monitor_lost':  # a tap lost on our side may be resumed after reconnect
            self._remember(call_id)
            self._marked.pop(call_id, None)
        try:
            call.session.poll(END_DRAIN_S)  # bounded drain: in-flight packets are delivered
        except OSError:
            pass
        self._drain(call, final=True)
        try:
            receiver = call.session.receiver_stats()
        except (AttributeError, TypeError, ValueError):  # review L2: keep the last read counts
            receiver = dict(self._call_counts.get(call_id, {}))
        self._add_receiver_totals(receiver, call_id)
        self._release(call)
        self.stats['ended'] += 1
        self._emit('ended', call_id, reason, receiver)

    def _add_receiver_totals(self, receiver, call_id=None):
        """Adds one ended call's receiver and jitter-buffer counts to `receiver_totals`
        and drops that call's in-call counts from `receiver_live` in the same step. The
        dicts are replaced, never changed in place, so another thread (the health server)
        can copy them while the loop thread adds a call. A call's counts only grow, so
        its final counts are never below its last refreshed ones: `receiver_live` never
        decreases, as a Prometheus counter must not."""
        final = {}
        _add_counts(final, receiver)
        for key, value in self._call_counts.pop(call_id, {}).items():  # never below the last read
            final[key] = max(value, final.get(key, 0))
        totals = dict(self.receiver_totals)
        _add_counts(totals, final)
        self.receiver_totals = totals
        self._publish_live()

    def _refresh_live(self):
        """Re-read every open call's counts (loop thread). A session whose counts cannot
        be read keeps its previous ones."""
        for call_id, call in self.calls.items():
            try:
                read = {}
                _add_counts(read, call.session.receiver_stats())
            except (AttributeError, TypeError, ValueError):
                continue
            # A key missing or lower than before (never from a real session) keeps its
            # previous value, so the exported counters cannot go down (review L1).
            counts = dict(self._call_counts.get(call_id, {}))
            for key, value in read.items():
                counts[key] = max(value, counts.get(key, 0))
            self._call_counts[call_id] = counts
        for call_id in [known for known in self._call_counts if known not in self.calls]:
            del self._call_counts[call_id]  # defensive: ended calls are dropped in `_end`
        self._publish_live()

    def _publish_live(self):
        live = dict(self.receiver_totals)
        for counts in self._call_counts.values():
            _add_counts(live, counts)
        self.receiver_live = live
        self.calls_degraded = sum(1 for call in self.calls.values()
                                  if call.monitor is not None and call.monitor.degraded)

    def _release(self, call):
        """Free the tap's own resources; the call channel is never touched."""
        for action, target in ((self.ari.destroy_bridge, call.bridge_id), (self.ari.hangup, call.media_id),
                               (self.ari.hangup, call.snoop_id)):
            if target is None:
                continue
            try:
                action(target)
            except (AriError, ValueError):
                self.stats['cleanup_errors'] += 1  # already gone with the call, typically
        self._ours.discard(call.media_id)
        self._ours.discard(call.snoop_id)
        if call.session is not None:
            call.session.close()

    # -- audio -------------------------------------------------------------------------

    def poll(self, timeout=0.0):
        """Read RTP for every tapped call, deliver new audio, apply the silence timeout."""
        for call in list(self.calls.values()):
            try:
                call.session.poll(timeout)
            except OSError:  # this call's socket failed: lose its tap, keep the others
                self._end(call.call_id, 'monitor_lost')
                continue
            self._drain(call)
            if call.session.seconds_since_packet() > self.silence_timeout_s:
                self._end(call.call_id, 'timeout')
        now = self._clock()
        if self._refreshed_at is None or not 0 <= now - self._refreshed_at < RECEIVER_REFRESH_S:
            self._refreshed_at = now  # also after a clock step back
            self._refresh_live()

    def _drain(self, call, final=False):
        """Hand the frames due now (all of them at the end) to the sink with their
        concealed-sample count, and feed the call's loss monitor; a sustained-loss episode
        is reported once through the sink's optional `quality(call_id, change)`
        (`degraded` / `recovered`, FR-I2 `system.health reason=media_quality`)."""
        frames = [(pcm, concealed) for pcm, concealed in call.session.take_frames(final=final) if pcm]
        if not frames:
            return
        # Per frame: (samples, concealed samples), so a consumer can place concealment
        # inside its own scoring windows (FR-I2 >10 % rule).
        self._emit('audio', call.call_id, b''.join(p for p, _ in frames),
                   tuple((len(p) // 2, c) for p, c in frames))
        if call.monitor is not None and hasattr(self.sink, 'quality'):
            for _, concealed in frames:
                change = call.monitor.add(concealed > 0)
                if change:
                    self._emit('quality', call.call_id, change)

    def run_once(self, connection, timeout=0.05):
        """Read at most one ARI event (waiting up to `timeout`), then poll the RTP
        sessions. Raises the connection's error if the WebSocket closed."""
        try:
            text = connection.recv(timeout=timeout)
        except TimeoutError:
            text = None
        if text is not None:
            try:
                event = json.loads(text)
            except (TypeError, ValueError, RecursionError):
                self.stats['bad_events'] += 1
            else:
                if isinstance(event, dict):
                    self.handle(event)
                else:
                    self.stats['bad_events'] += 1
        self.poll(0.0)

    def sweep(self):
        """Remove what an earlier tap process left in Asterisk (a crash skips cleanup):
        every bridge named TAP_TAG that this app does not own, and the channels in it
        (the tap's snoop and ExternalMedia channels; calls are never in these bridges).
        Call after (re)connecting. Returns the number of bridges removed."""
        owned = {call.bridge_id for call in self.calls.values()}
        try:
            bridges = self.ari.list_bridges() or []
            names = {c.get('id'): c.get('name', '') for c in self.ari.list_channels() or [] if isinstance(c, dict)}
        except AriError:
            self.stats['cleanup_errors'] += 1
            return 0
        removed = 0
        for bridge in bridges:
            if not isinstance(bridge, dict) or bridge.get('name') != TAP_TAG or bridge.get('id') in owned:
                continue
            for channel_id in bridge.get('channels') or []:
                if not str(names.get(channel_id, '')).startswith(OWN_PREFIXES):
                    continue  # only tap-type channels are ever hung up, never a call
                try:
                    self.ari.hangup(channel_id)
                except (AriError, ValueError):
                    self.stats['cleanup_errors'] += 1
            try:
                self.ari.destroy_bridge(bridge['id'])
                removed += 1
            except (AriError, ValueError):
                self.stats['cleanup_errors'] += 1
        self.stats['swept'] += removed
        return removed

    def resume(self):
        """After (re)connecting: sweep leftovers, then tap marked caller legs that are
        already answered (they send no further events, so the event path alone would
        miss them). Returns the number of calls tapped."""
        self.sweep()
        try:
            channels = self.ari.list_channels() or []
        except AriError:
            self.stats['cleanup_errors'] += 1
            return 0
        tapped = 0
        for channel in channels:
            if not isinstance(channel, dict) or channel.get('state') != 'Up' or not isinstance(channel.get('id'), str):
                continue
            channel_id = channel['id']
            if channel_id in self.calls or not str(channel.get('name', '')).startswith(self.tap_prefixes):
                continue
            try:
                marked = self.ari.get_variable(channel_id, MARK_VARIABLE) == MARK_VALUE
            except (AriError, ValueError):
                continue
            if marked:
                self._mark(channel_id)
                before = len(self.calls)
                self.handle({'type': 'ChannelStateChange', 'channel': channel})
                tapped += len(self.calls) - before
        return tapped

    def close(self):
        """Tap lost (shutdown or ARI connection gone): end every open tap."""
        for call_id in list(self.calls):
            self._end(call_id, 'monitor_lost')

    def _emit(self, name, *args):
        try:
            getattr(self.sink, name)(*args)
        except Exception:  # the sink is advisory; never break the event loop
            self.stats['sink_errors'] += 1
