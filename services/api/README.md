# Local analysis API (backlog 2.2)

`app.py` exposes the bounded analysis service (`services/inference/`) over HTTP for
the local prototype. CPU only, no runtime downloads, no authentication or
persistence yet; bind it to localhost.

| Endpoint | Behaviour |
|---|---|
| `POST /v1/analyze` (multipart `file`, PCM16 WAV) | up to 60 s: 200 with the analysis; 60 s to 10 min: 202 `{job_id, status: queued, poll}`; 411 without Content-Length; 413 over 100 MB or 10 min; 422 undecodable/unsupported, empty or missing field; 503 analyser busy, more than 2 concurrent uploads or job queue full; 500 detector failure |
| `GET /v1/jobs/{id}` | `{job_id, status}` with `queued`, `running`, `done` (+ `result`) or `failed` (+ `error.status`, `error.detail`); 404 unknown |
| `GET /healthz` | `status`, `detector_loaded`, `model_id` |
| `GET /openapi.json` | local schema; the `/docs` pages stay off (CDN assets), a recorded deviation from FR-A11 |
| `GET /v1/calls[?status=all\|active\|ended]` | FR-A3 summaries from `services/calls` (state, last score, 20-point score trend, alert counts) plus registry counters; 422 bad status; 503 without an event hub |
| `GET /v1/calls/{id}` | FR-A3 detail: alerts (`open`, `cleared`, `ended_open`) and the timeline (`full_resolution`, `downsampled`); 404 unknown |
| `GET /v1/calls/{id}/history` | FR-A3 full stored history from PostgreSQL (19 Nov): `{call_id, sessions, sessions_total, sessions_truncated}`, every session newest first (a restarted or killed scorer leaves one per session; `end_inferred` marks an end inferred after an abrupt stop) with its alerts and per-window scores in time order; at most 10,800 window rows per answer (`windows_truncated`); 404 nothing stored; 503 without `history=` or when the database is unavailable (`unavailable`), another read holds the connection for 2 s (`busy`), a read runs too long (`timeout`), the database is not migrated to 0002 (`schema`) or fails (`error`). The body is encoded in a worker thread, not on the event loop; at most 4 history requests run at once and further ones are `busy` at once (probes keep their threads); an answer that cannot be encoded is `error` |
| `GET /v1/history/calls[?status=all\|active\|ended&limit=1..200&before=]` | stored session summaries newest first (counts of alerts, open alerts, windows, scored windows, highest score; no window rows); `next_before` is the cursor for the next page, null on the last; `active` means an open row, which a lost `call.ended` can leave open indefinitely (check `last_event_ts`); 422 bad parameters; 503 as above |
| `GET /v1/alerts[?state=all\|open\|cleared\|ended_open&since=&until=&min_confidence=&limit=&cursor=&format=json\|csv]` | FR-A6 (19 Nov): stored alerts of every session, newest raised first, from PostgreSQL. `since`/`until` are Unix seconds on the raised time (since inclusive, until exclusive); `min_confidence` (0-1) is compared with each alert's `peak_ema`, the highest uncalibrated EMA it reached from raise to close (`services/store/README.md`), not a probability (alerts with no EMA never match). JSON `{alerts, next_cursor}`, limit 1-1,000 (default 100). `format=csv` downloads `ava-alerts.csv` (RFC 4180, ISO 8601 UTC times, IDs that would start a spreadsheet formula prefixed with `'`), limit 1-10,000 (default 1,000), next cursor in the `X-AVA-Next-Cursor` header (absent on the last page). Pass the cursor with the same filters. 422 bad parameters or cursor; 503 as above |
| `WS /v1/events[?call_id=]` | FR-A4 envelopes as JSON text, oldest first; a `{"type": "stream.gap", "dropped": n}` control message when a slow client's 256-envelope buffer overflowed, with `upstream_missing` (events the Redis relay counted lost) and `upstream_interruptions` (Redis outages or scorer restarts, size unknown) added only when nonzero (re-read REST); closes 1013 at the subscriber limit, 1011 without an event hub; client messages are ignored |

`GET /metrics` (FR-A8, FR-O6 subset, 14 Nov): `ava_analysis_seconds` is a histogram
(it was a summary) and `ava_analysis_stage_seconds{stage=decode|condition|inference}`
gives per-file stage times (each result also carries `stage_seconds`; inference is
detector calls only, model load excluded). Buckets: 0.01 s to 300 s. With an event
hub: `ava_calls_active`, `ava_calls_stale`, `ava_alerts_open`,
`ava_event_subscribers`, `ava_alerts_raised_total` (alerts per minute =
`rate()*60`; a re-raise of an alert ID refused by the per-call cap is counted again),
`ava_call_registry_events_total{outcome}` (mutually exclusive outcomes that sum to
the envelopes received), `ava_call_registry_lost_events_total` and
`ava_calls_idle_expired_total`. No call or alert IDs appear. With
`pipeline_stats` (a `services.scorer.pipeline.PipelineStats` the live loop publishes
to): `ava_live_windows_offered_total{outcome=accepted|stride_skipped|call_inactive}`,
`ava_live_windows_resolved_total{outcome=scored|dropped_oldest|cancelled|skipped_ended|
detector_failure|result_error|not_applied}` (accepted = resolved + waiting + in
batcher), `ava_live_batches_total`, `ava_live_detector_failures_total`,
`ava_live_unmonitored_entries_total`, `ava_live_metrics_publish_failures_total`, gauges
`ava_live_directions`, `ava_live_windows_waiting`, `ava_live_windows_in_batcher`,
`ava_live_directions_degraded`, `ava_live_directions_unmonitored`,
`ava_live_max_effective_stride_seconds`, `ava_live_pipeline_snapshot_age_seconds` (grows
when the loop stalls) and `ava_live_pipeline_snapshot_valid` (0 before the first
publish or for a malformed snapshot, which never breaks `/metrics`), and the histogram
`ava_live_inference_seconds{outcome=ok|failed}` (time of each live detector call per
batch, measured by the pipeline's monotonic `timer`; buckets 5 ms to 30 s travel with
the snapshot and are validated; untimed calls counted in
`ava_live_inference_timer_failures_total`). Since 17 Nov also the histogram
`ava_live_stage_seconds{stage=queue_wait|batch_wait|apply|window_to_result}`, with
`ava_live_stage_timer_failures_total` and `ava_live_alerts_raised_total` (defined in
`services/live/README.md`, "Per-stage latency"; `ava_live_alerts_raised_total` is what
the scoring loop published, while `ava_alerts_raised_total` above is what reached this
registry, and the two can differ). The structured JSON log is described under
"Operations log" below. Since 18 Nov the Compose profile `monitoring` scrapes this
`/metrics` and the live process's into Prometheus, with a provisioned Grafana dashboard
(`deploy/compose/README.md`, "Monitoring").

WebSocket delivery timing (FR-O6, 18 Nov), rendered with an event hub: the histogram
`ava_ws_delivery_seconds` (buckets 5 ms to 30 s, 0.2 s and 0.3 s among them) observes,
for each FR-A4 envelope sent on `WS /v1/events`, the wall-clock time from the envelope's
`ts` until its send completed, once per socket. With the Redis relay `ts` is the scorer's
hub publish time, so the span is "scorer hub publish -> API send": it includes the
scorer's Redis forwarder poll (0.2 s), Redis transit, the relay, the hub buffer and the
socket's 0.1 s poll. Those two polls alone put up to about 0.3 s on a healthy reading,
so a p95 above 0.2 s is expected by design and the metric is not a direct check of the
architecture §3 rows (it spans part of "publish" and the WS hop). "Send completed" means
handed to the server's transport: the network and the browser's render are not
included. The first lab reading (Compose, one call, 18 Nov) was a mean of 151 ms over 7
events; that is one run, not a measured p95. The two processes' clocks are
compared, which holds on one host (the Compose stack); a `ts` ahead of this clock, or
a fault in it, is counted in `ava_ws_delivery_unmeasured_total` instead. `stream.gap`
control messages and dropped envelopes are not timed. `ava_ws_slow_client_closes_total`
counts clients closed because a send did not finish within 10 s. `create_app(...,
wall_clock=)` injects the clock (tests).

The calls and events routes need `create_app(service, hub=EventHub())`: the in-process
hub the scorer publishes to. A background task drains the hub into the registry every
0.2 s and each calls request drains first, so responses include everything published
so far.

When the scorer runs in another process (`python -m services.live --redis-url-env`),
pass `relay=RedisRelay(lambda: client, hub)` with
`client = services.events.relay_loop.open_relay_client(url)` (16 Nov). While the app
runs, a `RelayLoop` thread moves events from Redis into the hub (architecture §2.5);
shutdown stops it and closes its subscription; the caller closes the client. Loss the
relay detects reaches WebSocket clients as `stream.gap` (see above). REST is not
corrected for it (no PostgreSQL recovery yet): `/v1/calls` reports
`registry.upstream_missing` and `registry.upstream_interruptions` (not exported as
registry metrics; the registry's interruptions also include source restarts and
out-of-order runs, which the relay counts as `ava_event_relay_source_restarts_total`
and `outcome="out_of_order"` rather than in `ava_event_relay_interruptions_total`, which
counts Redis outages only), so after a gap a
call may look active until its idle timeout. Reading `stream.gap`: one Redis outage
usually gives two messages (an interruption at once, then the counted
`upstream_missing` when the next event arrives), so do not add them up as separate
losses; a gap is sent at the start of the batch in which it became known, so a few
events after it may predate it (order by `seq`). Since 17 Nov each message carries the
scorer hub's random epoch (`source`), so a scorer restart is detected even when its
first events were lost (see `services/events/README.md`, "Source epoch"); two scorers
on one channel still look like constant restarts (one scorer per channel). Update this
API before the scorer: a relay from before 17 Nov refuses the new messages. `/metrics`
then adds
`ava_event_relay_messages_total{outcome=relayed|invalid|hub_errors|out_of_order}`,
`ava_event_relay_missing_events_total`, `ava_event_relay_gaps_total`,
`ava_event_relay_source_restarts_total`, `ava_event_relay_connection_errors_total`,
`ava_event_relay_interruptions_total`, `ava_event_relay_loss_report_errors_total`,
`ava_event_relay_thread_faults_total` (a valid event the hub refused is
`outcome="hub_errors"` and reaches clients as one missing event) and the
gauges `ava_event_relay_subscribed` and `ava_event_relay_running`.

## Command line (16 Nov)

```text
uv run --no-project --python 3.11 --with-requirements requirements-api.txt --with-requirements requirements-redis.txt \
    --with-requirements requirements-model.txt --extra-index-url https://download.pytorch.org/whl/cpu \
    python -m services.api --model-root D:/AVA-Data/models/antideepfake --port 8000 \
    [--dashboard dashboard/dist] [--redis-url-env AVA_REDIS_URL] [--database-url-env AVA_DATABASE_URL]
