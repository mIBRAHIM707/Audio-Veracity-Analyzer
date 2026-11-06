# Daily development scheduler cases

Design provenance: GPT-6 Astra (`gpt-6-astra`), executing the
`/root/scheduler_astra` test-designer task on 6 November 2026. Source baseline:
`2a43b60f39aeaccd9978edbbf86d431a72cf9350`, branch
`feat/4-daily-development`. These cases concern development automation, not AVA
runtime acceptance, ML accuracy, PBX behavior or browser behavior. No browser
cases require a Playwright executor. No assistant/API request is used by tests.

Task-local references SCH-01 through SCH-08 below describe the coordinator's
handoff and [implementation spec](../../_bmad-output/implementation-artifacts/spec-daily-development-runner.md),
not existing product FRs. Canonical mappings: FR-W1 to FR-W4 and ADR-15
(shared context, layered tests, bounded ownership); FR-E3/FR-E6 (private data
excluded from repository and test fixtures). Product offline serving requirements
do not claim the development assistant works offline.

## Requirements and assumptions

| Reference | Required behavior |
| --- | --- |
| SCH-01 | Start work only in [12:00, 13:45) Asia/Karachi, fixed UTC+05:00; finish by 14:00. A duration limit can only shorten today's cutoff. |
| SCH-02 | Dry-run invokes no assistant and changes no repository/index/branch/task registration. Local diagnostics may be written. |
| SCH-03 | One active runner per worktree with fixed `.local/automation` state directory; a process crash releases ownership without manual stale-file deletion. |
| SCH-04 | Pause/completion controls prevent new work; pause stops active owned work promptly, completion takes effect between iterations. |
| SCH-05 | Require expected worktree and branch; reject main/master and mismatches before assistant work. |
| SCH-06 | Failures are recorded and bounded; deadline/cancellation terminates owned child processes, never unrelated processes. |
| SCH-07 | Persist truthful last-result and diagnostic records; no automatic merge. |
| SCH-08 | Daily registration uses Pakistan local noon, handles existing task explicitly, and does not change machine timezone. |

Each case inherits these preconditions: temporary fictional repository/state,
Python 3.11, no credentials/audio/participant data; patched assistant launch or
harmless Python child only. Retain test name, assertion outcome, command and
actual environment. Cleanup temporary directories and all owned child processes
in `finally`/test cleanup, even if an assertion fails. P0 cases execute first.
Automation status and limitations are recorded with each case; a pending case is
not covered merely because related mocks pass.

## Cases

### DS-01 — wall-clock interval and timezone

- Reference/priority/layer: SCH-01; FR-W2, ADR-15; P0; unit.
- Input: 11:59:59.999999, 12:00, 13:44:59.999999, 13:45 and 14:00 PKT;
  UTC instants including 31 December at 20:00 UTC (1 January PKT); naive datetime.
- Expected: no launch before noon or at/after 13:45; noon has 7,200 seconds;
  final launch instant has at most 900.000001 seconds to hard cutoff;
  window date follows PKT across the year boundary. Naive timestamps must not silently use a
  machine-dependent timezone.
- Evidence: exact computed values/refusal. Status: automated, passed. Limitation:
  deterministic clock checks do not prove Windows wakes from sleep.

### DS-02 — bounded duration and malformed configuration

- Reference/priority/layer: SCH-01; FR-W2, ADR-15; P0; unit/CLI.
- Input: noon with duration 1, 7,200, 7,201; final launch instant with duration 7,200; zero,
  negative, NaN, +infinity, -infinity, malformed text and missing required args.
- Expected: valid limits never extend 14:00; 7,201 is rejected; invalid configuration
  refuses before child launch. Large finite limits cannot bypass 14:00.
- Evidence: deadline/cap values, CLI exit and zero launch calls. Status: automated,
  passed, including advancing clock to 14:00 during workspace validation and
  rejecting oversized/malformed config and invalid daily ledgers. Limitation:
  system-wide clock tampering was not performed.

### DS-03 — dry-run and out-of-window invocation

- Reference/priority/layer: SCH-01/SCH-02; FR-W3; P0; integration with fake clock.
- Input: valid expected branch/worktree; dry-run during window, normal invocation
  before noon and at cutoff; preserve a fictional uncommitted sentinel.
