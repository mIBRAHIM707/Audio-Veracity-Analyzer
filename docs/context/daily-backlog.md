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
- [ ] 1.2 Punjabi importer follow-ups and meaningful Windows fixture cleanup fix.
- [ ] 1.3 Astra split cases and independent review; annotate duplicate originals.
- [ ] 1.4 Versioned policy migration and pilot split rerun, preserving originals.
- [ ] 1.QA Review evidence and remaining source/identity limitations.

## 2 — Working upload/scoring prototype (FR-A1/A2, FR-U4)
- [ ] 2.1 Bounded analysis service wraps existing trained CPU checkpoint; no runtime download.
- [ ] 2.2 FastAPI upload/results contract; malformed/oversized input and failure cases.
- [ ] 2.3 Local React/TypeScript upload/results view with uncalibrated baseline label.
- [ ] 2.QA Actual public-pilot audio through browser/API/model; Playwright negative cases.

## 3 — Shared audio path (FR-D6/D12/D13, FR-I3/I4)
- [ ] 3.1 Shared offline/live conditioning parity; speaker-turn boundary contract.
- [ ] 3.2 VAD, speech-only windows, no-speech/partial-window semantics.
- [ ] 3.3 G.711/GSM and AMR-NB file codec pipeline; loss/jitter and source lineage.
- [ ] 3.4 Other PRD codec/noise paths with bounded reproducible fixtures.
- [ ] 3.QA Sample-level parity, malformed formats and immutable-original checks.

## 4 — Streaming decisions (FR-I4–I8)
- [ ] 4.1 Configuration validation and call-scoped reload behavior.
- [ ] 4.2 EMA, minimum evidence, hysteresis and undetermined outcomes.
- [ ] 4.3 Quality flags, direction selection and agent-direction semantics.
- [ ] 4.QA Boundary/state-transition/concurrency tests and independent review.

## 5 — Backend persistence/events (FR-A, FR-I11)
- [ ] 5.1 PostgreSQL/Redis data and event contracts, migrations and bounded queues.
- [ ] 5.2 Jobs/cancellation/idempotency, calls/alerts API and WebSocket events.
- [ ] 5.3 Restart recovery, in-flight handling and default no-audio-retention behavior.
- [ ] 5.QA Real service integration, disconnects, failures and state recovery.

## 6 — Asterisk laboratory (FR-T1–T5/T7, FR-I1/I2)
- [ ] 6.1 Docker/WSL preflight; Asterisk/SIPp endpoints and WAV injection.
- [ ] 6.2 ARI passive tap, lifecycle and RTP G.711/GSM decoder.
- [ ] 6.3 Jitter/loss/concealment and malformed RTP/SDP cases; AMR file/replay.
- [ ] 6.QA End-to-end public audio through lab; detector failure never drops calls.

## 7 — Dashboard and operations (FR-U, FR-A8)
- [ ] 7.1 Live board, alerts, timeline and batch job states.
- [ ] 7.2 Health/readiness/metrics and visible unmonitored/low-quality conditions.
- [ ] 7.3 P1 history/search/local authentication/audit and feedback after P0.
- [ ] 7.QA Astra-designed Playwright suite, offline assets, failures/reconnects.

## 8 — CPU capacity (FR-I9/I10)
- [ ] 8.1 Warm-up, batching, thread limits, live/batch priority and capacity refusal.
- [ ] 8.2 Degraded stride/unmonitored behavior and resource bounds.
- [ ] 8.QA Measure timings/RSS/load; separate observed results from PRD targets.

## 9 — Model research tooling (FR-M1–M6/M8–M12)
- [ ] 9.1 SLS and layer-sweep/training harness with tiny smoke runs.
- [ ] 9.2 Baseline adapters using the same manifests/augmentation contracts.
- [ ] 9.3 EER/minDCF/FAR, calibration/ECE, bootstrap and subset/evidence-length reports.
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
