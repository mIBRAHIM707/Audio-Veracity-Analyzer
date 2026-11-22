# AVA current state

## Current daily automation instruction — 6 November 2026

The user approved the autonomous software phase order and daily noon–2 PM
Asia/Karachi work. On `feat/4-daily-development`, commit verified tasks and push
to its dedicated draft PR; never merge. Historical no-commit notes below no longer
apply to that isolated worktree. Other pending edits remain in the original checkout.
See `daily-backlog.md` and `autonomous-development-plan.md` for scope, and
`../engineering/daily-development-prompt.md` for session rules. Docker is not
installed on the setup machine: report it when required and do independent work.
Setup progress/evidence is recorded in `handoffs/daily-scheduler.md` when finalized.

Updated 4 October 2026. This is shared, reviewable project memory, not a transcript.
The coordinator updates it after meaningful integration. Consult actual Git status
and latest test reports before relying on a historical result below.

Current user instruction: continue the todo list; **do not commit, push or open a
PR until Tughral supplies commit instructions**. Work remains in this checkout.
On 4 October he requested automatic progression in small steps: maintain and show
`docs/context/todo.md` after each completion, then continue without another prompt.
Earlier PR preparation created a separate `ava-pr-integration` sibling worktree
with local cherry-picks; it was not pushed and no PR was opened for this work.
That worktree does not contain subsequent conditioning work; do not treat it as
the current implementation or merge it automatically.

## Product and team

AVA is GIKI's four-person research prototype for passive CPU-only Urdu/Pakistani
Punjabi synthetic-speech detection. Tughral owns model/research infrastructure;
Ibrahim owns substantial backend, telephony and deployment work; Ilsa owns dataset
engineering; Ayesha owns data QA and dashboard code. These are working allocations,
not exclusive silos. Each person recruits/records their own participants after approval.

First-month recruitment proposal: 5–8 participants per person, 20–32 total. Protect
coding time for all four. The long-term PRD target remains 60–80 recorded speakers
and about 50,000 original clips, excluding codec copies.

## Canonical requirements and recent decisions

- Canonical docs: `_bmad-output/planning-artifacts/prd.md` and `architecture.md`.
  Root filenames are compatibility pointers, not competing sources of truth.
- Latest user-authored PR inspected: #26, “PRD and architecture v0.2 after title
  defence”, head `44bb37d024cf0e5ea9e7627cab74f2da138d7ecc`. It uses root document
  paths; this checkout moved them into BMAD output.
- Model: AntiDeepfake MMS-300M with SLS; XLS-R/SLS fallback. Mamba is superseded.
- Asterisk ARI passive tap is primary; SIPREC is stretch. AMR-NB is file/replay first.
- CPU-only inference. PRD permits separate training compute under its data rules,
  but current implementation work does not depend on GPU access.
- Tughral reported **approval obtained on 4 October**, superseding his 2 October
  pending status. Approval reference, scope and conditions are not yet recorded
  here. Follow the approved protocol and obtain individual consent for the relevant
  activity; institutional approval does not itself supply participant cloning consent.
- **5 October 2026, project-owner decision (Tughral):** public corpora whose
  licences permit derivative works may be used for voice cloning and training
  in this research under the university approval: CSALT Urdu (CC BY-NC 4.0) and
  Meta Omnilingual pnb_Arab (CC BY 4.0). Common Voice is excluded from cloning
  pending its no-reidentification terms. Participant cloning still needs the
  individual opt-in; no cloned voice is released. Recorded in PRD D11, FR-D11,
  Q1, R13, Appendix A and AGENTS.md. Existing manifests still carry the earlier
  conservative flags and are regenerated under todo 9e, not edited in place.
  Tughral also confirmed CTDISR-2025 imposes no obligation on this local
  project; the PRD already treats it as design motivation only.
- CSALT is accepted by the project owner as an established source for noncommercial
  detection research under its published licence. Retain published class/language
  labels with provenance. Automated checks apply to every file; sampled/targeted
  listening replaces any earlier per-file human approval gate for routine intake.
  Other unresolved public-source reviews and the same-speaker cloning-consent
  interaction remain open. This policy supersedes earlier blanket pending-use notes.
- Meta `pnb_Arab` source preflight is complete. One train shard was downloaded
  and hash-verified: 210 rows across all six published train speaker labels.
  The importer has 13 passing Astra tests, and on 4 October the real 12-source
  pilot was imported (todo 7b, see below). No listening or exact Majhi coverage
  established. Coordination transferred from Codex to Claude on 4 October; see
  `docs/context/handoffs/claude-resume.md` and `pnb-pilot-import.md`.
- User requests Astra for test-case design, Playwright for browser crawling/testing,
  BMAD, portable context, and simultaneous Claude Code/Codex development.

## Implemented locally

