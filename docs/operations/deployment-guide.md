# AVA deployment guide

PRD FR-O8 (P0): "Deployment guide covering hardware, PBX tap configuration, the
recording-failure policy (FR-T4), upgrade, backup, and the security controls in §6.3."
Written 22 November 2026 (backlog 11.2). It collects what the code and the lab tests
show today and links the detailed READMEs; it does not replace them.

AVA is a research prototype. This guide describes a laboratory deployment on one
machine. Each section says what has been tested and what has not. Read "Status" first.

## Status: what is tested and what is not

Tested on this project's development machine (Windows 11, Docker Desktop 29.9.3 on
WSL 2), with the lab PBX (Asterisk 20.6.0) inside the stack:

- the Compose stack (`deploy/compose/README.md`): a real call is tapped, scored, stored
  and shown through the API, with no internet access;
- the k3s manifests (`deploy/k8s/README.md`) in k3s v1.34.2 running inside Docker: the
  same call path, with Pod Security `restricted` and the NetworkPolicies;
- calls survive AVA being stopped, hung, crashed, killed or refusing to start;
- a refused model change and its rollback;
- a PostgreSQL backup and its restore, in place and onto a new volume (this guide,
  "Backup and restore").

Not tested or not built:

- a real PBX outside the stack. The network path from a site PBX to AVA (ARI over TLS,
  a fixed RTP port range, routes) is a site decision and has not been tried;
- a Linux host (the CI runs the unit tests on Linux, not the stack);
- TLS between the dashboard and the API, and publishing the dashboard at all;
- local accounts (`--auth on`) in Compose or k3s;
- an image upgrade with a database migration, and any backup on k3s;
- the trained AVA model. Every lab run used an untrained head: scores carry no meaning.

## 1. Hardware

The PRD's reference box A1 is 8 physical cores (16 threads), 32 GB RAM, x86-64 with
AVX2 or AVX-512 (PRD §4, assumption A1). All latency and capacity targets in PRD §6.1 refer to it.
No measurement on an A1 box exists yet (backlog 8.QA).

What has been measured, on the development laptop and not on A1:

| Item | Observed | Source |
|---|---|---|
| ONNX detector, 8 kept layers, 4 s window, 4 threads, batch 1 | FP32 median 0.69 s; dynamic INT8 median 0.43 s | backlog 8.QA (14 Nov), untrained head |
| API model load and warm-up (`/readyz`) | 26.3 s with 4 threads; 22.3 s under the 2-CPU limit | `deploy/compose/README.md` |
| `ava-api` image | 1.44 GB on disk | `deploy/compose/README.md` |
| Prometheus and Grafana images | 389 MB and 1.47 GB | `deploy/compose/README.md` |

Resource limits shipped with the deployments (estimates, not measured peaks):

| Service | Compose | k3s request / limit |
|---|---|---|
| `live` (tap and scorer) | 4 ONNX threads (`AVA_THREADS`); no CPU or memory limit | 1 CPU, 1 Gi / 4 CPU, 2 Gi |
| `api` (file analysis, dashboard) | 2 CPUs, 3 GB, 2 torch threads | 0.5 CPU, 2 Gi / 2 CPU, 3 Gi |
| `postgres` | none | 0.1 CPU, 256 Mi / 1 CPU, 1 Gi; 5 Gi volume |
| `redis` | none | 0.05 CPU, 64 Mi / 0.5 CPU, 512 Mi; 1 Gi volume |
| model volume | host folders, read-only | 10 Gi static volume, read-only |

Plan for: the model files (the AntiDeepfake checkpoint is about 1.3 GB in memory), the
images, the PostgreSQL volume, up to 2 GB of Prometheus data with the monitoring
profile, and room for backups. The file-analysis limit of 25 % of inference capacity
while calls are active (PRD FR-I9) works inside one process only; between the `api`
and `live` containers only the coarse CPU limit above applies.

