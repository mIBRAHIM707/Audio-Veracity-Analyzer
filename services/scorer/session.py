"""Per-direction scoring session: EMA, minimum evidence, hysteresis alert state machine
and `undetermined` outcomes (backlog 4.2; PRD FR-I5, FR-I7; architecture §2.4, ADR-11).

Pure domain logic with no I/O: `DirectionSession.add_window` takes one window's
`p_synthetic` and returns the events it causes; `end` returns the `call.ended`
summary. Persistence, Redis, event publishing, direction combination (FR-I8, backlog
4.3) and degraded mode (FR-I10, backlog 8.2) are outside this module.

Rules (architecture §2.4):
- EMA with `ema_alpha`; the first window seeds the EMA with its own score.
- A provisional score is published from the first window; no state change until
  `min_windows` windows. A session that ends with fewer than `min_windows` windows
  reports `verdict=undetermined`, never genuine.
- `monitoring -> suspicious` when EMA >= suspicious_enter;
  `suspicious -> alert` when EMA >= alert_enter for ALERT_CONSECUTIVE_WINDOWS
  consecutive windows; `monitoring -> alert` never happens directly;
  `alert -> suspicious` when EMA < alert_exit; `suspicious -> monitoring` when
  EMA < suspicious_exit. Each entry into `alert` gets a new alert ID.
- Ending in `alert` stores the alert as `ended_open` with no `alert.cleared`. PRD FR-I7
  also mentions ending in `suspicious`; this follows architecture §2.4 (only an alert
  still open is `ended_open`; ending in `suspicious` persists the state). Flagged for
  a PRD clarification.
- `state.changed` is an internal domain event; the published FR-A4 list carries the
  state in `score.updated`.

Interpretation choices (not fixed by the PRD, recorded in the README): at most one
transition per window; in `suspicious` the alert check runs before the exit check;
the consecutive-window streak counts windows from `min_windows` onward whatever the
state, so with the defaults the earliest alert is window 3.
"""
from collections import deque
from dataclasses import dataclass
import math
import numbers
import uuid

from .config import ScoringConfig

STATES = ('monitoring', 'suspicious', 'alert')
ALERT_CONSECUTIVE_WINDOWS = 2  # architecture §2.4, fixed (not a configuration key)
RECENT_SCORES = 30  # ring of recent window scores (architecture §2.4)
END_REASONS = ('bye', 'timeout', 'monitor_lost')


class SessionClosed(RuntimeError):
    """A window or end arrived after the session ended."""


def _real(value):
    """Finite real number, not a bool; accepts NumPy scalars such as float32."""
    return (isinstance(value, numbers.Real) and not isinstance(value, bool)
            and math.isfinite(value))


def _next_state(state, ema, streak, levels):
    if state == 'monitoring':
        return 'suspicious' if ema >= levels.suspicious_enter else 'monitoring'
    if state == 'suspicious':
        if streak >= ALERT_CONSECUTIVE_WINDOWS:
            return 'alert'
        return 'monitoring' if ema < levels.suspicious_exit else 'suspicious'
    return 'suspicious' if ema < levels.alert_exit else 'alert'


@dataclass(frozen=True)
class Event:
    type: str
    window: int
    data: dict


class DirectionSession:
    """Scoring state for one call direction under one immutable configuration snapshot."""

    def __init__(self, config, *, call_id, direction='caller', new_alert_id=None):
        if not isinstance(config, ScoringConfig):
            raise TypeError('DirectionSession needs a validated ScoringConfig')
        if not isinstance(call_id, str) or not call_id:
            raise ValueError('call_id must be a nonempty string')
        if direction not in config.score_directions:
            raise ValueError(f'direction {direction!r} is not configured for scoring')
        self.config, self.call_id, self.direction = config, call_id, direction
        self._new_alert_id = new_alert_id or (lambda: str(uuid.uuid4()))
        self.windows = 0
        self.ema = None
        self.max_score = None
        self.recent = deque(maxlen=RECENT_SCORES)
        self.state = 'monitoring'
        self.alert_id = None
        self.alert_streak = 0
        self.ended = None

    @property
    def provisional(self):
        return self.windows < self.config.min_windows

    def add_window(self, p_synthetic):
        """Score one window. Returns [score.updated (state after this window),
        state.changed?, alert.raised | alert.cleared?]. Nothing changes if the input is
        invalid or the alert-ID generator fails: the new state is computed first and
        committed only at the end. `state.changed` is an internal domain event, not
        part of the PRD FR-A4 published event list."""
        if self.ended is not None:
            raise SessionClosed(f'call {self.call_id} already ended')
        if not _real(p_synthetic) or not 0 <= p_synthetic <= 1:
            raise ValueError('p_synthetic must be a finite real number from 0 to 1')
        score, alpha, levels = float(p_synthetic), self.config.ema_alpha, self.config.thresholds
        windows = self.windows + 1
        ema = score if self.ema is None else alpha * score + (1 - alpha) * self.ema
        provisional = windows < self.config.min_windows
        streak, state, alert_id = self.alert_streak, self.state, self.alert_id
        changes = []
        if not provisional:
            streak = streak + 1 if ema >= levels.alert_enter else 0
            state = _next_state(self.state, ema, streak, levels)
            if state != self.state:
                base = {'call_id': self.call_id, 'direction': self.direction, 'ema': ema}
                changes.append(Event('state.changed', windows, {**base, 'from': self.state, 'to': state}))
                if state == 'alert':
                    alert_id = self._new_alert_id()
                    if not isinstance(alert_id, str) or not alert_id:
                        raise ValueError('alert-ID generator must return a nonempty string')
                    changes.append(Event('alert.raised', windows, {**base, 'alert_id': alert_id}))
                elif self.state == 'alert':
                    changes.append(Event('alert.cleared', windows, {**base, 'alert_id': self.alert_id}))
                    alert_id = None
        self.windows, self.ema, self.alert_streak, self.state, self.alert_id = (
            windows, ema, streak, state, alert_id)
        self.max_score = score if self.max_score is None else max(self.max_score, score)
        self.recent.append(score)
        return [Event('score.updated', windows, {
            'call_id': self.call_id, 'direction': self.direction, 'score': score,
            'ema': ema, 'provisional': provisional, 'state': state, 'alert_id': alert_id}), *changes]

    def validate_end(self, reason, *, speech_seconds):
        """Raise exactly as `end` would, without ending the session."""
        if self.ended is not None:
            raise SessionClosed(f'call {self.call_id} already ended')
        if reason not in END_REASONS:
            raise ValueError(f'reason must be one of {END_REASONS}')
        if not _real(speech_seconds) or speech_seconds < 0:
            raise ValueError('speech_seconds must be a finite non-negative number')

    def end(self, reason, *, speech_seconds):
        """Close the session; returns the call.ended Event. An alert still open at the
        end is `ended_open`; ending in `suspicious` persists the state only."""
        self.validate_end(reason, speech_seconds=speech_seconds)
        undetermined = self.windows < self.config.min_windows
        self.ended = Event('call.ended', self.windows, {
            'call_id': self.call_id, 'direction': self.direction, 'reason': reason,
            'speech_seconds': float(speech_seconds), 'windows': self.windows,
            'final_state': self.state, 'verdict': 'undetermined' if undetermined else self.state,
            'ema': self.ema, 'max_score': self.max_score,
            'ended_open_alert_id': self.alert_id if self.state == 'alert' else None,
        })
        return self.ended