- **17 Nov, attempt 2.** The analysis API and dashboard have an image
  (`deploy/api/Dockerfile`; the dashboard is built from its lockfile; 32 exact pins
  including torch 2.8.0+cpu) and a Compose `api` service. The service is reachable only
  on the internal network, since the API has no authentication. It runs read-only,
  limited to 2 CPUs and 3 GB, with `/readyz` health on the real checkpoint. The lab
  test shows a real call reaching the API WebSocket via Redis, the dashboard assets
  served, a file analysed by the real checkpoint (uncalibrated) and egress blocked.
  The FR-I9 25 % file cap is not enforced between the two containers. Also, the live
  `/metrics` now times the tap side per delivered chunk: `resample` (8 to 16 kHz) and
  `speech_feed` (VAD, windowing, offer). It also counts the audio seconds each stage
  processed, which gives the cost per audio second. The jitter buffer and WebSocket delivery
  are still untimed. AMR-NB replay through the lab now works: a public clip is AMR
  round-tripped with the pinned ffmpeg, then played as a mu-law call. It reaches the
  tap sample-identical to the decode in all three modes. Suspected defect in the AMR
  augmentation path (issue #28): waveform SNR is about 3 dB in every mode at any input
  level. Do not report AMR-augmented results until it is resolved. The analysis API
  now writes a structured JSON operations log (FR-O6), as the live service does:
  requests by fixed route label, jobs, detector readiness and relay losses. It never
  writes verdicts, scores, file names or query strings (committed 18 Nov, 0189ffe).

- **18 Nov.** The live `/metrics` RTP receiver and jitter-buffer counters now include
  calls in progress (re-read at most every second), not only ended calls, and never
  decrease; `ava_live_rtp_calls_degraded` shows open calls in sustained loss (FR-I2).
  Details in `daily-runs/2026-11-18.md`.
- **18 Nov (attempt 2).** FR-O6 Grafana dashboard: Compose profile `monitoring` adds
  Prometheus 3.16.0 and Grafana OSS 13.0.2 (digest-pinned) on a new internal `metrics`
  network. Both `live` and `api` are scraped. A provisioned 18-panel dashboard covers
  every FR-O6 item and the degraded, unmonitored and packet-loss conditions. Grafana's
  call-home settings are off, and nothing is published on the host. `live`'s health
  endpoint moved from 127.0.0.1 to its fixed `metrics` address, which `backend`
  containers cannot reach. A CI test checks every dashboard query against the rendered
  `/metrics`. In the lab, a real call's scored windows reached Prometheus and all 39
  dashboard queries returned data. See `deploy/compose/README.md`.
  Breaking change: env files now need `AVA_GRAFANA_ADMIN_PASSWORD`. Known limits:
  - the `metrics` subnet (default 10.231.250.0/24) must not clash with a host network;
  - Grafana's admin can read call metadata through its proxy;
  - Grafana is not published on the host.
- **18 Nov (attempt 3).** FR-O6 jitter-buffer hold time: `ava_live_jitter_hold_seconds`
  on the live `/metrics` times each received RTP frame from being read off the socket to
  being handed to scoring (architecture §3 budget 60 ms), for ended calls plus calls in
  progress. It is the buffer delay plus loop-turn quantisation (roughly 10-160 ms on a
  steady stream); socket-buffer wait before a read is not measured, so it is not a
  measurement against the 60 ms budget. The dashboard's per-stage
  p95 panel shows it as `jitter_hold`.
- **18 Nov (attempts 4-5).** FR-O6 WebSocket delivery timing, the last FR-O6 latency
  item; backlog 7.2 is now complete. The API `/metrics` has `ava_ws_delivery_seconds`:
  from an event's `ts` (scorer hub publish, kept across Redis) until the API hands it to
  the WebSocket transport, once per socket. It includes the scorer's 0.2 s forwarder poll
  and the API's 0.1 s socket poll, so up to about 0.3 s is expected by design. It
  excludes the network and browser render, so it is not a measurement of the §3
  "WS to browser render" 200 ms row. Clocks of two processes are compared (valid on one
  host). Also `ava_ws_delivery_unmeasured_total` and `ava_ws_slow_client_closes_total`,
  plus two dashboard panels. Compose lab: all 7 events of a real call timed (one run,
  mean 151 ms; not a p95 or load result).
- **18 Nov (attempt 5), 5.QA.** The Compose lab now restarts the live container during
  a real call. The call itself completes (AVA failure does not stop it). The old process
  ends it `monitor_lost`, the new one re-taps the still answered channel and scores it
  to `bye`, PostgreSQL keeps two rows, and the API's WebSocket client gets a `stream.gap`
  reporting the scorer restart. Defect found and fixed: the API call registry and the
  browser live board ignored the re-tapped session and showed the call as ended while
  it was being monitored; a `call.started` later than the end now opens a new session
  (the earlier session's alerts kept). Known gap: after an abrupt kill of the live
  process no `call.ended` is sent, and the registry and store still ignore the new
  session (needs the relay's source-restart signal passed on; design step).
- **18 Nov (attempt 6), 5.QA.** A second Compose lab case restarts PostgreSQL and then
  Redis during a real call. The call completes; live keeps monitoring it (one `bye`, no
  re-tap); the store pump waits through the database outage and then stores every scored
  window (one run: 24 stored = the live counter, backlog 0); the API relay resubscribes
  and reports the Redis outage as a `stream.gap`. No defect found. A third case restarts
  the API during a call: live is unaffected and every window stored; the new API
  resubscribes within seconds and its in-memory registry picks the call up from the next
  event and ends it `bye` (the earlier part of the call is not in its memory; PostgreSQL
  has it). No defect found.
- **18 Nov (attempt 7), 5.QA M1 (partial).** Abrupt live stop, API side. The API hub
  renumbers relayed envelopes, so after a kill the new session was merged into the old
  record, with the old alerts left open. It was not ignored, as the attempt 5 note said.
  A `call.started` with a higher `seq` and a later `ts` than an active call's start now
  replaces that session in the registry and the browser board (`superseded_session`; old
  open alerts become `ended_open`). This is covered by unit and Playwright cases, not a
  lab kill. Still open: the PostgreSQL store's duplicate start after a kill, and a
  SIGKILL lab case.
- **19 Nov (attempt 1), 5.QA M1 (store and lab).** Migration 0002 adds `end_inferred`
  and `start_event_at` (backfilled for open rows) and allows a missing `speech_seconds`
  only on an inferred `monitor_lost` end. In the PostgreSQL store a `call.started`
  strictly later than the open session's start supersedes it (old row inferred
  `monitor_lost`, open alerts `ended_open`, new row), exported as
  `ava_live_store_superseded_total`; `seq` is not used (the new hub restarts it).
  Architecture §2.4 records this as interim behaviour until resume exists. Evidence:
  Claude-authored PostgreSQL 16 tests (incl. upgrading a database with rows) and the
  Compose lab case `test_a_killed_live_process_never_stops_the_call`: SIGKILL mid-call,
  live healthy again in 13 s, the call completed, registry `superseded_session` 1, rows
  `monitor_lost` (inferred, no speech seconds) and `bye`, no alert left open. Details in
  `daily-runs/2026-11-19.md`.
- **19 Nov (attempt 1), 6.QA codecs.** Callers offering only A-law or only GSM are
  tapped through Asterisk's transcoding to the mu-law ExternalMedia. The negotiated
  format is asserted over ARI, and the tapped audio matches the sent audio's decode
  (A-law 0.9999, GSM 0.999 vs 0.61 to the clean signal). Public CSALT speech from those
  callers is scored end to end. This is codec plumbing evidence with a fake detector,
  not detector accuracy on these codecs.
- **19 Nov (attempt 2), 3.1 train/serve chain parity.** `tests/unit/test_train_serve_chain_parity.py`
  (Claude-authored): a prepared clip's `augment.py` mu-law copy with seeded loss
  (`repeat_then_silence`) and the same packets sent as RTP to the real `TapApp`, jitter
  buffer, scoring sink, `SpeechFeed` and pipeline give the detector the same samples,
  window for window; the concealment flags and each window's concealed share match the
  lost packets. A second case forces 4- and 5-packet bursts (repeat three, then
  silence). Not covered: late packets, first/last-packet loss (a known divergence),
  A-law (+8 silence), GSM/AMR, real VAD. Infrastructure evidence only.
- **19 Nov (attempt 3), 10.2 k3s manifests (PRD FR-O2/O3/O4, P0).** `deploy/k8s/base`
  (namespace `ava` with Pod Security restricted, read-only model PV/PVC, PostgreSQL and
  Redis StatefulSets with claims, live and api Deployments with probes, limits, init
  containers waiting for storage, default-deny NetworkPolicy plus named flows) and the
  `lab` overlay (Asterisk in `ava-lab-pbx`). Claude-authored static tests (PyYAML from the
  new `requirements-test.txt`; skipped in CI until a human adds that file to the
  workflow) and a lab case on a digest-pinned k3s v1.34.2 node in Docker: a real call
  tapped across namespaces, stored and shown in the API; no AVA pod connects to the
  internet. Open: CoreDNS still forwards outside names (FR-O3 DNS part), Ingress/TLS
  (auth decision), an external PBX's route to the pod network, monitoring on k3s.
- **19 Nov (attempt 4), 5.1/5.2 FR-A3 history from PostgreSQL (P0).**
  `services/store/history.py` reads stored calls in read-only REPEATABLE READ
  transactions (sessions newest first with alerts and window scores; bounded sessions,
  alerts and a shared window budget; busy/timeout/schema/unavailable failures); the API
  serves `GET /v1/calls/{id}/history` and `GET /v1/history/calls` (keyset paging) with
  `--database-url-env`, and `ava_call_history_reads_total`. Claude-authored tests (23,
  PostgreSQL cases on a local 16 container; rows written by the real event store incl. a
  superseded session). Not yet wired into Compose/k3s (their policies keep the API off
  PostgreSQL; a read-only role is an operator step); the dashboard does not use it yet.
- **19 Nov (attempt 5), 7.3 FR-A6 alert search and CSV export (P1).** `GET /v1/alerts`
  (state, raised-time range, minimum confidence, JSON or `format=csv`) from
  `PostgresCallHistory.alerts`; "confidence" is implemented as the alert's peak EMA from
  raise to close (uncalibrated, provisional while open) because the EMA at the raise
  sits at the threshold; this interpretation needs PRD wording. Exact keyset cursor;
  CSV with formula neutralisation; `ava_alert_search_reads_total`. Claude-authored tests
  (23, PostgreSQL cases on a local 16 container). Not measured: the peak query's cost on
  large stores. Same deployment gap as the history routes.
- **19 Nov (attempt 6), 7.3 FR-U6 dashboard history and search (P1).** `#history` in the
  dashboard (`dashboard/src/history.ts`, `HistoryPage.tsx`): stored sessions with
  status filter and paging, alert search (state, UTC time range, minimum confidence =
  peak EMA) with paging and CSV export, and the stored detail of a call (every session,
  inferred `monitor_lost` ends, alerts, window timeline). Read only; validated answers;
  store failures by reason; 1,000-row list cap. Claude-authored tests: 10 pure (CI) and
  13 Playwright, 7 of them on the real API reading a PostgreSQL 16 database written by
  the real event store (skipped without `AVA_TEST_POSTGRES_DSN`). Times are UTC only.
  Still open in 7.3: FR-A7 feedback, FR-U5 call detail with codec/snippets, FR-U7 login
  and audit.
- **19 Nov (attempt 7), 7.3 FR-U7 local accounts and §6.3 audit log (P1), API side.**
  Migration 0003 (`users`, `auth_sessions`, `audit_log`); `services/auth/` (Argon2id
  t=3/64 MiB/p=4 via argon2-cffi 25.1.0, `requirements-auth.txt`; store; login decisions
  with equal work for unknown names, 5-failure/15-min lockout per name, at most 2 hashes
  at once; operator CLI with no default account). API `--auth on`: login/logout/me, an
  HttpOnly SameSite=Strict Secure session cookie (token SHA-256 stored; 30 min idle,
  12 h absolute), 401 on every other /v1 route, cross-site writes and WebSocket
  handshakes refused, open WebSockets closed when the session ends, audit rows for
  logins, logouts, CSV exports, uploads and account changes. Claude-authored tests
  (PostgreSQL 16 cases included); independent review conditional pass, findings fixed.
  Then the dashboard sign-in (`AuthGate.tsx`, `auth.ts`): pages exist only while signed
  in; sign out; an ended session returns to the form; without accounts nothing changes.
  Claude-authored Playwright cases on the real API and accounts in PostgreSQL 16.
  Not yet: admin settings (FR-A9), feedback (FR-A7), Compose/k3s wiring, TLS (operator
  proxy), audit retention (decision). Analysts may upload files (PRD says "read,
  feedback"; decision to record).
- **19 Nov (attempt 8), 7.3 FR-A7 alert feedback (P1), API and storage.** Migration 0004
  `alert_feedback`; `services/store/feedback.py`: a `true_positive`/`false_positive`
  label, a JSON copy of the alert's window scores (the FR-A6 peak span, up to 3,600
  windows, `windows_total`) and the `alerts.feedback` audit row in one transaction on the
  account connection (`PostgresAuthStore.transaction`), alert row locked, labels
  appended (newest current, at most 20 per alert). `POST /v1/alerts/{id}/feedback` (201,
  404, 409, 422, 503) and `GET` (labels newest first), only with `--auth on`; metric
  `ava_alert_feedback_writes_total`. Claude-authored tests incl. PostgreSQL 16 (a clear
  waits for a label being written; rollback when the audit row fails); review
  conditional pass, findings fixed. Embeddings are not stored (no audio features in
  PostgreSQL); FR-O5 retained snippets share the alert ID. Not yet: dashboard feedback
  buttons, write time at the window cap measured, PRD wording for "embeddings".
- **19 Nov (attempt 9), 7.3 FR-A7 dashboard feedback (PRD plan 3.25 UI).**
  `dashboard/src/AlertFeedback.tsx`, `feedback.ts`, `session.ts` (`SessionContext` from
  `AuthGate`): in the `#history` call detail each alert has a collapsed "Label this alert"
  panel, shown only to a signed-in user; it reads the labels when opened and sends one
  POST per click (synchronous ref guard, never retried; an unanswered write says its
  outcome is unknown); 20-label cap shown. Claude-authored FS01-FS05 (pure, CI) and
  FB01-FB04 on the real API with production accounts and `AlertFeedbackStore` in
  PostgreSQL 16 (stored window copy and audit rows checked), FN01-FN03 [mock]. UI
  evidence only; no detector or feedback-quality claim.
- **19 Nov (attempt 10), 7.3 FR-U5 retained-snippet playback.**
  `services/retention/playback.py` `SnippetReader` decrypts and authenticates one
  alert's FR-O5 snippet into an in-memory WAV (reasons missing/expired/unreadable/error;
  TTL compared as cleanup does). API `GET /v1/alerts/{id}/snippet`, only with `--auth on
  --retention-dir --retention-key-file`: the `alerts.snippet` audit row is written
  before any audio is sent (no row, no audio), 404/410/500/503 with fixed texts, at most
  2 at once including the audit wait, `no-store`, `ava_snippet_reads_total`. Dashboard
  `AlertSnippet.tsx`: fetched only on request, WAV header checked, blob URL revoked on
  close/leave; the CSP gained `media-src 'self' blob:` (SB01 found the browser refused
  the audio without it). Claude-authored: 24 unit cases (production AES-GCM round trip,
  tampering, TTL boundary, path-like IDs, audit-before-send, cap), SS01-SS04 pure,
  SB01-SB04 on PostgreSQL 16 with the production store and reader (the browser decodes
  2.5 s), SN01-SN02 [mock]. Review conditional pass; M1 (cap released before the audit
  wait) fixed with a test whose mutant fails. Known limits in `services/api/README.md`
  (retention switch-off not seen by the API until cleanup; refused playbacks not
  audited). Not yet: codec detected (rest of FR-U5; done 20 Nov, below), Compose/k3s wiring.
- **20 Nov (attempt 1), 7.3 FR-A9 config file plus reload.**
  - **What:** `services/live/config_reload.py` with
    `python -m services.live --scoring-config FILE --scoring-config-watch-s N`. The
    file is reread on a pump thread and compared by SHA-256.
  - **Validation:** start-up rules plus the card window.
  - **Applied:** valid changes go through `ConfigStore.reload` to calls started
    afterwards (FR-I4). Running calls keep their snapshot.
  - **Refused:**
    - `hop_s` and the degraded-stride keys need a restart;
    - `score_directions` without `caller` is refused, since only the caller is tapped.
  - **Reporting:** applied/unchanged/rejected/unreadable go to the operations log and
    to `ava_live_config_reloads_total` / `ava_live_config_version`. No path is printed.
  - **Tests and review:** 21 Claude-authored tests on a real `LiveCalls`; review
    conditional pass, M1 fixed.
  - **Found:** live windowing ignores `hop_s` (constant 2 s), a start-up gap recorded
    for the PRD.
  - **Open:** audit-log rows for reloads, config version on `call.started`, the
    optional API endpoint. (Compose/k3s mounts done in attempt 3.)
- **20 Nov (attempt 2), 7.3 FR-U5 codec detected.**
  - **What:** the tap reads `CHANNEL(audionativeformat)` over ARI once a call's tap is
    set up (1 s bound; failures give None and count `codec_unknown` on the live
    `/metrics`; the tap and the call are never affected). `call.started` carries
    `codec`; the history reader serves `codec` from `start_data` (JSON string matching
    the format-name rule only, nothing else); the dashboard call detail shows "Codec
    detected" per session.
  - **Evidence:** Claude-authored `tests/unit/test_call_codec.py`, a PostgreSQL 16 case
    in `test_store_history.py`, Playwright HS11 and HB08 (real PostgreSQL), and lab cases
    on Asterisk 20.6: mu-law, A-law and GSM callers detected as `ulaw`/`alaw`/`gsm`; a
    caller offering mu-law and A-law is reported as `(ulaw)`, the codec in use. Unit suite
    1418 OK (219 skipped). Review conditional pass; M1 probed in the lab, L1-L4 fixed.
  - **Limits:** read once at the start; a re-INVITE codec change is not tracked; calls
    stored before 20 Nov show "not detected".
- **20 Nov (attempt 3), 7.3 FR-A9 Compose/k3s config mounts.**
  - **What:** both deployments start live with `--scoring-config` and
    `--scoring-config-watch-s 10`. The shipped `scoring.json` (Compose
    `deploy/compose/config/`, k3s `deploy/k8s/base/`) holds the §7.3 defaults, so no
    setting changes.
    - Compose: a read-only folder bind mount (`AVA_SCORING_CONFIG_DIR`). It is a folder
      because a single-file mount misses a rename.
    - k3s: the generated ConfigMap `ava-scoring`, with no hash suffix (an edit would
      otherwise roll the pod and end its calls) and no subPath (never updated).
  - **Evidence:** Claude-authored `tests/unit/test_deploy_scoring_config.py` (the defaults
    under the real start-up rules, the Compose command through the real parser, mount
    shapes).
    - Compose lab case: a host-side rename was applied (version 1 to 2), a hop edit
      refused, removal reported, the restored file unchanged, with no restart.
    - Full Compose lab case: passes with the default mount.
    - k3s lab: a ConfigMap patch was applied by live 10 s later (version 1 to 2), with
      no restart (rerun OK in 229 s; the first run hit a host stall, not an assertion).
    - Review: conditional pass. M1/M2 (k3s lab bugs) and L1-L4 docs fixed.
  - **Limits:** a missing file at start-up refuses start (as before for an invalid
    one); the k3s file update waits for the kubelet (often 60-90 s).
- **20 Nov (attempt 4), 5.1 Compose/k3s wiring of history with a read-only role.**
  - **What:** the API now serves the stored history and alert search in both
    deployments, logged in as `ava_reader`, never as the owner.
    `python -m services.store reader-role` (`services/store/roles.py`) migrates, then
    creates or updates the role. It gets SELECT on `calls`, `window_scores`, `alerts`
    and `schema_migrations` only, a read-only default, earlier settings and rights
    removed, and a client-side SCRAM verifier. In the database, PUBLIC loses TEMP and
    large-object creation, and the outcome is checked.
    - It refuses unsafe existing roles (superuser, member, owner, rights elsewhere) and
      an owner that cannot do all this.
    - Compose runs it as a one-shot `db-setup`, which `api` waits for. k3s runs it as
      the API pod's `db-reader-role` init container, with Secret key
      `db-reader-password` and api to postgres 5432 in the NetworkPolicy. The API image
      gains psycopg (35 packages). Live depends on none of it.
  - **Evidence:**
    - Claude-authored `test_store_roles.py`: 21 cases on PostgreSQL 16 with SCRAM,
      including the RFC 7677 verifier example. Mutants were caught: no lock, no table
      revoke, no PUBLIC revoke, no outcome check.
    - Static Compose and k8s checks.
    - Compose lab: a new history case (writes, account tables, temp tables and large
      objects denied; rotation), and the full stack with the call in history.
    - k3s lab: OK in 283 s, history served in-cluster.
    - Unit suite: 1449 OK (227 skipped), before the review fixes.
  - **Review:** M1 and M2 and L1-L6 were fixed. The re-review raised a Medium (a
    non-superuser revoke is only a warning); it was fixed with an outcome check and a
    test. That last fix was not re-reviewed.
  - **Limits:** CONNECT on other databases (use pg_hba), whole `calls` rows readable
    (including start/end data), advisory-lock delay, and `--auth on` still needs a
    writer login.
- **20 Nov (attempt 5), 9.2 AASIST baseline (PRD FR-M4).**
  - **What:** `models/baselines/aasist.py` is AASIST written against the reference
    implementation, clovaai/aasist (MIT) at commit a04c986. It is cloned only outside the
    repository (`D:/AVA-Data/vendor/aasist`); the module carries the upstream notice.
    It runs on AVA's window contract and the shared harness, in two configurations:
    `REFERENCE_CONFIG` (297,866 parameters) and AASIST-L (85,306). Upstream parameter
    names load strictly (`load_reference_state_dict`). The reference quirks are kept,
    and `REFERENCE_TRAINING` records what the shared harness does not do (cosine
    schedule, SWA, coupled L2).
  - **Evidence:** Claude-authored `tests/model/test_aasist.py`, 20 cases. Six
    compare against the clone at the pinned commit when `AVA_AASIST_UPSTREAM` is set.
    They skip otherwise, or fail with `AVA_REQUIRE_UPSTREAM=1`; `tests/model` is not
    run in CI. Parity was verified on 20 Nov:
    - the sinc taps are bit-identical;
    - eval logits agree within 1e-6, and training-mode logits and every BatchNorm
      statistic agree with the same seed;
    - the published weights load and score identically.
    Two mutants were caught. The RawNet2 port in the same clone agrees with both
    RawNet2 assumptions (the asvspoof-challenge original is still unchecked).
  - **Review:** independent, conditional pass, no High or Medium. The Lows were fixed:
    `temperatures[3]` unused (documented and tested), the commit pinned and checked,
    the AdamW decay strength stated, strict mode, the weights cases split, and a
    relative tolerance on the statistics.
  - **Limits:** not trained and no accuracy claimed. The published weights are a
    parity fixture only. XLS-R + AASIST still needs XLS-R weights.
- **20 Nov (attempt 6), 6.1 authenticated softphone endpoint (PRD FR-T1).**
  - **What:** the lab image gets an optional `softphone` PJSIP endpoint (digest auth,
    one registered contact, ulaw/alaw/gsm) only when the run passes
    `AVA_SOFTPHONE_PASSWORD`; extension 702 dials it as a tapped caller's destination.
    Endpoint identification order pinned; the credential umask is no longer inherited
    by Asterisk. Details in `lab/asterisk/README.md`.
  - **Evidence:** Claude-authored `tests/lab/test_softphone_lab.py`, 10 OK (Docker,
    `--network none`): registration needs the password, foreign-From REGISTER and
    unauthenticated INVITE refused/challenged, authenticated call and calls to the
    registered phone sample-identical, CHANUNAVAIL without a contact, weak passwords
    stop the container. Existing lab suites 19 OK on the new image.
  - **Review:** conditional pass, nine findings fixed (one real SIPp ACK-branch defect),
    re-review pass.
  - **Limits:** SIPp plays the softphone; no real softphone application was tried. The
    password protects the softphone's registration only (`anonymous` stays open).
  - **Blocker found:** this session cannot access `D:/AVA-Data` (worktree-only file
    access), so the 3.4 corpora and XLS-R weights stay unprovisioned until a human adds
    that directory to the runner's allowed directories.
- **21 Nov (attempt 1), 10.2 image SBOM (PRD FR-O7, partial).**
  - **What:** `scripts/image_sbom.py` uses only the standard library. It runs inside a
    built image with the checkout mounted read-only and writes CycloneDX 1.5 JSON.
  - **Recorded:** the image ID; the base image digests that the Dockerfile names; every
    installed Python distribution, with its licence, whether it is pinned, and a RECORD
    fingerprint; the interpreter; every dpkg-installed Debian package; and, for `api`,
    the dashboard's npm packages with their lockfile SHA-512. Output is deterministic.
  - **Pin check:** `--check-pins` exits 1 when the installed Python set differs from the
    image's requirement file. The only exemptions are pip, setuptools and wheel, which
    come from the base image.
  - **Shared reader:** `read_pins` moved out of `tests/unit/test_deploy_images.py` into
    the script, so the tests and the pin check read the image files the same way.
  - **Evidence:** Claude-authored `tests/unit/test_image_sbom.py`, which runs in CI. Its
    CycloneDX 1.5 schema case uses `cyclonedx-python-lib` and has run only on this host.
    `tests/lab/test_image_sbom_lab.py`, in both rebuilt images:
    - the pins match;
    - the Python list equals `pip list`, and the Debian list equals `dpkg-query`;
    - the exempt tools have the base image's versions;
    - two runs give identical bytes;
    - a mismatched requirement file exits 1.

    `live` has 109 components and `api` 242.
  - **Not done (human):** the CI job that builds the images and runs the script; the
    hashed locks, since `uv pip compile` is refused by the allowlist; wheel hashes; a
    dependency graph; Debian licences.
- **21 Nov (attempt 2), 7.QA recovery and offline-asset browser cases (partial).**
  - **What:** Claude-authored `tests/browser/dashboard-recovery.spec.cjs`. No route sits
    between page and server. RC01 kills the real API under an open live board and
    restarts it on the same port. The page says it is disconnected and keeps the last
    state, retries while the port is dead, then reconnects by itself and takes the new
    API's snapshot. RC02 does the same for the history page on PostgreSQL 16. RC03 checks
    offline assets: a scan of the built files, the served CSP (`'self'`/`'none'`/`data:`/
    `blob:` only), and all three pages loading with every other origin aborted.
  - **Defect found and fixed:** after an API restart, the new registry recreates a call
    that the live process keeps scoring (`started_seen` false) without its earlier
    alerts. The next snapshot then showed a never-cleared alert as "Cleared".
    `applySnapshot` now keeps such older alerts open, and also open alerts of calls the
    snapshot no longer lists. It marks them "not confirmed by the service since
    reconnecting". The mark is sticky: only `alert.cleared` or the call ending changes
    it (pure LS14, which runs in CI).
  - **Not done:** the `dashboard` Playwright project is not in CI. Sign-in, feedback and
    snippet pages have not been tested across a restart (done 22 Nov, below).
- **22 Nov, 7.QA signed-in pages across a real API restart (partial).**
  - **What:** Claude-authored `tests/browser/dashboard-auth-recovery.spec.cjs`, on
    PostgreSQL 16. The restarted test server reuses the killed one's schema, snippet
    folder and key (`serve_dashboard_api.py --schema`, `--snippet-dir`,
    `--snippet-key-file`). AR01: a signed-in live board keeps its session through the
    crash and reconnects with no new sign-in and no `auth.*` audit row. AR02: a label
    sent to the dead API says its outcome is unknown, is sent once, and is not stored;
    after the restart a label is stored and the snippet written before the crash plays,
    with exactly one label and one playback audited. AR03: a session revoked on the
    restarted API sends the board to sign-in.
  - **Defect found and fixed:** the "session could not be verified ... may be out of
    date" warning stayed on a reconnected live board until the tab got focus (up to the
    30-min idle check). While it shows, the gate now re-checks on the next 2xx answer
    any page gets (`SERVICE_ANSWERED_EVENT`, `dashboard/src/AuthGate.tsx`).
  - **Not done:** the `dashboard` project is still not in CI (human workflow edit).
