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
- [ ] 2.3 Local React/TypeScript upload/results view with uncalibrated baseline label.
  **Blocked 9 Nov:** React/Vite/TypeScript are not installed and `npm install`/`npm
  view` are outside the runner allowlist. Needs pinned deps plus lockfile added by a
  human, an allowlist change, or approval of a dependency-free page for 2.3.
- [ ] 2.QA Actual public-pilot audio through browser/API/model; Playwright negative cases.

## 3 — Shared audio path (FR-D6/D12/D13, FR-I3/I4)
- [ ] 3.1 Shared offline/live conditioning parity; speaker-turn boundary contract.
  Progress (9 Nov): file-mode parity tests in `tests/unit/test_conditioning_parity.py`
  (prepare vs analysis service, 5 formats, refusals, gain divergence recorded).
  Remaining: speaker-turn boundary contract (not defined in PRD/architecture; needs
  a scope definition) and live/streaming-side parity.
- [ ] 3.2 VAD, speech-only windows, no-speech/partial-window semantics.
  **Blocked 9 Nov:** no VAD package or Silero weights are installed; adding them is a
  dependency/model download outside the runner window rules. Needs a human to pin
  and provision them locally (or approve an energy-based interim VAD).
- [ ] 3.3 G.711/GSM and AMR-NB file codec pipeline; loss/jitter and source lineage.
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
  Claude-authored tests, review fixed. Remaining: GSM, AMR-NB (codec library).
- [ ] 3.4 Other PRD codec/noise paths with bounded reproducible fixtures.
  Progress (9 Nov): `data/prepare/noise.py` seeded additive noise at 0-20 dB SNR
  (FR-D7) with lineage and per-clip plans (`derive_noise`/`mix_planned`); 18
  Claude-authored tests. Remaining: noise corpora, RIRs, speech-active SNR (needs VAD, 3.2), Opus/G.729
  VoIP path. 11 Nov: noise wired through `augment.py` (noise file named by PCM hash,
  source ID and licence; clipping refusals keep the drawn SNR for bias audits).
- [ ] 3.QA Sample-level parity, malformed formats and immutable-original checks.

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
  non-blocking fan-out, 16 Claude-authored tests, review fixed. **Blocked part:**
  schema/migrations and Redis need a dependency decision: PostgreSQL 18 binaries are
  installed (architecture names 16) but no Python driver or Redis is pinned/installed.
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
- [ ] 5.3 Restart recovery, in-flight handling and default no-audio-retention behavior.
  Progress (12 Nov): default no-raw-audio retention on the analysis API verified by 3
  Claude-authored tests (spooled uploads removed with rollover proven, no audio in
  job records after completion or shutdown, small filename-free responses); review
  fixed. Remaining: restart recovery and in-flight handling (need durable jobs, 5.1),
  FR-O5 opt-in snippet retention on the live path.
- [ ] 5.QA Real service integration, disconnects, failures and state recovery.

## 6 — Asterisk laboratory (FR-T1–T5/T7, FR-I1/I2)
- [ ] 6.1 Docker/WSL preflight; Asterisk/SIPp endpoints and WAV injection.
- [ ] 6.2 ARI passive tap, lifecycle and RTP G.711/GSM decoder.
- [ ] 6.3 Jitter/loss/concealment and malformed RTP/SDP cases; AMR file/replay.
- [ ] 6.QA End-to-end public audio through lab; detector failure never drops calls.

