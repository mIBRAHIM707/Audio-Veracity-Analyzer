# AVA autonomous software backlog

Approved 6 November 2026, noon–2 PM Asia/Karachi. Daily work may commit/push only
to feat/4-daily-development and its dedicated draft PR; never merge. This is the
automation queue; docs/context/todo.md still tracks participant/data work.

Mark a task complete only with implementation, relevant tests and independent QA.
Since 8 November 2026 the runner uses Claude Code; Claude designs test cases and
labels them Claude-authored (Codex/Astra access ended). Playwright runs browser
cases. Each phase ends with a requirement-coverage review; no phase is promised to
fit one day. Scope: every task here is independent of the participant dataset;
items needing that data are listed at the end and stay out of the queue.

## 0 — Reliable daily runner (#4)
- [x] 0.1 Isolated worktree, bounded Windows schedule, overlap guard, dry-run,
  deadline cleanup, pause/complete markers, tests and live setup verification.
  Evidence: 23 Astra tests passed, independent review closed, authenticated CLI
  smoke passed; registered task outside-window action returned 0. Next run Nov 7.
- [x] 0.2 Switch runner from Codex to Claude Code (8 Nov 2026): 7 Nov run failed
  because the ChatGPT-account Codex cannot use gpt-6-astra; 8 Nov missed because the
  PC was asleep. Claude CLI headless with acceptEdits plus a shell allowlist (no
  permission bypass; merge and force push denied), late wake allowed but runner
  window unchanged. 25 scheduler tests pass (2 new Claude-authored).

## 1 — Finish existing foundations (#11, #18, #19)
- [x] 1.1 Parameterized prepared-output verifier; segmentation/metrics/flags/hash checks.
  Evidence (9 Nov): `scripts/verify_prepared_output.py`, 40 Claude-authored tests,
  real read-only PASS on three external prepared outputs, independent review
  findings fixed. Exact-mode pilot v2 lacks its input manifest; fixtures only.
- [x] 1.2 Punjabi importer follow-ups and meaningful Windows fixture cleanup fix.
  Evidence (9 Nov): review items L3/L4/L7/L8 closed, 8 Claude-authored pnb tests
  plus 4 cleanup tests; tests/pnb 36 OK twice, tests/unit 167 OK twice; real pilot
  verifier PASS under the new selection rule; independent review lows fixed.
- [x] 1.3 Split cases (Claude-authored since Astra access ended) and independent
  review; annotate duplicate originals. Evidence (9 Nov): spec decision 7 implemented
  (schema 2) plus decision 9; 16 Claude-authored cases + 20 updated baseline tests;
  todo 9d annotation with real read-only count (2 hashes, 4 rows); review fixed.
- [x] 1.4 Versioned policy migration and pilot split rerun, preserving originals.
  Evidence (9 Nov): `migrate_policy.py` + 8 Claude-authored tests, review fixed; 3 manifests
  migrated, re-prepared (450/50/187 clips) and verified PASS; schema-2 proposal
  validated (687 rows, zero crossings, 637 trainable). Originals and v1 kept.
- [x] 1.QA Review evidence and remaining source/identity limitations.
  Evidence (9 Nov): `docs/qa/phase-1-qa.md`, independent conditional pass as
  infrastructure; no data/product acceptance; medium documentation findings fixed,
  PRD FR-D11 wording and duplicate-identity cloning rule sent to Tughral.

## 2 — Working upload/scoring prototype (FR-A1/A2, FR-U4)
- [x] 2.1 Bounded analysis service wraps existing trained CPU checkpoint; no runtime download.
  (The checkpoint is the pretrained AntiDeepfake baseline, not AVA-trained.) Evidence
  (9 Nov): `services/inference/`, 19 Claude-authored tests, real-checkpoint smoke on
  two clips, independent review findings fixed. Open: file-mode mean vs EMA and
  partial-window evidence rule to record in the PRD.
- [x] 2.2 FastAPI upload/results contract; malformed/oversized input and failure cases.
  Evidence (9 Nov): `services/api/app.py`, pinned `requirements-api.txt`, 7
  Claude-authored TestClient tests (sync, 202 job, errors 404/411/413/422/500/503,
  shutdown), independent review fixed. In-memory jobs only; limits in its README.
- [x] 2.3 Local React/TypeScript upload/results view with uncalibrated baseline label.
  Evidence (14 Nov catch-up 2, after the owner's install decision lifted the 9 Nov
  blocker): `dashboard/` React 18.3.1 + TypeScript 5.6.3 + Vite 5.4.11, exact pins and
  lockfile; FR-U4 drag-and-drop/choose, job polling with bounded reads, verdict,
  uncalibrated label, scope, FR-U3 SVG timeline + table, §1.4 limitations, same-origin
  CSP; served by the API (`create_app(..., dashboard_dir=)`). 13 Claude-authored
  Playwright cases on the real API with a fake detector (Playwright project
  `dashboard`, `npm run test:dashboard`) + 6 mount unit tests; independent review
  (H1 CI pickup, M1 validation, M2 unbounded poll, lows) fixed and re-reviewed. Real
  checkpoint/public audio through the page is 2.QA.
- [x] 2.QA Actual public-pilot audio through browser/API/model; Playwright negative cases.
  Evidence (14 Nov catch-up 2): `tests/browser/real-model-dashboard.spec.cjs` (project
  `real-model`, local gitignored `.local/real-model-qa.json`): pinned checkpoint, `/readyz`
  warm-up, public CSALT genuine/synthetic concatenations, short clip, >60 s job, damaged
  files; page-equals-API parity (verdict, mean, model, flags, duration, reason, every
  row); 5 Claude-authored cases pass with the 13 fake-detector cases (negative paths).
  Independent review conditional pass; fixed vacuous pass without config, audio in
  failure traces, clip paths in errors, setup budget, Windows tree kill, strict WAV
  reader. Infrastructure evidence only: no accuracy claim (uncalibrated baseline).

## 3 — Shared audio path (FR-D6/D12/D13, FR-I3/I4)
- [ ] 3.1 Shared offline/live conditioning parity; speaker-turn boundary contract.
  Progress (9 Nov): file-mode parity tests in `tests/unit/test_conditioning_parity.py`
  (prepare vs analysis service, 5 formats, refusals, gain divergence recorded).
  Remaining: speaker-turn boundary contract (not defined in PRD/architecture; needs
  a scope definition) and live/streaming-side parity.
  15 Nov: tap sink stream resampling bit-identical to `canonicalize` (test_tap_scoring_sink).
  19 Nov: chain parity (`tests/unit/test_train_serve_chain_parity.py`, Claude-authored):
  an `augment.py` mu-law copy with seeded loss and the same packets as RTP through the
  real TapApp, jitter buffer, sink, SpeechFeed and pipeline reach the detector as the
  same samples in every window, with matching concealment flags and concealed shares;
  forced 4/5-packet bursts too. Review conditional pass, fixes re-reviewed (pass).
  Known divergences, recorded not fixed: first/last-packet loss, A-law silence (+8),
  per-clip peak normalization in preparation vs none at serving (3.QA M1). Remaining
  for 3.1: only the speaker-turn boundary contract (scope decision).
- [x] 3.2 VAD, speech-only windows, no-speech/partial-window semantics.
  Evidence (14 Nov catch-up, after the owner's install decision superseded the 9 Nov
  VAD blocker): windower, Silero adapter and `services/scorer/speech_feed.py` below,
  30 Claude-authored unit tests + 3 model tests, three independent reviews fixed; unit
  suite 662 OK (3 skips). Open PRD questions (not blockers): 1.5-2.5 s tails, no_speech
  delay/level/over-alert/numeric field, audio-time rule; Linux check of VAD pins. Done:
  `services/scorer/speech_windows.py` VAD-gated speech-only windows with the file-mode
  plan, partial/scorable rule, no-speech timing, VAD fault drop+reset; 11
  Claude-authored tests with fake VADs; review fixed. Then `services/inference/
  silero_vad.py` Silero ONNX adapter (onnxruntime 1.32.0 in `requirements-vad.txt`;
  silero-vad 6.2.3 `silero_vad.onnx`, MIT, SHA-256 pinned, at `D:/AVA-Data/models/
  silero-vad/6.2.3/`): 5 unit + 3 model tests incl. frame-by-frame parity (atol 1e-5)
  with the reference wrapper; review fixed. Then `SpeechFeed`: scorable windows to
  `LivePipeline.offer`, call-level `no_speech` once per quiet spell, tails before the
  end, VAD speech seconds for the scored directions, `sweep` after `end_all`, clock/
  late-audio/factory faults contained; 14 Claude-authored tests; review fixed. The tap
  that feeds it is 6.x.
