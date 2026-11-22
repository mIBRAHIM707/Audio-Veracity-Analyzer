# AVA operations runbooks

Backlog 11.2 ("runbooks"); PRD FR-O8 (operations), FR-O6 (metrics and logs), FR-A8
(health and readiness), FR-T4 (AVA failure must not affect the call). Written 22 November
2026. Each runbook starts from something an operator can see (a readiness reason, a
metric, a log line, a Grafana panel) and says what it means for calls, what to check and
what to do. It complements the deployment guide (`docs/operations/deployment-guide.md`),
which covers installation, upgrade, backup and the security controls.

`tests/unit/test_runbooks.py` checks that every metric (with its label values), log
event, readiness reason, panel, flag, guide section, path and test named here exists,
and that every live readiness reason and every warning or error log event of the live
service and the API relay has a runbook. That check says the references are real; it
does not say the procedures work. The **Tested** line of each runbook says what the
tests have shown.

## Before you start

- **AVA is advisory.** It is never in the media path. No AVA fault is a reason to change
  the PBX's call routing or dialplan, or to restart Asterisk, and no runbook below asks
  you to. Where a runbook says the PBX side needs attention, it means only what the
  deployment guide's "PBX tap configuration" section lists (the ARI user and password,
  the caller-leg marker, network reachability), changed by the PBX owner through their
  own change process. A call that AVA misses is a missed check, not a lost call.
- **Do not write caller numbers, scores, audio or passwords into incident notes.** Note
  the time, the signals seen, the actions taken and the outcome.
- `docker compose ...` below means
  `docker compose --env-file <file> -f deploy/compose/compose.yaml` with the profiles
  in use (`--profile lab`, `--profile monitoring`). On k3s use the matching `kubectl -n
  ava` command (`deploy/k8s/README.md`). Only the Compose commands have been run.
- Readiness of `live` is its `/readyz` (`services/live/health.py`); the Compose health
  check calls it, so `docker compose ... ps live` shows `healthy` or `unhealthy`. Docker
  does not restart an unhealthy container by itself. Each `/readyz` reason has a metric:
  `ava_live_ready` (0 or 1), `ava_live_ari_disconnects_total`,
  `ava_live_pipeline_snapshot_age_seconds` and `ava_live_detector_failure_run`.
- Logs: `docker compose ... logs --since 30m live` (or `api`). Operations log lines are
  JSON objects with `ts`, `level` and `event` (`services/live/README.md`,
  `services/api/README.md`). The process records `starting`, `refused` and `stopped`
  are JSON too but carry no `ts` or `level`: search for the event name, not by level.
- **Before restarting `live`, check that PostgreSQL is up** (`docker compose ... ps
  postgres`). A running live service rides out a database outage (R8), but a live
  service started while the database is unreachable refuses to start and keeps
  restarting (R4): an advisory storage outage becomes no monitoring at all.
- Restarting `live` while a call runs ends that call's AVA session as `monitor_lost`;
  the new process taps the call again. The call itself continues
  (`test_a_live_restart_mid_call_never_stops_the_call`). A live service that does not
  stop within the 20 s grace period is killed: it then sends no ends, its leftover
  bridges are removed by the next start, and the stored session gets an inferred end
  (`test_a_killed_live_process_never_stops_the_call`).

## Index

| What you see | Runbook |
|---|---|
| `live` unhealthy, `/readyz` reason `ari_disconnected` | R1 |
| `live` unhealthy, `/readyz` reasons `loop_stalled` or `loop_not_started` | R2 |
| `live` unhealthy, `/readyz` reason `detector_failing` | R3 |
| `live` restarting, `refused` lines in its log | R4 |
| calls not scored because capacity is full | R5 |
| directions degraded or unmonitored (CPU overload) | R6 |
| packet loss, low-quality windows | R7 |
| stored history falling behind | R8 |
| a background pump thread dead or stuck | R9 |
| dashboard events interrupted, Redis down | R10 |
| `api` unhealthy, refusing to start, or file analysis refused | R11 |
| scoring settings change not applied | R12 |
| errors in the tap or scoring path | R13 |
| `live` healthy but no call is tapped | R14 |
| retained snippets not written (retention on) | R15 |
| operations log lines lost | R16 |

## R1. Live service lost the PBX connection

**Signal.** `/readyz` reason `ari_disconnected`; `ava_live_ready` is 0;
`ava_live_ari_disconnects_total` rises; log `ari_disconnected`, then `ari_connected`
when it comes back; panel "Live scorer ready".

