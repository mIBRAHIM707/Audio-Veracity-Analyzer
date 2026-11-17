# Live service (backlog 8.1 partial, 6.QA; PRD FR-O4, FR-I1-FR-I11, FR-T2-FR-T5, FR-A4)

`service.py` composes the live path into one service object driven by one thread.

- `load_detector(card, onnx, scoring_document, threads=, per_window=)` is the start-up
  gate (FR-O4): `models/export/model_card.check_card` (card, served file name, size,
  hash, scoring config against the card's window), an onnxruntime CPU session opened
  from the hash the card names (`onnx_export.open_session`), `OnnxBatchDetector`, and
  one warm-up window that must give finite logits. The gate also reads the graph: a
  graph with `DynamicQuantizeLinear` nodes (dynamic INT8, whose output depends on batch
  partners) is refused unless `per_window=True`; this needs the `onnx` package
  (`requirements-export.txt`) next to onnxruntime. A `calibrated` card is refused until
  the serving path applies the calibration temperature (not built; spec question). Any
  failure raises `StartupError`: the service does not start half-ready.
- `LiveService(ari, detector, config, vad_factory, receiver_host=, max_calls=, ...)`
  builds EventHub, LiveCalls (CallGate of `max_calls`; the tap admits the same number),
  MicroBatcher, LivePipeline, SpeechFeed, TapScoringSink and TapApp. `run(stop_event)`
  connects the ARI event stream, calls `resume()` (sweep leftovers, tap answered calls),
  then loops `run_once` + `pump` + optional `tick_hooks` (cheap, non-blocking work only:
  the command line runs `StorePump`/`RedisForwarder` on their own threads instead,
  see below). When the stream drops,
  every open tap ends `monitor_lost`, the sink keeps ending those calls, and the loop
  reconnects with backoff 1-30 s. `ready` is set while connected. `stop()` sets the event
  and closes the stream; on exit queued ends complete with their own reason
  (`TapScoringSink.flush_ends`, e.g. `bye`) and every other call ends `monitor_lost`.
  Connection faults count as `disconnects`, other loop faults as `loop_errors`, and
  faults while shutting down as `shutdown_errors`; none stops the remaining steps.

Integration finding (15 Nov): `LiveCalls` expects `for_new_call()` to return a
`ScoringConfig`, but the real `ConfigStore` returns a `ConfigSnapshot`; earlier tests
used a fake source. `_CallConfigs` adapts it here (each new call gets the snapshot's
config; reloads stay atomic). The PRD/architecture do not need a change; a later
refactor could make `LiveCalls` take the store directly.

## Evidence (15 Nov 2026)

Unit `tests/unit/test_live_service.py` (Claude-authored): start-up gate (card hash used
to open the session; config disagreement, session failure, non-finite warm-up, wrong
graph and a missing file all refuse to start; a dynamic-INT8 graph without per-window
serving, an uninspectable graph and a calibrated card refuse); run loop with a fake ARI
whose connection drops once (taps end `monitor_lost`, re-tapped after the reconnect,
every call ended on stop, no hook errors); a `bye` end still queued at stop is
published `bye`; a loop fault and a failing tap close are counted, not raised.
An independent review (15 Nov) found the two start-up gaps and the three loop gaps
above; all are fixed with these tests.

Lab `tests/lab/test_live_service_lab.py` (opt-in, local files only): the 14 Nov static
INT8 8-layer export with an UNTRAINED head, a card written for it in a temporary folder,
real Silero VAD, Asterisk 20.6 call to extension 700 carrying three public CSALT genuine
clips. Observed on this laptop: card, hash, graph inspection and warm-up gate 2.6 s
(1.8 s before graph inspection); 5 windows scored live (finite scores in 0-1), exactly
one `call.ended reason=bye` including after shutdown, the service loop stopped cleanly.
Command: `uv run --no-project --python 3.11 --env-file .local/lab-test.env
--with-requirements requirements-tap.txt --with-requirements requirements-export.txt
python -m unittest tests.lab.test_live_service_lab -v` (env file sets
`AVA_TEST_DOCKER_LAB=1`).
The scores are real ONNX computations with an untrained head: no detection claim.

## Command line

```text
python -m services.live --card model_card.json --onnx ava.onnx --vad-model silero_vad.onnx \
    --ari-url http://127.0.0.1:8088 --ari-user ava --receiver-host <address Asterisk can reach> \
    [--scoring-config ava.json] [--per-window] [--threads 1] [--max-calls 8] \
    [--capacity-windows-per-s 4.0] [--tap-extension 700 ...] [--ari-app ava-tap] [--bind-host 0.0.0.0] \
    [--database-url-env AVA_DATABASE_URL] [--redis-url-env AVA_REDIS_URL] [--drain-timeout 5]
```

Needs the `requirements-tap.txt` and `requirements-export.txt` environments (onnx for the
graph inspection). The ARI password is read from `AVA_ARI_PASSWORD` (or the variable
named by `--ari-password-env`); it is never accepted on the command line or in the URL
(`AriClient` refuses a URL with a user, password, path, query or fragment) and never
printed. Start-up checks, before any call is tapped: password; receiver host (IPv4
address or host name, not 0.0.0.0/multicast; IPv6 not supported), bind host (local IPv4,
one test bind) and tap extensions (1-32 of `0-9A-Za-z*#+_-`); scoring config; model
card, model and warm-up; Silero VAD hash and load; one authenticated ARI request (HTTP
401/403 refuses; an unreachable Asterisk does not, the loop keeps reconnecting and the
start record says `ari_reachable: false`). Each refusal prints one JSON line
`{"event": "refused", ...}` to stderr and exits 2; usage errors exit 2 through argparse;
Ctrl+C during start-up exits 130. A running service prints `{"event": "starting", ...}`
(model version and hash, config fingerprint, limits; readiness itself is the ARI stream)
and, after Ctrl+C/SIGTERM/Ctrl+Break, `{"event": "stopped", ...}` with the service, tap,
sink and pipeline counts, exit 0. A second Ctrl+C during a stalled shutdown forces exit
130 (`"forced": true`).

Evidence (15 Nov, Claude-authored): `tests/unit/test_live_cli.py` (12: wiring, every
refusal path without a traceback or the password, usage errors, real bind check, Ctrl+C
during start-up, first and second SIGINT, a real `python -m services.live` process);
`tests/unit/test_tap.py` URL/user refusals; lab `test_command_line_serves_a_real_call`
(`main()` with the real factories on a real Asterisk call: `starting`, at least 3 windows
scored, `call.ended bye`, `stopped`, exit 0, no disconnect, password not printed).
Independent review: conditional pass; M1 password in the URL, M2 wrong ARI credentials
started "healthy", M3 Ctrl+C during start-up and a stalled shutdown, M4 unchecked hosts,
and lows (VAD runtime errors, imports before refusal handling, extension/user/env-name
validation, start record name, subprocess working directory) all fixed with tests.

## Event storage and Redis publishing (16 Nov 2026; PRD FR-I11, FR-A3, FR-A4; architecture §2.4)

Both optional; connection strings come only from environment variables named on the
command line and are never printed (driver and client errors are reported by type
name only). `--database-url-env NAME`: start-up connects once (5 s connect timeout,
30 s lock timeout) and applies pending migrations (`services/store/migrate.py`,
idempotent, advisory-locked); an unreachable database, a refused login, a migration
lock held elsewhere (`LockNotAvailable`), a missing `psycopg` (`requirements-store.txt`)
or a migration history mismatch refuses the start, because events written into a
missing schema would be refused permanently. This refusal is a project choice, not a
PRD requirement; two decisions are open (16 Nov report): whether an unreachable
database should start the service degraded instead (as an unreachable Asterisk does),
and whether the service should apply migrations at all (it needs DDL rights) or only
check that the schema is current. Store connections use TCP keepalives
(`tcp_user_timeout` where the OS supports it; these and `connect_timeout` are defaults
only where the connection string sets none) and per-session 10 s `statement_timeout`
and `lock_timeout`, so a database that stops answering cannot hold the store thread for
the OS TCP timeout. `--redis-url-env NAME` (`requirements-redis.txt`):
`redis.Redis.from_url` with 1 s socket timeouts and no client retries; Redis down at
start-up is reported `redis_reachable: false`, not refused (pub/sub is at-most-once). A
refusal, or any unexpected start-up exit including Ctrl+C, closes what was opened.

Each runs as `StorePump` / `RedisForwarder` on its own unfiltered hub subscription
(`fanout.MAX_BUFFER` = 4096) inside a `background.PumpThread`: one owned daemon thread
per pump, pumping back to back while there is work and every 0.2 s otherwise; faults
are counted (type name only) and never reach the scoring thread, which only publishes
to the bounded hub. During a database outage events wait in the pump backlog and then
the hub buffer, whose oldest events (any type, including `call.ended`) are dropped past
4096 and counted as `hub_dropped`. The threads start just before `LiveService.run`;
after it returns every pump is asked to drain the final `call.ended` events at once,
and all are joined within one deadline of `--drain-timeout` (above 0, at most 300 s)
plus 1 s, in short
slices so a second Ctrl+C is handled at once (exit 130, `forced: true`, no drain). A
pump stuck in a client call is reported `stuck: true`, its client left open, and the
process exits anyway. An unexpected exception from the loop exits 1 with `error` (type
name) after the same stop. The `starting` record adds `database_migrations_applied` /
`redis_reachable`; `stopped` adds `store` / `redis`: the thread snapshot (`drained` is
true only when nothing is left; `left_pending` counts events still waiting), pump
counts and, for the store, `backlog` and `consecutive_failures`.

Evidence (Claude-authored, 16 Nov): `tests/unit/test_live_background.py` (10: catch-up,
drain on stop, faults counted without messages, a hung store never blocking the hub
publisher or the stop, drain deadline, zero drain, `drained` false with a backlog during
an outage, parallel drains within one deadline, odd return values, validation);
`tests/unit/test_live_cli_storage.py` (11 pure: stored and published including the end
published at stop, options off leave records unchanged, every refusal without secrets
and closing what opened, Redis unreachable, hung database, database down at stop,
Ctrl+C during the drain handled within seconds, loop exception, unexpected start-up
errors, forced exit, usage errors incl. `--drain-timeout 301`; real PostgreSQL 16:
migrations applied once, rows written, `[]` on a second start, session timeouts set with
the connection string's `search_path` kept, a held migration lock refusing within the
lock timeout; real Redis 7: events on the channel); lab
`tests/lab/test_live_storage_lab.py` (a real Asterisk call with three public CSALT clips
through the command line with both options: SIPp runs on a thread and `window_scores`
already has rows while SIPp still holds the call, every published direction window is
stored before the stop with equal scores, one `calls` row ended `bye`, the Redis channel
carries the same event sequence, no store faults or publish failures). Independent
review conditional pass (M1-M4, L1-L7); re-review conditional pass found the lab test
polled only after SIPp hung up (R1, fixed as above), a forced stop not shortening the
drain (R2), wording (R3) and keepalive defaults overriding the connection string (R4);
all fixed.
Lab command: `uv run --no-project --python 3.11 --env-file .local/lab-test.env --env-file
.local/store-test.env --env-file .local/redis-test.env --with-requirements
requirements-tap.txt --with-requirements requirements-export.txt --with-requirements
requirements-store.txt --with-requirements requirements-redis.txt python -m unittest
tests.lab.test_live_storage_lab -v`. Infrastructure evidence; untrained head.

## Health, readiness and metrics (16 Nov 2026; PRD FR-A8, FR-O6)

`--health-port N` (and `--health-host`, default 127.0.0.1) starts `health.HealthServer`,
a standard-library HTTP server on its own daemon threads (no web framework in this
process, no authentication: keep it on loopback or a private network). The port is
bound exclusively during start-up (no `SO_REUSEADDR` on Windows, where it would let a
second process share the port; a port in use refuses the start, exit 2) and closed last
at stop; the start record adds `health`. At most 8 connections are served at once
(extra ones are closed and counted) and a client that sends nothing is dropped after 5 s.

- `GET /healthz`: 200 while the process serves HTTP.
- `GET /readyz`: 200 only when the ARI event stream is connected, the scoring loop
  published its pipeline snapshot in the last 10 s (one turn can hold a detector batch
  of up to ~2 s and an ARI request of up to 5 s), and the detector has not failed 3
  batches in a row since its last success; otherwise 503 with `reason`
  `ari_disconnected`, `loop_not_started`, `loop_stalled` (e.g. a hung detector) or
  `detector_failing` (a detector that fails fast). FR-A8 says `/readyz` "performs a
  warm-up inference": the warm-up runs once in the start-up gate (`load_detector`
  refuses to start without it) and readiness then follows recent detector success,
  rather than running inference per request on the HTTP thread (any client could
  trigger it and it would compete with scoring). Proposed FR-A8 wording for the PRD:
  "`/readyz` reflects the start-up warm-up inference, the telephony stream and recent
  detector success; it does not run inference per request." A failure run clears only
  when a batch succeeds, which needs a scored call, so after a brief detector fault the
  process can stay unready while idle (conservative; a spec question). Storage or Redis being down
  does not make the process unready (advisory); it shows in the metrics.
