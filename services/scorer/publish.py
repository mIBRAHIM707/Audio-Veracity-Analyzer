"""Map scorer domain events to published FR-A4 events (backlog 5.2 partial; PRD FR-A4,
FR-I5, FR-I6, FR-I7, FR-I8, FR-I10; architecture §2.4).

`services/scorer` emits internal events per direction (`score.updated`,
`state.changed`, `alert.raised`, `alert.cleared`, `call.quality`, `call.state`,
per-direction `call.ended`, `call.summary`). Only the FR-A4 types are published
(`services/events/contract.py`). `map_window_events` and `map_end_events` are pure;
`CallEventPublisher` tracks the call-level state between windows and publishes to an
`EventHub`, containing every publishing failure (AVA is advisory: a failed publish
must never stop scoring or a call).

Mapping (choices made here are marked):
- `score.updated` carries the window's score, EMA, provisional flag, direction, the
  direction's own state (`direction_state`) and the call-level `state` after the
  window (FR-I8: the more severe direction, with `driving_direction`), plus
  `quality`, `degraded` and `effective_stride_s` (FR-I10, supplied by the caller from
  the degraded-mode controller) and `scored: true`.
- `alert.raised` / `alert.cleared` carry `alert_id`, `direction`, `ema`, `window`.
- `state.changed` and `call.state` are not published: their information is in the
  next `score.updated` of the same window (session.py contract).
- `call.quality` (choice): published as `score.updated` with `score: null`,
  `scored: false`, `quality: "low"` and the concealed fraction, so the dashboard learns
  of the low-quality flag without waiting for the next scored window.
- Status updates without a window (`no_speech`, `unmonitored`, from the VAD and the
  degraded-mode controller): `status()` publishes `score.updated` with `score: null`
  and the given state (choice).
- `call.ended` (choice): one call-level event from `call.summary` and the
  per-direction ends: `final_state`, `reason`, `speech_seconds` = sum over the scored
  directions (speech of both parties when the agent is scored), with `verdict`
  (FR-I5 `undetermined`), `driving_direction`, `quality`, the per-direction speech
  seconds and windows, and `ended_open_alert_ids`.

Ordering: hub `seq` is assigned at publish, so a call's results must be published in
the order the scorer returned them. `live.LiveCalls` does this by applying and
publishing each result under the call's own lock; use it (or an equivalent single
owner) rather than calling `add_result` and `window` from different workers. The
publisher itself is not thread-safe. Open spec questions: whether a `no_speech` /
`unmonitored` status should be shown over an open alert, and whether both directions'
timeline points should carry the call-level state.
"""
INTERNAL_ONLY = ('state.changed', 'call.state')
STATUS_STATES = ('no_speech', 'unmonitored')


def _score_data(data, window, call):
    return {'score': float(data['score']), 'ema': float(data['ema']), 'provisional': bool(data['provisional']),
            'direction': data['direction'], 'direction_state': data['state'],
            'direction_alert_id': data['alert_id'], 'window': window, 'scored': True, **call}


def fold_call_state(events, call_state):
    """The call-level state (`state`, `driving_direction`, `quality`) after a window,
    from the state before it. Pure; kept separate so a mapping failure cannot lose it."""
    state = dict(call_state)
    for event in events:
        if event.type == 'call.state':
            state.update(state=event.data['state'], driving_direction=event.data['driving_direction'],
                         quality=event.data['quality'])
        elif event.type == 'call.quality':
            state['quality'] = event.data['quality']
    return state


def map_window_events(events, *, call_state, degraded, effective_stride_s):
    """Map one window's scorer events. `call_state` is the call-level state before the
    window. Returns `(published, new_call_state, unknown_types)`: `published` lists
    `(type, call_id, data)`; event types this module does not know are skipped and
    returned in `unknown_types` (the rest of the window is still published)."""
    state = fold_call_state(events, call_state)  # the state after the window applies to all of it
    call = {'state': state['state'], 'driving_direction': state['driving_direction'],
            'quality': state['quality'], 'degraded': bool(degraded),
            'effective_stride_s': float(effective_stride_s)}
    published, unknown = [], []
    for event in events:
        data, call_id = event.data, event.data['call_id']
        if event.type == 'score.updated':
            published.append(('score.updated', call_id, _score_data(data, event.window, call)))
        elif event.type in ('alert.raised', 'alert.cleared'):
            published.append((event.type, call_id, {'alert_id': data['alert_id'], 'direction': data['direction'],
                                                    'ema': float(data['ema']), 'window': event.window}))
        elif event.type == 'call.quality':  # same shape as a scored update, with nulls
            published.append(('score.updated', call_id, {
                'score': None, 'ema': None, 'provisional': False, 'direction': data['direction'],
                'direction_state': None, 'direction_alert_id': None, 'window': event.window,
                'scored': False, 'concealed_fraction': float(data['concealed_fraction']), **call}))
        elif event.type not in INTERNAL_ONLY:
            unknown.append(event.type)
    return published, state, unknown