Host software: Docker with Compose v2, or k3s. Check a Docker host with
`python -m lab.preflight` (`lab/asterisk/README.md`). No GPU is used or needed.

## 2. PBX tap configuration

AVA listens to calls through Asterisk's REST interface (ARI). It is never in the media
path: the PBX keeps carrying the call whatever AVA does. The tap
(`services/tap/README.md`) works like this:

1. AVA's live service connects to ARI and subscribes to all channel events. Calls are
   never sent to a Stasis application, so no call waits for AVA.
2. The dialplan marks the inbound caller leg with a plain channel variable.
3. When a marked PJSIP channel is answered, AVA creates, from outside the call, a snoop
   channel that copies the caller's audio (`spy=in`) and an ExternalMedia channel that
   sends it as mu-law RTP to AVA's receiver. Both sit in a bridge named `ava-tap`.
4. When the call ends, AVA removes what it created. After a crash, a restarted AVA
   removes leftover `ava-tap` bridges and taps calls that are still running.

What the PBX needs, as configured and tested in the lab (`lab/asterisk/`):

- **Asterisk 20** (20.6.0 tested) with ARI, its HTTP server, snoop and ExternalMedia
  (the modules load by default; see `lab/asterisk/config/modules.conf`).
- **An ARI user that may make changes** (`read_only = no`); AVA creates bridges and
  channels. In the lab, `lab/asterisk/entrypoint.sh` writes `http.conf` (port 8088) and
  `ari.conf` (user `ava`) only when a password is supplied. Use a long random password.
- **Dialplan marking** of the inbound caller leg, after `Answer()`:

  ```text
  exten => 700,1,Answer()
   same => n,Set(AVA_TAP=caller)
   ...
  ```

  Use `Set`, not an inherited variable (`_AVA_TAP`), so a leg created by `Dial()` (the
  agent) is never tapped as the caller (`lab/asterisk/config/extensions.conf`). Do not
  add `Stasis()`, `AGI()` or any other step that waits for AVA.
- **PJSIP endpoints with `direct_media=no`**, so the audio passes through Asterisk where
  it can be snooped. Calls with direct media were not tested.
- **Codecs**: mu-law, A-law and GSM callers were tested end to end (Asterisk transcodes
  for the tap). Other codecs were not.
- **Network flows**: AVA to the PBX on the ARI port (TCP 8088 in the lab); the PBX to
  AVA's receiver on UDP. Each tapped call gets its own receiver port from the operating
  system's ephemeral range (on Linux 32768-60999, `net.ipv4.ip_local_port_range`). The
  PBX's own RTP range (`rtp.conf`) is separate.

AVA's side, on the `python -m services.live` command line (`services/live/README.md`):

| Option | Meaning |
|---|---|
| `--ari-url`, `--ari-user`, `--ari-password-env` | the PBX's ARI address and login; the password only from an environment variable. `https://` URLs are accepted (WebSocket over TLS) but have not been tested |
| `--ari-app` | the ARI application name (default `ava-tap`); run one tap per Asterisk and application name, because a second tap's clean-up would remove the first one's taps |
| `--receiver-host`, `--bind-host` | the address the PBX sends tapped RTP to, and the local address the receivers bind |
| `--tap-extension` | tap only calls to these dialled extensions (repeatable); default every marked call |
| `--max-calls`, `--capacity-windows-per-s` | admission limits (FR-I9) |

Known gaps: the command line has no option for an allowed RTP source address (the
receiver locks onto the first sender instead; the service accepts one in code); ARI over
TLS and a network to a PBX outside the stack are not designed or tested (Compose
README "Scope: lab only"; k3s README "Deploying").

## 3. Recording-failure policy (FR-T4)

PRD FR-T4: AVA must never be in the media path, a failure of AVA must not affect the
call, and the PBX's recording-failure policy must be **"continue the call"**.

Required PBX settings:

