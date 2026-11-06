# Daily development scheduler — 6 November 2026

User approved daily 12:00–14:00 Asia/Karachi development, the proposed phase order,
and verified commits pushed to a dedicated branch/PR without automatic merging.

## Installed setup

- Worktree: `D:/Repositories/ava-daily-development`.
- Branch: `feat/4-daily-development`, based on PR #26 head
  `2a43b60f39aeaccd9978edbbf86d431a72cf9350`.
- Windows task: `AVA-Daily-Development`; daily noon PKT, no missed-run catchup.
- Next scheduled run observed: 7 November 2026, 12:00 PM PKT.
- Current interactive Windows user; least privilege, no stored password; keep
  that user logged in and machine on/online. Screen locking is fine. Desktop
  Codex window need not remain open for this CLI-based route.
- Runtime config, credentials-free pointer and private logs are ignored under
  `.local/automation/`; original data stays outside Git. Actual data-location
  pointer was copied into the isolated worktree's ignored `.local/` directory.
- Prompt: `docs/engineering/daily-development-prompt.md`. Queue: `daily-backlog.md`.
- No new assistant invocation after 13:45; the owned process tree is stopped by
  14:00. Six invocations/day maximum is not six guaranteed commits. Failed or
  partial work is preserved and honestly reported, not committed to satisfy a quota.

## Verification

- Actual authenticated Codex CLI read-only smoke: exit 0, response `READY`.
  No tools or project edits requested by that smoke.
- Runner dry-run: expected Git root/branch/remote, prompt and PKT window verified.
- Actual out-of-window runner execution: skipped, exit 0, no assistant launch.
- Task registration succeeded. Manual Task Scheduler launch outside the window
  completed with `LastTaskResult=0`; state returned to Ready. This validates the
  action/permissions path, not a completed future two-hour development session.
- Windows unit/OS integration tests use fictional repositories and harmless
  child processes. Exact final count is in `docs/qa/daily-scheduler-test-cases.md`.
- Foundation, repository data guard and diff whitespace checks passed.
- Independent review found five concrete issues; all patched: prelaunch cutoff
  drift, process-start containment race, alternate state-root overlap bypass,
  additional push URLs, and cooperative pause before durable reporting.
- Windows CI job now runs scheduler tests. Hosted CI outcome is separate from
  local results and is visible on the dedicated PR.

## Boundaries and operation

Docker is currently unavailable; tasks needing the live lab must record the
dependency and continue independent ready work. Existing data does not establish
final model accuracy, calibration or deployment capacity.

Create `.local/automation/PAUSED` to stop active work promptly; remove it when
ready, noting a failed day's ledger prevents automatic same-day retries.
`COMPLETE` prevents future assistant launches once the approved backlog is done.
To disable the recurring trigger: `Disable-ScheduledTask -TaskName AVA-Daily-Development`.
See `scripts/automation/README.md` for inspection/removal and runtime state.

The agent prompt forbids auto-merge, force-push, changing other checkouts and
publication of audio/weights/consent. These are agent instructions, not a security
sandbox; the supervisor provides process/time/branch-origin checks. Account limits,
authentication expiry and machine downtime can interrupt progress.

Original checkout's pending field-guide PDF/HTML and context edits were not staged
or committed by this setup. No unrelated product code was changed.
