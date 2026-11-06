# AVA daily development backlog — approved

Prepared 6 November 2026. Tughral approved the order and dedicated-PR publication on 6 November. Setup evidence is in the daily scheduler handoff.
Confirmed window: every day 12:00–14:00 Asia/Karachi (07:00–09:00 UTC).
Goal: complete independently testable software while participant collection runs.
Canonical PRD/architecture remain authoritative. Existing public pilots and the
pretrained checkpoint support integration; fixtures support deterministic tests.
Neither substitutes for final participant-based validation.

## Ordered backlog

Each row is a phase with multiple small tasks, not a promise of one day's work.
Each completed task needs requirement-linked tests and independent QA before commit.

| Phase | Work to implement | Completion evidence |
|---|---|---|
| 0 | Establish the latest integration baseline, preserve pending PDF/context edits; isolated automation worktree; verify Python/Node, CPU checkpoint and external data paths; check Docker/WSL and Astra availability | Dry run, ownership lock, restart and stop-time checks; dependency report |
| 1 | Close Punjabi importer/prepared-verifier review items; finish split validation, duplicate-source annotation and policy-version migration | Astra cases pass; external pilot verification; no altered originals or cross-split lineage |
| 2 | First useful prototype: bounded WAV analysis API backed by the existing trained CPU checkpoint; upload/result UI with clear uncalibrated-baseline label | Actual existing clip passes browser → API → CPU model → result; Playwright failure cases |
| 3 | Shared offline/runtime conditioning, VAD and 4-second/2-second windows; partial/no-speech handling; codec augmentation with source lineage | Sample-level parity; no-speech/short/malformed inputs; codec round trips; originals unchanged |
| 4 | Streaming score aggregation: EMA, evidence rule, hysteresis, quality gates, direction and configuration semantics | State-transition, boundary and concurrency cases; short evidence stays undetermined |
| 5 | Durable calls/jobs/events: PostgreSQL/Redis integration, bounded queues, cancellation, retry/recovery, WebSocket updates | Restart recovery, duplicate requests, bounded resource use and disconnect tests |
| 6 | Asterisk/SIPp lab, G.711/GSM injection, passive tap, RTP decode/jitter/loss handling; AMR-NB file/replay path | Existing audio drives real lab integration; malformed RTP/loss cases; AVA failure does not stop calls |
| 7 | Live dashboard, call timeline, upload job states, health/readiness/metrics; P1 history/search/auth/feedback after P0 | Astra-designed Playwright cases across normal, failure and recovery paths; assets work offline |
| 8 | CPU inference scheduling, warm-up, micro-batching, capacity refusal, degraded/unmonitored states | Measured CPU timings/memory and load tests; no unsupported capacity claim |
| 9 | Training/evaluation tooling: SLS/layer sweep, baseline adapters, EER/minDCF/FAR/intervals, calibration and subset reports | Known-answer metric tests, tiny training smoke, explicit insufficient-data reporting |
| 10 | Export/quantization/parity harness, model-card builder; container/Compose deployment then architecture deployment manifests | Reproducible builds, checkpoint-based parity where feasible, readiness and rollback tests |
| 11 | End-to-end integration, retention/deletion behavior, recovery/load/soak checks, documentation and requirement coverage audit | Evidence matrix separates passing, failed, blocked and unmeasured requirements |

P2 work (SIPREC, live AMR-NB, optional webhook) follows P0/P1 only after a separate
scope decision. Model/generator tooling can be built now, but full training,
generator intelligibility, final calibration, language/dialect accuracy and human
study results cannot be declared complete without the required data/human evidence.
New paid compute, account-gated downloads and external communications are not part
of unattended work. If blocked, record it and pick the next independent ready task.

## Proposed daily session

- 12:00–12:10: acquire single-run lock; inspect repository/previous handoff; select
  the highest-priority ready task; check remaining account/tool availability.
- 12:10–13:35: small implementation → targeted tests → independent review cycles.
  Use GPT-6 Astra for substantive test designs; Playwright for browser execution.
  Record actual model provenance. Missing Astra blocks that acceptance gate, not
  unrelated documentation/research work.
- 13:35–13:50: stop starting new features; integrate completed work and run checks.
- 13:50–14:00: commit only coherent verified changes; save any unfinished owned
  work separately from accepted progress; update todo and session report; stop.

Up to roughly six useful commits is an aspiration, not a quota. Zero commits is
valid when verification fails. No empty commits, artificial splitting, fabricated
test results or marking skeletons complete. A complex task may span several days.
Each commit should reference its issue/requirement and include relevant tests/docs.

## Scheduler implementation contract

Implemented through Windows Task Scheduler and the authenticated Codex CLI;
see `handoffs/daily-scheduler.md` for verification. A recurring
trigger alone does not enforce two-hour execution: require an external deadline,
graceful checkpoint period, owned-child-process cleanup and no new work after 14:00.
Skip missed windows rather than running after hours. A stale lock must not erase
work; resume from saved state. Test overlap prevention and interruption recovery.
Local machine must be available, awake and online with working authentication.
Model/account limits can halt a run; no automatic purchase or paid API fallback.

Use a dedicated worktree based on a reviewed integrated baseline; never share its
working tree/index with Claude writers. Do not silently commit pre-existing PDF
or context edits. Local task commits and pushes to the dedicated draft PR are
approved. Never auto-merge or push to main.

Daily report: completed IDs, commit hashes, tests/results, remaining/blocked work,
next task, stop reason and available usage evidence. Update docs/context/todo.md
without erasing collection tasks. Stop the recurring work when the agreed backlog
is complete, or report that only externally blocked tasks remain; do not invent
new scope or loop forever on a blocker.

## Recorded decisions

1. Phase order accepted; working upload/scoring prototype early.
2. Push verified commits to a dedicated feature branch and draft PR; never merge.
3. Windows local runner enforces the cutoff. User must keep the machine available,
   logged in and online; no password is stored.

This plan authorizes the scheduler setup and future verified task commits/pushes.
Registration and smoke-test evidence are recorded separately; no product acceptance is implied.
