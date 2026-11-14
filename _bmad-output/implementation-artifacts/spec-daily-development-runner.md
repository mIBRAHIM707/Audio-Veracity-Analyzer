---
title: 'Bounded daily development runner'
type: 'feature'
created: '2026-11-06'
status: 'done'
route: 'oneshot'
---

<frozen-after-approval reason="User approved backlog, noon–2 PM Pakistan time, commits and dedicated PR pushes on 6 November 2026">
## Intent

Provide a local daily development session from 12:00 to 14:00 Asia/Karachi that
continues the approved backlog in an isolated worktree. Verified coherent changes
are committed and pushed to a dedicated branch and draft PR, never merged.
Enforce time limits, prevent overlapping runs, preserve interrupted work, and
provide durable logs/backlog progress. Six commits is a target, not a quota.
Do not require new participant recordings to advance independent software tasks.
</frozen-after-approval>

## Implementation Notes

- Existing main checkout contains pending PDF/context changes and is preserved.
- New worktree D:/Repositories/ava-daily-development, feat/4-daily-development,
  based on PR #26 head 2a43b60f39aeaccd9978edbbf86d431a72cf9350.
- Use Windows Task Scheduler with current interactive user, no saved password;
  Python supervisor invokes authenticated Codex CLI with a bounded deadline.
- Task runs locally; machine must be awake/available with authentication/network.
- Parent owns backlog/prompt/spec and registration; implementer owns runner and
  registration script; actual Astra owns tests. Runtime config/logs are ignored.
- Existing #4 tracks developer environment. PR will be stacked on PR #26's branch
  to avoid duplicating its large foundational diff; a human controls integration.
- 23 actual Astra tests passed without skips, including real Windows locks and
  contained subprocess trees. Foundation/guard/diff checks passed. Authenticated
  read-only Codex smoke returned READY. Registered task action outside the window
  returned 0; next trigger observed 7 November noon PKT. Future full sessions and
  product performance remain unmeasured.

## Review Triage Log

- High / patched: prelaunch validation could consume the remaining deadline;
  recompute at launch and enforce absolute deadline inside polling loop.
- High / patched: child could spawn before job assignment; launch suspended,
  assign job, then resume before supplying prompt.
- Medium / patched: alternative state roots bypassed locks/attempt counters;
  require canonical worktree/.local/automation state.
- High / patched: first push URL check missed extra push destinations; require
  singleton approved fetch and push URLs using --all.
- Medium / patched: self-created PAUSED could interrupt final reporting; save
  report and verified work first, marker is the final tool action.
- Independent reviewer re-read final changes and confirmed all five closed.
- Limitation: private log disk size is not capped; runtime and attempts are bounded.
