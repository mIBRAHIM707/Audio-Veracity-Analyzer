"""Single-owner live inference loop (backlog 8.1/8.2 wiring; PRD FR-I9, FR-I10, FR-I11;
architecture §2.3 micro-batching and priority, §2.4 degraded mode and cleanup).

`LivePipeline` connects the pieces built separately, with an explicit monotonic `now`
and no threads (the caller owns the loop and the clock):

    window builder --offer--> PendingWindows (per call direction; stride, drop-oldest,
    StrideController) --schedule--> MicroBatcher (live first) --step--> detector on a
    [B, 64000] float32 batch --> p_synthetic --> LiveCalls.result (scorer + FR-A4 events)

- At most ONE window per direction is in the batcher or in inference at a time. A
  window offered while the previous one is still unscored therefore waits in the
  direction's queue, which `PendingWindows` reports as "behind", so the stride grows
  instead of a queue (FR-I10). Backlog never builds up in the shared batcher.
- `offer` validates the direction and the window and copies it. Windows for a call
  that is not active are discarded and counted. When the controller turns a direction
  `unmonitored` (stride cap), `LiveCalls.status(..., 'unmonitored')` is published once
  on entering the state.
- `schedule` hands each idle direction its oldest waiting window, starting from a
  rotating position so no call is always served first. `step` runs one due batch.
- Cancellation (FR-I11): `end_call` ends the call and drops its waiting windows;
  batched items of an ended call (or of an earlier call with the same ID, recognised by
  the direction-queue object they came from) are skipped; results after the end are
  discarded by `LiveCalls`; calls ended elsewhere are cleaned up at `schedule`.
- Containment: `now` must not go backwards (ValueError before any change). A detector
  fault or invalid output fails that batch only; an error applying one result is
  counted and the rest of the batch still applies (AVA is advisory). File batches are
  not driven here yet; one handed out by a shared batcher is counted, not run.

Spec questions (recorded, not decided here): scoring continues at the capped stride
while `unmonitored` (needed for recovery), so the next `score.updated` shows the
scorer's state rather than `unmonitored`; whether the call card should keep
`unmonitored` is a PRD decision. The detector call has no time limit. Known limit: the
batcher must be fed only through this pipeline (its own clock check could otherwise
reject a `submit` after a window was taken). An in-flight window is not "behind": the
stride grows once one window is in flight and another is already waiting. Not
thread-safe: one owner.
"""
import itertools

import numpy as np

from services.inference.analysis import WINDOW_SAMPLES, p_synthetic

from .config import DIRECTIONS
from .degraded import StrideController, _seconds
from .pending import PendingWindows


