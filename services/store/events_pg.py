"""Durable record of published FR-A4 events in PostgreSQL (backlog 5.1; PRD FR-I11
"scores and state are flushed to PostgreSQL every hop", FR-A3 "full history in
PostgreSQL"; architecture §2.4, §2.7).

`PostgresEventStore.record(envelope)` writes one validated envelope
(`services.events.contract.Envelope`) in its own transaction and returns an outcome:

- `call.started` opens a `calls` row (one open row per ID, by a partial unique
  index). A start for an implicitly started open call that is not later than its
  first event completes it (start data kept, `implicit_start` cleared, start time =
  the start's time). A second start for an open call is a `duplicate` when its time is
  not later than the applied start's (`start_event_at`; a replay or retried write).
  A start strictly later than the open session's start (the applied start, or the
  first event of an implicit one) supersedes it (19 Nov, 5.QA): an abrupt scorer stop
  sent no `call.ended`, and the restarted scorer re-tapped the call and published one
  new start before that session's other events. The old row ends `monitor_lost` at
  its last event with `end_inferred` and no speech seconds, its open alerts become
  `ended_open`, and a new row is opened (outcome `stored`, counted in `superseded`).
  The seq cannot be used: the restarted scorer's hub starts again at 1. An open
  explicit row without `start_event_at` (not written by this code; migration 0002
  fills it for older rows) never supersedes: `duplicate`. An ID may be reused only by
  a start strictly later than that ID's last end; otherwise the start is `after_end`.
- `score.updated` with a direction and window index inserts one `window_scores` row
  (primary key call/direction/window: a replay is a `duplicate` and does not move the
  call state back) and updates the call's current state; a status update
  (`no_speech`/`unmonitored`, no direction) only overwrites the call's current state:
  it is not kept as history and has no replay key.
- `alert.raised` inserts an `alerts` row (`open`); `alert.cleared` closes it
  (`cleared`); `call.ended` closes the call (final state, reason, speech seconds,
  verdict, end data) and marks its still-open alerts `ended_open`.
- Any other event for a call ID with no open row is `after_end` (nothing written) if
  that ID's latest call has ended, whatever its time (FR-I11 late results, a status
  published after the end, replays); with no earlier call it starts one implicitly
  (`implicit_start`), as the in-memory registry does. `system.health` is not stored
  here (`not_stored`).

Errors roll the whole envelope back and raise `StoreError`. `retryable` is True for a
lost or refused connection (the connection is closed and reopened on the next call,
and the same envelope can be retried: records are idempotent) and False for an
envelope the database will never accept (bad characters, out-of-range values or time,
a failed constraint); do not retry those. The store never holds audio. One writer per
database is assumed for ordering. Connection lifecycle is the caller's (`connect` is
injected) and the connection must not have a transaction open: psycopg would then
nest a savepoint and nothing would be committed, so such a connection is refused with
a plain ValueError (a misconfigured factory, not a bad envelope; never a StoreError).
"""
from datetime import datetime, timezone

STORED_TYPES = ('call.started', 'score.updated', 'alert.raised', 'alert.cleared', 'call.ended')
OUTCOMES = ('stored', 'duplicate', 'after_end', 'not_stored', 'unknown_alert')
TRANSIENT = ('OperationalError', 'InterfaceError')  # psycopg classes for connection faults
SUPERSEDED = 'superseded'  # internal: a start that closed an open session (counted as stored)


class StoreError(RuntimeError):
    """The write failed; nothing from this envelope was kept. `retryable` tells a
    lost/refused connection (retry the same envelope) from a refused envelope."""

    def __init__(self, message, *, retryable):
        super().__init__(message)
        self.retryable = retryable


def _transient(error):
    return isinstance(error, OSError) or any(cls.__name__ in TRANSIENT for cls in type(error).__mro__)


def _when(ts):
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as error:  # e.g. milliseconds passed as seconds
        raise ValueError(f'event time {ts!r} is out of range') from error


def _idle(connection):
    try:
        return getattr(connection.info.transaction_status, 'name', None) == 'IDLE'
    except Exception:  # a broken connection object: treat as not idle
        return False