- `GET /metrics` (Prometheus text 0.0.4): `ava_live_ready`; the live-pipeline families
  shared with the analysis API (`services/scorer/metrics_text.py`, moved there from
  `services/api/app.py` unchanged: windows offered/resolved, batches, detector failures,
  queues, stride, inference latency histogram, snapshot age/validity); ARI connects and
  disconnects; loop, hook and shutdown errors; the detector failure run; active calls,
  capacity and capacity refusals; tap and sink counters by kind; RTP receiver counts
  (`ava_live_rtp_receiver_total{kind}`: packets, malformed, gaps, missing, ...) and
  jitter-buffer counts (`ava_live_jitter_buffer_total{kind}`: played, `concealed_frames`,
  `dropped_handover`, resyncs, ...; kinds mix frames, packets and events, so do not sum
  across kinds), summed over ended calls only (a call in progress is counted
  when it ends; a call ended `monitor_lost` and re-tapped later is counted per tap); per pump
  alive/stuck/faults/moved/hub drops, store outcomes, waits (retry, misconfigured,
  faults), backlog and consecutive failures, Redis forwarded, failed and unheard; health
  requests and refused connections. No call IDs, numbers or audio; label values are
  checked identifiers. A family with an invalid value is dropped and counted
  (`ava_live_metrics_render_errors_total`); the endpoint stays up. Since 17 Nov the
  shared families also carry `ava_live_stage_seconds{stage}` and
  `ava_live_alerts_raised_total` (see "Per-stage latency" below). FR-O6 is still
  partial: the stages before `offer` (RTP arrival to frame, resampling, VAD,
  windowing) are not timed on their own, and WebSocket delivery is not timed.