- Expected: no assistant launch, no commit/push/merge, unchanged Git status/HEAD
  and sentinel; truthful dry-run/outside-window report. A late start never shifts
  the window or catches up after 14:00.
- Evidence: launch spy, result, before/after repository snapshot. Status: automated,
  passed. Limitation: fake assistant evidence is not authenticated Codex execution.

### DS-04 — worktree and branch refusal

- Reference/priority/layer: SCH-05; FR-W3, ADR-15; P0; integration.
- Input: real temporary Git repository on expected feature branch; wrong branch,
  main, master, detached HEAD, subdirectory masquerading as root, unexpected or
  additional push destinations, alternate state roots.
- Expected: only the exact expected feature checkout is accepted; all other
  scenarios refuse before assistant invocation, with no checkout/reset/stash.
- Evidence: Git read-only probes and error/result. Status: automated, passed.
- Limitation: this validates launch ownership, not every future assistant action.

### DS-05 — concurrent lock ownership and recovery

- Reference/priority/layer: SCH-03; FR-W3; P0; OS integration.
- Input: one process holds the lock; a second attempts it; terminate the holder
  abruptly and attempt again, leaving its lock file present.
- Expected: contender cannot enter while owner lives; a new process enters after
  owner exit without unlinking stale lock files. Normal release also permits reuse.
- Evidence: child readiness, contention result, process exit and reacquisition.
- Status: automated, passed on Windows. Limitation: local filesystem/OS locking
  only; network filesystems untested. Alternate state directories are rejected.

### DS-06 — stop controls before and during work

- Reference/priority/layer: SCH-04/SCH-06; P0; unit/OS integration.
- Input: PAUSED and COMPLETE independently present before launch; create pause
  while harmless child runs; remove pause and perform another eligible invocation.
- Expected: pre-existing flags prevent launch; mid-run control terminates owned
  work within polling bound; completion is never inferred from a successful child
  exit alone; clearing pause allows a later otherwise eligible run.
- Evidence: zero launch calls or child exit, result reason and restart eligibility.
- Status: automated, passed, including an actual child creating PAUSED and being
  terminated with exit 125. Clearing a pre-launch pause permits work; an active
  cancellation marks the day failed and prevents further automatic attempts that
  day. Limitation: actual user/application completion is separate from a sentinel
  file's mechanical effect.

### DS-07 — child failure and launch failure

- Reference/priority/layer: SCH-06/SCH-07; P0; unit/OS integration.
- Input: harmless child exits 7; nonexistent executable; assistant adapter raises.
- Expected: failure status retains exit/error information, no retry loop or second
  child, lock released, next eligible invocation can proceed.
- Evidence: invocation count, saved last-result and subsequent lock acquisition.
- Status: automated, passed. Limitation: no real assistant/network failure tested.

### DS-08 — deadline and descendant cleanup

- Reference/priority/layer: SCH-01/SCH-06; P0; OS integration.
- Input: harmless parent launches sleeping grandchild; impose a short deadline;
  keep a separate unrelated sleeping process alive as control.
- Expected: parent and grandchild stop by a bounded test timeout; control lives;
  runner records timeout and releases lock. No later iteration starts.
- Evidence: parent/grandchild PIDs, independent OS liveness checks, result and
  monotonic elapsed time. Status: automated, passed on Windows. Limitation: test process
  scheduling tolerance is not an exact hard real-time guarantee.

### DS-09 — truthful result and prompt boundaries

- Reference/priority/layer: SCH-07; FR-E3/FR-E6, FR-W1; P1; integration/static review.
- Input: successful fake iteration and failing iteration with fictional output;
  inspect generated assistant invocation and prompt.
- Expected: result distinguishes success/failure/cancellation; logs remain local;
  runner does not call merge or embed credentials/private manifests/audio; prompt
  preserves separate worktree scope and no outside contacts/publication.
- Evidence: result JSON, argument vector, prompt review. Status: successful/failed
  result assertions passed; prompt policy is coordinator/reviewer evidence, not
  proven by these tests. Limitation: prompts are instructions, not a security sandbox; private
  repository content must still be excluded by operational scope and review.