- **22 Nov (attempt 2), 10.QA model refusal and rollback in the Compose lab (partial).**
  - **What:** Claude-authored
    `test_a_refused_model_change_never_stops_calls_and_a_rollback_restores_scoring`. A
    card for another export, a missing model file and a scoring window that disagrees
    with the card are each deployed through the env file. Each one is refused on every
    restart attempt with the exact reason (FR-O4), and live never becomes healthy. A real
    call completes meanwhile, unmonitored. Restoring the env file brings live back in
    about 7 s with the same model, no migration re-applied and stored rows kept.
  - **Documented:** the rollback procedure in `deploy/compose/README.md`. A change made
    mid-call ends that call's session `monitor_lost`. An older image refuses a database
    that a newer image migrated, so back up before an image upgrade (not tested).
  - **Not done:** CPU parity (needs the trained head), image rollback with a database
    restore, and the same procedure on k3s.
- **22 Nov (attempt 3), 11.2 requirements and evidence audit (partial).**
  - **What:** `docs/qa/requirements-evidence-audit.md` gives every PRD functional
    requirement (83) a status, the code and tests that evidence it, and the gap.
    `scripts/check_requirements_audit.py` and Claude-authored
    `tests/unit/test_requirements_audit.py` keep it consistent with the PRD and the
    repository in CI.
  - **Result:** 24 implemented, 32 partial, 14 data-dependent, 3 not started, 8 human,
    2 deferred. Infrastructure evidence only; no accuracy claim.
  - **Corrections found by review:** seven rows the backlog had treated as built miss a
    PRD clause. Live windowing ignores `audio.hop_s` (FR-I4). from/to are not served
    (FR-T3, privacy decision). The scoring sink holds an ended call up to 10 s against
    FR-T5's 5 s. Published events lack `partial` (FR-I3). The API's model has no card
    check (FR-O4). Compose and k3s do not expose retention (FR-O5). The ≥8 s evidence
    selection is not built (FR-M11).
  - **Not done:** the FR-O8 deployment guide, runbooks and demo instructions.
