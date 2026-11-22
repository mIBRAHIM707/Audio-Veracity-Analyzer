"""Opt-in alert snippets in the live service (backlog 5.3; PRD FR-O5, §6.3; architecture
§2.4 "if retention is on, persist the 30 s ring buffer ending at the alert as one
snippet per alert_id").

`SnippetRecorder` wires `services/retention/snippets.py` to the tap and the event hub:

- Loop thread (the thread that runs the tap and scoring):
  - `started(call_id, created)` when the sink admits a call: a `CallRetention` under the
    policy of this process, only if the PBX created the call's channel at or after the
    policy took effect (FR-O5 "toggling applies to calls started after the change"; the
    policy is fixed per run). With retention off nothing is buffered.
  - `audio(call_id, pcm)` the tapped caller audio, 8 kHz PCM16 as the PBX carried it
    (before resampling or VAD), into a 30 s ring (the tap taps the caller direction only).
  - `pump_events()` once per loop turn: drains its own hub subscription; for each
    `alert.raised` of a buffered call it copies the ring (the 30 s ending now, which is
    shortly after the window that raised it) into a bounded queue; then it releases the
    rings of calls that are no longer active. The alert is already published when it
    is seen here, so retention can never delay or block it.
- Writer thread (`write_pending`, run by a `PumpThread`): seals and writes queued
  snippets (AES-256-GCM, one file per alert ID; `SnippetStore.put`) and runs the
  TTL/purge cleanup every `cleanup_interval_s`. Disk and crypto work never runs on the
  loop thread.

Nothing here raises into the call path: every fault is counted in `counts`. Bounds: one
30 s ring per admitted call (480 kB at 8 kHz) and at most `max_queued` queued snippets;
an alert arriving with the queue full is counted (`queue_full`) and gets no snippet.
An alert for the agent direction gets none (`no_audio`): only the caller is tapped.
No audio, call ID or alert ID is logged or exported; `counts` are numbers only.
"""
from collections import deque
from datetime import datetime
import threading
import time

import numpy as np

from services.events.fanout import MAX_BUFFER
from services.retention.snippets import CallRetention

DEFAULT_MAX_QUEUED = 16
DEFAULT_CLEANUP_INTERVAL_S = 600.0
TAP_SAMPLE_RATE = 8000
COUNTS = ('calls_buffered', 'started_before_policy', 'start_time_unknown', 'audio_errors', 'alerts_seen',
          'captured', 'no_audio', 'queue_full', 'written', 'write_failed', 'cleanup_runs', 'cleanup_deleted',
          'cleanup_errors', 'hub_dropped', 'released', 'event_errors')


def _ari_time(text):
    """Seconds since the epoch from an ARI timestamp, or None."""
    if not isinstance(text, str) or not text:
        return None
    for layout in ('%Y-%m-%dT%H:%M:%S.%f%z', '%Y-%m-%dT%H:%M:%S%z'):
        try:
            return datetime.strptime(text, layout).timestamp()
        except ValueError:
            continue
    return None


