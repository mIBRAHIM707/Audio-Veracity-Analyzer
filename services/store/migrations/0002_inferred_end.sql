-- Inferred ends after an abrupt live stop (backlog 5.QA, 19 Nov 2026; PRD FR-A3, FR-I11;
-- architecture §2.4 `monitor_lost`). A killed scorer sends no `call.ended`; when the
-- restarted scorer re-taps the call its new `call.started` closes the old open row.
-- That end is inferred: no speech seconds were reported, so none are invented.
-- Never edit after it is applied; add a new numbered file instead.

ALTER TABLE calls ADD COLUMN end_inferred BOOLEAN NOT NULL DEFAULT FALSE;
-- Time of the applied explicit `call.started`; NULL for an implicit start. The store
-- compares a new start with it to tell a retried write of the same start (same time)
-- from a restarted scorer's new session (a later time).
ALTER TABLE calls ADD COLUMN start_event_at TIMESTAMPTZ;
-- Rows written before this file: the live process applies migrations at start-up and
-- is the database's only writer, so an open row here belongs to a process that has
-- stopped. Its start time is taken as the start event's time, so a re-tap after an
-- abrupt stop can supersede it.
UPDATE calls SET start_event_at = started_at WHERE ended_at IS NULL AND NOT implicit_start;

-- 0001 declared three unnamed table checks; PostgreSQL named them calls_check,
-- calls_check1 and calls_check2 in order. calls_check2 is
-- (ended_at IS NULL) = (speech_seconds IS NULL). The PostgreSQL tests assert which
-- checks remain after this file, also on a database upgraded with rows in it.
ALTER TABLE calls DROP CONSTRAINT calls_check2;
ALTER TABLE calls ADD CONSTRAINT calls_speech_seconds_reported
    CHECK (end_inferred OR ((ended_at IS NULL) = (speech_seconds IS NULL)));
ALTER TABLE calls ADD CONSTRAINT calls_inferred_end_shape
    CHECK (NOT end_inferred OR (ended_at IS NOT NULL AND end_reason = 'monitor_lost' AND speech_seconds IS NULL));