- **22 Nov (attempt 4), 11.2 FR-O8 deployment guide (partial).**
  - **What:** `docs/operations/deployment-guide.md`: hardware (A1 and what was measured
    off A1), PBX tap configuration (ARI user, `Set(AVA_TAP=caller)`, `direct_media=no`,
    flows), recording-failure policy "continue the call" with the failure cases tested,
    install, upgrade and rollback, backup and restore, the §6.3 controls with their state,
    day-to-day checks and the gaps that block production. Each part says tested or not.
  - **Tested:** the Compose PostgreSQL backup and restore, by a Claude-authored lab case:
    an online `pg_dump`; a UTF-16-damaged copy refused by `pg_restore --list` before any
    drop; an in-place rollback restore that undoes a later call; a restore onto a new
    volume after `down -v`; `ava_reader` recreated by `db-setup` and still restricted;
    nothing migrated again; a new call stored after each. `tests/unit/test_deployment_guide.py`
    keeps the guide's paths, flags, test names, settings, §6.3 rows and restore steps in
    line with the repository in CI.
  - **Audit:** FR-O8 to partial, FR-T4 to implemented (25 implemented, 32 partial, 2 not
    started).
  - **Not tested:** an image upgrade with a migration, backup on k3s, a network to a real
    PBX. §6.3 TLS is not met (not built); accounts are not turned on in Compose or k3s.
- **22 Nov (attempt 5), 11.2 reproducible demo instructions (partial).**
  - **What:** `docs/operations/demo.md`, the PRD 5.9 demo script (live tapped call,
    timeline, batch upload, stored history, egress proof), with what the demo does not
    show (untrained live head, uncalibrated baseline, lab PBX). Compose profile `demo`
    adds `demo-proxy`, a standard-library byte forwarder (`services/edge`) published on
    127.0.0.1 only, so a browser on the demo machine reaches the internal API. `api`
    and every other service keep no route out; the forwarder is the only container on
    a non-internal network. `python -m lab.demo status|call|egress` places a public-audio
    lab call and runs the egress proof (no default route AND a failed connection). The
    API in Compose now serves only Host names 127.0.0.1, localhost and api
    (`--allowed-host`, `services/api/hosts.py`), against DNS rebinding.
  - **Tested:** `tests/lab/test_compose_demo_lab.py` runs the script on the real stack:
    the Playwright project `compose-demo` (DR01 a tapped call on the live board with
    its timeline, ending `bye`; DR02 a batch upload; DR03 the stored history), `/readyz`
    on loopback, `lab.demo egress` blocked for `live` and `api`, the forwarder's clean
    stop. Unit tests run in CI for the forwarder, the profile's isolation rules, the
    helper and the guide's references.
  - **Not done:** the recorded demo video (BO-4; a human records it); a trained model, so
    no live score means anything; FR-U7 accounts in this stack.
- **22 Nov (attempt 6), 11.2 operations runbooks (partial).**
  - **What:** `docs/operations/runbooks.md`, 16 runbooks from an observable signal
    (live/API `/readyz` reason, metric, JSON log event, Grafana panel) to the effect on
    calls (always none: AVA is advisory), checks, actions and the lab evidence. Linked
    from the deployment guide §8.
  - **Tested:** `tests/unit/test_runbooks.py` (CI) checks every cited name exists in the
    code and dashboard, and that every live readiness reason and warning/error log event
    has a runbook. The procedures themselves are only as tested as each **Tested** line
    says; several (Asterisk restart, real network loss, full disk, real load) are not.
- **17 Nov daily run (details in `daily-runs/2026-11-17.md`).** FR-O6 per-stage latency
  for the live loop (7.2/8.1): `ava_live_stage_seconds{stage}` with `queue_wait`,
  `batch_wait`, `apply` (scorer and hub publish) and `window_to_result` (offer to
  result applied), bounds including the architecture §2.3 budgets, plus
  `ava_live_alerts_raised_total` (alerts per minute by `rate`) and a stage timer
  failure counter, on both the live and the analysis API `/metrics`. The offer time is
  read when the audio chunk is fed, so it includes that chunk's VAD/windowing; the
  jitter buffer, resampling and WebSocket delivery are not timed on their own.
  Measurements of this host's loop only, not an A1 latency result.
  Telephony lab (6.1, FR-T1): two SIPp endpoints in a bridged call with the codec
  forced by the PBX per endpoint (A-law or GSM agent behind a mu-law caller). Lab tests
  show the forced SDP offer and native format, and sample-identical audio both ways
  through Asterisk's transcoding; an unreachable agent rings out (`NOANSWER`). SIPp
  stands in for softphones; plumbing evidence only. `python -m lab.preflight` checks a
  host for the lab without changing anything (8 PASS on this host). Redis events now
  carry the scorer hub's random epoch, so the API detects a scorer restart exactly.
  Scorer resume (FR-I11) awaits a topology decision: tap and scorer share one process
  here, and a restart ends open calls `monitor_lost`. FR-O5 alert snippets now work in
  the live service: off by default; `--retention on` stores one AES-256-GCM snippet of
  the tapped caller audio per alert, written off the scoring thread; a real lab call
  produced one, and it decrypted to the exact tapped audio. Open FR-O5 decisions are in
  `services/live/README.md` (purge when the folder is not given again, deletion only
  while running, snippet end vs alert window). Key provisioning stays with the operator.
  Deployment (10.2, lab scope): `deploy/live/Dockerfile` builds a non-root live-service
  image, and `deploy/compose/` runs it with PostgreSQL 16, Redis 7 and the lab Asterisk
  on an internal network with no internet; a real call through the stack was stored
  and logged. Important finding: ONNX Runtime's official builds send telemetry by
  default (1DS on Linux). All imports now go through `services/inference/ort_runtime.py`,
  which forces `ORT_DISABLE_TELEMETRY=1`; this is verified on Linux only, and the
  Windows ETW backend was not checked. FR-O7 is partial (no hashed lock: `uv pip
  compile` needs a human).
- **16 Nov daily run (details in `daily-runs/2026-11-16.md`).** Room impulse responses
  for FR-D7 (3.4): `data/prepare/reverb.py` (first-arrival aligned, level-preserving
  convolution with DRR and energy lineage) and an optional `reverb` spec in
  `augment.py`. The spec lists 1-64 hash-pinned RIRs and each clip takes one seeded
  pick before noise and the codec. The writer, verifier and training loader all take
  the RIR files, and an unusable RIR stops the run. Claude-authored tests and an
  independent review are recorded in the report. No RIR or noise corpus is provisioned:
  this session's sandbox cannot write to `D:/AVA-Data` (a blocker for a human, with the
  exact command in the report). Handset vs speakerphone labelling is an open PRD question.
  The live command line can also store every event in PostgreSQL and publish it on Redis,
  each on its own pump thread (5.1/5.2/8.1). In the lab, a real call's windows reached
  PostgreSQL while the call was still up. Two decisions are open: refusing to start
  when the database is unreachable, and the service applying migrations itself. The
  live process now serves `/healthz`, `/readyz` and `/metrics` (`--health-port`; 7.2/8.1).
  Readiness requires the ARI stream, a loop that is turning and a detector that is not
  failing. The PRD's FR-A8 wording ("readyz performs a warm-up inference") needs the
  proposed update; FR-O6 is still partial. Since 16 Nov attempt 5 the live `/metrics` also
  exports RTP receiver and jitter-buffer counts (incl. `dropped_handover`), summed when
  each call ends (`ava_live_rtp_receiver_total`, `ava_live_jitter_buffer_total`).
  Attempt 2 (5.2): the analysis API can take a `RedisRelay` (`create_app(..., relay=)`).
  A `RelayLoop` thread moves the scorer's events from Redis into the API hub. Relay
  loss (sequence gaps, scorer restarts, Redis outages) reaches WebSocket clients as
  `stream.gap` with `upstream_missing`/`upstream_interruptions`, and `/v1/calls` and
  `ava_event_relay_*` metrics count it. A real-Redis test carries events from the
  forwarder to a WebSocket and turns `CLIENT KILL` into `stream.gap`. REST is not
  corrected after a gap (no PostgreSQL recovery). The API now has a command line,
  `python -m services.api --model-root DIR --port N --redis-url-env NAME`. It is
  loopback by default (no authentication) and refuses a busy port with exit 2. It
  stops gracefully on SIGTERM or Ctrl+Break with a `stopped` record. Its tests
  need requirements-api.txt, so they skip on the Linux CI job.
  Tap robustness (6.3): seeded RTP fuzzing found that a sender jumping its sequence
  number could make the jitter buffer emit about 124x real time in concealed audio
  (resync hand-overs concealed every hole at once). Concealment in hand-overs now
  spends a real-time budget, while received frames are always played (2.1x after the
  fix, of which 1x is the received frames). The real Asterisk lab still passes. Tap I/O and inference share one thread
  in the live service, a latency risk still to measure.
  Attempt 3 (7.1): the dashboard now has a live call board at `#live`
  (`dashboard/src/live.ts`, `LiveBoard.tsx`). It shows one card per call (state as
  text, duration, smoothed score labelled uncalibrated, sparkline, quality, degraded
  and stale flags), an alert feed (banner, tab-title count, an optional Web Audio tone)
  and a per-call timeline. It reads `GET /v1/calls` first and then `WS /v1/events`,
  skipping any event already in the snapshot by its `seq`. After `stream.gap` or a
  reconnect it reads REST again. Registry summaries gained `last_ema`, `quality` and
  `last_seq`. Found: `requirements-api.txt` had no WebSocket library, so a real uvicorn
  refused `/v1/events`. `websockets==17.4` is now pinned. Attempt 4: independent
  review (conditional pass, no Highs) fixed: reconnect back-off now grows with jitter
  even when the server accepts and at once closes; the timeline read is never aborted
  by newer events and its table is capped at 120 rows; late events for pruned ended
  calls make no phantom card; idle-revived calls reopen their alerts; the API
  subscribes to the hub before accepting the WebSocket. Tests: 12 Claude-authored
  Playwright cases on the real API and hub plus 10 pure merge-rule cases
  (`tests/browser/live-state.spec.ts`, run on CI); 3 mutation checks fail as expected.
  Infrastructure evidence only (no detector, no browser run against a real Redis relay).
