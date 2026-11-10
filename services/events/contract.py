"""Published event envelope for `WS /v1/events` (backlog 5.1 partial; PRD FR-A4, FR-I6;
architecture §2.4, §2.5).

Only the FR-A4 types are publishable. Document-backed fields: `call.ended` carries
final state and reason (FR-A4, §2.4) and speech seconds (§2.4); `score.updated`
carries the degraded flag and the effective stride (FR-I6, §2.4); alerts carry an
alert ID (§2.4). Choices made here: requiring `score` and `state` on `score.updated`,
the names `effective_stride_s` (seconds) and `reason` on `system.health`, the state
list (FR-U1 states), and the envelope shape. Other fields are allowed. Internal domain events from
`services/scorer` (`state.changed`, `call.state`, `call.quality`, `call.summary`,
per-direction `call.ended`) are not publishable as they are; mapping them is backlog
5.2. Data must be JSON without NaN or Infinity and at most MAX_EVENT_BYTES encoded.
"""
from dataclasses import dataclass
import json
import math
import numbers

PUBLISHED_TYPES = ('call.started', 'score.updated', 'alert.raised', 'alert.cleared',
                   'call.ended', 'system.health')
REQUIRED_FIELDS = {
    'call.started': (),
    'score.updated': ('score', 'state', 'degraded', 'effective_stride_s'),
    'alert.raised': ('alert_id',),
    'alert.cleared': ('alert_id',),
    'call.ended': ('final_state', 'reason', 'speech_seconds'),
    'system.health': ('reason',),
}
END_REASONS = ('bye', 'timeout', 'monitor_lost')  # architecture §2.4
CALL_STATES = ('monitoring', 'suspicious', 'alert', 'no_speech', 'unmonitored')  # FR-U1
MAX_EVENT_BYTES = 16 * 1024  # UTF-8 bytes of the encoded data
MAX_DEPTH = 32
MAX_CALL_ID_CHARS = 128


class EventContractError(ValueError):
    pass


def _finite_real(value):
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True)
class Envelope:
    """Immutable: the data is held as canonical JSON and `data` returns a fresh copy,
    so one subscriber cannot change what another receives."""
    type: str
    call_id: object  # str, or None for system.health
    seq: int
    ts: float
    data_json: str

    @property
    def data(self):
        return json.loads(self.data_json)

    def to_json(self):
        return json.dumps({'type': self.type, 'call_id': self.call_id, 'seq': self.seq,
                           'ts': self.ts, 'data': self.data}, allow_nan=False, sort_keys=True,
                          ensure_ascii=False)


def make_envelope(event_type, call_id, data, *, seq, ts):
    """Validate and build an envelope; raises EventContractError. `data` is encoded
    once, so later changes to the caller's dict do not leak in."""
    if event_type not in PUBLISHED_TYPES:
        raise EventContractError(f'{event_type!r} is not a published FR-A4 event type')
    if not (call_id is None and event_type == 'system.health') and (
            not isinstance(call_id, str) or not 0 < len(call_id) <= MAX_CALL_ID_CHARS):
        raise EventContractError(f'{event_type} needs a call_id of 1 to {MAX_CALL_ID_CHARS} characters'
                                 + (' (or None)' if event_type == 'system.health' else ''))
    if type(seq) is not int or seq < 0:
        raise EventContractError('seq must be a non-negative integer')
    if not _finite_real(ts) or ts < 0:
        raise EventContractError('ts must be a finite non-negative number (seconds since the epoch)')
    if not isinstance(data, dict):
        raise EventContractError('data must be a dict')
    _check_structure(data, 0)
    missing = [k for k in REQUIRED_FIELDS[event_type] if k not in data]
    if missing:
        raise EventContractError(f'{event_type} is missing {missing}')
    _check_values(event_type, data)
    try:
        encoded = json.dumps(data, allow_nan=False, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError, RecursionError) as error:
        raise EventContractError(f'data is not strict JSON: {type(error).__name__}: {error}') from None
    if len(encoded.encode('utf-8')) > MAX_EVENT_BYTES:
        raise EventContractError(f'data exceeds {MAX_EVENT_BYTES} bytes')
    return Envelope(event_type, call_id, seq, float(ts), encoded)


def _check_structure(value, depth):
    """String keys at every level and bounded nesting (no RecursionError later)."""
    if depth > MAX_DEPTH:
        raise EventContractError(f'data nests deeper than {MAX_DEPTH} levels')
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise EventContractError('data keys must be strings at every level')
        for item in value.values():
            _check_structure(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_structure(item, depth + 1)


def _check_values(event_type, data):
    if event_type == 'call.ended':
        if data['reason'] not in END_REASONS:
            raise EventContractError(f'call.ended reason must be one of {END_REASONS}')
        if data['final_state'] not in CALL_STATES:
            raise EventContractError(f'call.ended final_state must be one of {CALL_STATES}')
        if not _finite_real(data['speech_seconds']) or data['speech_seconds'] < 0:
            raise EventContractError('call.ended speech_seconds must be a finite number >= 0')
    if event_type == 'score.updated':
        if not isinstance(data['degraded'], bool):
            raise EventContractError('score.updated degraded must be a boolean')
        if not _finite_real(data['effective_stride_s']) or data['effective_stride_s'] <= 0:
            raise EventContractError('score.updated effective_stride_s must be a finite number > 0')
        if data['score'] is not None and (not _finite_real(data['score']) or not 0 <= data['score'] <= 1):
            raise EventContractError('score.updated score must be None or a number from 0 to 1')
        if data['state'] not in CALL_STATES:
            raise EventContractError(f'score.updated state must be one of {CALL_STATES}')
