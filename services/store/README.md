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
null, relaxed by 0002 for inferred ends), `window_scores` (one row per call, direction and window index, flushed every
hop), `alerts` (`open` -> `cleared` or `ended_open`). The architecture's other tables
(`call_directions`, `batch_jobs`, `settings`) belong to later P0/P1
work and are not created yet. `users`, `auth_sessions` and `audit_log` come from
`0003_local_auth.sql` (19 Nov, attempt 7; PRD FR-U7, §6.3) and are written by
`services/auth/store.py` (see `services/auth/README.md`).

`0004_alert_feedback.sql` (19 Nov, attempt 8; PRD FR-A7): `alert_feedback`, written by
`feedback.py` (`AlertFeedbackStore`) through the account store's connection
(`PostgresAuthStore.transaction`), one row per label with its `alerts.feedback` audit
row in the same transaction. Append-only (the newest row is the alert's current label).
Each row keeps `user_id` (set NULL if the user were deleted) and `username`, the
alert's status when labelled, and `windows`: a JSON copy of the alert's window scores
(direction, window, ts, scored, score, ema, provisional, quality, degraded,
concealed_fraction) over the span `history.PEAK_EMA` reads (its direction, or every
direction without one, from the raise to its close or, while open, to the session's
last event), oldest first, at most 3,600 (2 h of one direction), with `windows_total`
counting all. The copy is as of labelling: windows stored later are not in it. The copy
is read first; then the alert row is locked until commit (`FOR NO KEY UPDATE`, waiting
at most 2 s) and, if the alert closed in between, copied again, so the status recorded
matches the copy's end while the event store's `alert.cleared`/`call.ended` waits only
for the count, insert and audit row. At most 20 labels are kept per alert (then
`conflict`); `created_at` is `clock_timestamp()` after the lock, so it follows ID order. No embeddings or audio
features (none are stored here; with FR-O5 snippet retention on, the alert's encrypted
snippet under the same alert ID, until its TTL purge, is the audio a label pairs with).
Rows follow their alert (and call) on delete.

`0002_inferred_end.sql` (19 Nov): `end_inferred` and `start_event_at` (the applied
explicit start's time) columns; open explicit rows written before it get
`start_event_at = started_at` (the live process, the only writer, applies migrations at
start-up, so such a row belongs to a stopped process). An inferred end (see the
supersede rule below) has `end_reason = 'monitor_lost'` and no speech seconds; every
other ended row must still carry speech seconds, so code summing `speech_seconds` must
expect NULL for inferred ends. It replaces
0001's unnamed check `calls_check2` (PostgreSQL's generated name, asserted by a
PostgreSQL test) with the named checks `calls_speech_seconds_reported` and
`calls_inferred_end_shape`.

## Event store (`events_pg.py`)

`PostgresEventStore(connect).record(envelope)` stores one published FR-A4 envelope
(`services.events.contract.Envelope`) in its own transaction and returns `stored`,
`duplicate` (a replay; the row exists and the call state is not moved back),
`after_end`, `unknown_alert` (clear for an alert never raised) or `not_stored`
(`system.health`). Rules:

- Any non-start event for a call ID whose latest call has ended is `after_end`,
  whatever its time (FR-I11 late results, a `no_speech`/`unmonitored` status the
  scorer can publish just after `call.ended`, replays), as in the in-memory registry.
- An ID never seen before starts implicitly (`implicit_start`); a `call.started` whose
  time is not later than the first event seen completes it (start data kept, start
  time = the start's time). A session's start is published before its other events.
- Abrupt live stop (19 Nov, 5.QA): a killed scorer sends no `call.ended`, and the
  restarted one re-taps the call and publishes a new `call.started` from a new hub
  (`seq` back at 1, so `seq` cannot be compared). A start strictly later than the open
  session's start (`start_event_at`, or the first event of an implicit session)
  supersedes it: the old row ends `monitor_lost` at its last event (never after the
  new start) with `end_inferred` true, no speech seconds and no verdict; its open alerts
  become `ended_open` (closed at that end, or at their raise if that is later); a new
  row opens. The outcome is `stored`; `PostgresEventStore.superseded` counts these and
  the live service exports it as `ava_live_store_superseded_total` (a supersede whose
  commit lands but whose connection breaks is retried as `duplicate` and not counted:
  `calls.end_inferred` is the source of truth). A start at the same or an earlier time
  is a `duplicate` (a retried write); so, defensively, is any later start for an open
  explicit row without `start_event_at`. A backwards clock step can defeat the rule:
  a new session whose start is not later than the old start is merged into the old
  row, and its windows collide with the old window keys and are dropped as
  duplicates.
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

## History reader (`history.py`, 19 Nov; FR-A3 "full history in PostgreSQL")

`PostgresCallHistory(connect)` reads what the event store wrote, for the API's
`GET /v1/calls/{id}/history` and `GET /v1/history/calls` (`services/api/README.md`):

- `call(call_id)`: every stored session of the ID, newest first (at most 16; totals and
  `*_truncated` flags say what was left out), each with its alerts (at most 256, by
  raise time) and its `window_scores` rows in `scored_at`, direction, window order. At
  most 10,800 window rows per answer, shared across its sessions (newest session first,
  oldest rows of each session first): 6 h of one direction or 3 h of both at a 2 s hop,
  so the end of a longer call is what is left out. None if nothing is stored, or for an
  ID the store could never hold (empty, over 128 characters, NUL).
- `recent(status, limit, before)`: session summaries newest first by row ID with alert,
  open-alert, window and scored-window counts and the highest score; `next_before` is
  the keyset cursor (null on the last page). The counts are computed per page; their
  cost on many long calls is not measured.
- `alerts(state, since, until, min_confidence, limit, cursor)` (19 Nov, attempt 5; PRD
  FR-A6): alerts of every stored session, newest raised first (ties by alert ID), each
  with its `call_id` and `session`. Filters: `state` (`all`, `open`, `cleared`,
  `ended_open`), raised time `since <= t < until` in Unix seconds (exact to the
  microsecond), and `min_confidence` (0-1) compared with `peak_ema`: the highest EMA the
  alert reached, from its raise to its close (or the session's last event while open),
  over the scored windows of its direction (all directions if it has none) and its EMA
  at the raise (used only if it is from 0 to 1; that column has no range check). The EMA
  at the raise alone is at or above the alert threshold, so it barely separates alerts
  (review, 19 Nov). Uncalibrated, not a probability; an alert with no EMA at all never
  matches. An open alert's peak is provisional: it can rise while the call runs, so the
  same filter can match it later. The peak is a correlated query per returned (and, with
  `min_confidence`, per candidate) row; with only the `raised_at` index a rare filter or
  a 10,000-row export over long calls can reach the 10 s statement timeout (`timeout`).
  Cost not measured; a writer-maintained peak column would be the fix if it is slow.
  The served `raised_ema` is the stored value even when out of range.
  At most 10,000 per page. `next_cursor` is opaque (base64url of the
  exact raised time in integer microseconds and the alert ID), so paging neither repeats
  nor skips tied alerts, and alerts raised after the first page do not shift later
  pages; pass the same filters with it. A malformed cursor is a ValueError.

`status: active` means the row is open. A row whose `call.ended` was lost and whose call
is never re-tapped stays open (see "Not done" below), so check `last_event_ts`.

Each read is one `READ ONLY, REPEATABLE READ` transaction (a consistent snapshot; no
write is possible whatever the role) with `SET LOCAL statement_timeout` (10 s). It needs
migration 0002 and never migrates. One connection serves one read at a time: a read
waits at most 2 s for it, and one that passes 15 s stops before its next statement, so
an answer takes at most about 25 s. Failures raise `HistoryError` with `reason`
`unavailable` (no or lost connection; the next read reconnects), `busy`, `timeout`
(statement or lock timeout, or the 15 s deadline), `schema` (not migrated) or `error`,
and only the driver's error class in the message. `close()` waits at most 2 s for a
read in progress, which then closes the connection itself. `start_data`/`end_data` are
not read, except `start_data`'s `codec` (FR-U5, 20 Nov: a JSON string matching the
format-name rule, else null); a non-finite number (the writer refuses them) would be served as null. Status-only updates were
never stored (see above), so the window rows are the whole stored timeline; the
in-memory registry's 10 s downsampling is not applied here. Not done: paging inside one
long session's windows (the bound and `windows_truncated` stand in), search of calls by
time or state (FR-U6, P1; alerts have it, above).

## Read-only history login (`roles.py`, `__main__.py`, 20 Nov; backlog 5.1)

In a deployment the API reads history as its own PostgreSQL login, not as the owner the
live service writes with. One command sets it up and is safe to run at every start
(the Compose `db-setup` job and the k3s API init container run it):

```text
python -m services.store --database-url-env AVA_DATABASE_URL reader-role --role ava_reader --password-env AVA_DB_READER_PASSWORD
```

It connects with the owner URL (libpq reads the owner password from `PGPASSWORD`) and
applies the pending migrations. Then `ensure_history_reader` does the rest in one
transaction, under an advisory lock (two concurrent runs create the role once):
- it creates the role, or updates it if it exists, as LOGIN, NOSUPERUSER, NOCREATEDB,
  NOCREATEROLE, NOINHERIT, NOREPLICATION, NOBYPASSRLS, with a connection limit (default
  8). It resets the role's earlier settings, role-wide and for this database, and sets
  `default_transaction_read_only = on`;
- it revokes every privilege granted to the role on the database;
- in the current schema it revokes every table and sequence privilege and CREATE, and
  grants USAGE plus SELECT on `calls`, `window_scores`, `alerts` and
  `schema_migrations`, the tables `history.py` reads;
- in the current database it takes TEMP and the large-object creation functions
  (`lo_creat`, `lo_create`, `lo_from_bytea`) from `PUBLIC` (review M1, 20 Nov). Without
  this, any login could still fill the disk with temporary tables or large objects.
  This applies to every non-superuser role in the database. The AVA owner is a
  superuser and is not affected. A REVOKE by a non-owner is only a warning in
  PostgreSQL, so the job checks the outcome (`has_database_privilege`,
  `has_function_privilege`) and refuses if the reader could still do either.

The role therefore cannot read `users`, `auth_sessions`, `audit_log` or
`alert_feedback`, and cannot write to the database. The read-only default is a
convenience the session could turn off; the privileges are the boundary. The password
goes to the server as a SCRAM-SHA-256 verifier computed in Python (RFC 5802/7677), so
the plain text never reaches a log. The verifier itself is logged with
`log_statement=ddl`; treat it like a password hash. The password must be 12-1024
printable ASCII characters, because SASLprep is not applied. A new password rotates it,
and every run writes a fresh salt.

It refuses, changing nothing:
- a role name that is not a plain lowercase identifier, or starts with `pg_`;
- a password outside the rule (both rules are checked before connecting);
- the connected user itself;
- an existing superuser or replication role;
- a member of any other role (for example `pg_read_all_data`);
- a role that owns database objects, large objects included;
- an existing role holding rights the revocations above do not reach: on other schemas
  or their tables, on columns or functions, default privileges, large objects, or
  other databases. Revoke them, or choose a new role name;
- a schema without the four tables;
- a connected user without the rights to do all this (CREATEROLE, plus a superuser for
  the `PUBLIC` revocations).

One JSON record goes to stdout (`reader_role`, with `migrated`, `created` and
`tables`). A refusal exits 2. A failure exits 1: an unreachable database, or a lock wait
over 60 s or a statement over 120 s (for example behind a stalled migration). Each
gives one JSON record on stderr. The URL and the passwords are never printed.

Not covered:
- CONNECT on the cluster's other databases (`postgres`, `template1`), which `PUBLIC`
  has by default; restrict it in `pg_hba.conf`;
- the advisory-lock functions, which `PUBLIC` may execute. A compromised reader could
  hold the migration lock and delay a later start's migration. This job gives up after
  60 s, and calls are never affected;
- review L4: the role reads whole `calls` rows, including `start_data` and `end_data`,
  which the history routes never serve (only `start_data.codec`). A compromised API
  could read them; column grants or a view would close this;
- a login for `--auth on`, which writes accounts, sessions, audit rows and feedback.
  With this role, such writes would fail with "permission denied" at request time
  (503), not at start-up.

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
cleanup of open rows whose `call.ended` was lost and whose call is never re-tapped
(only a later start of the same call ID ends them); retention of `window_scores`
(architecture: indefinite). Migration files with `END;` inside a function body are
refused by the transaction-control check (fail closed).

## Dependencies and tests

`requirements-store.txt`: psycopg 3.3.9 with the binary libpq package (LGPL-3.0-only,
used unmodified), typing-extensions, tzdata. Tested on PostgreSQL 16.16
(`postgres:16-alpine` image, digest
`sha256:f377d590e5474681d4d7838635031a2b50647923b7abfa803ce9b5ff35d5c663`).

`tests/unit/test_store_history.py` (Claude-authored, 19 Nov 2026) follows the same
rules for the history reader (its API and CLI cases also need requirements-api.txt);
`tests/unit/test_alert_search.py` (Claude-authored, 19 Nov 2026) for the alert search,
its cursor and the CSV export.

`tests/unit/test_store_roles.py` (Claude-authored, 20 Nov 2026) for the reader login:
- the SCRAM verifier against the RFC 7677 worked example;
- logging in as the role, and every write and account-table read refused even after
  the session turns read-only off;
- temporary tables and large objects refused (review M1; a mutant without the `PUBLIC`
  revocations fails four cases);
- rotation, removal of hand-added grants and settings, the refusals (including rights
  elsewhere, a large-object owner and an owner without CREATEROLE), and two concurrent
  runs;
- the real history reader through the role, and the command line.
The password checks need a server with password authentication. Under `trust` they
fail with `AVA_REQUIRE_POSTGRES=1`, and skip otherwise. Run a container with
`-e POSTGRES_PASSWORD=<local value> -e POSTGRES_HOST_AUTH_METHOD=scram-sha-256` and put
that password in the gitignored DSN.

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