1. **The call never depends on AVA.** No `Stasis()`, `AGI()`, `System()` or other
   dialplan step that calls AVA or waits for it. The only AVA-specific line is
   `Set(AVA_TAP=caller)`, which succeeds whether AVA runs or not.
2. **Any recording the site adds must not end the call when it fails.** The FR-T7
   fallbacks (used only if the snoop tap does not work) differ:
   - MixMonitor does not block; it may follow `Answer()` in the call's dialplan. Do not
     check its result, so a failed or full recording leaves the call running.
   - ChanSpy blocks the channel it runs on. Run it only on a separately originated
     channel (for example a Local channel the PBX starts), never in the call's own
     dialplan, where it would take over the caller's channel.
   - A recording written to a named pipe that nobody reads has not been tested for its
     effect on the call.
   The lab uses the snoop tap; no fallback was needed or tested
   (`services/tap/README.md`, "Evidence (15 Nov 2026)").
3. **ARI failure is harmless.** If AVA's ARI connection drops, Asterisk carries on. After
   an AVA crash the call's snoop ends with the call, and the `ava-tap` bridge and its
   ExternalMedia channel stay until AVA restarts and removes them. They never hold the
   call.

What happens to AVA's view of a call when AVA fails:

| Failure | Call | AVA record | Evidence |
|---|---|---|---|
| no tap running | completes normally | nothing | `tests/lab/test_ari_tap.py` |
| tap stopped cleanly mid-call | completes, fully recorded by the PBX | `monitor_lost`; nothing left in Asterisk | `tests/lab/test_ari_tap.py` |
| tap stops reading events for 8 s | completes | the tap catches up and ends the call | `tests/lab/test_ari_tap.py` |
| tap crashed mid-call | completes, fully recorded by the PBX | bridge and ExternalMedia channel left until a restarted tap removes them | `tests/lab/test_ari_tap.py` |
| detector raising on every batch | completes; one `bye` end | windows are not scored | `tests/lab/test_detector_fault_lab.py` |
| detector blocking the service thread | completes; one `bye` end | scoring resumes once the detector returns | `tests/lab/test_detector_fault_lab.py` |
| live restarted or killed mid-call | completes | old session `monitor_lost`, call re-tapped | `tests/lab/test_compose_stack_lab.py` |
| live refusing to start (bad model change) | completes | nothing stored for that call | `tests/lab/test_compose_stack_lab.py` |
| PostgreSQL, Redis or the API restarted | completes | no stored window lost | `tests/lab/test_compose_stack_lab.py` |

A call that starts while AVA is down is not monitored. AVA is advisory: such a call is
a missed check, not a stopped call. A live service that is down serves no `/metrics`,
so watch for the outage itself: the container's health (`docker compose ... ps live`),
and with the monitoring profile the scrape target and readiness panels. AVA cannot count
the calls it missed; the PBX's call records can. (The dashboard's "unmonitored" panels
mean something else: directions skipped under CPU overload, PRD FR-I10.)

Site acceptance check: place a test call through the tapped extension, stop the live
service in the middle (`docker compose ... stop live`, or scale the k3s Deployment to
0), and confirm the call continues and ends normally on the PBX.

## 4. Install and start

- Compose: build the three images, copy `deploy/compose/stack.env.example` to a file
  outside the repository, fill it in, then `docker compose --env-file <file> -f
  deploy/compose/compose.yaml --profile lab up -d --wait` (`deploy/compose/README.md`,
  "Build and run"). Without `--profile lab` the stack expects a PBX it cannot reach.
- k3s: import the images into the node, put the model files under
  `/var/lib/ava/models`, write a site overlay outside the repository that generates
  the Secrets, then `kubectl apply -k site/` (`deploy/k8s/README.md`, "Deploying").
- After start: `live` is ready only while its ARI connection is up; `api` is ready after
  its model warm-up. A refused start (FR-O4) shows as a restarting container with one
  `refused` JSON line per attempt.
