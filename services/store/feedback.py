"""Analyst feedback on stored alerts (backlog 7.3; PRD FR-A7, §6.3; architecture §2.7
`alert_feedback`). Table from migration 0004.

`AlertFeedbackStore.record(principal, alert_id, label)` stores one label
(`true_positive` or `false_positive`) for a stored alert, with a copy of the alert's
window scores when it was labelled, and its `alerts.feedback` audit row, in one
transaction on the account store's connection (`PostgresAuthStore.transaction`): a label
that cannot be audited is not stored. The copy covers the windows `PEAK_EMA` reads in
`services/store/history.py` (the alert's direction, or every direction if it has none,
from the raise to its close or, while it is open, to the session's last event), oldest
first, at most `max_windows` of them; `windows_total` counts all. Labels are appended:
a second label on an alert is a new row and the newest (highest ID) is the current one;
at most `MAX_LABELS` per alert (then `conflict`), which bounds what one alert can
store (review L4).

The window copy is read first, without a lock; then the alert row is locked (`FOR NO
KEY UPDATE`) until commit and its status and close time are read again: if the alert
closed in between, the copy is made again under the lock. So the status recorded and
the copy's end agree (review L1), the live event store's `alert.cleared`/`call.ended`
waits only for the count, insert and audit row (re-review R1; usually that copy is not
repeated), and labels of one alert are written one at a time across API processes, so
ID order is commit order (review L5). A label waits at most `LOCK_WAIT_MS` for the row
(then `timeout`).

`AlertFeedbackStore.list(alert_id)` returns the alert's labels, newest first, without
the window copies (their counts only).

Not stored: embeddings or any audio feature (the store holds none). With FR-O5 snippet
retention on (`--retention on` in the live service, off by default), the alert's
encrypted snippet is kept under the same alert ID until its TTL purge (7 days by
default), so a label can be paired with its audio for retraining (PRD 5.6) while the
snippet lasts; embeddings would be computed from it then.
Failures raise `AuthStoreError` with the account store's reasons, `not_found` when no
such alert is stored, or `conflict` when the alert has `MAX_LABELS` labels.
"""
import json
import math

from services.auth.store import AuthStoreError, check_audit
from services.store.history import valid_call_id

REQUIRED_MIGRATION = 4
LABELS = ('true_positive', 'false_positive')
# 3,600 windows = 2 h of one direction at a 2 s hop; a longer alert keeps its oldest windows.
MAX_WINDOWS = 3_600
MAX_LIST = 100
MAX_LABELS = 20  # per alert: enough for every analyst to change their mind, bounded storage
LOCK_WAIT_MS = 2_000  # a label waits this long for the alert row (the live writer holds it briefly)
# The windows of an alert, as in PEAK_EMA (which also reads `c.last_event_at` for an open one).
SPAN = ('FROM window_scores w JOIN alerts a ON a.alert_id = %s JOIN calls c ON c.id = a.call_pk '
        'WHERE w.call_pk = a.call_pk AND (a.direction IS NULL OR w.direction = a.direction) '
        'AND w.scored_at >= a.raised_at AND w.scored_at <= COALESCE(a.closed_at, c.last_event_at)')
# Keep SPAN and history.PEAK_EMA on one definition (a unit test compares their clauses).


def check_feedback(alert_id, label):
    """Validate before any database work (ValueError)."""
    if not valid_call_id(alert_id):  # alerts.alert_id has the same 1-128 character CHECK
        raise ValueError('alert ID must be 1-128 characters without NUL')
    if label not in LABELS:
        raise ValueError(f'label must be one of {LABELS}')


def _num(value):
    if value is None:
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _window(row):
    direction, window, ts, scored, score, ema, provisional, quality, degraded, concealed = row
    return {'direction': direction, 'window': window, 'ts': _num(ts), 'scored': scored, 'score': _num(score),
            'ema': _num(ema), 'provisional': provisional, 'quality': quality, 'degraded': degraded,
            'concealed_fraction': _num(concealed)}