class PostgresEventStore:
    def __init__(self, connect):
        self._connect, self._connection = connect, None
        self.counts = {outcome: 0 for outcome in OUTCOMES}
        self.failures = 0
        self.superseded = 0  # starts (also counted as 'stored') that ended an open session

    def close(self):
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:  # closing a broken connection must not mask the original error
                pass

    def record(self, envelope):
        if envelope.type not in STORED_TYPES:
            self.counts['not_stored'] += 1
            return 'not_stored'
        data = envelope.data
        try:
            if self._connection is None or self._connection.closed:
                self._connection = self._connect()
        except Exception as error:
            self.failures += 1
            raise StoreError(f'could not connect: {type(error).__name__}', retryable=_transient(error)) from error
        if not _idle(self._connection):
            # A misconfigured connection factory, not a bad envelope: raised outside
            # StoreError so a caller never discards valid events because of it.
            self.failures += 1
            self.close()
            raise ValueError('connect() returned a connection with a transaction open (or broken): '
                             'commit or roll back before returning it')
        try:
            with self._connection.transaction():
                outcome = self._apply(self._connection, envelope, data)
        except Exception as error:  # psycopg errors, lost connections, refused values
            self.failures += 1
            retryable = _transient(error)
            if retryable or self._connection is None or self._connection.closed or not _idle(self._connection):
                self.close()
            raise StoreError(f'{envelope.type} for a call was not stored: {type(error).__name__}',
                             retryable=retryable) from error
        if outcome == SUPERSEDED:
            self.superseded += 1
            outcome = 'stored'
        self.counts[outcome] += 1
        return outcome

    # -- one transaction per envelope -------------------------------------------------

    def _apply(self, db, envelope, data):
        when = _when(envelope.ts)
        if envelope.type == 'call.started':
            return self._start(db, envelope, data, when)
        call_pk = self._open_call(db, envelope, when)
        if call_pk is None:
            return 'after_end'
        handler = {'score.updated': self._score, 'alert.raised': self._raise,
                   'alert.cleared': self._clear, 'call.ended': self._end}[envelope.type]
        return handler(db, call_pk, envelope, data, when)

    def _start(self, db, envelope, data, when):
        open_row = db.execute('SELECT id, implicit_start, started_at, start_event_at FROM calls '
                              'WHERE call_id = %s AND ended_at IS NULL FOR UPDATE', (envelope.call_id,)).fetchone()
        if open_row:
            call_pk, implicit, started_at, start_event_at = open_row
            if implicit:
                if when > started_at:  # a session's start precedes its other events
                    return self._supersede(db, call_pk, envelope, when)
                db.execute('UPDATE calls SET implicit_start = FALSE, start_data = %s::jsonb, start_event_at = %s, '
                           'started_at = least(started_at, %s), last_event_at = greatest(last_event_at, %s), '
                           'last_seq = greatest(last_seq, %s) WHERE id = %s',
                           (envelope.data_json, when, when, when, envelope.seq, call_pk))
                return 'stored'
            if start_event_at is not None and when > start_event_at:
                return self._supersede(db, call_pk, envelope, when)
            return 'duplicate'  # a replay (same time) or an earlier start; NULL: defensive only
        ended = self._last_end(db, envelope.call_id)
        if ended is not None and ended >= when:
            return 'after_end'  # a replayed start of a call that has already ended
        self._insert_start(db, envelope, when)
        return 'stored'

    @staticmethod
    def _insert_start(db, envelope, when):
        db.execute('INSERT INTO calls (call_id, started_at, start_event_at, start_data, last_event_at, last_seq) '
                   'VALUES (%s, %s, %s, %s::jsonb, %s, %s)',
                   (envelope.call_id, when, when, envelope.data_json, when, envelope.seq))

    def _supersede(self, db, call_pk, envelope, when):
        """An abrupt scorer stop (crash, SIGKILL) sent no `call.ended`; the restarted
        scorer re-tapped the call and published a new start. The old row ends
        `monitor_lost` at its last event (never after the new start), with an inferred
        end: no speech seconds or verdict were reported, so none are written. Its open
        alerts become `ended_open` (closed at that end, or at their raise if later).
        Then the new session's row is opened."""
        end = db.execute("UPDATE calls SET ended_at = least(last_event_at, %s), final_state = state, "
                         "end_reason = 'monitor_lost', end_inferred = TRUE WHERE id = %s RETURNING ended_at",
                         (when, call_pk)).fetchone()[0]
        # Never closed before it was raised (a backwards clock step can put the end there).
        db.execute("UPDATE alerts SET status = 'ended_open', closed_at = greatest(raised_at, %s) "
                   "WHERE call_pk = %s AND status = 'open'", (end, call_pk))
        self._insert_start(db, envelope, when)
        return SUPERSEDED  # reported as 'stored' once the transaction has committed

    def _open_call(self, db, envelope, when):
        """The open row for the call ID, started implicitly if the ID was never seen;
        None if that ID's latest call has ended (the event comes after the end)."""
        row = db.execute('SELECT id FROM calls WHERE call_id = %s AND ended_at IS NULL FOR UPDATE',
                         (envelope.call_id,)).fetchone()
        if row:
            return row[0]
        if self._last_end(db, envelope.call_id) is not None:
            return None
        return db.execute('INSERT INTO calls (call_id, started_at, implicit_start, last_event_at, last_seq) '
                          'VALUES (%s, %s, TRUE, %s, %s) RETURNING id',
                          (envelope.call_id, when, when, envelope.seq)).fetchone()[0]

    @staticmethod
    def _last_end(db, call_id):
        return db.execute('SELECT max(ended_at) FROM calls WHERE call_id = %s', (call_id,)).fetchone()[0]

    def _touch(self, db, call_pk, envelope, when, **state):
        assignments = ', '.join(f'{column} = %s' for column in state)
        db.execute(f'UPDATE calls SET {assignments}{", " if state else ""}'
                   'last_event_at = greatest(last_event_at, %s), last_seq = greatest(last_seq, %s) WHERE id = %s',
                   (*state.values(), when, envelope.seq, call_pk))

    def _score(self, db, call_pk, envelope, data, when):
        call_state = {'state': data['state'], 'driving_direction': data.get('driving_direction'),
                      'quality': data.get('quality'), 'degraded': data['degraded'],
                      'effective_stride_s': data['effective_stride_s']}
        direction, window = data.get('direction'), data.get('window')
        outcome = 'stored'
        if direction is not None and window is not None:
            inserted = db.execute(
                'INSERT INTO window_scores (call_pk, direction, window_idx, scored_at, seq, scored, score, ema, '
                'provisional, direction_state, call_state, quality, degraded, effective_stride_s, concealed_fraction) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) '
                'ON CONFLICT (call_pk, direction, window_idx) DO NOTHING RETURNING call_pk',
                (call_pk, direction, window, when, envelope.seq, bool(data.get('scored', data['score'] is not None)),
                 data['score'], data.get('ema'), bool(data.get('provisional', False)), data.get('direction_state'),
                 data['state'], data.get('quality'), data['degraded'], data['effective_stride_s'],
                 data.get('concealed_fraction'))).fetchone()
            if not inserted:
                return 'duplicate'  # a replayed window must not move the call state back
        self._touch(db, call_pk, envelope, when, **call_state)
        return outcome

    def _raise(self, db, call_pk, envelope, data, when):
        row = db.execute('INSERT INTO alerts (alert_id, call_pk, direction, status, raised_at, raised_window, '
                         'raised_ema) VALUES (%s, %s, %s, %s, %s, %s, %s) '
                         'ON CONFLICT (alert_id) DO NOTHING RETURNING alert_id',
                         (data['alert_id'], call_pk, data.get('direction'), 'open', when, data.get('window'),
                          data.get('ema'))).fetchone()
        if not row:
            return 'duplicate'
        self._touch(db, call_pk, envelope, when)
        return 'stored'

    def _clear(self, db, call_pk, envelope, data, when):
        row = db.execute("UPDATE alerts SET status = 'cleared', closed_at = %s, cleared_window = %s, "
                         "cleared_ema = %s WHERE alert_id = %s AND call_pk = %s AND status = 'open' "
                         'RETURNING alert_id',
                         (when, data.get('window'), data.get('ema'), data['alert_id'], call_pk)).fetchone()
        if not row:
            known = db.execute('SELECT 1 FROM alerts WHERE alert_id = %s', (data['alert_id'],)).fetchone()
            return 'duplicate' if known else 'unknown_alert'
        self._touch(db, call_pk, envelope, when)
        return 'stored'

    def _end(self, db, call_pk, envelope, data, when):
        db.execute("UPDATE alerts SET status = 'ended_open', closed_at = %s WHERE call_pk = %s AND status = 'open'",
                   (when, call_pk))
        db.execute('UPDATE calls SET ended_at = %s, final_state = %s, end_reason = %s, speech_seconds = %s, '
                   'verdict = %s, end_data = %s::jsonb, state = %s, last_event_at = greatest(last_event_at, %s), '
                   'last_seq = greatest(last_seq, %s) WHERE id = %s',
                   (when, data['final_state'], data['reason'], data['speech_seconds'], data.get('verdict'),
                    envelope.data_json, data['final_state'], when, envelope.seq, call_pk))
        return 'stored'