- [x] 3.3 G.711/GSM and AMR-NB file codec pipeline; loss/jitter and source lineage.
  Completed 14 Nov (catch-up 3) as file-pipeline infrastructure; evidence below.
  Progress (9 Nov): `data/prepare/codecs.py` G.711 mu/A-law + 8 kHz roundtrip with
  lineage; 10 Claude-authored tests (exhaustive audioop oracle). Seeded 0-5 % packet
  loss with concealment; 12 Claude-authored tests. Remaining: GSM, AMR-NB (need a
  codec library; none installed), manifest wiring (must pass the concealment mode
  explicitly). 10 Nov: architecture §2.2 `repeat_then_silence` concealment (3 repeats,
  then silence; hard cut, no fade) with `repeat_limit` lineage.
  Per-clip rate/seed derivation (`derive_loss`) added 9 Nov. Jitter as late-packet
  loss with per-clip plans (9 Nov); 10 Nov: loss and jitter plans combined in
  `telephone_roundtrip` as one concealed mask, 13 Claude-authored tests. Open spec
  question: whether serving should fade rather than hard-cut after the repeats.
  11 Nov: manifest wiring done as a separate stage, `data/prepare/augment.py` (noise
  -> G.711 with loss/jitter plans -> 16 kHz; explicit concealment; content-based
  derivation ID; copies not counted; read-only whole-report rebuild `--verify`); 15
  Claude-authored tests, review fixed. 14 Nov (catch-up 2): `data/prepare/
  ffmpeg_codecs.py` GSM 06.10 and AMR-NB 4.75/7.4/12.2 roundtrip through pinned ffmpeg
  9.0.2 (winget, GPL-3.0, hash recorded) with known-answer frame/mode checks, measured
  AMR frame rule and 40-sample delay compensation, lineage with exact args; 19
  Claude-authored tests (7 real-ffmpeg), review fixed. Then wired into `augment.py`
  (codec `gsm`/`amr_nb_*` with explicit no-loss spec, injected tool, `--ffmpeg`/
  `--ffmpeg-sha256`, abort before writing on tool failure, tool-hash check in verify,
  training loader `ffmpeg=`); 10 Claude-authored tests, review fixed.
  Then AMR-NB seeded frame loss (NO_DATA frames, decoder-native concealment, G.711 mask
  rule and plans); 5 Claude-authored tests, review fixed; selectable in `augment.py`
  specs (`packet_loss: true` for AMR-NB). GSM frame loss by repeat_then_silence frame
  substitution, also selectable; then jitter (late frames) for GSM/AMR joined with loss
  in one concealed mask, G.711-compatible record names; Claude-authored tests, reviews
  fixed. 14 Nov (catch-up 3): per-clip codec mix (`codec_mix` spec of 2-8 weighted
  channels, seeded per-clip choice recorded as `chain.codec_choice`, per-codec counts,
  loader codec per window; single-channel specs pinned unchanged against the pre-mix
  code); 9 Claude-authored unit tests + 1 model test, review conditional pass fixed. Remaining: Opus/G.729 (3.4), AMR decode of RFC 4867 RTP payloads (6.x).
- [ ] 3.4 Other PRD codec/noise paths with bounded reproducible fixtures.
  Progress (9 Nov): `data/prepare/noise.py` seeded additive noise at 0-20 dB SNR
  (FR-D7) with lineage and per-clip plans (`derive_noise`/`mix_planned`); 18
  Claude-authored tests. Remaining: noise corpora, RIRs, speech-active SNR (needs VAD, 3.2), Opus/G.729
  VoIP path. 11 Nov: noise wired through `augment.py` (noise file named by PCM hash,
  source ID and licence; clipping refusals keep the drawn SNR for bias audits).
  15 Nov: Opus VoIP path (FR-D6 "Opus at 8 to 16 kbps or G.729"; ffmpeg has no G.729
  encoder): `opus_8/12/16` in `data/prepare/ffmpeg_codecs.py` (libopus CBR 20 ms VoIP,
  strict Ogg/OpusHead/TOC checks with page CRCs, 48 kHz decode, measured 4-sample delay,
  soxr to 8 kHz), seeded loss/jitter as TOC-only packets concealed by libopus PLC,
  selectable in `augment.py` specs and mixes; 20 Claude-authored tests
  (`tests/unit/test_ffmpeg_opus.py`), review conditional pass, lows fixed. 15 Nov:
  speech-active SNR (`noise.speech_mask` with the serving VAD frame rule; spec
  `noise.snr_basis: speech_active` + `vad_sha256`; writer/verifier/loader take the VAD;
  VAD faults systemic; insufficient-speech refusals tagged and counted; mask rule and
  per-frame decisions recorded); 13 Claude-authored unit + 1 real-Silero model test;
  review conditional pass, findings fixed. 16 Nov: room impulse responses
  (`data/prepare/reverb.py`: first-arrival direct path, polarity normalised, level-
  preserving FFT convolution, DRR/energy lineage; optional `reverb` spec of 1-64
  hash-pinned RIRs, seeded pick per clip before noise, `--rir`, unusable RIR stops the
  run, `reverb_clipping` refusals, loader `rir_paths`); Claude-authored tests, review
  conditional pass, findings fixed. Remaining: provisioning SLR28 RIRs/noises (blocked:
  this session cannot write to `D:/AVA-Data`), RIR preparation (channels, trimming,
  per-database licences), handset/speakerphone labelling (PRD question).
- [ ] 3.QA Sample-level parity, malformed formats and immutable-original checks.
  Progress (14 Nov, catch-up): `docs/qa/phase-3-qa.md` independent conditional pass as
  infrastructure for the implemented scope; L1-L6 fixed (one plain-PCM fmt rule in
  `data/prepare/wav_format.py` on every entry point plus a verifier mirror, truncated
  header routing, nested JSON, non-empty quarantine errors, copies start unreviewed),
  14 Claude-authored tests; re-review pass. Box stays open while 3.1-3.4 are partial;
  M1 (§2.2 8 kHz batch rule and normalization unit) awaits a PRD/architecture decision.

## 4 — Streaming decisions (FR-I4–I8)
- [x] 4.1 Configuration validation and call-scoped reload behavior.
  Evidence (10 Nov): `services/scorer/config.py` (FR-I4 rules, §7.3 defaults, JSON
  loader, serialized atomic reload with fingerprint), 22 Claude-authored tests incl. a
  mutation-checked overlapping-reload case; independent review fixed. No YAML/Pydantic.
- [x] 4.2 EMA, minimum evidence, hysteresis and undetermined outcomes.
  Evidence (10 Nov): `services/scorer/session.py` (§2.4 FSM, provisional/undetermined,
  ended_open, new alert IDs, atomic commit), 25 Claude-authored tests incl. a 300-trial
  comparison with an independent reference model; review fixed. File-mode mean vs
  EMA and the ended_open-in-suspicious wording remain PRD questions.
- [x] 4.3 Quality flags, direction selection and agent-direction semantics.
  Evidence (10 Nov): `services/scorer/call.py` (caller default, agent after human
  answer, >10 % concealed windows not scored with quality=low, more severe direction
  drives, call verdict), 17 Claude-authored tests with hand-worked timelines; review
  fixed. Interpretations (sticky quality, tie to caller, verdict rule) await the PRD.
- [x] 4.QA Boundary/state-transition/concurrency tests and independent review.
  Evidence (10 Nov): `docs/qa/phase-4-qa.md`, independent conditional pass as
  infrastructure; M1 concurrency contract documented, L1/L2 fixed with tests, L3 and
  the reorder/duplicate boundary check assigned to backlog 5; ten PRD questions listed.

