"""Bounded per-call pending-window queue (backlog 8.2 remainder; PRD FR-I10;
architecture §2.4 degraded mode: "the stride grows instead of the queue").

`PendingWindows` sits between the window builder and inference for one call
direction. The builder offers every window (one per hop, numbered by `index`); the
queue accepts only windows that respect the controller's current stride, keeps at
most `max_pending` accepted windows waiting for inference, and reports each
acceptance to the `StrideController`, which then grows or recovers the stride.

Interpretation choices (not fixed by the documents):
- A scheduling decision happens when a window is accepted. Windows skipped by the
  stride are counted but are not decisions and are not reported.
- "Behind" means at least one accepted window is still waiting (not yet taken for
  inference) when the next one is accepted. A window taken by `take()` is in flight
  and no longer counts.
- When the queue is full the oldest waiting window is dropped (counted), so memory
  stays bounded and the freshest audio is scored; the drop is also a `behind` report.
- `take()` is first-in first-out so scores reach the EMA in time order.
- `max_pending` has no documented value; the caller must choose it (>= 1). It
  bounds the number of windows held, not the size of each window object.

Window indexes must strictly increase. Invalid input or a controller error (for
example a backwards clock) changes nothing; the caller must catch it so an AVA fault
degrades scoring, never the call. One owner per call, not thread-safe.
"""
from collections import deque

from .degraded import StrideController, _seconds


class PendingWindows:
    def __init__(self, controller, *, max_pending):
        if not isinstance(controller, StrideController):
            raise TypeError('PendingWindows needs a StrideController')
        if type(max_pending) is not int or max_pending < 1:
            raise ValueError('max_pending must be an integer >= 1')
        self.controller = controller
        self.max_pending = max_pending
        self._queue = deque()
        self._last_offered = None
        self._last_accepted = None
        self._last_now = None
        self.counts = {'offered': 0, 'accepted': 0, 'skipped': 0, 'dropped': 0, 'taken': 0}

    def __len__(self):
        return len(self._queue)

    def offer(self, now, index, window):
        """Offer window number `index` at monotonic time `now`. Returns a dict with
        `accepted` (bool), `dropped_index` (the index dropped to make room, or None) and
        `changes` (the controller's (field, old, new) tuples)."""
        now = _seconds(now, 'now')
        if self._last_now is not None and now < self._last_now:
            raise ValueError('now went backwards; use a monotonic clock')
        if type(index) is not int or index < 0:
            raise ValueError('index must be a non-negative integer')
        if self._last_offered is not None and index <= self._last_offered:
            raise ValueError('window indexes must strictly increase')
        due = self._last_accepted is None or index - self._last_accepted >= self.controller.factor
        if not due:
            self._last_offered, self._last_now = index, now
            self.counts['offered'] += 1
            self.counts['skipped'] += 1
            return {'accepted': False, 'dropped_index': None, 'changes': []}
        behind = len(self._queue) > 0
        changes = self.controller.observe(now, behind=behind)  # raises before any change here
        dropped = None
        if len(self._queue) >= self.max_pending:
            dropped = self._queue.popleft()[0]
            self.counts['dropped'] += 1
        self._queue.append((index, window))
        self._last_offered = self._last_accepted = index
        self._last_now = now
        self.counts['offered'] += 1
        self.counts['accepted'] += 1
        return {'accepted': True, 'dropped_index': dropped, 'changes': changes}

    def take(self):
        """Remove and return the oldest waiting (index, window), or None if empty."""
        if not self._queue:
            return None
        self.counts['taken'] += 1
        return self._queue.popleft()

    def put_back(self, item):
        """Return an (index, window) just taken to the front, when handing it on failed.
        Call only directly after the `take()` that returned it."""
        self._queue.appendleft(item)
        self.counts['taken'] -= 1