**Calls.** Not affected. Every open tap ends `monitor_lost`; calls that start while
the connection is down are not monitored. After the reconnect the live service taps
calls that are still running.

**Check.** Is Asterisk up, and is its ARI port (8088 in the lab) reachable from the live
container? Did the ARI password change? A wrong password at start-up is refused (R4);
the running service keeps retrying.

**Act.** Restore network reachability to the ARI port, or ask the PBX owner to restore
the ARI user. The live service reconnects by itself with a back-off of 1 to 30 s; no
restart is needed. If the password changed, update the env file or Secret and recreate
`live` (after checking PostgreSQL, see "Before you start").

**Tested.** `test_drop_ends_taps_reconnects_and_stop_ends_every_call` (unit, fake ARI
connection drop). A drop of the real ARI connection and an Asterisk restart under a
running live service have not been tested.

## R2. Scoring loop stalled or not started

**Signal.** `/readyz` reasons `loop_stalled` or `loop_not_started` (no pipeline snapshot
for more than 10 s, or none yet); `ava_live_pipeline_snapshot_age_seconds` keeps growing;
panel "Detector and loop health"; logs `loop_error` or `hook_error` may come first.

**Calls.** Not affected. While the loop is stuck no window is scored, so no alert can be
raised.

**Check.** The live log for `loop_error`, `hook_error` and a traceback; the panel
"Detector call time per batch" (a detector that never returns stalls the loop); host CPU
and memory.

