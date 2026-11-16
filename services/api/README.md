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
`ava_live_inference_timer_failures_total`). FR-O6 is still partial: telephony
metrics, structured JSON logs and Grafana remain.

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
registry metrics: the relay counters carry the same numbers), so after a gap a
call may look active until its idle timeout. Reading `stream.gap`: one Redis outage
usually gives two messages (an interruption at once, then the counted
`upstream_missing` when the next event arrives), so do not add them up as separate
losses; a gap is sent at the start of the batch in which it became known, so a few
events after it may predate it (order by `seq`). Not detectable: a scorer restart whose
first events were also lost reads as an ordinary gap with too small a count, and two
scorers on one channel look like constant restarts (needs a source epoch; one scorer
per channel). `/metrics` then adds
`ava_event_relay_messages_total{outcome=relayed|invalid|hub_errors}`,
`ava_event_relay_missing_events_total`, `ava_event_relay_gaps_total`,
`ava_event_relay_source_restarts_total`, `ava_event_relay_connection_errors_total`,
`ava_event_relay_interruptions_total`, `ava_event_relay_loss_report_errors_total`,
`ava_event_relay_thread_faults_total` (a valid event the hub refused is
`outcome="hub_errors"` and reaches clients as one missing event) and the
gauges `ava_event_relay_subscribed` and `ava_event_relay_running`. There is no API
command line yet, so the relay is wired by whoever builds the app (tests today).

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
durable jobs and cancellation (5.2), authentication and audit (7.3).