### Per-stage latency and alerts per minute (17 Nov 2026; PRD FR-O6, architecture §2.3)

`LivePipeline` times each window through the stages it owns, in one histogram
`ava_live_stage_seconds{stage}` (bounds 1 ms to 30 s, including the architecture's
per-hop budgets 20 ms, 100 ms, 1.2 s and 1.6 s):

| `stage` | From → to | Clock |
|---|---|---|
| `queue_wait` | `offer` → handed to the micro-batcher | loop `now` |
| `batch_wait` | handed to the batcher → its batch taken by `step` | loop `now` |
| `apply` | `LiveCalls.result` for one window (scorer + FR-A4 events to the hub) | `timer` |
| `window_to_result` | `offer` → this window's result applied | loop `now` passed to `step`, then `timer` |

The `offer` time is the loop `now` the sink read when it fed the audio chunk that
completed the window, before VAD and windowing ran on it, so `queue_wait` and
`window_to_result` include that chunk's VAD/windowing time (part of the architecture's
"resample + VAD + windowing" row). The time spent in `schedule` and batching before the
detector call is not counted, since one `now` serves both. In `LiveService` the loop clock is
`time.monotonic`; on Windows with Python 3.11 it ticks in about 15.6 ms steps, so the
loop-clock stages there read as 0 or a multiple of ~15.6 ms (the `timer` stages use
`perf_counter`). Trust the millisecond loop-clock buckets only on a host with a fine
monotonic clock (for example Linux); the serving OS is not yet fixed in the architecture.

