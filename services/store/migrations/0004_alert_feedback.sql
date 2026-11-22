-- Analyst feedback on stored alerts (backlog 7.3, 19 Nov 2026; PRD FR-A7
-- "`POST /v1/alerts/{id}/feedback` lets analysts mark true or false positive; stores the
-- label with per-window scores", PRD §6.3 feedback is audited; architecture §2.7
-- `alert_feedback`). Written by services/store/feedback.py in the same transaction as
-- its `alerts.feedback` audit row. Never edit after it is applied; add a new file instead.
--
-- Append-only by the code that writes it: a second label on the same alert is a new row
-- and the newest row (highest id) is the alert's current label, so a changed mind keeps
-- its history. `windows` is a copy of the alert's window scores when it was labelled (its
-- direction, or every direction if it has none, from the raise to its close or, while it
-- is open, to the session's last event), oldest first, at most the writer's cap;
-- `windows_total` counts every such window. Scores only: there is no embedding column,
-- because this database keeps no audio or audio features; with FR-O5 snippet retention
-- on, the alert's encrypted snippet (same alert ID, until its TTL purge) is the audio a
-- label can be paired with for retraining.
CREATE TABLE alert_feedback (
    id            BIGSERIAL PRIMARY KEY,
    alert_id      TEXT NOT NULL REFERENCES alerts (alert_id) ON DELETE CASCADE,
    label         TEXT NOT NULL CHECK (label IN ('true_positive', 'false_positive')),
    user_id       BIGINT REFERENCES users (id) ON DELETE SET NULL,
    username      TEXT NOT NULL CHECK (username ~ '^[a-z][a-z0-9._-]{0,63}$'),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    alert_status  TEXT NOT NULL CHECK (alert_status IN ('open', 'cleared', 'ended_open')),
    windows       JSONB NOT NULL,
    windows_total INTEGER NOT NULL CHECK (windows_total >= 0),
    -- CASE, not AND: PostgreSQL does not promise the order of the checks, and
    -- jsonb_array_length raises on anything but an array.
    CHECK (CASE WHEN jsonb_typeof(windows) = 'array' THEN jsonb_array_length(windows) <= windows_total
                ELSE FALSE END)
);
CREATE INDEX alert_feedback_alert ON alert_feedback (alert_id, id);