class AlertFeedbackStore:
    def __init__(self, auth_store, *, max_windows=MAX_WINDOWS):
        if isinstance(max_windows, bool) or not isinstance(max_windows, int) or not 1 <= max_windows <= MAX_WINDOWS:
            raise ValueError(f'max_windows must be an integer from 1 to {MAX_WINDOWS}')
        self._store, self.max_windows = auth_store, max_windows
        self._between = None  # tests only: called between the unlocked copy and the row lock

    def record(self, principal, alert_id, label):
        """Store `label` for the alert by `principal` (a resolved session's Principal).
        Returns the stored record (without the window copy)."""
        check_feedback(alert_id, label)
        detail = {'alert_id': alert_id, 'label': label}
        check_audit(principal.username, 'alerts.feedback', 'ok', detail)  # before any database work

        def alert_state(db, lock):
            row = db.execute('SELECT status, closed_at FROM alerts WHERE alert_id = %s'
                             + (' FOR NO KEY UPDATE' if lock else ''), (alert_id,)).fetchone()
            if row is None:
                raise AuthStoreError('no such alert', reason='not_found')
            return row

        def copy(db):
            rows = db.execute(
                'SELECT count(*) OVER (), w.direction, w.window_idx, extract(epoch FROM w.scored_at), w.scored, '
                f'w.score, w.ema, w.provisional, w.quality, w.degraded, w.concealed_fraction {SPAN} '
                'ORDER BY w.scored_at, w.direction, w.window_idx LIMIT %s',
                (alert_id, self.max_windows)).fetchall()
            return (rows[0][0] if rows else 0), [_window(row[1:]) for row in rows]

        def work(db):
            # Re-review R1: the long part (the window copy) runs before the row lock, so a
            # label holds the lock the live event store's clear/end waits on only for the
            # re-check, insert and audit row. If the alert closed in between, the copy is
            # made again under the lock, so status and copy still agree.
            seen = alert_state(db, lock=False)
            total, windows = copy(db)
            if self._between is not None:
                self._between()
            db.execute(f'SET LOCAL lock_timeout = {int(LOCK_WAIT_MS)}')
            alert = alert_state(db, lock=True)
            if tuple(alert) != tuple(seen):
                total, windows = copy(db)
            labels = db.execute('SELECT count(*) FROM alert_feedback WHERE alert_id = %s', (alert_id,)).fetchone()[0]
            if labels >= MAX_LABELS:
                raise AuthStoreError(f'the alert has {MAX_LABELS} labels already', reason='conflict')
            # clock_timestamp(), not now(): taken after the lock, so `ts` follows ID order (R4).
            stored = db.execute(
                'INSERT INTO alert_feedback (alert_id, label, user_id, username, created_at, alert_status, windows, '
                'windows_total) VALUES (%s, %s, %s, %s, clock_timestamp(), %s, %s::jsonb, %s) '
                'RETURNING id, extract(epoch FROM created_at)',
                (alert_id, label, principal.user_id, principal.username, alert[0],
                 json.dumps(windows, allow_nan=False, separators=(',', ':')), total)).fetchone()
            self._store.audit_in(db, principal.username, 'alerts.feedback', 'ok',
                                 {**detail, 'feedback_id': stored[0]})
            return {'feedback_id': stored[0], 'alert_id': alert_id, 'label': label, 'username': principal.username,
                    'ts': float(stored[1]), 'alert_state': alert[0], 'windows_stored': len(windows),
                    'windows_total': total}
        return self._store.transaction(work, required_migration=REQUIRED_MIGRATION)

    def list(self, alert_id, limit=MAX_LIST):
        """The alert's labels, newest first (`current` is the newest label or None).
        `not_found` when no such alert is stored."""
        if not valid_call_id(alert_id):
            raise ValueError('alert ID must be 1-128 characters without NUL')
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST:
            raise ValueError(f'limit must be an integer from 1 to {MAX_LIST}')

        def work(db):
            if db.execute('SELECT 1 FROM alerts WHERE alert_id = %s', (alert_id,)).fetchone() is None:
                raise AuthStoreError('no such alert', reason='not_found')
            total = db.execute('SELECT count(*) FROM alert_feedback WHERE alert_id = %s', (alert_id,)).fetchone()[0]
            rows = db.execute(
                'SELECT id, label, username, extract(epoch FROM created_at), alert_status, '
                'jsonb_array_length(windows), windows_total FROM alert_feedback WHERE alert_id = %s '
                'ORDER BY id DESC LIMIT %s', (alert_id, limit)).fetchall()
            feedback = [{'feedback_id': i, 'label': label, 'username': name, 'ts': float(ts), 'alert_state': state,
                         'windows_stored': stored, 'windows_total': windows_total}
                        for i, label, name, ts, state, stored, windows_total in rows]
            return {'alert_id': alert_id, 'current': feedback[0]['label'] if feedback else None,
                    'feedback': feedback, 'feedback_total': total, 'feedback_truncated': total > len(feedback)}
        return self._store.transaction(work, required_migration=REQUIRED_MIGRATION)