def map_end_events(events):
    """Map `CallScorer.end` output (per-direction `call.ended` plus `call.summary`) to
    one published `(type, call_id, data)`."""
    ends = [e for e in events if e.type == 'call.ended']
    summaries = [e for e in events if e.type == 'call.summary']
    unknown = [e.type for e in events if e.type not in ('call.ended', 'call.summary')]
    if len(summaries) != 1 or unknown:
        raise ValueError('end events need exactly one call.summary and only call.ended otherwise')
    summary = summaries[0].data
    speech = {e.data['direction']: float(e.data['speech_seconds']) for e in ends}
    return ('call.ended', summary['call_id'], {
        'final_state': summary['final_state'], 'reason': summary['reason'],
        'speech_seconds': sum(speech.values()), 'speech_seconds_by_direction': speech,
        'windows_by_direction': {e.data['direction']: e.data['windows'] for e in ends},
        'verdict': summary['verdict'], 'driving_direction': summary['driving_direction'],
        'quality': summary['quality'], 'ended_open_alert_ids': dict(summary['ended_open_alert_ids'])})


class CallEventPublisher:
    """Publishes one call's events to a hub. Every method returns the envelopes that
    were published and never raises for a hub or contract failure; failures are counted
    in `failures` (by exception type name). Mapping errors from malformed scorer events
    are counted the same way."""

    def __init__(self, hub, call_id):
        if not isinstance(call_id, str) or not call_id:
            raise ValueError('call_id must be a nonempty string')
        self._hub, self.call_id = hub, call_id
        self.call_state = {'state': 'monitoring', 'driving_direction': None, 'quality': 'ok'}
        self.failures = {}
        self.published = 0

    def _publish(self, items):
        envelopes = []
        for event_type, call_id, data in items:
            try:
                envelopes.append(self._hub.publish(event_type, call_id, data))
                self.published += 1
            except Exception as error:  # advisory: never propagate into scoring
                self._count(type(error).__name__)
        return envelopes

    def _guard(self, build):
        try:
            return self._publish(build())
        except Exception as error:  # a mapping fault must not reach the scoring loop either
            self._count('mapping:' + type(error).__name__)
            return []

    def started(self, **data):
        return self._guard(lambda: [('call.started', self.call_id, data)])

    def _count(self, name, n=1):
        self.failures[name] = self.failures.get(name, 0) + n

    def window(self, events, *, degraded=False, effective_stride_s):
        """Publish one window's scorer events (e.g. `OrderedCallScorer.add_result(...)
        ['events']`, an empty list for a rejected result). The tracked call state is
        updated first, so it follows the scorer even if mapping or publishing fails. A
        window can be partly published if one of its events fails the contract; the
        failure is counted, not traced to the window."""
        try:
            if any(e.data.get('call_id') != self.call_id for e in events):
                raise ValueError('events belong to another call')
            self.call_state = fold_call_state(events, self.call_state)
        except Exception as error:
            self._count('mapping:' + type(error).__name__)
            return []

        def build():
            items, _, unknown = map_window_events(events, call_state=self.call_state, degraded=degraded,
                                                  effective_stride_s=effective_stride_s)
            if unknown:
                self._count('unknown_event', len(unknown))
            return items
        return self._guard(build)

    def status(self, state, *, degraded, effective_stride_s, reason=None):
        """A state without a scored window: only `no_speech` (VAD) or `unmonitored`
        (degraded-mode stride cap); alert states come only from the scorer."""
        def build():
            if state not in STATUS_STATES:
                raise ValueError(f'status state must be one of {STATUS_STATES}')
            data = {'score': None, 'ema': None, 'provisional': False, 'direction': None, 'scored': False,
                    'state': state, 'driving_direction': self.call_state['driving_direction'],
                    'quality': self.call_state['quality'], 'degraded': bool(degraded),
                    'effective_stride_s': float(effective_stride_s)}
            if reason is not None:
                data['reason'] = reason
            return [('score.updated', self.call_id, data)]
        return self._guard(build)

    def ended(self, events, **extra):
        """Publish the call-level `call.ended`; `extra` fields (e.g.
        `speech_seconds_known=False`) are added to its data."""
        def build():
            event_type, call_id, data = map_end_events(events)
            if call_id != self.call_id:
                raise ValueError('end events belong to another call')
            if set(extra) & set(data):
                raise ValueError(f'extra fields may not replace mapped ones: {sorted(set(extra) & set(data))}')
            return [(event_type, call_id, {**data, **extra})]
        return self._guard(build)