### DS-10 — task registration timezone and duplicate handling

- Reference/priority/layer: SCH-08; FR-W3; P0; Windows registration integration.
- Preconditions override: isolated disposable Windows scheduled-task name and
  authorized registration; machine timezone PKT and a non-PKT variant.
- Input: inspect registration dry-run; duplicate task; mismatched system timezone;
  inspect task trigger, execution limit and concurrency policy.
- Expected: no accidental overwrite; explicit replacement required if supported;
  trigger represents Pakistan noon; non-PKT host either translates correctly or
  refuses clearly; no machine timezone mutation. Execution bounded to two hours,
  duplicate instances rejected; scheduler not enabled by test-design work.
- Evidence: exported task XML and before/after registry query; cleanup remove only
  disposable task. Status: test author performed static inspection only; coordinator
  reports actual registration and out-of-window task execution below. Limitation:
  source inspection cannot prove a real
  scheduled trigger, authenticated login session or reboot behavior.

### DS-11 — cancellation, restart and output bounds

- Reference/priority/layer: SCH-03/SCH-06/SCH-07; P1; OS integration.
- Input: terminate runner while a child tree is active; restart; child emits large
  stdout/stderr. Interrupt at result-write boundary.
- Expected: no orphan work, recoverable lock, bounded memory/log policy, valid old
  or new last-result (never torn JSON). No duplicate active assistants after restart.
- Evidence: OS liveness and file sizes/JSON parse. Status: actual supervisor crash,
  child termination and lock recovery passed on Windows. Normal atomic replacement
  parses correctly; crash during replacement and high-volume output remain unrun.
  Limitation: log disk quota is a specification gap unless an explicit cap/rotation
  policy is adopted; do not infer it from bounded runtime.

## Execution record and handoff

Test author changed only this catalog and `tests/automation/test_daily_runner.py`;
coordinator owns shared PRD/current-state and consolidated handoff. Source inspected:
`scripts/automation/daily_runner.py`, `register-task.ps1`, AGENTS, current-state,
workflow, role, canonical PRD FR-W/FR-E and architecture ADR-15. No model or PBX
test applies. No scheduler was enabled, model invoked, commit created or push made
by this test task.

Commands on 6 November 2026, Windows, Python 3.11 via uv:

```text
uv run --no-project --python 3.11 python -m unittest discover -s tests/automation -v
# Initial 19 cases: PASS in 9.645 s.
# Expanded 23 cases after implementation fixes: PASS in 9.120 s, zero skips.
uv run --no-project --python 3.11 python scripts/check_foundation.py
# PASS: 16 adapters, 29 skills/214 files, 8 shared roles.
git diff --check
# PASS (tracked changes; new owned files reviewed separately).
```

Concrete findings returned to implementer and fixed before final run:

- High: stale remaining-time budget across workspace validation could permit a
  late launch; final deadline revalidation regression passes.
- Medium: launch exception did not persist terminal failure, allowing another
  automatic same-day attempt; failure/no-retry regression passes.
- Medium: malformed daily ledger `[]`/`null` escaped graceful refusal; regression
  passes. Reviewer's additional state-root bypass and multi-push destination
  findings are covered by passing regressions.

Registration static inspection: UTC `07:00:00Z` trigger, daily interval, duplicate
task refusal, `IgnoreNew`, `PT2H`, interactive token and no machine timezone
mutation are present. This task did not register anything. The coordinator reports
actual task registration and `Start-ScheduledTask` outside the launch window with
`LastTaskResult=0`, no model invocation, next run 7 November at noon PKT; also a
separate read-only authenticated Codex smoke returned `READY`. These are coordinator
observations, not assertions of this suite. A natural noon trigger, duplicate-task
and changed-host-timezone execution remain untested here. Current log output is streamed to
disk with no size cap/rotation; disk exhaustion is an open operational limitation.
Native Windows process-tree evidence does not establish Linux crash behavior.

Next action: coordinator integrates this evidence, completes independent review,
and records any authorized registration separately. Proposed shared-context change:
describe scheduler as tested local development infrastructure with these precise
limits; do not claim AVA product or authenticated assistant acceptance.
