# Calls read model (backlog 5.2 partial; PRD FR-A3)

`registry.py` keeps the in-memory call list behind `GET /v1/calls` and
`GET /v1/calls/{id}`. It consumes published FR-A4 envelopes (`services/events`), the
same stream the API receives, so it does not depend on scorer internals.
`RegistryFeed` drains an `EventHub` subscription into it; envelopes the subscription
dropped are counted as `lost_events`.

Document-backed: active and historical calls with state, score trend and alerts
(FR-A3); the last 30 minutes of a call at full resolution, older points downsampled
(architecture §2.5); an alert open at the end becomes `ended_open` with no clear
(§2.4).

Choices made here, not in the PRD:
- "Last 30 minutes" is measured from the call's newest point (the hub clock), not
  the wall clock, so an ended call keeps its final 30 minutes at full resolution.
- Older points go into 10 s buckets per direction, aligned to multiples of 10 s:
  point count, scored count, mean and maximum of non-null scores, most severe state
  (`alert` > `suspicious` > `unmonitored` > `monitoring` = `no_speech`), degraded if
  any point was.
- A call is created by `call.started` or by the first event of an unknown call
  (`started_seen=false`), so calls in progress appear after an API restart.
- Per call, an envelope whose `seq` is not above the last applied one is ignored as a
  duplicate or stale; events after `call.ended` are ignored. Both are counted.
- An `alert.raised` sets the call state to `alert` until the next `score.updated`.
- A clear for an unknown alert is recorded with `raised_ts: null`.
- The summary's `score_trend` is the last 20 full-resolution scores (null when a
  window was not scored).

- An active call with no event for 300 s of event time (the newest envelope `ts`
  the registry has seen) stays active but is shown with `stale: true`; it may have
  lost its `call.ended`, or the scorer may be silent during a hold. Only when the
  64-call active limit is reached is the longest-idle call ended with
  `end_reason: idle_expired` (not an FR-A4 reason; its open alerts become
  `ended_open`) to admit a new call. A later event for it resumes the call
  (`revived`). A forward jump of the hub clock can mark every call stale at once.
  Open question: whether the scorer publishes during no-speech stretches.
- A handler fault is counted in `apply_errors` and the envelope skipped; the
  registry never stops its consumer. A fault part-way through an update can leave
  that call partly updated (not observed; no rollback). `/v1/calls` also reports the API's
  `pump_errors`.

Bounds (counts, not measured bytes): 3,600 full-resolution points per call (older
ones are bucketed early), 2,880 buckets per call (oldest dropped, counted in
`dropped_buckets`), 256 alerts per call (further ones counted in `alerts_dropped`),
64 active calls (further new calls refused and counted), 128 ended calls (oldest
forgotten). Direction labels over 16 characters are stored as null. Timeline
compaction is incremental (front of a deque), so per-event work does not grow with
the number of points; a point stamped earlier than newer points (a clock step) can
stay at full resolution until those newer points leave the window.

Known limits: in memory only (PostgreSQL history is backlog 5.1), so a restart
forgets ended calls and alerts raised before it; a call spanning an event-hub
restart ignores events until `seq` passes its old value (no restart marker yet,
FR-I11); `system.health` is not stored; a reused call ID after `call.ended` is
ignored until the ended call is forgotten; `score_trend` mixes directions. The
`/v1/calls` and `/v1/events` routes have no authentication yet (7.3) and expose
call IDs: bind the API to localhost.

```text
uv run --no-project --python 3.11 --with-requirements requirements-api.txt python -m unittest tests.unit.test_call_registry tests.unit.test_api_calls_events -v
```