## 7 — Dashboard and operations (FR-U, FR-A8)
- [ ] 7.1 Live board, alerts, timeline and batch job states.
- [ ] 7.2 Health/readiness/metrics and visible unmonitored/low-quality conditions.
  Progress (12 Nov): FR-A8 on the analysis API: `/readyz` with a validated warm-up
  inference (sticky once warmed, stays ready while busy), Prometheus `/metrics`
  without a client library (bounded route labels, job states, queue, uploads in flight,
  ready/loaded, analysis seconds); 13 Claude-authored tests, review fixed. Remaining:
  unmonitored/low-quality visibility (needs the live path), FR-O6 histograms and
  telephony metrics, dashboard (blocked with 2.3). 14 Nov: `ava_analysis_seconds` now a
  histogram, per-stage `ava_analysis_stage_seconds` (decode/condition/inference; results
  carry `stage_seconds`), live-call gauges/counters from the call registry (active,
  stale, open alerts, alerts raised, exclusive event outcomes, lost events,
  subscribers); 5 Claude-authored tests, review fixed. Remaining FR-O6: telephony,
  live per-window/queue/dropped-window metrics, JSON logs, Grafana.
- [ ] 7.3 P1 history/search/local authentication/audit and feedback after P0.
- [ ] 7.QA Astra-designed Playwright suite, offline assets, failures/reconnects.

## 8 — CPU capacity (FR-I9/I10)
- [ ] 8.1 Warm-up, batching, thread limits, live/batch priority and capacity refusal.
  Progress (10 Nov): `services/inference/capacity.py` call gate (refusal with
  `system.health reason=capacity`) and live-first micro-batcher with a 25 % token
  bucket and separate live/file queue limits; 14 Claude-authored tests; review fixed.
  Remaining: ONNX thread settings, warm-up/`/readyz` (need the exported model, 10.x),
  wiring into an inference loop, measured `capacity_windows_per_s` (8.QA).
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
  contained); Claude-authored fake-detector tests. Remaining: a real detector/ONNX
  session and threads (10.1), file jobs through the batcher, tap input (6.x).
- [ ] 8.QA Measure timings/RSS/load; separate observed results from PRD targets.
  Progress (12 Nov): `models/ava/benchmark_cpu.py` (interleaved seeded rounds, p95 only
  with >= 20 timings, RTF only at batch 1, raw timings, limitations); 7
  Claude-authored tests, review fixed. FP32 random-weight proxy on the dev laptop
  recorded in the 12 Nov report (not a §6.1 result). Remaining: INT8 ONNX (blocked on
  onnx/onnxruntime), box A1, concurrency/load and RSS per worker.

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
- [ ] 9.3 EER/minDCF/FAR, calibration/ECE, bootstrap and subset/evidence-length reports.
  Progress (10 Nov): `models/evaluation/metrics.py` core metrics, 15 Claude-authored
  known-answer tests, review fixed; FR-M9 evidence-length curve with a fixed cohort and
  scorer EMA parity; FR-M5 subset table, FR-M6 reliability bins and FR-M10 dialect FAR
  (30 tests total). 12 Nov: `models/evaluation/report.py` scores loaded windows and
  builds the FR-M5/M6/M10 report with recorded costs/temperature status; 11
  Claude-authored tests, review fixed. Remaining: running it on real scores; PRD
  decisions on speaker-clustered intervals and EER method; ASVspoof 5 cost constants. Needs a human: confirm
  ASVspoof 5 cost constants (no web access); speaker-clustered intervals and EER method.
- [ ] 9.QA Known-answer metrics, insufficient-data cases and training smoke evidence.

## 10 — Export/deployment (FR-M7, architecture deployment)
- [ ] 10.1 ONNX/INT8 pipeline and checkpoint-based parity harness/fallback rules.
- [ ] 10.2 Model-card generator, container/Compose and deployment manifests.
- [ ] 10.QA Reproducible startup, CPU parity where measurable, rollback/readiness.

## 11 — Final software integration
- [ ] 11.1 Recovery/load/soak, retention/deletion and end-to-end integration tests.
- [ ] 11.2 Requirements/evidence audit, runbooks and reproducible demo instructions.
- [ ] 11.QA Separate complete software from data-dependent/unmeasured acceptance.

Not autonomous acceptance: full representative model training, final calibration/
accuracy, native listening/intelligibility decisions, participant recruitment,
institutional details or human study. P2 SIPREC/live AMR/webhooks need a later
scope decision. Access/compute blockers must be reported; do independent work next.