class LivePipeline:
    def __init__(self, live, batcher, detector, config, *, max_pending=4):
        if type(max_pending) is not int or max_pending < 1:
            raise ValueError('max_pending must be an integer >= 1')
        StrideController(config)  # validate the degraded-mode settings now, not at first offer
        self.live, self.batcher, self.detector, self.config = live, batcher, detector, config
        self.max_pending = max_pending
        self._directions = {}  # (call_id, direction) -> PendingWindows
        self._in_flight = set()  # keys with a window in the batcher or in inference
        self._unmonitored = set()
        self._cursor = itertools.count()
        self._last_now = None
        self.counts = {'offered_inactive': 0, 'dropped_pending': 0, 'cancelled_pending': 0,
                       'skipped_ended': 0, 'scored': 0, 'batches': 0, 'detector_failures': 0,
                       'windows_lost_to_detector': 0, 'result_errors': 0, 'file_batches_not_run': 0}

    def _clock(self, now):
        now = _seconds(now, 'now')
        if self._last_now is not None and now < self._last_now:
            raise ValueError('now went backwards; use a monotonic clock')
        self._last_now = now
        return now

    # --- intake --------------------------------------------------------------------------
    def offer(self, call_id, direction, index, window, now):
        """Offer one built window. Returns the PendingWindows outcome, or None when the
        call is not active. Invalid input raises ValueError and changes nothing."""
        if direction not in DIRECTIONS:
            raise ValueError(f'direction must be one of {DIRECTIONS}')
        window = np.array(window, copy=True)  # the builder may reuse its buffer
        if window.shape != (WINDOW_SAMPLES,) or window.dtype != np.float32 or not np.isfinite(window).all():
            raise ValueError(f'window must be finite float32 of shape ({WINDOW_SAMPLES},)')
        now = _seconds(now, 'now')
        if self._last_now is not None and now < self._last_now:
            raise ValueError('now went backwards; use a monotonic clock')
        if not self.live.is_active(call_id):
            self.counts['offered_inactive'] += 1
            return None
        key = (call_id, direction)
        pending = self._directions.get(key)
        fresh = pending is None
        if fresh:
            pending = PendingWindows(StrideController(self.config), max_pending=self.max_pending)
        outcome = pending.offer(now, index, window)  # raises before any change on bad index
        if fresh:
            self._directions[key] = pending
        self._last_now = now
        if outcome['dropped_index'] is not None:
            self.counts['dropped_pending'] += 1
        controller = pending.controller
        if controller.unmonitored and key not in self._unmonitored:
            self._unmonitored.add(key)
            self.live.status(call_id, 'unmonitored', degraded=True,
                             effective_stride_s=controller.effective_stride_s, reason='stride_cap')
        elif not controller.unmonitored:
            self._unmonitored.discard(key)
        return outcome

    def schedule(self, now):
        """Give each idle direction its oldest waiting window, while the batcher has live
        room. Returns the number moved."""
        now = self._clock(now)
        for key in [k for k in self._directions if not self.live.is_active(k[0])]:
            self._cancel(key)  # the call ended without end_call (e.g. LiveCalls.end_all)
        keys = list(self._directions)
        if keys:
            start = next(self._cursor) % len(keys)
            keys = keys[start:] + keys[:start]
        moved = 0
        for key in keys:
            pending = self._directions[key]
            if key in self._in_flight or not len(pending):
                continue
            if self.batcher.pending()['live'] >= self.batcher.max_pending_live:
                break
            index, window = pending.take()
            self.batcher.submit((key, pending, index, window), kind='live', now=now)
            self._in_flight.add(key)
            moved += 1
        return moved

    # --- inference -----------------------------------------------------------------------
    def step(self, now):
        """Run one due live batch, if any. Returns the number of results applied."""
        due = self.batcher.next_batch(self._clock(now))
        if due is None:
            return 0
        if due[0] != 'live':
            self.counts['file_batches_not_run'] += 1
            return 0
        items = []
        for item in due[1]:
            key, pending = item[0], item[1]
            if self._directions.get(key) is pending and self.live.is_active(key[0]):
                items.append(item)
                self._in_flight.discard(key)  # its result is applied below
            else:  # call ended (or replaced by a new call with the same ID)
                self.counts['skipped_ended'] += 1
        if not items:
            return 0
        self.counts['batches'] += 1
        try:
            logits = self.detector(np.stack([item[3] for item in items]))
            scores = [p_synthetic([float(v) for v in row]) for row in logits]
            if len(scores) != len(items):
                raise ValueError('detector returned a different number of rows')
        except Exception:  # AVA fault: lose this batch's scores, never the calls
            self.counts['detector_failures'] += 1
            self.counts['windows_lost_to_detector'] += len(items)
            return 0
        applied = 0
        for ((call_id, direction), pending, index, _), score in zip(items, scores):
            status = pending.controller.status()
            try:
                outcome = self.live.result(call_id, direction, index, score, degraded=status['degraded'],
                                           effective_stride_s=status['effective_stride_s'])
            except Exception:  # one bad result must not stop the rest of the batch
                self.counts['result_errors'] += 1
                continue
            applied += bool(outcome.applied)
        self.counts['scored'] += applied
        return applied

    # --- lifecycle -----------------------------------------------------------------------
    def end_call(self, call_id, reason, *, speech_seconds):
        """End the call and cancel its waiting windows. Returns the published envelopes."""
        envelopes = self.live.end(call_id, reason, speech_seconds=speech_seconds)
        for key in [k for k in self._directions if k[0] == call_id]:
            self._cancel(key)
        return envelopes

    def _cancel(self, key):
        pending = self._directions.pop(key)
        self.counts['cancelled_pending'] += len(pending)
        while pending.take() is not None:  # free the windows now; a batched item may keep the queue
            pass
        self._unmonitored.discard(key)
        self._in_flight.discard(key)  # a batched item from it is skipped in step

    def snapshot(self):
        """Per-direction queue and stride state without window data."""
        return {'counts': dict(self.counts),
                'directions': {f'{c}/{d}': {'waiting': len(p), 'in_flight': (c, d) in self._in_flight,
                                            **p.controller.status(), **p.counts}
                               for (c, d), p in self._directions.items()}}
