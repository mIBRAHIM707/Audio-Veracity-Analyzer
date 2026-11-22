-- AVA durable live-call records (backlog 5.1; PRD FR-I11, FR-A3; architecture §2.4, §2.7).
-- Metadata and scores only: no audio, no caller audio features. Applied by
-- services/store/migrate.py inside one transaction; never edit after it is applied
-- (the runner refuses a changed checksum); add a new numbered file instead.

CREATE TABLE calls (
    id                 BIGSERIAL PRIMARY KEY,
    call_id            TEXT NOT NULL CHECK (char_length(call_id) BETWEEN 1 AND 128),
    started_at         TIMESTAMPTZ NOT NULL,
    implicit_start     BOOLEAN NOT NULL DEFAULT FALSE,
    start_data         JSONB NOT NULL DEFAULT '{}'::jsonb,
    state              TEXT NOT NULL DEFAULT 'monitoring'
                       CHECK (state IN ('monitoring', 'suspicious', 'alert', 'no_speech', 'unmonitored')),
    driving_direction  TEXT CHECK (driving_direction IN ('caller', 'agent')),
    quality            TEXT,
    degraded           BOOLEAN NOT NULL DEFAULT FALSE,
    effective_stride_s DOUBLE PRECISION CHECK (effective_stride_s > 0),
    last_event_at      TIMESTAMPTZ NOT NULL,
    last_seq           BIGINT NOT NULL CHECK (last_seq >= 0),
    ended_at           TIMESTAMPTZ,
    final_state        TEXT CHECK (final_state IN ('monitoring', 'suspicious', 'alert', 'no_speech', 'unmonitored')),
    end_reason         TEXT CHECK (end_reason IN ('bye', 'timeout', 'monitor_lost')),
    speech_seconds     DOUBLE PRECISION CHECK (speech_seconds >= 0),
    verdict            TEXT,
    end_data           JSONB,
    CHECK ((ended_at IS NULL) = (end_reason IS NULL)),
    CHECK ((ended_at IS NULL) = (final_state IS NULL)),
    CHECK ((ended_at IS NULL) = (speech_seconds IS NULL))
);
-- One open record per call ID; an ID may be reused after its call ended.
CREATE UNIQUE INDEX calls_one_open_per_call_id ON calls (call_id) WHERE ended_at IS NULL;
CREATE INDEX calls_call_id ON calls (call_id, started_at);
CREATE INDEX calls_started_at ON calls (started_at);

-- One row per scored (or quality-gated) window and direction, flushed every hop.
CREATE TABLE window_scores (
    call_pk            BIGINT NOT NULL REFERENCES calls (id) ON DELETE CASCADE,
    direction          TEXT NOT NULL CHECK (direction IN ('caller', 'agent')),
    window_idx         INTEGER NOT NULL CHECK (window_idx >= 0),
    scored_at          TIMESTAMPTZ NOT NULL,
    seq                BIGINT NOT NULL CHECK (seq >= 0),
    scored             BOOLEAN NOT NULL,
    score              DOUBLE PRECISION CHECK (score BETWEEN 0 AND 1),
    ema                DOUBLE PRECISION CHECK (ema BETWEEN 0 AND 1),
    provisional        BOOLEAN NOT NULL,
    direction_state    TEXT,
    call_state         TEXT NOT NULL
                       CHECK (call_state IN ('monitoring', 'suspicious', 'alert', 'no_speech', 'unmonitored')),
    quality            TEXT,
    degraded           BOOLEAN NOT NULL,
    effective_stride_s DOUBLE PRECISION NOT NULL CHECK (effective_stride_s > 0),
    concealed_fraction DOUBLE PRECISION CHECK (concealed_fraction BETWEEN 0 AND 1),
    PRIMARY KEY (call_pk, direction, window_idx),
    CHECK (scored = (score IS NOT NULL)),
    CHECK (scored = (ema IS NOT NULL))
);

CREATE TABLE alerts (
    alert_id           TEXT PRIMARY KEY CHECK (char_length(alert_id) BETWEEN 1 AND 128),
    call_pk            BIGINT NOT NULL REFERENCES calls (id) ON DELETE CASCADE,
    direction          TEXT CHECK (direction IN ('caller', 'agent')),
    status             TEXT NOT NULL CHECK (status IN ('open', 'cleared', 'ended_open')),
    raised_at          TIMESTAMPTZ NOT NULL,
    raised_window      INTEGER,
    raised_ema         DOUBLE PRECISION,
    closed_at          TIMESTAMPTZ,
    cleared_window     INTEGER,
    cleared_ema        DOUBLE PRECISION,
    CHECK ((status = 'open') = (closed_at IS NULL))
);
CREATE INDEX alerts_call_pk ON alerts (call_pk);
CREATE INDEX alerts_raised_at ON alerts (raised_at);
