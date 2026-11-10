"""Serialized, in-order boundary for one call's scorer (backlog 5 boundary check from
phase-4 QA M1 and L3; PRD FR-I5, FR-I7, FR-I10; architecture §2.3 micro-batching).

`CallScorer` and `DirectionSession` assume one owner fed in window order. Inference
results come back from micro-batches, possibly on more than one worker, so this
wrapper is the boundary that enforces the contract:

- every call into the scorer is serialized by one lock per call;
- each result carries the direction's `window_idx`; per direction the index must
  strictly increase. A duplicate (same index) or stale (lower index) result is not
  applied and is counted; gaps are allowed, because windows are legitimately skipped
  by the degraded-mode stride or dropped by the pending queue (`pending.py`);
- the events returned are deep copies, so a consumer that edits `data` cannot change
  the scorer's stored `call.ended` events (QA L3).

Interpretation choices: late results are dropped rather than re-ordered (re-ordering
would need a wait and a buffer; dropping keeps the EMA order and memory bounded); the
index is the window builder's per-direction index, not the session's count of scored
windows. A scorer error leaves the last applied index unchanged, so the same result
may be retried. AVA errors are raised to the caller, who must contain them so the
call is never affected. Open (PRD): if the window builder's index restarts (stream
reconnect), every later result is `stale` and that direction silently stops scoring;
rejections are visible only in `snapshot()`. A stream epoch or an `unmonitored`
signal after repeated stale results is the likely fix once telephony (backlog 6)
defines reconnects.
"""
import copy
import threading

from .call import CallScorer
from .config import DIRECTIONS
from .session import SessionClosed


class OrderedCallScorer:
    def __init__(self, scorer):
        """Wrap `scorer`. The wrapper must be its only holder; prefer `create`, which
        builds the scorer so no unguarded reference exists."""
        if not isinstance(scorer, CallScorer):
            raise TypeError('OrderedCallScorer needs a CallScorer')
        self._scorer = scorer
        self._lock = threading.Lock()
        self._last = {d: None for d in DIRECTIONS}
        self._rejected = {'duplicate': 0, 'stale': 0}

    @classmethod
    def create(cls, config, *, call_id, new_alert_id=None):
        return cls(CallScorer(config, call_id=call_id, new_alert_id=new_alert_id))

    @property
    def call_id(self):
        return self._scorer.call_id

    @property
    def rejected(self):
        with self._lock:
            return dict(self._rejected)

    def last_applied(self, direction):
        with self._lock:
            return self._last[direction]

    def add_result(self, direction, window_idx, p_synthetic, *, concealed_fraction=0.0):
        """Apply one inference result. Returns {'applied', 'reason', 'events'}; reason
        is None, 'duplicate' or 'stale'. Invalid input changes nothing; any result for
        an ended call raises SessionClosed."""
        if direction not in DIRECTIONS:
            raise ValueError(f'direction must be one of {DIRECTIONS}')
        if type(window_idx) is not int or window_idx < 0:
            raise ValueError('window_idx must be a non-negative integer')
        with self._lock:
            if self._scorer.ended is not None:
                raise SessionClosed(f'call {self._scorer.call_id} already ended')
            last = self._last[direction]
            if last is not None and window_idx <= last:
                reason = 'duplicate' if window_idx == last else 'stale'
                self._rejected[reason] += 1
                return {'applied': False, 'reason': reason, 'events': []}
            events = self._scorer.add_window(direction, p_synthetic,
                                             concealed_fraction=concealed_fraction)
            self._last[direction] = window_idx
            return {'applied': True, 'reason': None, 'events': copy.deepcopy(events)}

    def human_agent_answered(self):
        with self._lock:
            self._scorer.human_agent_answered()

    def end(self, reason, *, speech_seconds):
        with self._lock:
            return copy.deepcopy(self._scorer.end(reason, speech_seconds=speech_seconds))

    def snapshot(self):
        """A consistent copy of the call state for readers (API, dashboard)."""
        with self._lock:
            s = self._scorer
            return {'call_id': s.call_id, 'state': s.state, 'driving_direction': s.driving_direction,
                    'quality': s.quality, 'ended': s.ended is not None,
                    'last_applied': dict(self._last), 'rejected': dict(self._rejected)}