`apply` is the architecture's "scorer + flush + publish" row without the flush: storage
and Redis run on the pump threads, so their delay shows in the pump/store families, not
here. Windows of one batch apply in turn, so a later window's `window_to_result`
includes the earlier windows' `apply`. Only applied results get a `window_to_result`;
dropped, cancelled, skipped, detector-lost, failed and not-applied windows do not (their
outcome is in `ava_live_windows_resolved_total`). A failed or backwards `timer` reading
is counted in `ava_live_stage_timer_failures_total` and never affects scoring. A
`LiveCalls.result` that raises is not timed (it is in `result_errors`).
`ava_live_alerts_raised_total` counts `alert.raised` events the loop published (alerts
per minute = `rate(...[5m]) * 60`); an alert whose publication failed is not counted
(publish failures are in `LiveCalls.counters`). This is the FR-O6 alerts-per-minute
counter for the scoring process. The analysis API's `ava_alerts_raised_total` counts
what reached its call registry (through Redis when the scorer runs elsewhere) and can
differ. These measure this host's loop, not a product latency result (8.QA measures on
box A1).

`LiveService` now keeps its `CallGate` as `gate` and hands a `PipelineStats` snapshot to
other threads after each loop turn (`pipeline_stats`), also while reconnecting.

Evidence (Claude-authored, 16 Nov): `tests/unit/test_live_health.py` (13: readiness
transitions with a manual clock including the stale boundary and a stall; a detector
failing 3 batches in a row makes it unready and one success resets it, also through the
real pipeline with a raising ONNX session; a real loop turning keeps it ready and
stopping makes it unready; validation; metrics parse as valid exposition text, one TYPE
per family, no call ID or number; a bad counter and a malformed snapshot dropped and
counted; store-outage pump families; RTP receiver/jitter totals exported with every
kind at 0 before a call ends, summed after, unknown keys not exported and a corrupted
total dropped alone (added later on 16 Nov, with a `test_tap.py` case summing two real
ended calls); HTTP routes, 404, 405 for every other method incl.
HEAD/OPTIONS, content type; silent clients dropped after the timeout and connections
over the cap closed; four concurrent scrapers while the loop turns, none hung; the port
freed at stop;
command line: port in the start record, served while running, closed after, a busy port
refused, bad ports usage errors); the storage lab case above also reads `/readyz` (200)
and `/metrics` (`ava_live_calls_active 1`) while SIPp holds the real call.

## Detector faults on a real call (6.QA)

`tests/lab/test_detector_fault_lab.py` (Claude-authored, 15 Nov): with a detector that
raises on every batch, a lab call with three public clips completes normally (SIPp exit
0), one `call.ended reason=bye`, no scored update, every lost window counted, no
reconnect, nothing left in Asterisk. With a detector that blocks the service thread
from 5.0 s into the call until after the call ended, the call still ran its scripted
15.7 s for 13.9 s of audio and completed normally; after release the service ended the
same call `bye` without a reconnect. This test found that `stop()` closing the stream
was counted as a disconnect (fixed, unit-tested).

## Not done

A deployment configuration file (beyond the scoring JSON); FR-O6 timing of the stages
before `offer` (jitter buffer, resampling, VAD, windowing), live (in-call) RTP metrics (receiver/jitter counts are exported only
when a call ends), JSON logs and a Grafana dashboard (P1); scorer resume from stored state (FR-I11); agent direction; capacity measurement on box A1 (8.QA); file-mode jobs through the same
batcher.
