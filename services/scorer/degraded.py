"""Degraded-mode stride control (backlog 8.2 partial; PRD FR-I10, FR-I6; architecture
§2.4 degraded mode, §7.3 `degraded_max_stride_factor`, `degraded_recovery_s`).

Rules from the PRD and architecture: if inference falls behind, the stride grows
instead of the queue; the stride is capped at `degraded_max_stride_factor` x hop,
beyond which the call shows `state=unmonitored`; degraded mode clears only after
`degraded_recovery_s` seconds under budget; `score.updated` carries `degraded` and the
effective stride.

Interpretation choices (not fixed by the documents): the caller decides what "behind"
means and reports it once per scheduling decision through `observe(now, behind=)`
(for example, the previous window's result was late or more than one window is
pending); each `behind` report grows the stride by one hop; a `behind` report at the
cap makes the call unmonitored; any `behind` report restarts the recovery clock; after
the full recovery period with every report under budget the controller returns
straight to one hop, not stepwise. The recovery clock starts at the first under-budget
report; one later report at least `recovery_s` afterwards is enough, however sparse
the reports. While unmonitored the caller keeps scheduling at the capped stride and
reports every decision, otherwise the call can never recover. Risk for 8.QA: under
sustained load near capacity, instant growth and a jump back to one hop can cycle
(about 20 s to unmonitored, 30 s back); stepwise recovery is the alternative.

Integration notes: `observe` raises ValueError for a backwards clock; the caller must
catch it so a clock fault degrades AVA, never the call. Which state wins on the call
card when unmonitored meets an open alert is a PRD question. Remaining 8.2 work: the
bounded per-call pending queue this controller assumes. Pure logic with an explicit
clock; one owner per call, not thread-safe.
"""
import math
import numbers

from .config import ScoringConfig


def _seconds(value, name):
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ValueError(f'{name} must be a finite number of seconds')
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f'{name} must be a finite number of seconds')
    return float(value)


class StrideController:
    def __init__(self, config):
        if not isinstance(config, ScoringConfig):
            raise TypeError('StrideController needs a validated ScoringConfig')
        self.hop_s = config.hop_s
        self.max_factor = config.degraded_max_stride_factor
        self.recovery_s = config.degraded_recovery_s
        # A ScoringConfig built directly is not validated; check what this needs.
        if type(self.max_factor) is not int or self.max_factor < 1:
            raise ValueError('degraded_max_stride_factor must be an integer >= 1')
        if _seconds(self.recovery_s, 'degraded_recovery_s') <= 0 or _seconds(self.hop_s, 'hop_s') <= 0:
            raise ValueError('degraded_recovery_s and hop_s must be > 0')
        self.factor = 1
        self.unmonitored = False
        self._under_budget_since = None
        self._last_now = None

    @property
    def degraded(self):
        return self.factor > 1 or self.unmonitored

    @property
    def effective_stride_s(self):
        return round(self.factor * self.hop_s, 9)  # avoid 0.30000000000000004 in events

    def status(self):
        """Fields for `score.updated` (FR-I6) and the call card."""
        return {'degraded': self.degraded, 'effective_stride_s': self.effective_stride_s,
                'unmonitored': self.unmonitored}

    def observe(self, now, *, behind):
        """Record one scheduling decision at monotonic time `now` (seconds). Returns the
        list of changes as (field, old, new) tuples; empty when nothing changed.
        Invalid input or a clock that goes backwards changes nothing."""
        now = _seconds(now, 'now')
        if not isinstance(behind, bool):
            raise ValueError('behind must be a boolean')
        if self._last_now is not None and now < self._last_now:
            raise ValueError('now went backwards; use a monotonic clock')
        before = (self.factor, self.unmonitored)
        self._last_now = now
        if behind:
            self._under_budget_since = None
            if self.factor < self.max_factor:
                self.factor += 1
            else:
                self.unmonitored = True
        elif self.degraded:
            if self._under_budget_since is None:
                self._under_budget_since = now
            elif now - self._under_budget_since >= self.recovery_s:
                self.factor, self.unmonitored, self._under_budget_since = 1, False, None
        changes = []
        if self.factor != before[0]:
            changes.append(('effective_stride_s', round(before[0] * self.hop_s, 9), self.effective_stride_s))
        if self.unmonitored != before[1]:
            changes.append(('unmonitored', before[1], self.unmonitored))
        return changes