- Model files: FR-O4 start-up checks refuse a model that disagrees with its card or the
  scoring settings. Nothing is downloaded at run time.
- Local accounts (FR-U7): the API supports `--auth on` and an operator command
  `python -m services.auth` (`services/auth/README.md`), but neither deployment turns it
  on yet. Until it does, do not publish the API (section 7).

## 5. Upgrade and rollback

Two kinds of change:

- **A new model or scoring settings** (files on the model volume): follow
  `deploy/compose/README.md`, "Model change and rollback". Keep each model version in
  its own folders, keep the previous env file, change the env file and run `up -d live`.
  A bad change is refused and the call path is unaffected; rolling back is putting the
  previous env file back. Tested: `test_a_refused_model_change_never_stops_calls_and_a_rollback_restores_scoring`.
- **New images** (new code):
  1. Make the change when no calls are active: recreating `live` ends a running call's
     AVA session as `monitor_lost` (the call itself continues).
  2. Back up PostgreSQL first (section 6), and note the schema version:
     `docker compose ... exec -T postgres psql -U ava -d ava -tAc "SELECT max(version) FROM schema_migrations"`.
     There are no down migrations: a newer image may apply migrations that an older
     image refuses ("database has migration N ... that this checkout does not know",
     `services/store/migrate.py`).
  3. Keep the running images under a second name before building the new ones. The
     Compose file names its images `ava-live:dev`, `ava-api:dev` and
     `ava-lab-asterisk:dev`, so: `docker tag ava-live:dev ava-live:previous` (the same
     for `ava-api`), then build, then `up -d --wait`. On k3s, import the new images
     under a new tag and set it with a kustomize `images:` entry in the site overlay.
  4. To roll back: put the old images back under the names the deployment uses
     (`docker tag ava-live:previous ava-live:dev`, the same for `ava-api`). If the
     schema version from step 2 has changed, restore the backup taken in step 2
     (section 6) before starting; otherwise start the old images directly. Both
     `db-setup` and `live` may apply migrations, whichever starts first, so check the
     version in the database rather than one service's log.

  The images are tagged, not pinned by digest, so record which build was running. The
  image upgrade itself has not been tested; the restore step has.

## 6. Backup and restore

What to back up:

| Item | Where | Why |
|---|---|---|
| PostgreSQL database `ava` | volume `pgdata` (Compose), claim of StatefulSet `postgres` (k3s) | calls, window scores, alerts, feedback, accounts, audit log |
| site settings | the env file or site overlay, the scoring folder | needed to start the same deployment again |
| secrets | kept with the settings, separately protected | ARI, database and Grafana passwords |
| model folders | `AVA_ONNX_DIR`, `AVA_CARD_DIR`, `AVA_VAD_DIR`, `AVA_MODEL_ROOT` | hash-pinned; a different file is refused at start-up |
| retained snippets and their key | `--retention-dir` and `--retention-key-file`, if retention is on | without the key the snippets cannot be read |

Redis is not backed up: it only fans events out, and a lost event is reported to clients
as a gap. Prometheus and Grafana data are optional. Retention is off by default and is
not wired into Compose or k3s, so a default deployment holds no audio to back up.

A database dump contains call metadata (caller tokens, times, scores), password hashes
and the audit log. Store it outside the repository, with the same protection as the
database, and delete old dumps on a schedule.

**Backup (Compose; tested).** The database can stay online: `pg_dump` reads one
consistent snapshot. Write the dump inside the container, copy it out, then delete the
copy in the container (its filesystem keeps it otherwise). Do not redirect `pg_dump`'s
output in Windows PowerShell 5.1 (or PowerShell 7 before 7.4): its `>` re-encodes the
output and damages the binary dump.

