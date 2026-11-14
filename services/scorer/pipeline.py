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
  the direction-queue object they came from and the `LiveCalls.token` it was bound to)
  are skipped; results after the end are
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
import copy
import itertools
import math
import numbers
import threading
import time

import numpy as np

from services.inference.analysis import WINDOW_SAMPLES, p_synthetic

from .config import DIRECTIONS
from .degraded import StrideController, _seconds
from .pending import PendingWindows


# Live detector-call duration buckets (seconds, inclusive upper bounds) for FR-O6.
INFERENCE_BUCKETS_S = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0)
INFERENCE_OUTCOMES = ('ok', 'failed')


class LivePipeline:
    def __init__(self, live, batcher, detector, config, *, max_pending=4, timer=time.perf_counter):
        """`timer` is a monotonic wall-time source used only to measure detector calls
        (FR-O6); scheduling still uses the caller's explicit `now`."""
        if type(max_pending) is not int or max_pending < 1:
            raise ValueError('max_pending must be an integer >= 1')
        if not callable(timer):
            raise ValueError('timer must be callable')
        self._timer = timer
        self._inference = {outcome: {'buckets': [0] * len(INFERENCE_BUCKETS_S), 'sum': 0.0, 'count': 0}
                           for outcome in INFERENCE_OUTCOMES}
        StrideController(config)  # validate the degraded-mode settings now, not at first offer
        self.live, self.batcher, self.detector, self.config = live, batcher, detector, config
        self.max_pending = max_pending
        self._directions = {}  # (call_id, direction) -> PendingWindows
        self._tokens = {}  # (call_id, direction) -> LiveCalls.token of the call it belongs to
        self._in_flight = set()  # keys with a window in the batcher or in inference
        self._unmonitored = set()
        self._cursor = itertools.count()
        self._last_now = None
        self.counts = {'offered_inactive': 0, 'dropped_pending': 0, 'cancelled_pending': 0,
                       'skipped_ended': 0, 'scored': 0, 'batches': 0, 'detector_failures': 0,
                       'windows_lost_to_detector': 0, 'result_errors': 0, 'file_batches_not_run': 0,
                       'accepted': 0, 'stride_skipped': 0, 'result_not_applied': 0,
                       'unmonitored_entered': 0, 'metrics_publish_failures': 0, 'status_publish_failures': 0,
                       'timer_failures': 0}

    def check_clock(self, now):
        """Raise ValueError if `now` is invalid or earlier than the last one; no change."""
        now = _seconds(now, 'now')
        if self._last_now is not None and now < self._last_now:
            raise ValueError('now went backwards; use a monotonic clock')
        return now

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
        token = self.live.token(call_id)
        if token is None:
            self.counts['offered_inactive'] += 1
            return None
        key = (call_id, direction)
        pending = self._directions.get(key)
        stale = pending is not None and self._tokens.get(key) is not token  # ID reused by a new call
        fresh = pending is None or stale
        if fresh:
            pending = PendingWindows(StrideController(self.config), max_pending=self.max_pending)
        outcome = pending.offer(now, index, window)  # raises before any change on bad index
        if stale:
            self._cancel(key)  # the earlier call's windows never reach the new call
        if fresh:
            self._directions[key], self._tokens[key] = pending, token
        self._last_now = now
        self.counts['accepted' if outcome['accepted'] else 'stride_skipped'] += 1
        if outcome['dropped_index'] is not None:
            self.counts['dropped_pending'] += 1
        controller = pending.controller
        if controller.unmonitored and key not in self._unmonitored:
            self._unmonitored.add(key)
            self.counts['unmonitored_entered'] += 1
            try:  # the window is already queued: a publish fault must not raise into the caller
                self.live.status(call_id, 'unmonitored', degraded=True,
                                 effective_stride_s=controller.effective_stride_s, reason='stride_cap')
            except Exception:
                self.counts['status_publish_failures'] += 1
        elif not controller.unmonitored:
            self._unmonitored.discard(key)
        return outcome

    def schedule(self, now):
        """Give each idle direction its oldest waiting window, while the batcher has live
        room. Returns the number moved."""
        now = self._clock(now)
        for key in [k for k in self._directions if self.live.token(k[0]) is not self._tokens[k]]:
            self._cancel(key)  # ended without end_call (e.g. LiveCalls.end_all), or ID reused
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
            try:
                self.batcher.submit((key, pending, index, window), kind='live', now=now)
            except Exception:  # e.g. the batcher's clock is ahead: the window waits, not lost
                pending.put_back((index, window))
                raise
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
            if self._directions.get(key) is pending and self.live.token(key[0]) is self._tokens[key]:
                items.append(item)
                self._in_flight.discard(key)  # its result is applied below
            else:  # call ended (or replaced by a new call with the same ID)
                self.counts['skipped_ended'] += 1
        if not items:
            return 0
        self.counts['batches'] += 1
        started = self._read_timer()
        try:
            logits = self.detector(np.stack([item[3] for item in items]))
            scores = [p_synthetic([float(v) for v in row]) for row in logits]
            if len(scores) != len(items):
                raise ValueError('detector returned a different number of rows')
        except Exception:  # AVA fault: lose this batch's scores, never the calls
            self._observe_inference(started, 'failed')
            self.counts['detector_failures'] += 1
            self.counts['windows_lost_to_detector'] += len(items)
            return 0
        self._observe_inference(started, 'ok')
        applied = 0
        for ((call_id, direction), pending, index, _), score in zip(items, scores):
            status = pending.controller.status()
            try:
                outcome = self.live.result(call_id, direction, index, score, degraded=status['degraded'],
                                           effective_stride_s=status['effective_stride_s'])
                was_applied = bool(outcome.applied)
            except Exception:  # one bad result must not stop the rest of the batch
                self.counts['result_errors'] += 1
                continue
            if was_applied:
                applied += 1
                self.counts['scored'] += 1
            else:
                self.counts['result_not_applied'] += 1
        return applied

    def _read_timer(self):
        try:
            value = self._timer()
            ok = isinstance(value, numbers.Real) and not isinstance(value, bool) and math.isfinite(value)
            return float(value) if ok else None
        except Exception:  # includes OverflowError from a huge int
            return None

    def _observe_inference(self, started, outcome):
        """Record one detector call (including output conversion) in the FR-O6 histogram;
        a broken or backwards timer is counted and never affects scoring."""
        ended = self._read_timer()
        if started is None or ended is None or ended < started:
            self.counts['timer_failures'] += 1
            return
        seconds, series = ended - started, self._inference[outcome]
        for i, bound in enumerate(INFERENCE_BUCKETS_S):
            if seconds <= bound:
                series['buckets'][i] += 1
        series['sum'] += seconds
        series['count'] += 1

    # --- lifecycle -----------------------------------------------------------------------
    def end_call(self, call_id, reason, *, speech_seconds):
        """End the call and cancel its waiting windows. Returns the published envelopes."""
        envelopes = self.live.end(call_id, reason, speech_seconds=speech_seconds)
        for key in [k for k in self._directions if k[0] == call_id]:
            self._cancel(key)
        return envelopes

    def _cancel(self, key):
        pending = self._directions.pop(key)
        self._tokens.pop(key, None)
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

    def metrics_snapshot(self):
        """ID-free aggregate for FR-O6 metrics: no call IDs, no window data. Every
        accepted window is accounted for exactly once:
        accepted == dropped_pending + cancelled_pending + skipped_ended + scored
                    + windows_lost_to_detector + result_errors + result_not_applied
                    + waiting + in_batcher
        (`in_batcher` counts the batcher's live items, valid while only this pipeline
        feeds it, as documented above). Owner thread only: other threads read the copy
        handed to `PipelineStats`."""
        statuses = [p.controller.status() for p in self._directions.values()]
        return {'counts': dict(self.counts), 'directions': len(self._directions),
                'waiting': sum(len(p) for p in self._directions.values()),
                'in_batcher': self.batcher.pending()['live'],
                'degraded': sum(s['degraded'] for s in statuses),
                'unmonitored': sum(s['unmonitored'] for s in statuses),
                'max_effective_stride_s': max((s['effective_stride_s'] for s in statuses), default=0.0),
                'inference_seconds': {'bounds': list(INFERENCE_BUCKETS_S),
                                      'series': {outcome: {'buckets': list(s['buckets']), 'sum': s['sum'],
                                                           'count': s['count']}
                                                 for outcome, s in self._inference.items()}}}

    def publish_metrics(self, stats):
        """Hand the current aggregate to a `PipelineStats` read by other threads. Call it
        from the owning loop (e.g. after each step); it never raises into the loop."""
        try:
            stats.publish(self.metrics_snapshot())
        except Exception:  # metrics must not stop scoring
            self.counts['metrics_publish_failures'] += 1


class PipelineStats:
    """Thread-safe holder of the latest `LivePipeline.metrics_snapshot()`: the single
    owner publishes, the API's `/metrics` reads. `read()` returns (copy, age in seconds by
    `clock`) or (None, None) before the first publish, so a stalled loop shows as age."""

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._latest, self._at = None, None

    def publish(self, snapshot):
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('counts'), dict):
            raise ValueError('snapshot must come from LivePipeline.metrics_snapshot()')
        held = copy.deepcopy(snapshot)  # small: counts, gauges and one histogram
        at = self._clock()
        with self._lock:
            self._latest, self._at = held, at

    def read(self):
        with self._lock:
            latest, at = self._latest, self._at
        if latest is None:
            return None, None
        return copy.deepcopy(latest), max(0.0, self._clock() - at)