class SnippetRecorder:
    def __init__(self, store, policy, hub, live, *, sample_rate=TAP_SAMPLE_RATE, max_queued=DEFAULT_MAX_QUEUED,
                 cleanup_interval_s=DEFAULT_CLEANUP_INTERVAL_S, clock=time.monotonic, wall_clock=time.time):
        if type(max_queued) is not int or not 1 <= max_queued <= 256:
            raise ValueError('max_queued must be an integer from 1 to 256')
        if isinstance(cleanup_interval_s, bool) or not isinstance(cleanup_interval_s, (int, float)) \
                or not 1 <= cleanup_interval_s <= 86400:
            raise ValueError('cleanup_interval_s must be from 1 to 86400 seconds')
        if policy.enabled and getattr(store, 'cleanup_only', False):
            raise ValueError('retention on needs a store that can write snippets')
        self.store, self.policy, self.live = store, policy, live
        self.sample_rate, self.max_queued = sample_rate, max_queued
        self.cleanup_interval_s, self._clock = float(cleanup_interval_s), clock
        # Subscribed only when on: with retention off no event or audio is held here.
        self._subscription = hub.subscribe(buffer=MAX_BUFFER) if policy.enabled else None
        self._calls = {}
        self._queue = deque()
        self._lock = threading.Lock()  # the queue and counts are shared with the writer thread
        self._next_cleanup = None
        # When this policy took effect (wall time, as the PBX reports channel creation).
        self.policy_since = float(wall_clock())
        self.counts = dict.fromkeys(COUNTS, 0)

    def _count(self, name, amount=1):
        with self._lock:
            self.counts[name] += amount

    # -- loop thread ---------------------------------------------------------------------
    def started(self, call_id, created=None):
        """`created`: the PBX channel's creation time (ARI `creationtime`, e.g.
        '2026-11-17T12:00:00.000+0000'). Review M2 (FR-O5 "toggling applies to calls
        started after the change"): a call the PBX started before this policy took
        effect, such as one re-tapped by `TapApp.resume()` after a restart that turned
        retention on, is not buffered; neither is one whose start time is missing or
        unreadable (fail closed). Clock skew moves the boundary: a PBX clock behind this
        host's excludes calls created up to the skew after the start, one ahead buffers
        calls created up to the skew before it, so keep both on NTP."""
        if not self.policy.enabled:
            return
        try:
            old = self._calls.pop(call_id, None)
            if old is not None:  # a re-tap: the earlier session's ring is not this call's audio
                old.close()
            began = _ari_time(created)
            if began is None:
                self._count('start_time_unknown')
                return
            if began < self.policy_since:
                self._count('started_before_policy')
                return
            self._calls[call_id] = CallRetention(call_id, self.policy, self.store, sample_rate=self.sample_rate,
                                                 directions=('caller',))
            self._count('calls_buffered')
        except Exception:
            self._count('audio_errors')

    def audio(self, call_id, pcm):
        """Tapped caller audio for an admitted call: PCM16 little-endian bytes at 8 kHz."""
        entry = self._calls.get(call_id)
        if entry is None:
            return
        try:
            if not entry.feed('caller', np.frombuffer(pcm, dtype='<i2').astype(np.int16)):
                self._count('audio_errors')
        except Exception:
            self._count('audio_errors')

    def pump_events(self):
        """Capture a snippet for each new `alert.raised`, then release the rings of
        calls that ended. Returns the number of snippets queued. Never raises."""
        if self._subscription is None:
            return 0
        queued = 0
        try:
            envelopes, dropped = self._subscription.drain()
        except Exception:
            self._count('event_errors')
            return 0
        if dropped:
            self._count('hub_dropped', dropped)  # an alert among them gets no snippet
        for envelope in envelopes:
            if envelope.type == 'alert.raised':
                queued += self._capture(envelope)
        for call_id in list(self._calls):
            try:
                active = self.live.is_active(call_id)
            except Exception:
                active = False
            if not active:  # alerts published before the end were handled above
                self._calls.pop(call_id).close()
                self._count('released')
        return queued

    def _capture(self, envelope):
        self._count('alerts_seen')
        try:
            data = envelope.data
            alert_id, direction = data.get('alert_id'), data.get('direction')
            entry = self._calls.get(envelope.call_id)
            pcm = entry.capture(direction) if entry is not None and isinstance(alert_id, str) else None
        except Exception:
            self._count('event_errors')
            return 0
        if pcm is None:
            self._count('no_audio')
            return 0
        with self._lock:
            if len(self._queue) >= self.max_queued:
                self.counts['queue_full'] += 1
                return 0
            self._queue.append((alert_id, envelope.call_id, direction, pcm))
            self.counts['captured'] += 1
        return 1

    def close(self):
        """Release every ring (shutdown); queued snippets stay for the writer's drain."""
        for entry in self._calls.values():
            entry.close()
        self._calls.clear()
        if self._subscription is not None:
            try:
                self._subscription.close()
            except Exception:
                pass

    # -- writer thread -------------------------------------------------------------------
    def write_pending(self):
        """Write every queued snippet, then clean up when due. Returns the number of
        snippets handled (a PumpThread pumps again at once while this is above 0)."""
        handled = 0
        while True:
            with self._lock:
                if not self._queue:
                    break
                alert_id, call_id, direction, pcm = self._queue.popleft()
            try:  # SnippetStore.put never raises; a stand-in might (review L6)
                ok = self.store.put(alert_id, call_id=call_id, direction=direction, pcm=pcm,
                                    sample_rate=self.sample_rate)
            except Exception:
                ok = False
            self._count('written' if ok else 'write_failed')
            handled += 1
        self._cleanup_if_due()
        return handled

    def _cleanup_if_due(self):
        now = self._clock()
        if self._next_cleanup is not None and now < self._next_cleanup:
            return
        self._next_cleanup = now + self.cleanup_interval_s
        try:
            deleted = self.store.cleanup(self.policy)
        except Exception:  # SnippetStore.cleanup does not raise; a stand-in might
            self._count('cleanup_errors')
            return
        self._count('cleanup_runs')
        self._count('cleanup_deleted', deleted)

    def pending(self):
        with self._lock:
            return len(self._queue)

    def report(self):
        """Numbers only, for the stopped record and `/metrics`."""
        with self._lock:
            counts = dict(self.counts)
            queued = len(self._queue)
        store_failures = dict(getattr(self.store, 'failures', {}))
        return {'snippets': counts, 'queued': queued, 'store_failures': sum(store_failures.values()),
                'mode': 'on' if self.policy.enabled else 'purge'}