## 5 — Backend persistence/events (FR-A, FR-I11)
- [ ] 5.1 PostgreSQL/Redis data and event contracts, migrations and bounded queues.
  Progress (10 Nov): `services/events/` FR-A4 envelope contract and bounded
  non-blocking fan-out, 16 Claude-authored tests, review fixed. (The 10 Nov driver/
  Redis blocker is superseded by the 14 Nov install decision and Docker.) 15 Nov:
  `services/store/` versioned SQL migrations (one transaction, advisory lock, checksum
  drift refused, LF-normalised, transaction control refused) and `0001_live_records`
  (`calls`, `window_scores`, `alerts`); `PostgresEventStore` writes each FR-A4 envelope
  per hop (idempotent replays, `after_end` like the registry, implicit starts,
  retryable vs permanent errors); psycopg 3.3.9 in `requirements-store.txt`; 22
  Claude-authored tests (15 on a PostgreSQL 16.16 container); review M1-M4 and lows
  fixed, re-review pass. 15 Nov (dc9e8d1): `services/store/pump.py` StorePump, an
  ordered hub-to-PostgreSQL pump (retryable errors keep order and wait, permanent ones
  are skipped and counted, a bounded backlog, a faulty head skipped after bounded
  retries); Claude-authored tests, review fixed. 16 Nov: the live command line runs
  the pump (and the Redis forwarder) on its own thread (`services/live/background.py`
  PumpThread; `--database-url-env`/`--redis-url-env`, migrations applied at start-up,
  bounded drain on stop); lab: a real call's windows are in PostgreSQL while it runs.
  Remaining: scorer resume (FR-I11), other architecture tables, Redis session state.
  17 Nov scope note: architecture §2.4 resume assumes the scorer restarts apart from the
  `tap-receiver`; here tap and scorer share one process and a restart ends open calls
  `monitor_lost` (§2.4), so resume needs a topology decision (split process, or resume
  re-tapped calls) before it can be built.
  19 Nov (attempt 4): FR-A3 "full history in PostgreSQL": `services/store/history.py`
  read-only reader (REPEATABLE READ, READ ONLY; sessions newest first with alerts and
  window scores; bounds incl. a 10,800-row window budget per answer; busy/timeout/
  schema/unavailable reasons) and API routes `GET /v1/calls/{id}/history`,
  `GET /v1/history/calls` (keyset paging), `--database-url-env`, history metric;
  Claude-authored tests (PostgreSQL 16); review conditional pass, M1-M2 and lows fixed.
  Remaining: Compose/k3s wiring with a read-only role, dashboard use, durable job table.
  20 Nov (attempt 4): Compose/k3s wiring with a read-only role. `services/store/roles.py`
  and `python -m services.store reader-role` (migrate, then `ava_reader`: SELECT on the
  four history tables only, read-only default, settings and stray rights removed, PUBLIC
  TEMP/large-object creation revoked with an outcome check, SCRAM verifier, unsafe roles
  refused); Compose one-shot `db-setup` + API as `ava_reader`; k3s `db-reader-role` init
  container, `db-reader-password`, api->postgres policy; API image + psycopg.
  Claude-authored unit (PostgreSQL 16, SCRAM), static, Compose and k3s lab cases; review
  and re-review findings fixed. (Dashboard use was done 19 Nov, attempt 6.) Remaining:
  durable job table (5.2 scope decision), scorer resume (topology decision).
- [ ] 5.2 Jobs/cancellation/idempotency, calls/alerts API and WebSocket events.
  Progress (10 Nov): `services/scorer/ordered.py` per-call serialized boundary
  (phase-4 QA M1/L3): monotonic per-direction `window_idx`, duplicate/stale results
  rejected and counted, copied events; 12 Claude-authored tests incl. a deterministic
  lock test; review fixed. Open: index restart after a stream reconnect (PRD/6.x).
  14 Nov: FR-A3 `services/calls/registry.py` read model (30 min full resolution, 10 s
  buckets, alert lifecycle, bounds, stale/idle eviction) and API `GET /v1/calls`,
  `/v1/calls/{id}`, `WS /v1/events` on the in-process hub; 25 Claude-authored tests,
  two review rounds fixed. 14 Nov: scorer-to-FR-A4 mapping (`services/scorer/
  publish.py`) and live call table (`services/scorer/live.py`: gate admission with
  capacity health event, call-scoped config, ordered publish per call, FR-I11 late
  result discard, monitor_lost end_all); 29 Claude-authored tests, reviews fixed.
  Remaining: job cancellation/idempotency (not in the PRD; needs a scope decision),
  Redis pub/sub and durable state (5.1), wiring to the tap/inference loop (6.x/8.1).
  15 Nov: `services/events/redis_bridge.py` Redis pub/sub forwarder (hub -> channel,
  faults counted, one attempt per pump when Redis is down) and relay (untrusted
  messages re-validated, source `ts` kept, seq gaps/restarts counted, resubscribe after
  errors); `EventHub.publish(ts=)`; redis-py 8.1.0 in `requirements-redis.txt`; 17
  Claude-authored tests (3 on a Redis 7.4.12 container); review conditional pass, M1 and
  lows fixed. 16 Nov: forwarder thread in the live service (see 5.1). 16 Nov (attempt 2):
  relay loss reported to the hub (`EventHub.report_upstream_loss`, `drain_batch`: gaps,
  source restarts, one interruption per Redis outage, hub refusals), `RelayLoop` thread
  (`services/events/relay_loop.py`, backoff, bounded stop), `create_app(..., relay=)`
  with `stream.gap` `upstream_missing`/`upstream_interruptions`, registry upstream
  counters and `ava_event_relay_*` metrics; Claude-authored tests incl. a real-Redis
  WebSocket case; review conditional pass fixed. Then `python -m services.api`
  (`services/api/__main__.py`: loopback by default, bind before announcing,
  `--redis-url-env` relay, graceful SIGTERM/Ctrl+Break with a `stopped` record); 12
  Claude-authored tests incl. child processes with real uvicorn and Redis; review fixed.
  17 Nov: source epoch (`EventHub.epoch` sent as `source` by the forwarder; the relay
  detects restarts exactly, also with lost first events, and drops non-advancing seqs
  within an epoch as `out_of_order`); Claude-authored tests incl. real Redis; review
  fixed. Remaining: job cancellation/idempotency (scope decision).
