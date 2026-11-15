# Store (backlog 5.1 partial; PRD FR-I11, FR-A3; architecture §2.4, §2.7, ADR-7)

Durable PostgreSQL records of live calls: metadata and scores only, never audio.

## Migrations (`migrate.py`, `migrations/`)

Plain numbered SQL files `NNNN_lowercase_name.sql`, contiguous from 0001. Never edit
an applied file; add the next number. `apply_migrations(connection)` takes a
transaction-scoped advisory lock, creates `schema_migrations` if needed, checks the
recorded history (same names and SHA-256, no unknown or missing versions) and applies
every pending file plus its bookkeeping row in **one** transaction, so concurrent
starts apply once and a failing file leaves nothing behind. There is no downgrade.
Files are hashed and executed with LF line endings (a CRLF Windows checkout matches),
statements that would break the single transaction (BEGIN, COMMIT, ROLLBACK, END,
SAVEPOINT, RELEASE, VACUUM, CONCURRENTLY) are refused at load, and the connection must
be idle.

`0001_live_records.sql`: `calls` (one open row per call ID by a partial unique index;
IDs may be reused after an end; start/end data as JSONB; end columns all set or all
null), `window_scores` (one row per call, direction and window index, flushed every
hop), `alerts` (`open` -> `cleared` or `ended_open`). The architecture's other tables
(`call_directions`, `alert_feedback`, `users`, `audit_log`, `batch_jobs`, `settings`)
belong to later P0/P1 work and are not created yet.

## Event store (`events_pg.py`)

`PostgresEventStore(connect).record(envelope)` stores one published FR-A4 envelope
(`services.events.contract.Envelope`) in its own transaction and returns `stored`,
`duplicate` (a replay; the row exists and the call state is not moved back),
`after_end`, `unknown_alert` (clear for an alert never raised) or `not_stored`
(`system.health`). Rules:

- Any non-start event for a call ID whose latest call has ended is `after_end`,
  whatever its time (FR-I11 late results, a `no_speech`/`unmonitored` status the
  scorer can publish just after `call.ended`, replays), as in the in-memory registry.
- An ID never seen before starts implicitly (`implicit_start`); a later `call.started`
  completes it (start data kept, earlier start time kept).
- An ID may be reused only by a `call.started` strictly later than its last end (the
  registry refuses reuse until the ended call is evicted: a deliberate difference).
- Status updates without a direction change the current call state only: they are
  not kept as history (FR-A3 timeline points for them cannot be rebuilt from the
  database yet) and have no replay key, so a replayed old status can move the state.

Errors roll back the whole envelope and raise `StoreError` with `retryable`: True for
a lost or refused connection (closed, reopened on the next call; retry the same
envelope), False for an envelope the database will never accept (NUL characters, an
out-of-range time such as milliseconds passed as seconds, a value failing a CHECK);
do not retry those. The injected connection must not have a transaction open
(otherwise psycopg nests a savepoint and commits nothing): such a connection raises a
plain `ValueError` (a misconfigured factory, never a `StoreError`, so no caller
discards valid events because of it). Concurrent writers are not supported: a second
writer's racing insert of the same call is a unique violation, classified permanent.
Each envelope is its own transaction: a crash between a window's
`score.updated` and its `alert.raised` leaves the call state `alert` without the alert
row until the alert envelope is stored.

Choices to confirm in the PRD: whether call IDs can be reused, `after_end` by event
order rather than time, alert IDs unique across calls (a raise reusing another call's
ID is a `duplicate`), `system.health` and status history not persisted, one writer per
database for ordering, and what `start_data`/`end_data` may hold (they keep whatever
`call.started`/`call.ended` carry, indefinitely; caller numbers or SIP headers would
need a privacy and retention decision).

## Pump (`pump.py`, 15 Nov)

`StorePump(subscription, store).pump()` moves an unfiltered hub subscription into the
store in order: it stores what waits first and takes from the hub only what fits the
backlog (default 10,000), so the pump never drops; retryable failures (and a
misconfigured factory) keep the envelope and everything after it; permanent refusals
are counted and skipped; a head that keeps failing unexpectedly is skipped after 5 tries;
`consecutive_failures` is the stall signal; nothing is raised. During a long outage
the hub subscription's buffer is what overflows (`hub_dropped`, oldest first, any type),
and a lost lifecycle event leaves wrong database state (an open call, an implicit start,
an unknown alert): subscribe with `fanout.MAX_BUFFER`, pump often, and give `connect()`
a connect timeout. Run it on its own thread. Tests `tests/unit/test_store_pump.py` (Claude-authored; one
PostgreSQL case kills the store's connection mid-call and checks the catch-up).

Not done: scorer resume from persisted state (FR-I11: the flush
exists, resume does not; hysteresis counters and `direction_alert_id` are not stored);
cleanup of open rows whose `call.ended` was lost; retention of `window_scores`
(architecture: indefinite). Migration files with `END;` inside a function body are
refused by the transaction-control check (fail closed).

## Dependencies and tests

`requirements-store.txt`: psycopg 3.3.9 with the binary libpq package (LGPL-3.0-only,
used unmodified), typing-extensions, tzdata. Tested on PostgreSQL 16.16
(`postgres:16-alpine` image, digest
`sha256:f377d590e5474681d4d7838635031a2b50647923b7abfa803ce9b5ff35d5c663`).

`tests/unit/test_store_postgres.py` (Claude-authored, 15 Nov 2026): pure migration and
plan tests always run; database tests need `AVA_TEST_POSTGRES_DSN` pointing at a
**disposable** database (each test creates and drops its own schema) and skip
otherwise, as in CI. Local run:

```text
docker run -d --rm --name ava-store-test -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=ava_test -p 127.0.0.1:55432:5432 postgres:16-alpine
uv run --no-project --python 3.11 --env-file .local/store-test.env --with-requirements requirements-store.txt python -m unittest tests.unit.test_store_postgres
docker stop ava-store-test
```

with `.local/store-test.env` (gitignored) holding
`AVA_TEST_POSTGRES_DSN=postgresql://postgres@127.0.0.1:55432/ava_test?connect_timeout=5`
and `AVA_REQUIRE_POSTGRES=1`. Trust authentication is for this throwaway local
container only, bound to 127.0.0.1.