- **15 Nov daily run (Docker now available; details in `daily-runs/2026-11-15.md`).**
  Opus VoIP codec path with loss/jitter (3.4); speech-active noise SNR bound to the
  Silero model hash (3.4); PostgreSQL 16 migrations and per-hop event store (5.1,
  psycopg 3.3.9, tested on a container); Redis pub/sub forwarder/relay (5.1/5.2, redis-py
  8.1.0); Asterisk 20.6/SIPp 3.7.2 lab image with RTP injection and codec forcing (6.1);
  passive ARI tap that never holds calls in Stasis (snoops the dialplan-marked caller
  leg from outside; review H1), per-call RTP receiver and 60 ms jitter buffer that
  conceals only real holes (6.2/6.3); receiver concealment carried to the FR-I2 window
  gate; tap-to-scoring sink whose streamed 8->16 kHz output is bit-identical to file-mode
  conditioning. Lab evidence (local Windows/Docker, opt-in tests): mu-law/A-law calls
  sample-identical, tapped audio sample-identical after <= 200 ms, cleanup < 5 s, calls
  unharmed with no/stopped/hung/crashed tap, agent leg not tapped, 14 s of public speech
  through a real call scored end to end with a FAKE detector. Later the same day: an
  ordered hub-to-PostgreSQL pump (5.1); a model card builder with an FR-O4 start-up
  check (10.2); and a live service process (8.1). That process has a start-up gate:
  card, hash, graph inspection forcing per-window serving for dynamic INT8, calibrated
  cards refused, and a finite warm-up. It runs one reconnecting loop. In the lab it
  scored a real Asterisk call with the real 14 Nov static INT8 export: untrained head,
  5 windows, gate 2.6 s. The Stasis wording was resolved 20 Nov: architecture
  §2.1/§3/§6 and PRD FR-T2 now describe the no-Stasis tap. Open questions: FR-I2's sample definition and "sustained" window; call-ID reuse; start-data privacy;
  admission before tapping; the calibration temperature in serving; and a reload
  changing hop/stride. No detector or accuracy claim.
- **Backlog 2.1 complete (9 Nov): bounded analysis service.** `services/inference/`
  analyses one PCM16 WAV with the pinned pretrained AntiDeepfake MMS-300M CPU
  baseline (hash-verified local load, no downloads): shared conditioning to 16 kHz,
  4 s windows every 2 s, mean of full windows, fewer than two full windows →
  `undetermined`, typed errors with HTTP hints (413, 422, 202 job, 503 busy, 500),
  single-flight lock, explicit `uncalibrated_baseline` labelling. 19 Claude-authored
  tests; real smoke on two clips (1-window genuine → undetermined; 3-window synthetic
  benchmark → synthetic, load 24.8 s, scoring 6.5 s). No accuracy claim.
- **Backlog 2.2 complete (9 Nov): local analysis API.** `services/api/app.py`
  (FastAPI, pinned in `requirements-api.txt`): `POST /v1/analyze` (≤60 s sync, to
  10 min as an in-memory job), `GET /v1/jobs/{id}`, `/healthz`, local
  `/openapi.json`; 411/413/422/503/500 mapping; bounded uploads and job queue;
  shutdown stops the worker. 7 Claude-authored tests. Jobs are not durable (5.2);
  `/docs` pages off (CDN) is a recorded FR-A11 deviation.
- **Backlog 2.3 complete (14 Nov): batch upload page (FR-U4).** `dashboard/` React
  18.3.1 + TypeScript + Vite 5.4.11 with exact pins and lockfile, built to static files
  the API serves on its origin (`create_app(..., dashboard_dir="dashboard/dist")`):
  drag-and-drop, bounded job polling, verdict with the uncalibrated-baseline label,
  FR-U3 SVG timeline, §1.4 limitations, same-origin CSP. 13 Claude-authored Playwright
  cases (real API, fake detector; `npm run test:dashboard`) and 6 mount tests;
  independent review fixed. Infrastructure only.
- **Backlog 2.QA complete (14 Nov): real checkpoint through the page.** Local-only
  Playwright project `real-model` (`npm run test:real-model`, gitignored
  `.local/real-model-qa.json`): pinned AntiDeepfake checkpoint with `/readyz` warm-up,
  public CSALT genuine and synthetic-benchmark audio, a >60 s job and damaged files;
  the page shows exactly the API's result. 5 Claude-authored cases pass; review fixed.
  Observations only (uncalibrated baseline, concatenated clips): genuine 24 s mean
  0.000, synthetic 23 s mean 0.611; not an accuracy result.
- **Backlog 7.2/8.2 progress (14 Nov catch-up 3): live pipeline metrics (FR-O6).**
  `LivePipeline.metrics_snapshot()` (ID-free; every accepted window accounted for once:
  dropped, cancelled, skipped, scored, lost, result error, not applied, waiting or in
  the batcher), `PipelineStats` hand-over to the API, `/metrics` `ava_live_*` families
  with snapshot age and validity. Review fixes: a failed batcher submit keeps the
  window; per-direction state bound to `LiveCalls.token` so reused call IDs never get an
  earlier call's windows. Fake detector only; not wired to a real tap or model.
- **Backlog 10.1 progress (14 Nov catch-up 4): ONNX export and FR-M7 parity harness.**
  `models/export/onnx_export.py`: FP32 opset-17 export with a dynamic batch (opset read
  back, outside-repo, no-overwrite), CPU-only session with hash pin, I/O contract check,
  parity harness (>= 100 windows, strict < 1e-3) and the pure INT8 -> static -> FP32
  rule; `requirements-export.txt` pins onnx 1.23.2. Dynamic INT8 of MatMul/Gemm
  (`quantize_int8_dynamic`) with a drift report. Finding: dynamic INT8 output depends
  on batch partners (cross-call micro-batching question; INT8 EER must state its
  batching). Static INT8 (`quantize_int8_static`, MinMax calibration, fixed scales)
  is batch-independent by test. `checkpoint_parity.py` real run (8 layers of the
  AntiDeepfake MMS-300M checkpoint, untrained seeded SLS head, 100 noise windows): FP32
  ONNX parity max |logit diff| 4.95e-6 (< 1e-3); dynamic INT8 458 -> 153 MB, drift max
  0.024 / mean 0.003 logits, no class flips. Graph numerics only; trained-head parity on
  dev windows and dev EER remain. 8.QA: `benchmark_onnx.py`; on this laptop (4 threads,
  batch 1) the 8-layer FP32 graph takes a median 0.69 s and dynamic INT8 0.43 s per 4 s
  window (not box A1, untrained head; no §6.1 claim). 8.1: `services/inference/
  onnx_detector.py` `OnnxBatchDetector` feeds an ONNX session to `LivePipeline`
  (`per_window` required; True for dynamic INT8); tested through the real pipeline
  with tiny exports. Open PRD/architecture question: per-window INT8 versus ADR-3
  micro-batching for FR-I9 capacity. 7.2: live inference latency
  histogram added to `/metrics` (1839139).
- **Backlog 3.3 advanced (14 Nov): GSM 06.10 and AMR-NB codecs.**
  `data/prepare/ffmpeg_codecs.py` through pinned ffmpeg 9.0.2 (winget user install,
  GPL-3.0, Windows hash pinned): known-answer frame/mode checks, measured AMR frame
  rule and 40-sample delay compensation, lineage with exact arguments; 19
  Claude-authored tests (7 need the local ffmpeg; CI skips them). Same day: wired into
  `augment.py` (and the training window loader), seeded frame loss (AMR NO_DATA,
  GSM repeat_then_silence) and jitter from the G.711 plans, concealed once, with
  G.711-compatible record names. Catch-up 3: per-clip codec mix (`codec_mix` spec,
  seeded weighted choice per derivation clip ID recorded as `chain.codec_choice`,
  per-codec counts, codec per window in the loader). Backlog 3.3 complete as file
  pipeline infrastructure; Opus/G.729 belong to 3.4, RFC 4867 RTP payloads to 6.x.
- **Backlog 3.1 in progress (9 Nov): offline/serving conditioning parity.**
  `tests/unit/test_conditioning_parity.py` (Claude-authored) runs one WAV through
  `prepare_audio.py --condition` and the analysis service: identical canonical PCM
  hash and sample count for 16/8/48 kHz mono and 44.1/22.05 kHz stereo; prepared clip
  offsets address the service's samples; both paths refuse PCM24, 3-channel and
  6 kHz input. Recorded divergence: the service applies no peak normalization and
  skips a final tail under 1.5 s. Open for 3.1: speaker-turn boundary contract.
- **Backlog 3.3 in progress (9 Nov): G.711 file codec.** `data/prepare/codecs.py`
  mu-law/A-law encode/decode (exhaustively equal to CPython 3.11 `audioop`) and an
  8 kHz telephone roundtrip with lineage; seeded 0-5 % 20 ms packet loss with
  silence/repeat concealment; recorded per-clip loss plans (`derive_loss`);
  jitter as late-packet loss behind a fixed buffer with per-clip plans
  (`derive_jitter`); since 10 Nov `telephone_roundtrip(loss_plan=, jitter_plan=)`
  conceals lost and late packets as one mask and records both plus the union
  (`packet_concealment`). Open for Tughral: the per-mechanism 5 % bounds let the
  union exceed 5 % of a clip's packets; FR-D6 does not say whether 0-5 % bounds the
  sum. GSM and AMR-NB remain. 3.2 VAD is blocked on an unprovisioned VAD
  dependency/model. 11 Nov: `data/prepare/augment.py` wires noise (FR-D7) and the G.711
  loss/jitter roundtrip over a published prepared root into new augmented-copy roots
  with the chain recorded per clip, explicit concealment, content-based derivation IDs,
  `counts_toward_clip_total: false` and a read-only whole-report rebuild check; 15
  Claude-authored fixture tests, independent review fixed. Infrastructure only; no
  real corpus was augmented. Open data decision: noise-clipping refusals can bias SNR
  toward 20 dB on −3 dBFS parents (refusal SNRs are recorded).
- **Backlog 4.1 complete (10 Nov): scoring configuration.** `services/scorer/config.py`
  validates the FR-I4/§2.4 rules (both threshold pairs `0 < exit < enter <= 1`,
  `0 < hop <= window`, window equal to the model card) plus labelled sanity rules,
  with §7.3 defaults; `ConfigStore` gives calls immutable snapshots, serialized atomic
  reloads from a mapping or a hardened JSON file, and a restart-stable fingerprint.
  Deviation: no YAML parser or Pydantic is pinned, so JSON stands in. Open for the
  PRD: ordering between suspicious and alert thresholds, `min_windows` upper bound,
  agent-only scoring.
- **Backlog 4.2 complete (10 Nov): per-direction scoring session.**
  `services/scorer/session.py`: EMA (α from config, first window seeds), provisional
  until `min_windows`, `undetermined` below it, the architecture §2.4 hysteresis
  transitions with a 2-window alert streak, a new alert ID per alert, `ended_open`
  for an alert open at the end. Pure logic; persistence/events are backlog 5. Open
  for the PRD: FR-I7 `ended_open` wording for `suspicious`, threshold ordering,
  end-of-call verdict vocabulary, `state.changed` as an internal event.