```text
docker compose --env-file <file> -f deploy/compose/compose.yaml exec -T postgres pg_dump -U ava -d ava --format=custom --file=/tmp/ava.dump
docker compose --env-file <file> -f deploy/compose/compose.yaml cp postgres:/tmp/ava.dump <backup folder>/ava-<date>.dump
docker compose --env-file <file> -f deploy/compose/compose.yaml exec -T postgres rm -f /tmp/ava.dump
```

**Restore (Compose; tested).** The same steps serve a rollback (the database still has
its tables) and a lost volume (`postgres` starts with an empty database). Stop the
services that use the database, empty it, then restore. A restore over existing tables
fails, so do not skip the second step.

```text
docker compose ... stop live api
docker compose ... up -d --wait postgres
docker compose ... cp <backup folder>/ava-<date>.dump postgres:/tmp/ava.dump
docker compose ... exec -T postgres pg_restore --list /tmp/ava.dump
docker compose ... exec -T postgres psql -U ava -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE ava WITH (FORCE)" -c "CREATE DATABASE ava OWNER ava"
docker compose ... exec -T postgres pg_restore -U ava -d ava --no-owner --no-privileges --single-transaction --exit-on-error /tmp/ava.dump
docker compose ... exec -T postgres rm -f /tmp/ava.dump
docker compose ... --profile lab up -d --wait
```

The second line only matters after a lost volume, when nothing is running yet. The
`--list` line reads the dump's header and table of contents; if it fails (not a dump, a
re-encoded file, a file cut off early), stop there: the current database has not been
touched yet. It does not read the table data, so a file cut off later passes it, and
the restore then fails after the drop. For a rollback, therefore, first take a fresh
dump of the current database as a way back, and keep the checksum of every backup
(`Get-FileHash` or `sha256sum`) to compare before restoring.
`--no-privileges` is needed because the read-only login `ava_reader` is a cluster
object that a dump does not hold; `db-setup` creates or updates it and grants its rights
at the next `up`. `--single-transaction` makes a failed restore change nothing. Do not
use `down -v` to empty the database: it also deletes the Redis, lab and monitoring
volumes.

Evidence (Claude-authored, 22 Nov 2026):
`test_a_database_backup_restores_into_a_new_stack_and_scoring_resumes` in
`tests/lab/test_compose_stack_lab.py` runs these commands. A real call was stored, the
dump was taken online, and a second call was stored after it. Then:
- the in-place restore gave back every table with the backup's row count and content
  digest (the second call gone); a second restore over it failed and changed nothing;
- at the next start nothing was migrated again, and the API served the restored call
  through `ava_reader`, which still could not write or read the account tables;
- after `down -v` (a lost volume) the same steps gave the same content, `db-setup`
  created `ava_reader` again, and a new call was stored next to the restored row;
- a copy of the dump re-encoded as UTF-16 (what a PowerShell `>` does) was refused by
  the `--list` check, and the database was untouched;
- no password appeared in any log, and no dump was left in the container after the
  backup or either restore.

**k3s (not tested).** The same commands through the pod. Write the dump to the
database volume, not `/tmp`: in that pod `/tmp` is a 64 Mi memory volume
(`deploy/k8s/base/postgres.yaml`), and exceeding it evicts the database pod. For
example `kubectl -n ava exec postgres-0 -- pg_dump -U ava -d ava --format=custom
--file=/var/lib/postgresql/data/ava.dump` (on the volume, outside the `pgdata`
folder PostgreSQL uses), then `kubectl -n ava cp
postgres-0:/var/lib/postgresql/data/ava.dump <file>` and delete it in the pod. To
restore, scale `live` and `api` to 0 first, because the API's init container migrates at
every pod start, then empty the database and restore as above.

## 7. Security controls (PRD §6.3)

The first column quotes the start of each PRD §6.3 item, in the PRD's order
(`tests/unit/test_deployment_guide.py` checks this, so a new PRD item needs a row).

