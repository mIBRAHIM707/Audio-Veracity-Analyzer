"""Call-level scoring: direction selection, agent-direction semantics and the window
quality gate (backlog 4.3; PRD FR-I8, FR-I2 quality gate, FR-U1 flags; architecture
§2.2 step 1 and §2.4).

Pure domain logic over `DirectionSession`. Rules:
- Only directions in `score_directions` are scored (caller by default, FR-I8).
- The agent direction starts only after `human_agent_answered()`; agent windows before
  that (IVR prompts) are counted and ignored.
- A window whose concealed fraction exceeds `max_concealed_fraction` (10 % by default,
  FR-I2) is not scored, and the call is marked `quality=low`. Interpretation choices:
  the flag stays low for the rest of the call (FR-I2 does not say whether it
  recovers); it is call-level (architecture §2.4 lists quality per direction; drop
  counts stay per direction); only windows that would otherwise be scored are gated.
- The call state is the more severe of the scored directions, with the driving
  direction shown. Interpretation choices: ties go to the caller; no driver while
  everything is `monitoring`.
- `no_speech` (needs the VAD, backlog 3.2) and `unmonitored` (degraded mode, backlog
  8.2) are not produced here.
- Not thread-safe: one owner per call, calls serialized, windows in order per
  direction (same contract as `session.py`). Event window numbers count that direction's scored
  windows; directions do not share one timeline.
"""
from .config import DIRECTIONS, ScoringConfig
from .session import END_REASONS, DirectionSession, Event, SessionClosed, _real

SEVERITY = {'monitoring': 0, 'suspicious': 1, 'alert': 2}


class CallScorer:
    def __init__(self, config, *, call_id, new_alert_id=None):
        if not isinstance(config, ScoringConfig):
            raise TypeError('CallScorer needs a validated ScoringConfig')
        if not isinstance(call_id, str) or not call_id:
            raise ValueError('call_id must be a nonempty string')
        self.config, self.call_id, self._new_alert_id = config, call_id, new_alert_id
        self.sessions = {}
        if 'caller' in config.score_directions:
            self.sessions['caller'] = self._session('caller')
        self.agent_answered = False
        self.quality = 'ok'
        self.dropped_low_quality = {d: 0 for d in DIRECTIONS}
        self.ignored = {'direction_not_scored': 0, 'before_human_answer': 0}
        self.ended = None
        self.state, self.driving_direction = self._combined()

    def _session(self, direction):
        return DirectionSession(self.config, call_id=self.call_id, direction=direction,
                                new_alert_id=self._new_alert_id)

    def human_agent_answered(self):
        """A human agent answered (IVR finished). Starts agent scoring if configured."""
        if self.ended is not None:
            raise SessionClosed(f'call {self.call_id} already ended')
        if not self.agent_answered:
            self.agent_answered = True
            if 'agent' in self.config.score_directions:
                self.sessions['agent'] = self._session('agent')

    def add_window(self, direction, p_synthetic, *, concealed_fraction=0.0):
        """Route one window. Returns the direction session's events plus `call.quality`
        and `call.state` events when those change. Invalid input changes nothing."""
        if self.ended is not None:
            raise SessionClosed(f'call {self.call_id} already ended')
        if direction not in DIRECTIONS:
            raise ValueError(f'direction must be one of {DIRECTIONS}')
        for name, value in (('p_synthetic', p_synthetic), ('concealed_fraction', concealed_fraction)):
            if not _real(value) or not 0 <= value <= 1:
                raise ValueError(f'{name} must be a finite real number from 0 to 1')
        if direction not in self.config.score_directions:
            self.ignored['direction_not_scored'] += 1
            return []
        if direction == 'agent' and not self.agent_answered:
            self.ignored['before_human_answer'] += 1
            return []
        if concealed_fraction > self.config.max_concealed_fraction:
            self.dropped_low_quality[direction] += 1
            if self.quality == 'low':
                return []
            self.quality = 'low'
            return [Event('call.quality', self.sessions[direction].windows, {
                'call_id': self.call_id, 'direction': direction, 'quality': 'low',
                'concealed_fraction': float(concealed_fraction)})]
        events = self.sessions[direction].add_window(p_synthetic)
        state, driver = self._combined()
        if (state, driver) != (self.state, self.driving_direction):
            self.state, self.driving_direction = state, driver
            events.append(Event('call.state', events[0].window, {
                'call_id': self.call_id, 'state': state, 'driving_direction': driver,
                'quality': self.quality, 'window_direction': direction}))
        return events

    def _combined(self):
        """Most severe direction state and the direction driving it; no driver while
        every direction is `monitoring`; ties go to the caller (listed first)."""
        if not self.sessions:
            return 'monitoring', None
        direction = max(self.sessions, key=lambda d: (SEVERITY[self.sessions[d].state],
                                                      -DIRECTIONS.index(d)))
        state = self.sessions[direction].state
        return state, (None if state == 'monitoring' else direction)

    def validate_end(self, reason, *, speech_seconds):
        """Raise exactly as `end` would for these arguments, changing nothing."""
        if self.ended is not None:
            raise SessionClosed(f'call {self.call_id} already ended')
        if reason not in END_REASONS:
            raise ValueError(f'reason must be one of {END_REASONS}')
        if not isinstance(speech_seconds, dict) or set(speech_seconds) != set(self.sessions):
            raise ValueError(f'speech_seconds must give exactly the scored directions {sorted(self.sessions)}')
        for direction, session in self.sessions.items():  # validate everything before ending any
            session.validate_end(reason, speech_seconds=speech_seconds[direction])

    def end(self, reason, *, speech_seconds):
        """End every scored direction. `speech_seconds` is a dict giving each scored
        direction's speech seconds. Returns each call.ended event and a call.summary.
        Verdict (interpretation): `suspicious`/`alert` if some direction is in that state
        (only a direction with enough evidence can be); `monitoring` only if every
        scored direction reached `min_windows`; otherwise `undetermined`."""
        self.validate_end(reason, speech_seconds=speech_seconds)
        ended = [session.end(reason, speech_seconds=speech_seconds[direction])
                 for direction, session in self.sessions.items()]
        if self.state != 'monitoring':
            verdict = self.state
        elif ended and all(e.data['verdict'] != 'undetermined' for e in ended):
            verdict = 'monitoring'
        else:
            verdict = 'undetermined'
        self.ended = Event('call.summary', max((e.window for e in ended), default=0), {
            'call_id': self.call_id, 'reason': reason, 'final_state': self.state,
            'driving_direction': self.driving_direction,
            'verdict': verdict, 'quality': self.quality,
            'dropped_low_quality': dict(self.dropped_low_quality), 'ignored': dict(self.ignored),
            'ended_open_alert_ids': {e.data['direction']: e.data['ended_open_alert_id']
                                     for e in ended if e.data['ended_open_alert_id']},
        })
        return [*ended, self.ended]