- **Partial backlog 5.1, 8.1, 8.2, 9.3 (10 Nov), pure logic only.**
  `services/events/` FR-A4 envelope and bounded non-blocking fan-out;
  `services/inference/capacity.py` call gate (capacity refusal) and live-first
  micro-batcher with a 25 % token bucket; `services/scorer/degraded.py` FR-I10 stride
  controller and `services/scorer/pending.py` bounded per-call pending-window queue
  that defines "behind" (a waiting window at acceptance; interpretation for the PRD);
  `services/scorer/ordered.py` serialized in-order per-call boundary (phase-4 QA M1/L3);
  `models/evaluation/metrics.py` EER, FAR at FRR 5 %, minDCF (costs required:
  ASVspoof 5 Track 1 constants confirmed 14 Nov, see below), bootstrap, ECE, temperature, FR-M9 evidence curve,
  FR-M5 subset table, reliability bins and FR-M10 per-dialect FAR. Unit suite 472 OK
  (9 skips) on 10 Nov; daily report `docs/context/daily-runs/2026-11-10.md`. Blocked:
  PostgreSQL/Redis need a driver/Redis decision (PostgreSQL 18 binaries present, the
  architecture names 16); warm-up/threads need the exported model; capacity needs
  measurement.
- **Backlog 9.1 complete (12 Nov): AVA model harness.** `models/ava/` truncated
  wav2vec 2.0 backbone (strict state truncation; tiny-model test shows equality with
  the full model's first layers), SLS head (learned softmax layer weighting, 256-d
  projection, attentive statistics pooling, 256-d embedding, 2 classes), FR-M8
  mean-pool head, `float32[B,64000] -> logits[B,2], embedding[B,256]` with in-model
  per-window layer norm, AdamW 2e-5/1e-3 with frozen-encoder epochs, class-weighted
  CE and a layer-sweep harness. 28 Claude-authored tiny random-model tests;
  independent review fixed (centred variance, whole-set eval loss, no decay on layer
  logits/biases/norms, warm median timing). `models/ava/windows.py` loads FR-M11
  windows (zero-pad; >4 s keeps the first 64,000 samples, to confirm) from conditioned
  prepared roots or verified augmented roots: train split only, trainable rows only,
  refuses split-shared speakers/source hashes, quarantined sources (unless allowed),
  duplicates and altered clips; 12 Claude-authored fixture tests, review fixed. No
  checkpoint run, no accuracy or capacity evidence; the real checkpoint is outside the
  runner's allowed directory.