| PRD §6.3 control | State | How, and evidence |
|---|---|---|
| No network egress from inference, ingestion or storage services | partly met: tested in the lab; DNS names still resolve on k3s; no CI run (FR-O3) | Compose: one internal network, connection to 1.1.1.1 blocked from `live` and `api` (`tests/lab/test_compose_stack_lab.py`). k3s: default-deny NetworkPolicy plus named flows (`tests/lab/test_k3s_stack_lab.py`); cluster DNS still forwards outside names (`deploy/k8s/README.md`, "Egress") |
| TLS between dashboard and API | not met | not built. Do not publish the dashboard until a TLS proxy or Ingress and `--auth on` are in place; the proxy must forward the browser's `Host` header (`services/auth/README.md`) |
| Secrets via Kubernetes Secrets; no credentials in images | met | k3s: `ava-secrets` generated by the site overlay. Compose: an env file outside the repository. Images built from an allowlist `.dockerignore`; passwords travel in environment variables and are checked absent from logs by the lab tests. Anyone with Docker or cluster-admin access can read them |
| Audio is processed in memory and discarded by default | met | `--retention off` by default; with it on, a 30 s ring, one AES-256-GCM snippet per alert and TTL clean-up (`services/retention/README.md`). Key provisioning and rotation are an operator decision |
| Role-based access and an audit log | built, not deployed | `--auth on`: two roles, Argon2id, audit rows for logins, exports, feedback and operator changes (`services/auth/README.md`). Not turned on in Compose or k3s. Settings-change rows are not built (no settings routes yet; FR-A9 settings are a file). With it off, no audit writer runs (the PRD's `actor=system` path is not built) |
| No third-party analytics, fonts or telemetry | met | ONNX Runtime telemetry forced off (`services/inference/ort_runtime.py`); Grafana's call-home settings off; dashboard assets bundled and checked with other origins blocked (`tests/browser/dashboard-recovery.spec.cjs`) |
| Raw audio, consent forms and speaker-level manifests never enter a repository | met | `.gitignore` and `scripts/repository_guard.py` (run in CI and before each commit) |

Further site controls:

- Keep `api` and the live health endpoint off any network a browser or another tenant
  reaches. Both serve `/metrics` without a login; the API also serves `/v1/calls` and
  file analysis without one while `--auth` is off.
- Redis has no password; it relies on the internal network. Add `requirepass` if that
  network is shared.
- PBX side: a long random ARI password, ARI reachable only from AVA's address, and
  only the PBX allowed to send UDP to AVA. Each receiver binds a port chosen by the
  operating system, so the "range" is the whole ephemeral range unless the site narrows
  `net.ipv4.ip_local_port_range` for the live container (not tested).

## 8. Day-to-day checks

| Check | Where |
|---|---|
| live connected to the PBX and scoring | `live` `/readyz`; `docker compose ... ps live` (healthy) |
| refused or restarting services | `docker compose ... ps`; `refused` lines in `docker compose ... logs live` |
| unmonitored or degraded calls, packet loss, dropped windows | Grafana "AVA overview (lab)" (profile `monitoring`) or `/metrics` |
| API healthy and reading the database | `docker compose ... ps api` (healthy); `GET /v1/history/calls` answers 200 |
| backups | the date of the newest dump; a test restore after any change to this procedure |

When one of the first four checks fails, the runbooks (`docs/operations/runbooks.md`)
say what the signal means for calls, what to check and what to do. Backups are covered
by section 6.

## 9. Gaps that block a production deployment

TLS and publication of the dashboard; local accounts wired into Compose and k3s; a
designed and tested network to a site PBX (ARI over TLS, allowed RTP source); an A1
benchmark with the trained model; an image upgrade test with a migration; backup on
k3s; hashed requirement locks and SBOM generation in CI (FR-O7, human workflow edits).
The requirements audit (`docs/qa/requirements-evidence-audit.md`) tracks each one.