```

`--database-url-env NAME` (19 Nov; add `--with-requirements requirements-store.txt`)
names the variable holding the PostgreSQL URL the live service stores calls in, and
serves the history routes and `/v1/alerts` from `services/store/history.py`. The URL is never printed.
One probe read runs at start-up; a database that is down, not migrated or failing is
reported in the `starting` record (`history_reachable`: true, `unavailable`, `schema` or
`error`), not refused, and the routes answer 503 until it is fixed. Every `starting`
record has `call_history` (true with this option). Reads run in `READ ONLY, REPEATABLE
READ` transactions on one connection (requests take turns), with a 5 s connect timeout,
TCP keepalives and 10 s statement/lock timeouts as in the live process; the API never
applies migrations, so it can use a read-only role (recommended: `SELECT` on `calls`,
`window_scores`, `alerts`, `schema_migrations`). `start_data`/`end_data` are never served.
`/metrics` then adds
`ava_call_history_reads_total{outcome=ok|not_found|unavailable|busy|timeout|schema|error}`
and `ava_alert_search_reads_total{outcome=ok|unavailable|busy|timeout|schema|error}`
(`/v1/alerts`; it shares the one history connection, so a 10,000-row export can make
history reads `busy`).
Not yet wired into the Compose or k3s deployments: their network policies keep the API
away from PostgreSQL, and a read-only role is an operator step (backlog 10.2).

`--auth on` (19 Nov, attempt 7; PRD FR-U7; add `--with-requirements
requirements-auth.txt`) needs `--database-url-env`: accounts, sessions and the audit log
are in that database (migration 0003; `python -m services.auth migrate`, then
`add-user`, see `services/auth/README.md`). The accounts use their own read-write
connection, separate from the read-only history connection, so the role needs
`SELECT, INSERT, UPDATE` on `users`, `SELECT, INSERT, UPDATE, DELETE` on
`auth_sessions` (DELETE for the purge of old sessions; without it the purge is skipped,
logins still work) and `INSERT, SELECT` on `audit_log` (and the sequences). Every /v1 route then needs a login (401 without one;
login, logout and `GET /v1/auth/me` are added; login and `me` answer `username`, `role`,
`expires_ts`, the server's idle limit `idle_s` and its clock `now_ts`); cross-site writes and WebSocket
handshakes are refused 403; CSV exports of `/v1/alerts` are audited (`alerts.export`,
filters and row count; not sent if the row cannot be written), and so is every other
mutating request after it is answered (`api.analyze`; failures counted in
`ava_audit_write_failures_total`). At most 8 session checks/audit writes run at once
(more answer 503). Deployment notes (TLS, `Host` header, one database role) are in
`services/auth/README.md`. The `starting` record
has `auth` (always) and, with it on, `auth_reachable` (true, `unavailable`, `schema` or
`error`; a failing store answers 503, never lets a request through) and
`cookie_secure`. The cookie is Secure unless `--cookie-insecure` (plain HTTP on a lab
address; browsers accept Secure cookies from http://localhost). `/metrics` adds
`ava_auth_logins_total{outcome=ok|invalid|locked|busy|unavailable}` and
`ava_auth_refused_requests_total{reason=no_session|cross_origin|store_unavailable}`.
The dashboard shows a sign-in form when accounts are on (`dashboard/README.md`). Not yet:
admin-only settings routes (FR-A9/FR-U7 settings page), TLS (the operator's proxy).

`services/api/__main__.py` serves the app with uvicorn on `--host` (default 127.0.0.1);
a non-loopback address is refused without `--auth on` or `--allow-non-loopback`.
Start-up checks: torch is installed and the model directory holds `model.safetensors`
(the baseline is hash-verified and loaded on first use; `/readyz` runs the warm-up),
the dashboard has `index.html`, the listening socket binds (a port in use refuses;
`--port 0` takes a free port), and with `--redis-url-env NAME` the variable is set and
the URL parses. The URL is never on the command line or printed. Redis down at
start-up is reported (`redis_reachable: false`) and the relay thread keeps retrying; it
is not refused. Records go to stderr as JSON lines: `starting` (port, pid, threads,
`allow_non_loopback`) after the bind; `stopped` (stop signal, relay counters) after a
graceful shutdown on SIGINT, SIGTERM or Windows Ctrl+Break, exit 0; `refused`, exit 2;
`interrupted` for Ctrl+C during start-up, exit 130. uvicorn re-raises the stop signal
after shutting down; the command line installs flag-only handlers first so the
process is not killed before the `stopped` record. Open connections get 10 s at stop.

Tests: `tests/unit/test_api_cli.py` (Claude-authored, 16 Nov). In-process cases inject
the detector, Redis opener and server; child-process cases run
`tests/unit/api_cli_child.py` (the real `main`, fixture detector) with real uvicorn: a
stop signal gives exit 0 and a `stopped` record (Ctrl+Break on Windows, SIGTERM
elsewhere; checked to fail when the handlers are removed), a busy port exits 2 with one
`refused` record, and with real Redis a published call reaches `/v1/calls`. They need
requirements-api.txt, so they skip on the Linux CI job (it installs only
requirements-audio.txt; workflows are not edited by the daily runner). On Windows the
venv `python.exe` is a launcher: stopping a child means waiting until its server stops
answering, with the `starting` pid as a last resort.

### Operations log (17 Nov 2026; PRD FR-O6 "Structured JSON logs", §6.3)

`--event-log stderr` (the default) or `off`. `services/api/oplog.py` `ApiLog` writes one
JSON object per line to stderr, between the `starting` record and the `stopped` record
(which then carries the log's `lines`, `dropped` and `write_failures` counts). Callers
only enqueue, into a bounded queue (1,000 lines); one writer thread writes, so a slow
stderr never stalls a request. Each line has `ts` (UTC, ms), `level` and `event`, then
only these fields:

| event | fields | notes |
|---|---|---|
| `request` | `route`, `method`, `status`, `duration_ms` | Route is the fixed label (`/v1/jobs/:id`, never the ID; unknown paths `other`), method one of the standard ones or `other`. Level `warning` for 4xx and 503 (the API's deliberate load shedding or "no live source"), `error` for other 5xx. Duration runs to the response start. Probe routes (`/healthz`, `/readyz`, `/metrics`) are not logged. Rate-limited to 50 per second |
| `job_queued`, `job_done`, `job_failed` | `job_id`; `status` (HTTP code) on failure | Never verdicts, scores, file names or error text. Queued jobs failed at shutdown are logged too |
| `detector_ready` | `model_id`, `warmup_seconds` | The first successful warm-up only |
| `relay_gap`, `relay_interruption`, `relay_source_restart`, `relay_connection_error` | `count` | Changes in the Redis relay's loss counters, checked with each registry pump (0.2 s) |
| `log_dropped` | `count` | Lines lost to the rate limit, a full queue or an emit after stop. Reported at most every 0.5 s and at stop, after the lines it follows, so the log stays in `ts` order |

Query strings, upload bytes, caller data, URLs and credentials are never written. A
fault in the log never reaches a request or a job. A `job_id` is a random UUID. Anyone
who can read the log and reach the API (which has no authentication) can fetch that job
while it is held. The `stopped` record's `event_log.stopped_in_time` is false when a
blocked stderr kept the writer past its 2 s stop timeout; a line may then follow
`stopped`. The WebSocket route is not logged
per connection (its loss reports are the relay lines and `stream.gap`). Tests
(Claude-authored, 17 Nov): `tests/unit/test_api_oplog.py` (20 cases; one added 18 Nov), and the uvicorn
child case in `test_api_cli.py`. That case checks one `request` line for `/v1/calls`,
none for the `/healthz` probes, and the `stopped` record last. The cases that pin the
exact start and stop records pass `--event-log off`.

Long files are routed to a job before the analyser is touched, so they queue even
while another analysis runs. Jobs are held in a bounded in-memory store (4 queued,
64 records) served by one worker thread; they are lost on restart (durable jobs and
cancellation are backlog 5.2). Shutdown fails queued jobs and stops the worker.
Multipart parsing keeps uploads over 1 MB in a temporary file deleted at the end of
the request; the bytes then stay in memory only while queued or analysed.

Known limits (independent review, 9 Nov): a running long job holds the single
analyser, so synchronous requests get 503 until it ends, and steady sync traffic can
delay a job (it retries every 0.2 s; follow-up in 5.2/FR-I9). Peak memory is bounded
by 2 concurrent uploads and 4 queued jobs of up to 100 MB each, not by a global cap.
Client-visible errors include exception type names. FR-A1 parts still unmet: MP3,
AMR and GSM input, language hint, codec detection, the 8 kHz path, calibrated
confidence. Python 3.11 `wave` rejects WAVE_FORMAT_EXTENSIBLE PCM16 (422).

Results carry the service's labels: `calibration: uncalibrated_baseline`, the
model ID and scope text. Verdicts are advisory research output.

```text
uv run --no-project --python 3.11 --with-requirements requirements-api.txt python -m unittest tests.unit.test_analysis_api -v
```

`create_app(service, dashboard_dir="dashboard/dist")` also serves the built upload page
(backlog 2.3, `dashboard/README.md`) at `/` on the same origin, mounted after every API
route; a directory without `index.html` is refused at startup.

Not yet: MP3/AMR/GSM decode and the 8 kHz path (backlog 3.x), language hint,
durable jobs and cancellation (5.2), authentication and audit (7.3). The history routes
expose call IDs and scores without authentication, like `/v1/calls`: bind to localhost.

Tests for the history routes and option: `tests/unit/test_store_history.py`
(Claude-authored, 19 Nov; the PostgreSQL cases need `AVA_TEST_POSTGRES_DSN`, see
`services/store/README.md`); for `/v1/alerts` and its CSV export
`tests/unit/test_alert_search.py` (Claude-authored, 19 Nov).