**Act.** If the snapshot does not resume within a minute, check PostgreSQL, then restart
`live` (`docker compose ... restart live`). A hung loop cannot see the stop request, so
expect the kill after the grace period (see "Before you start"). If it stalls again
after the restart, roll back the last model or image change (deployment guide, "Upgrade
and rollback").

**Tested.** `test_a_hung_detector_never_holds_the_call` (lab: a detector blocking the
service thread; the call ends normally and scoring resumes when it returns). Restarting
a hung live service has not been tested.

## R3. Detector failing

**Signal.** `/readyz` reason `detector_failing` (3 failed batches in a row);
`ava_live_detector_failure_run` is 3 or more; `ava_live_detector_failures_total` rises;
log `detector_failing`, then `detector_recovered`; panel "Detector and loop health".

**Calls.** Not affected. Windows are not scored while the detector fails.

**Check.** The live log around the first `detector_failing` line; whether the model
files or settings changed; memory limits. The failure run only clears when a batch
succeeds, which needs a scored call: after a brief fault an idle service can stay
unready until the next call.

**Act.** If calls are active and the run keeps growing, check PostgreSQL, then restart
`live`; the start-up gate runs a warm-up inference and refuses to start if it fails (R4).
If a model change preceded it, roll back (deployment guide, "Upgrade and rollback").

**Tested.** `test_a_failing_detector_never_affects_the_call` (lab);
`test_a_detector_failing_every_batch_makes_it_unready` and
`test_detector_failure_run_is_logged_once_each_way` (unit).

## R4. Live service refuses to start

**Signal.** `docker compose ... ps live` shows it restarting; each attempt writes one
`refused` record with a `reason` and exits 2; no `starting` record.

**Calls.** Not affected, and not monitored while live is down.

**Check.** The `reason` of the newest `refused` record (`services/live/README.md`,
"Command line"). Causes the code refuses on:
- the model: the file does not match its card (hash, size, name), a file is missing, the
  warm-up fails, or the scoring window disagrees with the card; the VAD hash differs;
- the PBX: the ARI login answered 401 or 403 (an unreachable Asterisk is not refused);
- storage: the database is unreachable or refused the login, a migration fails or the
  database holds a migration this image does not know (deployment guide, "Upgrade and
  rollback"); the Redis URL is missing or invalid;
- the host: the health port is already in use; an invalid host, extension or setting;
- retention (only with `--retention on`): the key file or folder is unusable; and
  (by hand only) a `--retention-key-file` without an explicit `--retention`.

In Compose and k3s with `AVA_RETENTION=on` (with `off` the job does nothing), a broken
key file stops live one step earlier: the `retention-key` job
refuses (exit 2, one `refused` record, `services/retention/keygen.py`), so live is not
started at all: `up` reports that `retention-key` did not complete, and in k3s the pod
stays in `Init:CrashLoopBackOff` (`kubectl -n ava logs deploy/live -c retention-key`). Its reason names
the rule (size, symlink, group-readable, folder missing). Do not delete the key to get
past this while snippets exist.

**Act.** Correct the cause: bring PostgreSQL back (R8), or correct the setting, or put
the previous env file back (deployment guide, "Upgrade and rollback"), then `docker
compose ... up -d live`. Do not edit the model card to match a different file: the check
exists to refuse that.

**Tested.** `test_a_refused_model_change_never_stops_calls_and_a_rollback_restores_scoring`
(lab: three refused model and settings changes, a call completes meanwhile, the rollback
is healthy in about 7 s). The other refusals have unit tests: `tests/unit/test_live_cli.py`,
`test_storage_refusals_exit_2_without_secrets_and_close_what_was_opened` and
`test_store_sessions_are_time_bounded_and_a_held_migration_lock_refuses` (storage),
`test_health_port_is_bound_at_start_up_reported_and_freed` (health port),
`test_refusals_never_show_the_key_or_path` (retention).

## R5. Calls refused for capacity

**Signal.** `ava_live_calls_refused_capacity_total` rises; `ava_live_calls_active` equals
`ava_live_calls_capacity`; log `call_refused_capacity`; panel "Active calls and
capacity".

**Calls.** The refused calls go on normally; AVA does not score them.

**Check.** How often it happens and at which times. The limit is `--max-calls` (default
8; the Compose file does not set it).

**Act.** Accept it if rare. Raise `--max-calls` only after measuring the host: no
capacity measurement on the PRD reference box exists yet (deployment guide, "Hardware").
Changing it means editing the live command and recreating `live`, which ends every
running session as `monitor_lost`: do it when no calls are active. Raising it on a slow
host moves the problem to R6.

**Tested.** Unit tests of the call gate (`tests/unit/test_capacity.py`). Not tested in the
lab.

## R6. Directions degraded or unmonitored

**Signal.** `ava_live_directions_degraded` or `ava_live_directions_unmonitored` above 0;
`ava_live_unmonitored_entries_total` rises; log `direction_unmonitored`; panels
"Degraded and unmonitored" and "Queue depth"; `ava_live_max_effective_stride_seconds`
above the hop.

**Calls.** Not affected. The scorer is behind: it scores fewer windows (a longer
stride), and at the cap a direction is unmonitored until it catches up (30 s of
recovery).

**Check.** Host CPU: file analyses in `api` (panel "File job queue") and other
programs compete with `live`; `ava_live_inference_seconds` in the panel "Detector call
time per batch".

**Act.** Stop or postpone large file analyses and free CPU on the host. Lowering
`--max-calls` (R5, same restart cost) and keeping `AVA_THREADS` at or below the host's
physical cores are the longer-term settings.

**Tested.** `test_gauges_follow_degraded_and_unmonitored_directions` (unit). Not tested
under real load (backlog 8.QA).

## R7. Packet loss and low-quality audio

**Signal.** `ava_live_rtp_calls_degraded` above 0 (more than 5 % of the last 10 s
concealed); log `system_health` lines with a `media_state`; panel "RTP loss and
concealment per second"; `ava_live_rtp_receiver_total{kind="other_source"}` or
`ava_live_rtp_receiver_total{kind="malformed"}` rising.

**Calls.** Not affected. Windows with more than 10 % concealed audio are not scored
(`quality=low`), so a call can end with little evidence.

**Check.** The network between the PBX and the live receiver (loss, a busy link);
whether another sender is hitting the receiver port (`other_source`).

**Act.** Fix the network path. Narrow which addresses may send UDP to AVA at the
firewall (deployment guide, "Security controls (PRD §6.3)").

**Tested.** Unit tests of the jitter buffer and hostile RTP
(`tests/unit/test_tap_hostile_rtp.py`). Real network loss has not been tested.

## R8. Storage stalled

**Signal.** `ava_live_store_consecutive_failures` above 0 and
`ava_live_store_waits_total{kind="retry_waits"}` rising: these are the outage signals.
`ava_live_store_backlog` rises up to about 512 (one batch) and stays there; panel "Storage
and event publishing". `live` stays ready: storage is advisory.

**Calls.** Not affected. Scoring and alerts continue; the dashboard's history page
falls behind.

**Check.** `docker compose ... ps postgres` and its log; disk space on the database
volume. How long the outage has lasted is the risk measure: after the first batch, new
events wait in the store pump's hub buffer (4096 events, no metric), which drops its
oldest events when full. How fast it fills depends on traffic (each scored window is one event); it has not
been measured.
`ava_live_pump_hub_dropped_total` shows such a loss only after the database is back.

**Act.** Bring PostgreSQL back; do not restart `live` meanwhile (see "Before you
start"). The store pump then stores every waiting event in order and the backlog
returns to 0. If `ava_live_pump_hub_dropped_total` rose, the stored history for that
period may show calls still open or alerts it does not know; note the period in the
incident record. An event the store refuses for good shows as
`ava_live_store_events_total{outcome="refused"}`; keep the log for a bug report.

**Tested.** `test_postgres_and_redis_restarts_mid_call_lose_no_stored_window` (lab: a
short PostgreSQL and Redis restart; the stored scored windows equal the scorer's count,
backlog 0). A long outage and the hub buffer overflowing have not been tested.

## R9. Background pump dead or stuck

**Signal.** The likely case: `ava_live_pump_moved_total` stays flat for one `pump` label
while calls are active (a pump waiting inside a database or Redis call; store calls time
out after 10 s, so a long flat line means something else). Rarer: `ava_live_pump_faults_total`
rising, or `ava_live_pump_alive` 0 (the pump thread catches and counts its errors, so a
dead thread is unlikely). Panel "Storage and event publishing". After a stop, the
`stopped` record shows a pump that did not end. A stuck store pump does not raise
`ava_live_store_consecutive_failures`, so R8's signals stay quiet.

**Calls.** Not affected. What that pump fed stops: storage, Redis publishing, the
operations log, snippet writes or the settings watcher.

**Check.** Which pump (the `pump` label), and the live log around the first fault.

**Act.** Check PostgreSQL, then restart `live` at a quiet time; keep the log for a bug
report. Events not yet stored when the pump died are lost from history.

**Tested.** Unit tests of the pump thread (`tests/unit/test_live_background.py`). A pump
dying in a running service has not been tested.

## R10. Dashboard events interrupted

**Signal.** On the API: `ava_event_relay_subscribed` is 0;
`ava_event_relay_interruptions_total` or `ava_event_relay_gaps_total` rises; logs
`relay_interruption`, `relay_gap`, `relay_source_restart` or `relay_connection_error`;
panel "Event relay to the dashboard". On live: `ava_live_redis_publish_failures_total`
rises.

**Calls.** Not affected; scoring and storage go on. The live board shows the
interruption and re-reads its snapshot when events return.

**Check.** `docker compose ... ps redis`. A `relay_source_restart` alone is expected
after a live restart.

**Act.** Bring Redis back; the relay resubscribes by itself. Lost events are not
replayed: the history page reads them from PostgreSQL.

**Tested.** `test_postgres_and_redis_restarts_mid_call_lose_no_stored_window` (lab: the
API reports the outage as a gap and the call still ends `bye`).

## R11. API not ready, refusing to start, or file analysis refused

**Signal.** `api` unhealthy; its `/readyz` answers 503 with reasons `busy`, `job worker
stopped` or a model loading error (`busy` only before the first successful warm-up;
readiness then stays on); `ava_ready` or `ava_detector_loaded` is 0; a restarting `api`
writes `refused` records; log `job_failed`; panels "File job queue" and "API responses
per second"; `ava_job_queue_depth` at `ava_job_queue_capacity` (uploads get 503). The
history routes answer 503 while the database cannot be read.

**Calls.** Not affected: the API reads results; `live` taps and scores without it. The
dashboard and history are unavailable while it is down.

**Check.** The `api` log; the checkpoint folder and its hashes; the 3 GB memory limit;
`db-setup` having finished (the API reads history as `ava_reader`); whether someone is
uploading many files.

**Act.** A full queue clears as jobs finish; ask users to wait. For `job worker
stopped`, a refusal or a model error, fix the cause and `docker compose ... restart
api` (the model load and warm-up took 22 to 26 s on the development laptop; the health
check allows longer). For history 503s, bring PostgreSQL back (R8).

**Tested.** `test_an_api_restart_mid_call_never_stops_the_call` and
`test_the_api_reads_stored_history_through_the_read_only_login` (lab).

## R12. Scoring settings change not applied

**Signal.** Log `config_rejected` or `config_unreadable` from `live`;
`ava_live_config_reloads_total{outcome="rejected"}` rises; `ava_live_config_version`
unchanged.

**Calls.** Not affected. The running settings are kept; new calls keep using them.

**Check.** The `problems` field of the `config_rejected` line. Some keys (the hop, the
degraded stride) need a restart and are refused while running.

**Act.** Correct the file. For a key that needs a restart, apply it when no calls are
active and restart `live`.

**Tested.** `test_a_scoring_config_edit_on_the_host_reaches_live_without_a_restart` (lab:
an edit applied and a hop change refused).

## R13. Errors in the tap or scoring path

**Signal.** Live logs `tap_failure`, `ari_bad_event`, `sink_error`, `scoring_pump_error`,
`scoring_sink_error`, `scoring_result_error` or `shutdown_error`, each with a `count`;
`ava_live_tap_events_total{kind="tap_failures"}` or
`ava_live_sink_events_total{kind="errors"}` rising.

**Calls.** Not affected. A failed tap means that call is not scored.

**Check.** Whether it repeats on every call or once. A `tap_failure` line also names the
latest failure's step (`last_stage`: `rtp_session`, `bridge`, `snoop`, `snoop_join`,
`external_media`, `media_join`) and reason (`last_reason`: `http_<status>` from ARI,
`too_large` or `not_json` for a bad reply, or an error class such as `TimeoutError`,
`ConnectionRefusedError` or `KeyError`). With a `count` above 1 only the latest failure is
named. `snoop_join` or `media_join` with `http_422` means Asterisk had still not put
the tap's own channel into its app after five tries over 0.2 s (a loaded PBX);
`ava_live_tap_events_total{kind="join_retries"}` rising with no `tap_failure` is that
retry working and needs no action. A `tap_failure` on every call
usually means the ARI user cannot create the snoop or ExternalMedia channels (for
example it is read-only; deployment guide, "PBX tap configuration"). After a crash,
leftover `ava-tap` bridges are removed when live starts
(`ava_live_tap_events_total{kind="swept"}`).

**Act.** Ask the PBX owner to restore the ARI user's rights as the guide lists them;
keep the log for a bug report if the errors continue on a correct setup. A single
`shutdown_error` during a stop needs no action.

**Tested.** `test_call_goes_on_when_the_tap_crashes_mid_call` (lab: leftovers removed by
the next start). The individual error counters have unit tests only, as do the step and
reason fields (`test_tap_failure_names_the_later_steps_and_non_ari_errors`,
`test_a_tap_failure_line_names_the_latest_step_and_reason`).

## R14. Live healthy but no call is tapped

**Signal.** `live` healthy, the PBX carries calls, but
`ava_live_tap_events_total{kind="tapped"}` stays flat and `ava_live_calls_active` stays
0; or calls are tapped but `ava_live_rtp_receiver_total{kind="packets"}` stays flat (no
audio arrives). Everything else looks green: monitoring is silently lost.

**Calls.** Not affected; they are not monitored.

**Check.** Place a test call through a tapped extension and watch the counters. No tap:
the caller leg is not marked (`AVA_TAP=caller`), or `--tap-extension` does not include the
dialled extension. Tapped but no packets: the PBX cannot reach `--receiver-host`, or a
firewall drops the UDP. `ava_live_tap_events_total{kind="codec_unknown"}` only means the
codec could not be read; the call is still tapped.

**Act.** Correct `--tap-extension` or `--receiver-host` on AVA's side; ask the PBX owner
to restore the marker as the guide shows it; open the UDP path.

**Tested.** `test_tap_receives_the_caller_audio_and_the_call_lifecycle` and
`test_only_the_caller_leg_is_tapped_when_an_agent_answers` (lab, the working case). The
misconfigurations themselves have not been tested.

## R15. Retained snippets not written

Only when retention is turned on (`--retention on`; in Compose and k3s `AVA_RETENTION=on`,
22 Nov). It is off by default.

**Signal.** `ava_live_snippets_total{outcome="write_failed"}` rising (a failed write has
no log line of its own); `ava_live_snippets_total{outcome="queue_full"}` rising
(at most 16 snippets wait); `ava_live_snippets_queued` staying high. Log
`retention_error` means handing audio to the recorder failed on the scoring loop.

**Calls.** Not affected. Alerts are raised; the snippet for them is missing.

**Check.** Free space on the retention folder; the key file is present and readable.

**Act.** Free space or fix the key file. Do not replace the key while snippets exist:
older snippets can only be read with the key that sealed them.

**Tested.** Unit tests (`tests/unit/test_live_retention.py`),
`test_an_alert_on_a_real_call_stores_its_encrypted_caller_audio` (lab, the working
case) and `test_retention_on_stores_one_sealed_snippet_per_alert_and_off_purges_them`
(Compose lab: written, then purged when switched off). A full disk has not been tested.

## R16. Operations log lines lost

**Signal.** Log `log_dropped` with a `count` (live or API).

**Calls.** Not affected.

**Check.** On live, call-event lines exceeded the rate limit (50 lines/s by default) or
overflowed the log's event buffer; service lines (connections, errors, detector state)
are never dropped. On the API, a full log queue can drop any line.

**Act.** None needed; the metrics still count everything. Use `/metrics` or Grafana for
the period.

**Tested.** `test_rate_limit_drops_and_reports_lost_lines` (unit).