- [ ] 5.3 Restart recovery, in-flight handling and default no-audio-retention behavior.
  Progress (12 Nov): default no-raw-audio retention on the analysis API verified by 3
  Claude-authored tests (spooled uploads removed with rollover proven, no audio in
  job records after completion or shutdown, small filename-free responses); review
  fixed. Remaining: restart recovery and in-flight handling (need durable jobs, 5.1),
  FR-O5 opt-in snippet retention on the live path. 14 Nov: `services/retention/
  snippets.py` (policy fixed at call start, 30 s ring per direction, one sealed file
  per alert ID, TTL and purge-when-off cleanup, failures contained) with Claude-authored
  tests. 14 Nov (catch-up, after the owner's install decision): AES-256-GCM on pinned
  `cryptography==50.0.2` (`requirements-retention.txt`), key file outside the repo,
  `open_snippet` authentication, fail-closed TTL headers; 11 Claude-authored tests incl.
  NIST GCM known answers; review fixed. 17 Nov: wired into the live service
  (`services/live/retention.py` SnippetRecorder: 30 s ring of tapped caller audio per
  admitted call, one sealed snippet per `alert.raised` written on its own pump thread,
  TTL/purge cleanup every 10 min, `--retention off|on`, `--retention-dir`,
  `--retention-key-file`, `--retention-ttl-days`, live metrics); 20 Claude-authored unit
  tests + 1 lab case (real call, encrypted snippet sample-identical to the injected
  audio); review fixed. Remaining: key provisioning/rotation (operator decision),
  restart recovery and in-flight handling (scope, see 5.1 note).
- [ ] 5.QA Real service integration, disconnects, failures and state recovery.
  Progress (14 Nov): in-process live-path integration test
  (`tests/unit/test_live_path_integration.py`: pipeline -> LiveCalls -> hub -> API
  registry/WebSocket/metrics, capacity refusal, monitor_lost, detector outage).
  16 Nov: `tests/lab/test_live_storage_lab.py` real Asterisk call through the command
  line with real PostgreSQL 16 and Redis 7: stored windows equal the published ones,
  flushed while the call runs; the Redis channel carries the same sequence.
  16 Nov (attempt 2): the API side reading from a real Redis (forwarder -> Redis ->
  relay thread -> WebSocket; `CLIENT KILL` reaches the client as `stream.gap`).
  Remaining: process restarts and state recovery.
  18 Nov (attempt 5): live-process restart during a real call in the Compose lab
  (`test_a_live_restart_mid_call_never_stops_the_call`, Claude-authored): the call
  completes; the old process ends it `monitor_lost`, the new one re-taps it and scores it
  to `bye`; two PostgreSQL rows; the API subscriber gets a `stream.gap` with
  `upstream_interruptions`. Defect found and fixed: the API call registry ignored the
  re-tapped session (showed the call ended), and so did the browser live board; a later
  `call.started` now opens a new session in both (`new_session`), as the store does,
  keeping the earlier session's alerts. Review conditional pass fixed. Remaining: API,
  Redis and PostgreSQL restarts; an abrupt (SIGKILL) live stop, which sends no
  `call.ended`, so the registry and the store ignore the new session as stale or
  duplicate (needs the relay's source epoch passed to consumers: design step).
  18 Nov (attempt 6): PostgreSQL and Redis restarted during a real call in the Compose
  lab (`test_postgres_and_redis_restarts_mid_call_lose_no_stored_window`,
  Claude-authored): the call completes and is never lost by live (one `bye`, no re-tap,
  no live restart); the store pump waits through the outage and then stores every scored
  window (rows equal the live counter, backlog 0); the API relay resubscribes and reports
  the Redis outage as a `stream.gap`; the registry ends the call `bye`. No defect found.
  Then an API restart during a real call (`test_an_api_restart_mid_call_never_stops_the_call`,
  Claude-authored): live never notices; the new API resubscribes, a new WebSocket client
  gets the rest of the call, and the registry creates the call from its next event and
  ends it `bye`. No defect found. Remaining: abrupt live stop (design step above).
  18 Nov (attempt 7): abrupt stop, API side. The API hub renumbers relayed envelopes, so
  the new session was merged into the old record (old alerts left open), not ignored. A
  `call.started` with a higher `seq` and a later `ts` than an active call's start now
  supersedes that session in the registry and the browser board (`superseded_session`);
  Claude-authored unit and Playwright cases. Remaining: the PostgreSQL store's duplicate
  start after a kill (needs the source epoch or a start-up sweep: design step) and a
  SIGKILL lab case.
  19 Nov: store side done without the epoch. Migration 0002 (`end_inferred`,
  `start_event_at`, backfilled for open rows; the speech-seconds check relaxed only for
  inferred `monitor_lost` ends); a start strictly later than the open session's start
  supersedes it (old row `monitor_lost`, inferred, no speech seconds; open alerts
  `ended_open`); `ava_live_store_superseded_total`; architecture §2.4 interim note.
  Claude-authored PostgreSQL tests (incl. an upgrade with rows) and the Compose lab case
  `test_a_killed_live_process_never_stops_the_call` (SIGKILL mid-call: the call
  completes; registry supersedes; two stored rows). Remaining for 5.QA: none known for
  restarts; scorer resume from persisted state stays a topology decision (5.1 note).

## 6 — Asterisk laboratory (FR-T1–T5/T7, FR-I1/I2)
- [ ] 6.1 Docker/WSL preflight; Asterisk/SIPp endpoints and WAV injection.
  Progress (15 Nov): Docker Desktop running. `lab/asterisk/` image (ubuntu:24.04 digest,
  Asterisk 20.6.0 and SIPp 3.7.2 pinned), PJSIP anonymous endpoint (ulaw/alaw/gsm),
  record-what-the-caller-sends extension, SIPp single-codec offer scenario,
  `rtp_pcap.py` (PCM/WAV -> G.711/GSM RTP pcap, CLI), `harness.py` disposable container;
  `.pcap`/raw telephony extensions blocked by the guard. Claude-authored tests: 5 pure +
  2 guard (CI) and 4 Docker lab cases (mu-law/A-law sample-identical, GSM > 0.99, G.722
  refused 488); review conditional pass, M1-M4 and lows fixed. 17 Nov: two SIPp endpoints
  in a bridged call with PBX-forced codecs (`agent_alaw`/`agent_gsm`, extensions 810/811,
  `sipp/uas_agent.xml` echo UAS, MixMonitor on both legs); 3 Claude-authored lab cases
  (forced offer and native format, sample-identical A-law and GSM paths both ways,
  unreachable agent `NOANSWER`); review fixed. 17 Nov: read-only Docker/WSL preflight
  `python -m lab.preflight` (8 checks incl. the lab test env, `AVA_LAB_IMAGE` and the
  ffmpeg pin; exit 1 on failure); 19 Claude-authored unit tests with fakes; review fixed;
  8 PASS on this host. Remaining: Compose file, a softphone endpoint.
  20 Nov (attempt 6): authenticated `softphone` PJSIP endpoint, present only when the run
  passes `AVA_SOFTPHONE_PASSWORD` (same rules as ARI): digest auth, one registered
  contact, ulaw/alaw/gsm, `from-softphone` context, extension 702 dials it (tapped
  caller); identifier order pinned; credential umask no longer inherited by Asterisk.
  SIPp register/auth-call/probe scenarios; 10 Claude-authored lab cases
  (`tests/lab/test_softphone_lab.py`) incl. wrong password, foreign-From REGISTER,
  challenge without credentials, ACK on the INVITE transaction (defect found and fixed),
  sample-identical audio both ways, CHANUNAVAIL without a contact, weak passwords refused;
  review conditional pass, all findings fixed. The Compose stack exists (`deploy/compose`,
  17 Nov). Remaining: a real softphone application (needs a person and a published port).
- [ ] 6.2 ARI passive tap, lifecycle and RTP G.711/GSM decoder.
  Progress (15 Nov): `services/tap/` ARI client, per-call RTP receiver (source lock,
  bounded hand-off) and `TapApp`: calls never enter Stasis (review H1: a Stasis hand-off
  makes calls wait on the tap); answered PJSIP calls are snooped from outside (verified
  on Asterisk 20.6), ExternalMedia mu-law to the receiver, `ava-tap` bridges swept after
  a crash; only the dialplan-marked inbound caller leg is tapped (`AVA_TAP=caller`);
  `resume()` re-taps after reconnect; lab ARI enabled by a per-run password.
  Claude-authored: 17 unit tests (CI) and 6 lab tests (tapped audio sample-identical
  after <= 200 ms start-up; ended < 5 s with nothing left in Asterisk; agent leg not
  tapped; calls unharmed with no tap, a stopped, a hung and a crashed tap). Two
  independent reviews (H1 Stasis dependency, H-A1 agent leg, M1-M5) fixed.
  Answers the Phase 0 tap spike. 15 Nov (b7f2bd2): `services/tap/scoring_sink.py` feeds
  tapped audio into LiveCalls/SpeechFeed/LivePipeline (per-call soxr stream equal to the
  file path's resampling, concealment mask at 16 kHz, ends after the tail is scored).
  15 Nov (cee5ffb): `services/live/service.py` service process with ARI reconnect.
  20 Nov (attempt 7): architecture §2.1/§3/§6 and PRD FR-T2 now describe the
  no-Stasis tap (channel events, marked caller leg), recorded as an accepted change.
  Remaining: agent direction (FR-I8, P2; needs a "human agent answered" event design).
- [ ] 6.3 Jitter/loss/concealment and malformed RTP/SDP cases; AMR file/replay.
  Progress (15 Nov): `services/tap/jitter.py` JitterBuffer (60 ms, reorder, repeat 3 then
  silence = the training rule for mu-law; re-anchors after a sender pause or sequence
  jump without losing received frames; absorbs +/-100 ppm drift; bounded output) and
  LossMonitor (5 % over 10 s); 14 Claude-authored tests incl. parity with
  `codecs._conceal`; review H-B1/M-B1/M-B3 fixed (not re-reviewed). 15 Nov (2f698b7):
  wired into the tap (conceals only real holes). 15 Nov (120c6e7): concealment mask
  through the windower to the FR-I2 >10 % window gate (`quality=low`, unscored); 8
  Claude-authored tests, review fixed. 16 Nov: hostile/malformed RTP
  (`tests/unit/test_tap_hostile_rtp.py`: seeded parser fuzzing, RTCP/DTMF/garbage on a
  real socket, output bounds). Found and fixed a ~124x concealed-audio amplification
  through resync hand-overs (a real-time budget for concealment only; received frames
  always played); review H1 fixed; Asterisk lab 7 OK. 17 Nov (attempt 2): AMR-NB
  file/replay through the lab (`tests/lab/test_amr_replay_lab.py`: public clip ->
  pinned-ffmpeg AMR round trip at 4.75/7.4/12.2 -> mu-law call -> ARI tap,
  sample-identical to the decode, impairment present); Claude-authored; review
  conditional pass fixed. Suspected AMR codec-path defect, issue #28: about 3 dB
  waveform SNR in every mode at any level. Do not report AMR-augmented results until it
  is resolved. Remaining: live AMR over RTP (P2, no open Asterisk codec); whether
  skipped holes count as FR-I2 loss (PRD); malformed SDP is Asterisk's (the tap parses
  none).
- [ ] 6.QA End-to-end public audio through lab; detector failure never drops calls.
  Progress (15 Nov): lab tests play three public CSALT genuine clips into a real
  Asterisk 20.6 call through tap, jitter buffer, real Silero VAD and the pipeline
  (b7f2bd2, fake detector), and through `LiveService` with the real 14 Nov static INT8
  export, untrained head (cee5ffb): 5 windows scored, one `call.ended reason=bye`.
  Calls unharmed with no, stopped, hung or crashed tap (6.2). 15 Nov (8387661): a
  detector that raises on every batch, and one that blocks the service thread from
  about 5 s into the call, never affect a real lab call (SIPp completes normally, one
  `call.ended bye`, no reconnect, nothing left in Asterisk); found and fixed `stop()`
  counted as a disconnect. Remaining: codecs other than mu-law end to end, the trained
  model.
  19 Nov: A-law and GSM callers end to end (Claude-authored lab cases): the call
  negotiates only the offered codec (read over ARI), Asterisk transcodes for the tap's
  mu-law ExternalMedia, and the tapped audio matches the decode (A-law 0.9999, GSM
  0.999). Public speech from those callers is scored through VAD and the pipeline
  (fake detector: plumbing only). Remaining: the trained model (data-dependent).

## 7 — Dashboard and operations (FR-U, FR-A8)
- [x] 7.1 Live board, alerts, timeline and batch job states.
  Evidence (16 Nov): `#live` board (`dashboard/src/live.ts`, `LiveBoard.tsx`): FR-U1
  cards, FR-U2 feed with banner/title/tone, FR-U3 per-call timeline; REST snapshot plus
  `WS /v1/events` merged by `seq`, gap/reconnect re-reads, jittered back-off. Batch job
  states from 12 Nov (DU03, DU11-DU13). 12 Playwright (LB01-LB12) + 10 pure (LS01-LS10)
  Claude-authored cases; review conditional pass, fixes re-reviewed. Not yet: a browser
  run against a real scorer/Redis relay (lab), Urdu/RTL text.
- [x] 7.2 Health/readiness/metrics and visible unmonitored/low-quality conditions.
  Progress (12 Nov): FR-A8 on the analysis API: `/readyz` with a validated warm-up
  inference (sticky once warmed, stays ready while busy), Prometheus `/metrics`
  without a client library (bounded route labels, job states, queue, uploads in flight,
  ready/loaded, analysis seconds); 13 Claude-authored tests, review fixed. Remaining:
  unmonitored/low-quality visibility (needs the live path), FR-O6 histograms and
  telephony metrics, dashboard (blocked with 2.3). 14 Nov: `ava_analysis_seconds` now a
  histogram, per-stage `ava_analysis_stage_seconds` (decode/condition/inference; results
  carry `stage_seconds`), live-call gauges/counters from the call registry (active,
  stale, open alerts, alerts raised, exclusive event outcomes, lost events,
  subscribers); 5 Claude-authored tests, review fixed. 14 Nov (catch-up 3): live
  pipeline window/queue/dropped-window metrics (`LivePipeline.metrics_snapshot` with a
  tested conservation invariant, thread-safe `PipelineStats`, `ava_live_*` families,
  snapshot age/validity); 12 Claude-authored tests, two review rounds fixed (incl. a
  lost window on a batcher clock error and reused call IDs after `end_all`). 14 Nov
  (catch-up 4, commit 1839139): live inference latency histogram
  `ava_live_inference_seconds{outcome}` with timer-failure counter; review fixed.
  16 Nov: live process `/healthz`, `/readyz` (ARI stream + loop turning) and `/metrics`
  (`services/live/health.py`, `--health-port`; pipeline families shared with the API via
  `services/scorer/metrics_text.py`, calls/capacity, tap/sink, pump and store health);
  lab: ready and one active call reported during a real call.
  16 Nov: RTP receiver and jitter-buffer counts (incl. `dropped_handover`) summed over
  ended calls on the live `/metrics`. 17 Nov: per-stage loop latency
  `ava_live_stage_seconds{stage=queue_wait|batch_wait|apply|window_to_result}` and
  `ava_live_alerts_raised_total` (`services/scorer/pipeline.py`, `metrics_text.py`); 11
  Claude-authored tests, review conditional pass fixed. 17 Nov: structured JSON
  operations log of the live process (`services/live/eventlog.py`, `--event-log`,
  allowlisted fields, rate limit, merged counter lines); 14 Claude-authored tests and a
  real-call lab check; two review rounds fixed. 17 Nov (attempt 2): tap-side stage
  timing per delivered chunk, `ava_live_tap_stage_seconds{stage=resample|speech_feed}`
  plus `ava_live_tap_audio_seconds_total` for the cost per audio second
  (`services/tap/scoring_sink.py`, `services/live/health.py`); Claude-authored tests;
  review conditional pass fixed. 17 Nov (attempt 2): structured JSON operations log for
  the analysis API (`services/api/oplog.py`, `--event-log stderr|off`: request lines
  with fixed route labels, rate-limited, probes skipped; job, detector-ready and
  relay-loss lines; bounded queue and writer thread); Claude-authored tests incl. a real
  uvicorn child; review conditional pass fixed (committed 18 Nov after a re-review pass).
  18 Nov: in-call RTP/jitter counts on the live `/metrics` (`TapApp.receiver_live`: ended
  calls plus open calls re-read at most every 1 s, never decreasing) and
  `ava_live_rtp_calls_degraded` (open calls in sustained loss); 9 Claude-authored tests;
  review pass, lows fixed. 18 Nov (attempt 2): Grafana (FR-O6). Compose profile
  `monitoring` adds digest-pinned Prometheus 3.16.0 and Grafana OSS 13.0.2 on an
  internal `metrics` network; `live`'s health endpoint binds only there. A provisioned
  dashboard covers each FR-O6 item and the degraded, unmonitored and loss conditions,
  with every call-home setting off. A Claude-authored CI test checks each query against
  the rendered `/metrics`, and the Compose lab test has a real call reach Prometheus and
  Grafana, with isolation probes. 18 Nov (attempt 3): jitter-buffer hold time
  `ava_live_jitter_hold_seconds` (socket read to handed to scoring, per received frame;
  integer counts in `JitterBuffer.stats`, ended plus open calls) and its p95 on the
  dashboard (includes loop-turn quantisation, excludes socket wait: not a §3 budget
  measurement); 13 Claude-authored tests, review conditional pass fixed, Compose lab
  40/40 queries with data. 18 Nov (attempts 4-5): WebSocket delivery timing on the API
  `/metrics`: `ava_ws_delivery_seconds` (envelope `ts` = scorer hub publish, to handed to
  the WebSocket transport, once per socket; includes the 0.2 s forwarder and 0.1 s socket
  polls, excludes network and browser render, one-host clocks),
  `ava_ws_delivery_unmeasured_total`, `ava_ws_slow_client_closes_total`; two dashboard
  panels; 11 Claude-authored tests; Compose lab timed all 7 events of a real call (one
  run, mean 151 ms); review conditional pass fixed. Done: every FR-O6 item has a metric
  and a panel. Not measured: browser render time and a load-test p95 (8.QA).
- [ ] 7.3 P1 history/search/local authentication/audit and feedback after P0.
  Progress (19 Nov, attempt 5): PRD FR-A6 `GET /v1/alerts` from PostgreSQL
  (`PostgresCallHistory.alerts`: state, raised-time range, minimum confidence = the
  alert's peak EMA from raise to close, uncalibrated; opaque exact keyset cursor) and
  `format=csv` export (`services/api/alert_csv.py`, RFC 4180, formula neutralisation,
  cursor in `X-AVA-Next-Cursor`), `ava_alert_search_reads_total`; Claude-authored tests
  (PostgreSQL 16); review conditional pass, M and lows fixed, re-review pass. Remaining:
  FR-U6 dashboard history/search, FR-A7 feedback, FR-A5, local auth/audit; PRD wording
  for "confidence"; cost of the peak query not measured.
  19 Nov (attempt 6): FR-U6 `#history` dashboard page (`dashboard/src/history.ts`,
  `HistoryPage.tsx`): stored sessions (status filter, `next_before` paging), alert
  search (state, UTC range, minimum peak-EMA confidence, cursor paging) with CSV export
  (`blob:` download, truncation stated), stored call detail (every session, inferred
  ends, alerts, timeline); 10 pure + 13 Playwright Claude-authored cases (HB01-HB07 on
  real PostgreSQL 16 via `serve_dashboard_api.py --history-dsn-env`). Remaining: FR-A7
  feedback, FR-U5, FR-U7 auth/audit, FR-A5; UTC-only times.
  19 Nov (attempt 7): FR-U7 local accounts and the PRD §6.3 audit log, API side:
  migration 0003, `services/auth/` (Argon2id, PostgreSQL store, login decisions, operator
  CLI), `python -m services.api --auth on` (session cookie, 401 on /v1, cross-site
  refusal, WebSocket check, audited logins/exports/uploads/account changes); Claude-authored
  tests incl. PostgreSQL 16; review and re-review conditional pass, all findings fixed or
  documented. Then the dashboard sign-in (`AuthGate.tsx`, `auth.ts`; Claude-authored
  Playwright cases on the real accounts in PostgreSQL 16). Remaining: admin settings
  (FR-A9), FR-A7 feedback, FR-U5, FR-A5.
  19 Nov (attempt 8): FR-A7 alert feedback, API and storage: migration 0004
  `alert_feedback`, `services/store/feedback.py` (label + copy of the alert's window
  scores + `alerts.feedback` audit row in one transaction, alert row locked, at most 20
  labels per alert, newest current), `POST`/`GET /v1/alerts/{id}/feedback` with accounts
  on; Claude-authored tests incl. PostgreSQL 16; review conditional pass, findings fixed.
  Remaining: dashboard feedback buttons (PRD 3.25 UI), admin settings (FR-A9), FR-U5,
  FR-A5; embeddings are not stored (snippets via FR-O5 retention instead).
  19 Nov (attempt 9): FR-A7 dashboard feedback (PRD 3.25 UI): `AlertFeedback.tsx`,
  `feedback.ts`, `SessionContext`; per-alert collapsed panel in the stored call detail,
  shown only to signed-in users; current and earlier labels, one POST per click (ref
  guard, never retried, unknown outcome stated), 20-label cap; Claude-authored FS01-FS05
  (pure) and FB01-FB04 on real PostgreSQL 16 + accounts, FN01-FN03 [mock]. Remaining:
  admin settings (FR-A9), FR-U5, FR-A5.
  19 Nov (attempt 10): FR-U5 retained-snippet playback: `services/retention/playback.py`
  `SnippetReader` (fixed reasons missing/expired/unreadable/error, WAV in memory), API
  `GET /v1/alerts/{id}/snippet` with accounts (audit row `alerts.snippet` before any
  audio, 404/410/500/503, 2-slot cap incl. the audit write, `no-store`,
  `ava_snippet_reads_total`), `--retention-dir/--retention-key-file/--retention-ttl-days`;
  dashboard `AlertSnippet.tsx`/`snippet.ts` (on request only, WAV checked, blob revoked;
  CSP `media-src 'self' blob:`, a defect SB01 found). Claude-authored: 24 unit cases,
  SS01-SS04 pure, SB01-SB04 on PostgreSQL 16 + production AES-GCM store, SN01-SN02.
  Review conditional pass, M1 fixed. Remaining: FR-U5 codec detected, FR-A9, FR-A5;
  Compose/k3s wiring of playback (key in the API, `cryptography` in its image).
  20 Nov: FR-A9 config file plus reload in the live process
  (`services/live/config_reload.py`, `--scoring-config-watch-s`): the file is reread on a
  pump thread, then validated; valid changes reach calls started afterwards (FR-I4).
  Process-wide keys (hop, degraded stride) are refused as "needs a restart". Outcomes go
  to the operations log and `/metrics`. Claude-authored tests. Remaining: FR-U5 codec
  detected, FR-A5, the optional FR-A9 endpoint (API, with accounts), Compose/k3s config
  mounts.
  20 Nov (attempt 2): FR-U5 codec detected. The tap reads `CHANNEL(audionativeformat)`
  over ARI once the tap is set up (1 s bound, failures contained, `codec_unknown`
  counted), `call.started` carries `codec`, the history reader serves it (JSON string
  and format-name rule only, nothing else from `start_data`), and the dashboard call
  detail shows "Codec detected". Claude-authored unit, PostgreSQL, Playwright (HS11,
  HB08) and lab cases (ulaw/alaw/gsm callers; a two-codec offer reported as the one in
  use). Review conditional pass, findings fixed. Remaining: FR-A5, the optional FR-A9
  endpoint, Compose/k3s config mounts.
  20 Nov (attempt 3): FR-A9 Compose/k3s config mounts. Live runs with the reload on (10 s)
  in both deployments. The shipped `scoring.json` holds the §7.3 defaults. Compose uses a
  read-only host folder mount (`AVA_SCORING_CONFIG_DIR`); k3s uses the `ava-scoring`
  ConfigMap with no hash suffix and no subPath. Claude-authored unit tests; a Compose lab
  case (host rename applied, hop refused, no restart); a k3s ConfigMap-patch lab step.
  Review conditional pass, fixed. Remaining: FR-A5, the optional FR-A9 endpoint.
- [ ] 7.QA Astra-designed Playwright suite, offline assets, failures/reconnects.
  Progress (21 Nov, attempt 2; Claude-authored, not Astra): `tests/browser/dashboard-recovery.spec.cjs`.
  RC01 kills the real API under an open board and restarts it on the same port. RC02
  does the same for the history page on PostgreSQL 16. RC03a scans the built files for
  off-origin loads and checks the served CSP. RC03b loads all pages with every other
  origin aborted. RC01 found a defect: after an API restart, a never-cleared alert of a
  continuing call showed as "Cleared". Fixed in `applySnapshot`, with pure case LS14.
  Remaining: the dashboard project is not in CI (a human workflow edit); sign-in,
  feedback and snippet pages across a restart; Astra-style catalogue review.
  22 Nov: signed-in pages across a real restart (`tests/browser/dashboard-auth-recovery.spec.cjs`,
  Claude-authored, PostgreSQL 16; the restarted server reuses the schema, snippet folder
  and key). AR01 live board keeps its session through a crash; AR02 feedback and snippet
  requests to the dead API are reported and never claimed or retried, then work after the
  restart with exactly one label and one playback audited; AR03 a session revoked on the
  restarted API returns the board to sign-in. AR01 found a defect: the "session could not
  be verified" warning stayed on a reconnected board until focus; fixed in `AuthGate.tsx`
  (re-check on the next 2xx while the warning shows). Remaining: CI (human), catalogue
  review.

## 8 — CPU capacity (FR-I9/I10)
- [ ] 8.1 Warm-up, batching, thread limits, live/batch priority and capacity refusal.
  Progress (10 Nov): `services/inference/capacity.py` call gate (refusal with
  `system.health reason=capacity`) and live-first micro-batcher with a 25 % token
  bucket and separate live/file queue limits; 14 Claude-authored tests; review fixed.
  Remaining: ONNX thread settings, warm-up/`/readyz` (need the exported model, 10.x),
  wiring into an inference loop, measured `capacity_windows_per_s` (8.QA).
  15 Nov (cee5ffb): `services/live/service.py` `load_detector` start-up gate (model card
  check, session opened from the card's hash with bounded threads, graph inspection
  forcing per-window serving for dynamic INT8, calibrated cards refused until serving
  applies the temperature, one finite warm-up) and `LiveService` (one thread: tap,
  pipeline, batcher, CallGate = tap admission, reconnect with backoff, faults counted,
  queued ends keep their reason on stop); 6 unit + 1 lab Claude-authored tests, review
  H1/H2/M1-M3 fixed. 15 Nov (a6f5106): `python -m services.live` command line (password
  from the environment only, start-up checks of hosts, extensions, config, model, VAD
  and ARI credentials, JSON start/stop records, exit 2 on refusal, signal stop with a
  forced second Ctrl+C); 12 unit + 1 lab Claude-authored tests; review M1-M4 and lows
  fixed. 16 Nov: store/Redis pumps on their own threads (see 5.1); `/readyz` and
  metrics for this process (see 7.2). Remaining: measured capacity (8.QA), file jobs
  through the batcher.
- [ ] 8.2 Degraded stride/unmonitored behavior and resource bounds.
  Progress (10 Nov): `services/scorer/degraded.py` stride controller (grow per behind
  report, cap, unmonitored, 30 s recovery), 13 Claude-authored tests, review fixed.
  10 Nov: `services/scorer/pending.py` bounded per-call pending queue (stride skips,
  drop-oldest, FIFO, "behind" = a window still waiting at acceptance), 12
  Claude-authored tests, review fixed. Remaining: wiring into the live inference loop
  (needs 6.x/8.1); PRD to confirm "behind"; EMA per window vs per second under stride.
  14 Nov: `services/scorer/pipeline.py` wires pending queue, stride controller,
  micro-batcher, a batched detector and `LiveCalls` in a single-owner explicit-clock
  loop (unmonitored published at the cap, cancellation of ended calls, detector faults
  contained); Claude-authored fake-detector tests. 14 Nov (catch-up 4):
  `services/inference/onnx_detector.py` ONNX session adapter for the pipeline
  (validation, `per_window` for dynamic INT8), tested through the real pipeline with
  tiny exports. Remaining: a service process owning the loop with the real exported
  model, warm-up/`/readyz`, file jobs through the batcher, tap input (6.x).
- [ ] 8.QA Measure timings/RSS/load; separate observed results from PRD targets.
  Progress (12 Nov): `models/ava/benchmark_cpu.py` (interleaved seeded rounds, p95 only
  with >= 20 timings, RTF only at batch 1, raw timings, limitations); 7
  Claude-authored tests, review fixed. FP32 random-weight proxy on the dev laptop
  recorded in the 12 Nov report (not a §6.1 result). 14 Nov (catch-up 4):
  `models/export/benchmark_onnx.py` ONNX session timing with the same method; 5
  Claude-authored unit + 1 model test; real 8-layer exports on this laptop: FP32 median
  0.69 s, dynamic INT8 0.43 s per 4 s window (4 threads, batch 1; untrained head, not
  A1). Remaining: box A1 with the trained model, concurrency/load and RSS per worker.

## 9 — Model research tooling (FR-M1–M6/M8–M12)
- [x] 9.1 SLS and layer-sweep/training harness with tiny smoke runs.
  Evidence (12 Nov): `models/ava/` truncated backbone (strict state truncation, equal
  to the full model's first layers), SLS head with attentive statistics pooling,
  FR-M8 mean-pool head, `float32[B,64000] -> logits, embedding` contract with in-model
  layer norm, AdamW 2e-5/1e-3 with frozen-encoder epochs, weighted CE and a layer
  sweep; `windows.py` FR-M11 window loader from conditioned prepared/verified
  augmented roots (train split, trainable rows, split-leak refusal). 40
  Claude-authored tiny-model/fixture tests, two independent reviews fixed. Human
  follow-up: real-checkpoint truncation smoke (checkpoint is outside the runner's
  allowed directory); PRD to confirm the >4 s clip window rule.
- [ ] 9.2 Baseline adapters using the same manifests/augmentation contracts.
  Progress (12 Nov): `models/augmentation/rawboost.py` NumPy RawBoost (algorithms 1-3,
  modes 1-8, reference defaults, per-clip seeded draws with a record; scipy filters
  re-implemented, parity with scipy not established); 16 Claude-authored tests, review
  fixed. 12 Nov: `models/baselines/rawnet2.py` RawNet2 (ASVspoof 2021 layout) on the
  shared window/harness contract with the reference training config; 13
  Claude-authored tests, review fixed; upstream fidelity to verify before reporting.
  Remaining: AASIST and XLS-R + AASIST (XLS-R weights are not provisioned locally).
  20 Nov (attempt 5): `models/baselines/aasist.py` AASIST against the reference
  implementation (clovaai/aasist, MIT, commit a04c986, cloned outside the repository).
  AASIST (297,866 parameters) and AASIST-L (85,306) configurations, upstream names
  loaded strictly, reference quirks kept, and training differences recorded. 20
  Claude-authored tests. The six upstream parity cases check that the sinc taps are
  bit-identical, that logits agree within 1e-6 in eval mode and in training mode with
  the same seed, and that the published weights score identically. They need
  `AVA_AASIST_UPSTREAM` at the pinned commit; they skip without it, or fail with
  `AVA_REQUIRE_UPSTREAM=1`. Review conditional pass, Lows fixed. The RawNet2 port in
  the clone agrees with both RawNet2 assumptions. Remaining: XLS-R + AASIST (needs
  XLS-R weights).
- [ ] 9.3 EER/minDCF/FAR, calibration/ECE, bootstrap and subset/evidence-length reports.
  Progress (10 Nov): `models/evaluation/metrics.py` core metrics, 15 Claude-authored
  known-answer tests, review fixed; FR-M9 evidence-length curve with a fixed cohort and
  scorer EMA parity; FR-M5 subset table, FR-M6 reliability bins and FR-M10 dialect FAR
  (30 tests total). 12 Nov: `models/evaluation/report.py` scores loaded windows and
  builds the FR-M5/M6/M10 report with recorded costs/temperature status; 11
  Claude-authored tests, review fixed. 14 Nov (catch-up 4): ASVspoof 5 Track 1 costs
  (c_miss 1, c_fa 10, pi_spoof 0.05) confirmed from Evaluation Plan v0.6 Table 1 and
  added as `asvspoof5_track1_costs()` with a cited source string; FR-M5 and
  architecture §4.5 "DF-track" corrected to Track 1; 5 Claude-authored tests, review
  fixed. Remaining: running it on real scores; actDCF (needs LLR scores). Needs a
  human/PRD: speaker-clustered intervals and EER method.
- [ ] 9.QA Known-answer metrics, insufficient-data cases and training smoke evidence.

## 10 — Export/deployment (FR-M7, architecture deployment)
- [ ] 10.1 ONNX/INT8 pipeline and checkpoint-based parity harness/fallback rules.
  Progress (14 Nov catch-up 4): `models/export/onnx_export.py` FP32 export (opset 17
  read back, static 64,000 input, dynamic batch, outside-repo, no-overwrite hard-link
  publish), CPU-only session with hash pin, I/O contract check, FR-M7 parity harness
  (>= 100 windows, strict < 1e-3), pure FR-M7 INT8 -> static -> FP32 decision rule;
  `requirements-export.txt` (onnx 1.23.2); 9 Claude-authored unit + 14 model tests on a
  tiny random detector; review fixed. Then dynamic INT8 of MatMul/Gemm only
  (`quantize_int8_dynamic`, hash-pinned source, refuses no-op/contract-breaking
  output) and `compare_sessions` drift report; 7 more Claude-authored model tests,
  review fixed. Finding: dynamic INT8 output
  depends on batch partners (cross-call micro-batching question for the PRD). Then
  static INT8 (`quantize_int8_static`, QOperator, MinMax calibration with recorded set
  hash; batch-independent by test); 7 Claude-authored model tests, review pass. Then
  `models/export/checkpoint_parity.py`: real AntiDeepfake MMS-300M backbone truncated,
  seeded untrained SLS head, FP32 export + parity on noise windows, optional dynamic
  INT8 drift, report with INCOMPLETE marker; 8 Claude-authored tests with an injected
  loader; real 8-layer run recorded in the 14 Nov report. Remaining: the AVA-trained
  head and parity on real dev windows, INT8 latency and dev-EER measurement (with the
  batching policy and a disjoint calibration set).
- [ ] 10.2 Model-card generator, container/Compose and deployment manifests.
  Progress (15 Nov, 789be31): `models/export/model_card.py` card builder (FR-O4 fields,
  `not_measured` metrics, uncalibrated by default, contract from `onnx_export`), JSON
  and Markdown writers (no overwrite) and `check_card` start-up check (hash, size, file
  name, schema, scoring window); Claude-authored tests, review fixed; used by the live
  service gate. 17 Nov: live service image (`deploy/live/Dockerfile`, digest-pinned base,
  non-root, read-only, allowlist `.dockerignore`) and Compose stack (`deploy/compose/`:
  PostgreSQL 16, Redis 7 AOF, live, lab Asterisk; internal network with no egress);
  ONNX Runtime telemetry found ON by default in official builds and forced off
  (`services/inference/ort_runtime.py`, image env); Claude-authored lab test (real call
  stored and logged, egress blocked) and unit tests; review fixed. 17 Nov (attempt 2):
  API/dashboard image (`deploy/api/Dockerfile`: digest-pinned Node 22 stage builds the
  dashboard from the lockfile; Python stage with 32 exact pins incl. torch 2.8.0+cpu,
  `--no-deps`, uid 10001) and Compose `api` service (internal only, read-only, 2 CPUs/3 GB,
  `/readyz` health check with the real checkpoint, Redis relay); lab test extended (real
  call reaches the API WebSocket via Redis, `/v1/calls`, dashboard assets, file analysis
  with the real checkpoint, no egress) and a CI unit test of the pinning rules; review
  conditional pass fixed. Remaining: publishing the dashboard (auth decision), FR-I9 cap
  across containers, Kubernetes manifests and NetworkPolicy, hashed requirements/SBOM.
  19 Nov (attempt 3): k3s manifests (PRD FR-O2/O3/O4, P0) in `deploy/k8s/`: `base/`
  (namespace `ava` with Pod Security restricted enforced, ConfigMap, static read-only
  model PV/PVC instead of hostPath, PostgreSQL and Redis StatefulSets with claims, live
  (Recreate, RTP to `$(POD_IP)`) and api Deployments with startup/readiness/liveness
  probes and limits, default-deny NetworkPolicy plus named flows; PBX placeholder
  192.0.2.10) and `lab/` overlay (Asterisk in `ava-lab-pbx`); operator site overlay with
  `secretGenerator` in the README. Claude-authored: 15 static tests
  (`tests/unit/test_k8s_manifests.py`; PyYAML 6.0.3 in the new `requirements-test.txt`,
  so 14 skip in CI until a human adds that file to the workflow) and a k3s-in-Docker lab
  case (`tests/lab/test_k3s_stack_lab.py`, rancher/k3s v1.34.2 by digest): real call
  tapped across namespaces, stored and ended in the API; no AVA pod connects out or
  queries a public resolver; only the named in-namespace flows work. Review conditional
  pass, M1-L5 fixed. Remaining: CoreDNS still forwards outside names (FR-O3 DNS part),
  Ingress/TLS for the dashboard (auth decision), monitoring on k3s, how a PBX outside
  the cluster reaches the pod network, hashed requirements/SBOM.
  21 Nov: FR-O7 SBOM generator. `scripts/image_sbom.py` is standard-library only and
  writes CycloneDX 1.5 from inside the image. It records base digests from the
  Dockerfile, the Python packages (licence, pinned flag, RECORD hash), the interpreter,
  the dpkg packages and the dashboard npm lock (SHA-512). `--check-pins` checks the
  installed set against the pins; pip, setuptools and wheel are exempt as base tools.
  `--source-revision` is recorded. Claude-authored unit tests (CI) cover this, plus a
  CycloneDX schema case (local only) and a lab case in both real images (pip/dpkg
  oracles, base-tool versions, determinism, a negative). Review conditional pass, all
  findings fixed. Remaining: a CI job that runs it (human: workflow edit), hashed locks
  (human: `uv pip compile` is outside the allowlist), the rest above.
- [ ] 10.QA Reproducible startup, CPU parity where measurable, rollback/readiness.
  Progress (22 Nov, attempt 2): FR-O4 refusal and model rollback in the Compose lab
  (`test_a_refused_model_change_never_stops_calls_and_a_rollback_restores_scoring`,
  Claude-authored). Three bad changes made through the env file are each refused on every
  restart attempt with the exact reason: a card for another export, a missing model
  file, and a 2 s config window against a 4 s card. Live writes no `starting` line, is
  never healthy, and logs no host path or password. A real call completes while live
  refuses, with nothing stored. Restoring the env file brings live back healthy in about
  7 s with the same model and hash; no migration is re-applied, the earlier row is kept,
  and a new call is stored to `bye`. The README now has a model change and rollback
  procedure; an older image refuses a database that a newer image migrated (back up first;
  untested). Review conditional pass, fixed. Remaining: CPU parity where measurable
  (needs the trained head), an image upgrade/rollback with a database restore, and the same
  on k3s.

## 11 — Final software integration
- [ ] 11.1 Recovery/load/soak, retention/deletion and end-to-end integration tests.
  Progress (22 Nov, attempt 7): FR-O5 retention in the deployments. `AVA_RETENTION`
  (default off) and `AVA_RETENTION_TTL_DAYS` in the Compose env file and k3s
  `ava-config`; live always gets the fixed snippet folder, so `off` purges at start. Key
  job `services/retention/keygen.py` (Compose `retention-key`, k3s init container):
  with `on` it creates a 32-byte 0400 key once and never replaces it; with `off` it does
  nothing. Snippet and key volumes/claims; key read-only in live. Claude-authored unit,
  static and lab tests (Compose and k3s: default, on, off; one alert gave one sealed
  snippet, purged after off). Review M1 (off depended on the key volume) and L1-L7 fixed.
  Remaining: a switch without a live restart, key rotation, snippet backup test,
  FR-U5 playback in the stacks (needs `--auth on`), recovery/load/soak.
  22 Nov (attempt 8): load and soak in the Compose lab
  (`test_concurrent_calls_over_repeated_rounds_lose_no_window_and_do_not_grow_live`,
  Claude-authored; `AVA_SOAK_ROUNDS`/`AVA_SOAK_CALLS`): concurrent real calls per round,
  every call stored to `bye` with all scored windows, events whole over the WebSocket,
  nothing left in live or Asterisk, threads/fds flat, memory without steady growth.
  10 rounds x 4 passed (960 windows, +1.1 MB). Defect found: about 1 in 30 concurrent
  calls was not tapped, logged only as `tap_failure`. The tap now logs `last_stage`/
  `last_reason`, which showed `snoop_join` `http_422` (channel not yet in the Stasis app);
  joins are retried on 422 for up to 0.2 s (`join_retries` metric). The retry is
  unit-tested only: no 422 occurred in the final run. Two review rounds (conditional pass)
  fixed. Remaining: the retry seen working on the stack, recovery under load, a longer
  soak, plus the retention items above.
- [ ] 11.2 Requirements/evidence audit, runbooks and reproducible demo instructions.
  Progress (22 Nov, attempt 3): `docs/qa/requirements-evidence-audit.md`, one row per PRD
  functional requirement (83) with status, code, tests and gap; checked in CI by
  `scripts/check_requirements_audit.py` (stdlib; every PRD ID once, priorities, statuses,
  cited files exist, gap rules, summary counts) and `tests/unit/test_requirements_audit.py`
  (Claude-authored). Result: 24 implemented, 32 partial, 14 data-dependent, 3 not started,
  8 human, 2 deferred. Independent review moved FR-I3, FR-I4 (live hop fixed at 2 s),
  FR-T3 (from/to not served), FR-T5 (10 s end grace vs 5 s), FR-M11, FR-O4 and FR-O5 from
  implemented to partial. Remaining: FR-O8 deployment guide and runbooks, demo
  instructions.
  22 Nov (attempt 4): FR-O8 deployment guide `docs/operations/deployment-guide.md`
  (hardware, PBX tap configuration, recording-failure policy "continue the call",
  install, upgrade and rollback, backup and restore, §6.3 controls table, day-to-day
  checks, blocking gaps), each part marked tested or not. Its Compose PostgreSQL backup
  and restore is lab-tested (`test_a_database_backup_restores_into_a_new_stack_and_scoring_resumes`,
  Claude-authored: online dump; UTF-16-damaged copy refused by `pg_restore --list` before
  any drop; in-place rollback restore undoes a later call; restore after `down -v`;
  `ava_reader` recreated and still restricted; nothing re-migrated). CI check
  `tests/unit/test_deployment_guide.py` (paths, flags, test names, settings, sections,
  §6.3 rows in PRD order, restore steps equal to the lab test's). Audit: FR-O8 partial,
  FR-T4 implemented. Review H1 (restore failed unless the volume was deleted) and
  re-review N1 (drop before checking the dump) fixed. Remaining: demo instructions;
  untested guide parts (image upgrade with a migration, k3s backup, site PBX network).
  22 Nov (attempt 5): reproducible demo instructions `docs/operations/demo.md` (PRD 5.9:
  live tapped call, timeline, batch upload, stored history, egress proof; what it does
  not show). Compose profile `demo`: `demo-proxy` (`services/edge`, stdlib byte forwarder
  to `api:8000`, 32 connections, idle/connect timeouts, no per-connection log) published
  on 127.0.0.1 only; `api` and all other services stay without a route out.
  `python -m lab.demo status|call|egress`. Claude-authored: unit tests (forwarder,
  profile isolation, helper, guide references; CI) and the lab rehearsal
  `tests/lab/test_compose_demo_lab.py` with Playwright project `compose-demo` (DR01-DR03)
  on the real stack. Review conditional pass: forwarder stalled-write bound, egress proof
  needing no default route, DNS-rebinding Host allowlist on the API (`services/api/hosts.py`,
  `--allowed-host`), guide provenance; fixed and re-run (109 s). Remaining: runbooks
  beyond the guide, the recorded BO-4 video (human).
  22 Nov (attempt 6): operations runbooks `docs/operations/runbooks.md` (16 signal ->
  calls -> check -> act -> tested procedures: ARI lost, loop stalled, detector failing,
  start-up refused, capacity, degraded/unmonitored, packet loss, storage stalled, pump
  dead, event relay, API not ready, config rejected, tap/scoring errors, tapping nothing,
  snippets, lost log lines; check PostgreSQL before restarting live; no PBX call-path
  change). Review conditional pass (M1-M6: outage numbers, restart during a database
  outage, PBX wording, overclaimed Tested lines, per-metric labels, missing runbooks) fixed.
  CI check `tests/unit/test_runbooks.py` (Claude-authored): every cited metric, label
  value, log event, readiness reason, panel, path and test exists; every live readiness
  reason and warning/error log event has a runbook. Remaining: the recorded BO-4 video
  (human); then 11.2 can close.
- [ ] 11.QA Separate complete software from data-dependent/unmeasured acceptance.

Not autonomous acceptance: full representative model training, final calibration/
accuracy, native listening/intelligibility decisions, participant recruitment,
institutional details or human study. P2 SIPREC/live AMR/webhooks need a later
scope decision. Access/compute blockers must be reported; do independent work next.