- **Backlog 9.2 in progress (12 Nov): RawBoost.** `models/augmentation/rawboost.py`
  re-implements RawBoost (Tak et al. 2022) in NumPy with the reference defaults and
  modes 1-8, per-clip seeded draws and a record (seed, clip, parameters, NumPy version);
  inputs must be in [-1, 1]. 16 Claude-authored tests (filter properties, FFT check,
  SNR bounds, every mode's composition, normalization known answers, refusals);
  review fixed. Not bit-parity with scipy (not installed). Unit suite 503 OK (9 skips).
  `models/baselines/rawnet2.py` RawNet2 baseline in the ASVspoof 2021 layout on the
  shared window and harness contract, `RAWNET2_TRAIN_CONFIG` (1e-4, decay 1e-4,
  weights 0.1/0.9); 13 Claude-authored random-weight tests; review fixed. Written
  without the upstream source: verify fidelity before reporting baseline numbers.
- **Backlog 7.2 progress (12 Nov): FR-A8 readiness and metrics.** `services/api/app.py`
  adds `/readyz` (first call runs `AnalysisService.warm_up()`: one silent window with
  validated logits; sticky; 503 with reason when busy before warm-up, on detector
  failure or when the job worker stopped) and `/metrics` in Prometheus text 0.0.4.
  Known gaps: the model still loads lazily (FR-O4 "fail loudly at startup" not met;
  use a startup probe or longer timeout in k3s), readiness does not revert after
  later detector faults, `ava_analysis_seconds` is a summary not the planned stage
  histograms, and FR-O6 telephony metrics are absent. Unit suite 514 OK (2 skips)
  with API dependencies.
- **Backlog 5.3 progress (12 Nov): no-audio-retention default verified on the API.**
  `tests/unit/test_api_no_audio_retention.py` shows uploads over 1 MB really spool to
  a temporary file that is gone after the request, job records keep no raw or decoded
  audio after completion or shutdown, and responses are small and filename-free. Not
  covered: process memory, logs, swap, other framework versions; on Linux the spool
  check skips. Unit suite 517 OK (2 skips).
- **Backlog 5.2 progress (14 Nov): FR-A3 calls read model and FR-A4 WebSocket.**
  `services/calls/registry.py` builds the call list from published FR-A4 envelopes
  (30 min of each call at full resolution, older points in 10 s per-direction buckets,
  alert lifecycle incl. `ended_open`, per-call stale/duplicate rejection, bounded
  calls/points/buckets/alerts, idle calls flagged `stale` and evicted only under slot
  pressure). `services/api/app.py` serves `GET /v1/calls`, `GET /v1/calls/{id}` and
  `WS /v1/events` (call filter, `stream.gap` on overflow, 1013 at the subscriber limit,
  send timeout) when given an in-process `EventHub`. 25 Claude-authored tests, two
  independent review rounds fixed. Not done: scorer-to-FR-A4 mapping, Redis,
  PostgreSQL history, authentication; unit suite with API deps 542 OK (2 skips).
  Later the same day: `services/scorer/publish.py` maps scorer events to FR-A4
  (call-level state on every `score.updated`, unscored update for the quality gate,
  call-level `call.ended` summing speech seconds) and `services/scorer/live.py`
  owns live calls (CallGate admission with `system.health` capacity refusal, config
  snapshot per call, apply+publish under a per-call lock, FR-I11 late-result discard,
  `monitor_lost` end of all calls). 29 Claude-authored tests, three review rounds
  fixed; still not wired to a tap or inference loop, no Redis/PostgreSQL. Unit suite
  with API deps 571 OK (2 skips).
- **Backlog 5.QA progress (14 Nov): in-process live-path integration.** Three
  Claude-authored tests drive pipeline -> LiveCalls -> hub -> API (registry,
  WebSocket, metrics) with a fake detector: alert lifecycle, capacity refusal event,
  end reasons, late result after `monitor_lost`, detector outage. No telephony,
  Redis/PostgreSQL or restart recovery. Unit suite 607 OK (2 skips).
- **Owner decision (Tughral, 14 Nov 13:22, commit b4db2b7):** scheduled sessions may
  install the free/open-source software the backlog needs (pinned, licence recorded,
  stored outside Git); earlier missing-package blockers are superseded. Installs needing
  admin rights, reboots or interactive prompts are recorded for a human.
- **Backlog 3.QA progress (14 Nov catch-up): phase-3 audio-path QA.**
  `docs/qa/phase-3-qa.md` (independent conditional pass as infrastructure; file-mode
  sample parity proven). Fixed: one plain-PCM fmt rule (`data/prepare/wav_format.py`)
  for preparation, augmentation, the analysis service and the verifier; inflated
  header sizes refused before duration routing; nested JSON as ValueError; empty
  quarantine errors; augmented copies start `unreviewed`. 14 Claude-authored tests.
  Open: §2.2 8 kHz batch rule and normalization unit (M1, PRD decision).
- **Backlog 3.2 progress (14 Nov catch-up): FR-I3 speech windows (fake VAD).**
  `services/scorer/speech_windows.py` excludes non-speech frames (time still advances),
  windows the speech-only stream exactly like file mode, marks partial windows below
  2.5 s of speech not scorable, reports no-speech time; a VAD fault drops that audio
  and resets the VAD. 11 Claude-authored tests; review fixed. Silero VAD 6.2.3 ONNX
  (MIT, hash-pinned, local under `D:/AVA-Data/models/silero-vad/6.2.3/`) runs through
  `services/inference/silero_vad.py` on onnxruntime 1.32.0 CPU; it matches the
  reference wrapper frame by frame (atol 1e-5) on synthetic audio. VAD accuracy on
  Urdu/Punjabi telephone speech is not measured. `services/scorer/speech_feed.py`
  connects windows to the live pipeline (call-level `no_speech`, tails before the end,
  VAD speech seconds at the end, faults contained). Backlog 3.2 checked; the tap
  feeding it is 6.x. Unit suite with audio, API and retention deps 662 OK (3 skips);
  model tests 3 OK.
- **Backlog 5.3 progress (14 Nov catch-up): AES-256-GCM for FR-O5 snippets.**
  `AesGcmSealer` on pinned `cryptography==50.0.2` (`requirements-retention.txt`), key file
  outside the repository, `open_snippet` authenticates header/alert binding; 11
  Claude-authored tests incl. NIST GCM known answers; review fixed. Unit suite with
  audio, API and retention deps 632 OK (3 skips). Key provisioning/rotation open.
- **Backlog 5.3 progress (14 Nov): FR-O5 snippet retention without encryption.**
  `services/retention/snippets.py` implements the retention rules (default off,
  policy fixed at call start, 30 s ring per scored direction, exactly one sealed file
  per alert ID, TTL and purge-when-off cleanup, failures never raised). AES-256-GCM is
  blocked on a crypto dependency, so the store refuses to run in production. 11
  Claude-authored tests with an insecure test sealer; review fixed.
- **Backlog 8.1/8.2 progress (14 Nov): live inference loop wired (fake detector).**
  `services/scorer/pipeline.py` connects per-direction pending queues and stride
  control, the micro-batcher, a batched detector and `LiveCalls` with one window per
  direction in flight, so slow inference grows the stride rather than a queue;
  `unmonitored` published at the cap; cancellation and fault containment. 17
  Claude-authored tests; review fixed. No real detector, threads or tap yet.
- **Backlog 7.2 progress (14 Nov): FR-O6 metrics subset.** `/metrics` histograms for
  analysis time and per-stage time (decode, condition, inference; model load
  excluded, verified with a stepping clock) and live-call gauges/counters from the
  call registry without IDs. 5 Claude-authored tests; the 12 Nov parser now checks
  histogram buckets. Unit suite 576 OK (2 skips).
- **Backlog 8.QA progress (12 Nov): FP32 CPU proxy timings.** `models/ava/benchmark_cpu.py`
  times the real-size truncated MMS-300M + SLS with random weights. On the dev laptop
  (Intel family 6 model 140, 4 physical/8 logical CPUs, torch 2.8.0+cpu, 4 threads,
  batch 1, 3 interleaved rounds x 7 calls) medians per 4 s window were 4L 0.46 s, 6L
  0.56, 7L 0.62, 8L 0.65, 10L 0.74, 12L 0.81 (p95 0.58-0.96 s; RTF medians 0.12-0.20).
  Two earlier single-round runs on the same host gave medians up to 1.26 s with one
  12.7 s outlier and an unstable cut order. FP32 PyTorch eager, random weights, a
  noisy laptop: no PRD §6.1 target is met or missed and INT8 latency is not bounded.
  INT8/ONNX is blocked: onnx and onnxruntime are not installed or cached.
- **Backlog 9.3 progress (12 Nov): report generator.** `models/evaluation/report.py`
  scores loaded windows (`p_synthetic = sigmoid((l_syn - l_gen)/T)`), builds the FR-M5
  subset table (language, codec, dialect, generator family, source partition, source),
  ECE/reliability with a required temperature status, optional FR-M10 dialect FAR,
  and writes new JSON outside the repository; windows now carry codec and provenance.
  11 Claude-authored tests, review fixed. FR-M5 (e) code-switched and (g) phone-call
  subsets lack manifest fields. 14 Nov: minDCF costs confirmed from the ASVspoof 5
  Evaluation Plan v0.6 Table 1 (Track 1: C_miss 1, C_fa 10, pi_spf 0.05) and exposed
  as `asvspoof5_track1_costs()` / `ASVSPOOF5_TRACK1_SOURCE`; PRD FR-M5 and
  architecture §4.5 corrected from "DF-track" to Track 1.
- **Phase 4 QA (10 Nov): conditional pass as infrastructure** (`docs/qa/phase-4-qa.md`).
  The scorer objects are single-owner and not thread-safe (documented); backlog 5 must
  serialize per call, reject reordered/duplicate results and copy event data on
  publish. Ten interpretation choices await PRD decisions.
- **Backlog 4.3 complete (10 Nov): call-level scoring.** `services/scorer/call.py`
  scores only configured directions (caller by default), starts the agent direction
  at the human answer (IVR windows ignored), drops windows over the FR-I2 10 %
  concealment gate and marks the call `quality=low`, and reports the more severe
  direction with the driving direction. Interpretations awaiting the PRD: quality
  stays low for the call, ties go to the caller, a call is `undetermined` unless every
  scored direction has enough evidence or one is suspicious/alert. `no_speech`,
  `unmonitored` and answer detection belong to 3.2, 8.2 and 6.
- **Backlog 3.4 in progress (9 Nov): additive noise at SNR (FR-D7).**
  `data/prepare/noise.py` mixes same-rate PCM16 noise at 0-20 dB whole-clip SNR
  from a seeded, recorded offset with clipping and achieved SNR recorded, plus
  per-clip SNR/seed plans (`derive_noise`, `mix_planned`); 18 Claude-authored tests.
  Library only: no noise corpus, RIRs, speech-active SNR or manifest wiring yet.
- **Backlog 1.1 complete (9 Nov, daily branch): prepared-output verifier.**
  `scripts/verify_prepared_output.py` re-derives every `prepare_audio.py` output
  without importing it: conditioning, metrics and flags (exact float sums), pause
  boundaries and the retained-sample optimum (tie-break independent), byte-exact
  clips, exact field sets, summary totals, root contents, original FLAC hashes and
  a transcript/Arabic-script scan. Read-only PASS on the Punjabi (187 clips), CSALT
  conditioned (450) and synthetic benchmark (50) outputs, matching earlier totals.
  40 Claude-authored tests; independent review findings fixed. The exact-mode CSALT
  pilot v2 cannot be re-verified: its input manifest is not on disk.
  See `handoffs/prepared-verifier.md`.
- **Backlog 1.2 complete (9 Nov): Punjabi importer follow-ups and cleanup fix.**
  `csalt_pilot.inspect_pcm` gives signal inspection without a publisher claim
  (CSALT `inspect_audio` output unchanged); Punjabi `review_flags` are now lists;
  rows declared longer than ~104 s are excluded before ranking (counted as
  `excluded_over_duration_bound`, a speaker losing all rows fails before output),
  mirrored in `scripts/verify_pnb_pilot.py`; archive root must be outside the repo.
  The real 12-row pilot re-verifies PASS under the new rule (selection unchanged).
  Intermittent Windows WinError 145 fixture-cleanup errors are handled by the
  bounded `tests/fixture_cleanup.py` retry. Existing pilot manifests keep string
  flags; only new imports change.
- **Backlog 1.3 complete (9 Nov): split proposals at schema 2.** Spec decision 7
  is implemented: benchmark rows follow their speaker (`benchmark_follows_speaker`)
  and keep `benchmark_only: true`/`training_eligible: false`; there is no
  `benchmark_only` partition; reports carry benchmark and trainable tables.
  Decision 9 (coordinator): benchmark scope must be explicit, and the validator
  recomputes the `proposal_id` content digest. 16 Claude-authored cases plus 20
  updated baseline tests. Todo 9d: CSALT intake annotates duplicate originals; the
  real v2 inventory has 2 cross-speaker duplicate hashes (4 rows, all selected).
  Open: benchmark-only speaker groups still take allocation slots (not material now).
- **Backlog 1.4 complete (9 Nov): FR-D11 policy applied to data (todo 9e).**
  `data/manifest/migrate_policy.py` wrote policy-versioned copies
  (`fr-d11-2026-10-05`) of the CSALT genuine (340 rows trainable), CSALT synthetic
  benchmark (68, still excluded) and Meta pnb (12 trainable) manifests; consent
  status unchanged. Re-prepared outputs match the earlier totals and verify PASS.
  Split proposal v2 (schema 2, seed `ava-split-2026-11-09`): 687 rows, 23 speakers
  in 22 groups, train/dev/test 520/75/92 rows, 637 trainable, zero crossings. This
  supersedes the v1 proposals for planning; it is a proposal, not an accepted
  split. CSALT publisher synthetic rows stay benchmark-only, so all 637 trainable
  rows (Urdu and Punjabi) are genuine until AVA-generated fakes exist (D11 shortcut risk;
  deviation from todo 9e wording awaits Tughral). Use the `-frd11-r2` outputs.
  See `handoffs/policy-migration.md`.
- **Todo 8b complete: collection preparation written; 8a and 8c blocked on the
  approval document.** `docs/data-collection/collection-plan.md` lists the eight
  fields only the approval can supply (reference, body, protocol and form
  versions, activities, pilot size, storage and withdrawal terms, compensation,
  off-site processing, publication intent), the four-person allocation (all four
  code and collect; Ilsa and Ayesha lead outreach to women participants; Tughral
  and Ibrahim, who have Claude Max and Codex Pro, take integration and review and
  pair with the other two, whose coding tasks arrive as scoped issues with tests),
  recruitment and scheduling, the session procedure, the intake flow into the
  external intake CSV and validator, QA cadence and explicit limits.
  `session-script.md` is the Parts A/B/C template with placeholders for approved
  prompt text. `source_register.json` and `approval-pack.md` now say "approval
  reported 4 October, reference pending" instead of "pending approval"; the start
  gate remains unevidenced and recording has not started. GitHub: the repository
  has two collaborators (mIBRAHIM707 admin, tughral1 write); Ilsa and Ayesha still
  need access. PR #26 (PRD and architecture v0.2) is open awaiting review.
- **Todo 7d, verifier part, complete.** `scripts/verify_pnb_pilot.py` makes the
  7b verification reproducible from the repository: it takes the archive,
  manifest and raw roots plus the publisher archive hash on the command line,
  refuses under `python -O`, uses no bare asserts, re-derives selection and clip
  IDs without importing the importer, re-reads only the selected rows' audio,
  and reports aggregates only. With `--write-verification` it records its own
  SHA-256, dependency versions and per-file transcript hashes, and compares
  transcript hashes against a prior record when one exists. Read-only run on the
  real pilot: PASS, 12 rows, 6 speakers, 2 per speaker, 13.0646 minutes, all
  48 kHz mono, 15 aggregate checks. Tests: 15 Claude-authored fixture cases
  (tampered WAV, FLAC and transcript, flipped policy fields, INCOMPLETE markers,
  write-once, `-O` refusal, prior-hash match and mismatch); the whole `tests/pnb`
  folder (13 Astra plus 15 new) passes. The machine-local prototype in `.local/`
  is superseded but kept; its `verification.json` occupies the output name, so the
  live transcript-hash comparison needs a fresh record in a new manifest root.
- **Todo 7 reviewed: PASS.** An independent ava-reviewer pass on 7b found no
  blocking defects, confirmed lineage, external storage and policy fields by its
  own recomputation, and raised two medium findings about the verifier (assert
  stripping under `-O`, fixed; non-reproducible machine-local script, queued as
  todo 7d) plus low importer follow-ups (also 7d). A second independent pass on
  7c also returned PASS: all 187 clips recomputed from the FLAC originals with an
  independent re-implementation of the pause segmentation yielding identical
  ranges, every metric and flag recomputed, no transcript text or Arabic-script
  code point anywhere in the processed report. Its medium finding (the 7c
  verifier checked tiling and counted flags rather than recomputing them) is
  covered by the review for this run and folded into 7d. All ten 7c flags are
  `mostly_quiet`; one of two repeat unit-suite runs hit an intermittent Windows
  temporary-directory cleanup error after assertions passed.
- **Todo 7b complete: Punjabi pilot import.** `data/collect/meta_pnb_pilot.py`
  ran against the verified first Meta `pnb_Arab` train shard (464,239,672 bytes,
  publisher SHA-256 matched before and after extraction) and selected two
  recordings from each of the six publisher train speakers: 12 recordings,
  13.0646 minutes, all native 48 kHz mono PCM16 FLAC of roughly one minute each,
  preserved byte-exact beside decoded native-rate WAV copies and source-only
  transcript files. 13 Astra tests passed, foundation checks passed, and an
  independent verifier re-read the 12 embedded payloads straight from the shard,
  recomputed the deterministic selection and compared FLAC bytes, decoded samples,
  WAV and FLAC hashes, durations, transcripts, metadata, namespacing and every
  policy field: 13 aggregate checks passed. Every row carries `training_eligible:
  false`, `cloning_status: not_authorized`, `consent_status:
  not_individually_verified`, `dialect: unknown`, `split: unassigned` and the
  publisher `train` split. Whole-file screening raised only the expected format
  and duration flags. Six speakers from one shard is a pilot, not Punjabi
  coverage; dev, test and the second train shard were not fetched. Outputs and
  verification live outside Git via `.local/data-location.json`. Independent
  reviewer pass requested; see `docs/context/handoffs/pnb-pilot-import.md`.
- **Todo 7c complete: Punjabi pilot preparation.** The shared `--condition`
  path (`data/prepare/prepare_audio.py`) turned the 12 decoded WAVs into 187
  conditioned candidates of 3 to 6 s: 762.993 s retained, 20.884 s uncovered,
  10 review-flagged, 7 sources fully and 5 partially covered, zero errors, 66 s
  wall time. All 12 sources were resampled by soxr HQ from 48 kHz; outputs are
  16 kHz mono PCM16 at −3 dBFS sample peak. An independent verifier rebuilt every
  clip from the immutable WAV (mono mean, soxr HQ, quantization, slice, peak gain)
  and matched bytes and hashes, confirmed canonical PCM hashes, unique output
  hashes, identical lineage fields across pilot, source and clip rows, no
  transcript text on clip rows, gap-free retained-plus-uncovered accounting, and
  the summary totals. Clips per speaker range from 26 to 39, so equal source
  counts did not yield equal clip counts. Unit suite: 104 tests, 102 passed, two
  Windows symlink skips. These are review candidates with `review_status:
  unreviewed`, not an accepted dataset; no telephone-band round-trip, codec
  augmentation, VAD, listening QA or split assignment occurred.
- **Todo 6 complete: CPU baseline smoke.** The pinned official MMS-300M
  AntiDeepfake model scored 12 local clips (6 genuine, 3 VITS, 3 Tacotron),
  55.98275 seconds of audio, with four CPU threads. Final hardened v3 run:
  19.38719 seconds inference, RTF 0.3463; loading 9.7096 seconds and warmup
  2.6785 seconds separate; sampled process peak RSS 2.772 GB including loading.
  Strictly loaded 421 backbone tensors and both original trained head tensors;
  eight known pretraining-only tensors explicitly omitted. No random SLS head.
  All input hashes and finite scores verified; repeat logits matched v2 within
  1e-6. Python connection/DNS guard observed zero attempts (not an OS network
  trace). Twenty Astra model tests passed, no skips, and independent review closed.
  Fairseq/ONNX parity, language accuracy and telephone capacity remain unestablished.
  See `docs/context/handoffs/cpu-detector-smoke.md` and the separate optional model
  environment. v1 is incomplete; v2 predates snapshot hardening; use v3 evidence.
- **Todo item5 complete: separate existing-synthetic benchmark.** Imported68
  publisher-hash-verified originals (34 VITS-family,34 Tacotron;2per17folders per
  family),10.490minutes, from the same pinned CSALT revision. Prepared50clips,
  237.8265seconds:9VITS and41Tacotron,10signalflags,zeroquarantines.391.561seconds
  remain uncovered; no forced cuts. This generator imbalance after pause selection
  limits representative evaluation; the sample is only for CPU smoke tests.
  `benchmark_only=true`, `training_eligible=false` and unknown version/reference/
  target-identity fields survive both preparation modes. No cloning performed.
  Independent source/output reconstruction, hashes, peak/format and scope checks
  passed.104Python tests discovered,102passed,twoexistingWindowssymlinkskips;
  all12Astra benchmark cases passed. Two integration review findings fixed.
  External reports located by `.local/data-location.json`.
- **Todo item 4 complete: balanced-source conditioning pilot.** The 340 originals
  produced 450 derived 3–6 second candidates, 1,958.4425 seconds (32.641 minutes),
  zero quarantines, 154 signal-flagged candidates. All outputs are 16 kHz mono PCM16
  with −3 dBFS sample peaks. Nine native 20 kHz sources were resampled; 331 bypassed
  resampling. Originals, publisher metadata and inventory IDs remain traceable.
  All 520 original hashes were independently rechecked. Every output hash, format,
  gain-transformed sample slice, source ID and retained/uncovered range passed
  independent reconstruction checks. 771.6716 seconds remain uncovered; source
  outcomes: 224 full coverage, 55 partial, 59 no valid pause segments, two too short.
  All 17 speakers appear in outputs, with 10–40 candidates per speaker: equal
  source counts do not imply balanced derived clips. No final splits assigned.
  Final output is conditioned v2 via `.local/data-location.json`; v1 predates review
  fixes and is preserved separately. QA: 92 tests discovered, 90 passed, two Windows
  symlink skips. One prior full run had a Windows temporary-directory cleanup error
  after assertions; an unchanged rerun passed. All 16 Astra conditioning tests and
  independent fix review passed. See `docs/context/handoffs/balanced-audio-conditioning.md`.
- `data/prepare/conditioning.py` supplies class-independent soxr HQ conversion
  and per-clip peak normalization. Explicit `--condition` uses these in preparation;
  legacy exact-sample mode remains. Pinned CPU dependencies are installed through
  `requirements-audio.txt`; CI/test commands updated. Native clipping warnings
  survive stereo downmix, and pre-gain flags survive normalization. This is source
  preparation, not telephone-domain simulation, VAD or full train/serve parity.
- **Current CSALT working selection:** 340 genuine originals, 20 each from all 17
  published speaker folders, 45.502 minutes / 87.975 MB. Consolidated local
  inventory: 520 verified originals; 180 extra Speaker_01 originals are retained
  but excluded from this selection. Downloaded 270 additional files. All 250
  previous original hashes remain unchanged; inventory/selection independently
  checked and the structural validator passed all 520 rows. Splits unassigned.
  Final native formats: 331 selected files at 16 kHz mono and nine at 20 kHz mono,
  all PCM16. Conditioning now supports both formats; originals stay untouched. Final manifests
  are v2 and located by `.local/data-location.json`; v1 is a preserved diagnostic
  attempt with no balanced output. Importer `data/collect/csalt_pilot.py` and handoff
  `docs/context/handoffs/balanced-csalt-intake.md` document the workflow. Tests:
  76 discovered, 74 passed, two existing Windows symlink skips; all 15 new importer
  tests passed. These results supersede the earlier local-holdings totals below.
- Offline listening review is available via `data/review/build_review.py`. The
  generated page lives beside the external v2 report; its location is recorded in
  `.local/data-location.json`. It displays 66 local candidates, all unreviewed by
  default, with reviewer decisions and report-bound JSON export/import. No server
  or upload. QA: 61 Python tests discovered, 59 passed and two symlink skips;
  17 Playwright tests passed (seven for review UI). Actual page/one-WAV decoding
  smoke check passed without HTTP requests. Assistant listening remains unavailable.
- User reports some clips sound fine based on their native Urdu listening review.
  Exact filenames and raw/prepared status are pending clarification; do not mark
  the whole batch accepted. Local CPU ASR screening is a possible next aid, not
  yet installed or executed; transcription is not perceptual listening QA.
- Implemented `data/prepare/prepare_audio.py`: bounded, hash-verified immutable
  WAV preparation, 3–6 second energy-pause candidates, quality flags, source/speaker
  sample-offset lineage and uncovered ranges. Final pilot v2: 50 sources -> 66
  candidates (287.607 seconds), 24 flagged for review, 98.439 seconds uncovered;
  zero quarantined inputs. 34 sources fully covered, 7 partly covered, 7 with no
  valid pause segments, 2 too short. All 50 original hashes remained unchanged;
  every output was independently checked against the exact source sample slice,
  and retained+uncovered ranges account for every original sample. No listening
  acceptance, normalization, codec augmentation or split assignment is implied.
  `.local/data-location.json` points to the final v2 output and report; v1 is an
  earlier run preserved separately. See `data/prepare/README.md` for limitations.
  Integrated QA: 52 tests discovered, 51 passed, one source-symlink test skipped
  because Windows denied symlink creation. Four independent review findings were
  fixed and rechecked. See `docs/context/handoffs/pilot-audio-preparation.md`.
- Expanded CSALT intake on 3 October with five genuine clips from each of
  `Speaker_02` through `Speaker_11`: 50 files, 10 additional speakers, 12.36 MB,
  6.43 minutes. All matched publisher SHA-256 hashes and decoded as 16 kHz mono
  16-bit PCM. Signal screening flagged 32 outside 3–6 seconds, 11 below -35 dBFS
  whole-file RMS, and six with more than 50% quiet windows (20 ms RMS < -40 dBFS).
  No near-full-scale screening flags; no download errors. Flags may overlap and
  do not establish audible quality or mandate rejection. No originals modified.
  Combined local holdings: 250 genuine files from 11 source speaker IDs, heavily
  weighted toward Speaker_01; this is not a balanced or approved training set.
  On the current machine `.local/data-location.json` points to audio and reports.
  It is ignored and does not transfer through Git. Receiving agents should read
  `pilot-report-speakers02-11.json` there before repeating or extending the work.
- Downloaded the complete CSALT `Bonafide/Speaker_01` folder into external local
  data storage on 3 October: 200 WAV files, 53.42 MB, 27.82 minutes. All 200 matched
  publisher SHA-256 hashes and passed WAV frame-read checks. Source revision:
  `eb8f16623108324867e4424a646937409c64b82b`. Source README and a download report
  are stored beside the audio outside Git. This single-speaker starter has not
  undergone listening QA or been approved for training/cloning; it is not the
  proposed multi-speaker pilot or a participant corpus.
- Five reproducibly sampled CSALT files received automated signal inspection
  (seed 20261003): all were readable 16 kHz mono 16-bit PCM and matched stored
  hashes; none had samples near digital full scale. Four exceeded the final
  6-second clip limit; two had substantially lower RMS levels than the others.
  Report `signal-inspection-five.json` is alongside the external download.
  No audio-listening tool was available: intelligibility, actual language,
  background noise and audible distortion remain unassessed. Screening thresholds
  are heuristics, not validated acceptance criteria. Originals were unchanged.
- Genuine-audio intake source register, CSV template and stdlib validator in
  `data/collect/`. This is metadata groundwork, not full audio QA or a dataset.
- Astra expanded regression cases and fixed malformed quoted CSV acceptance.
- Shared `AGENTS.md`, Claude entrypoint, role contracts, native agent adapters,
  BMAD Codex skill mirror and drift checks.
- Playwright CLI/crawler and controlled browser fixture tests. No AVA UI exists.
- Context export, repository artifact guard, pre-commit installer and CI workflow.
- Root PRD/architecture pointers preserve a single canonical location.

## Evidence and limits

Integrated QA: 36 Python tests passed (26 intake and 10 foundation tooling).
The final browser implementation agent run passed 10 Playwright fixture tests on
Chromium, including failed-resource and trace-write regressions. These results
cover infrastructure and metadata only. Foundation checks validated eight roles,
16 adapters and the 29-skill BMAD mirror; the repository artifact guard passed.
See `docs/context/handoffs/foundation-setup.md` for commands and limitations.

Tooling provisioned: Python 3.11.15, Node 22, Playwright 1.63.0/Chromium. Existing
BMAD version is 6.12.0; it was not upgraded. Native agent files require client
discovery/reload; static file validation is not proof of a launched native role.
An actual gpt-6-astra agent authored the test catalogs during this session.

## Not implemented or not established

No AVA API, dashboard, live tap, shared audio-conditioning runtime, trained AVA SLS
model, ONNX benchmark, collected participant corpus or final ML metrics exist.
No test pass establishes model quality, eight-call capacity, deployment readiness,
compliance certification, or freedom from future bugs. The final test set is not built.
The CI workflow is authored locally; a hosted CI run requires pushing the changes.
These setup results do not imply hosted CI or remote publication. See the current
user instruction and local-worktree note at the top before any Git actions.

## Existing workspace state to preserve

At setup start, develop was ahead 2/behind 2 of cached origin/develop. BMAD PRD and
architecture had user modifications; docs and ava-viva were untracked. Intake work
was also uncommitted. Do not discard these or create worktrees assuming they are in
HEAD. Review/stage a coherent baseline before starting concurrent writer worktrees.

GitHub read access worked once the old sandbox network restriction was removed;
the earlier auth failure report was not reliable evidence of invalid credentials.
Do not log credential values or put machine-specific paths into shared instructions.

## Next bounded work

Current step: todo7, a small `pnb_Arab` train-shard pilot. Metadata preflight is
in `docs/context/handoffs/pnb-source-preflight.md`: pinned official source,
CC BY4.0, six train speaker labels, long source recordings and FLAC/shard lineage
requirements. Use one verified train shard first; preserve publisher dev/test.
No cloning consent or exact Majhi coverage inferred. No final split assignment.
Commits, pushes and PR creation still await user instruction.

Current todo sequence: items 1–3 (QA policy, speaker coverage, consolidated
inventory), item4 (consistent preparation) and item5 (separate synthetic benchmark)
and item6 (CPU smoke) complete. Items7–9 remain: Punjabi pilot, approved
original collection, and reproducible speaker/lineage-disjoint splits.

1. Review/integrate this foundation on an issue-linked branch (existing #3/#4 cover
   repository/developer setup; source intake relates to #6/#11).
2. Confirm recording ethics body/protocol and separate cloning consent; prepare
   before collecting. No outside messages have been sent.
3. Verify permitted source access, especially pnb_Arab availability, and implement
   a small source importer plus decoded-audio QA with fictional tests.
4. Run a CPU pretrained checkpoint smoke test, preserving its classifier and
   recording class order, timing and memory; do not claim random SLS heads detect fakes.
5. Implement a first real batch API/UI vertical slice before product Playwright cases.

Some existing issue titles still use v0.1 numbering or Common Voice pa-PK. Use the
latest PRD and reconcile issue scope explicitly. The team plan Page is supplementary;
this repository's requirements and verified state are the cross-model starting point.
