# Daily development runner

**8 November 2026: the runner now launches Claude Code, not Codex.** Config key
`codex_path` is replaced by `claude_path` (absolute path to `claude.exe`) plus an
optional `model` (`opus` default). Sessions run `claude -p` with `acceptEdits`
and an explicit shell allowlist defined in `daily_runner.py`; permission checks
are never bypassed, and merge, force push, hard reset and web access are denied.
The final message is extracted from the stream-json log into `<date>-<n>.md`.
The task is registered with StartWhenAvailable so a late wake still runs, but
the runner's own 12:00–13:45 launch window and 14:00 cutoff are unchanged.
Text below describing Codex is historical.

**Manual catch-up (9 November 2026).** `--catch-up` runs one make-up session for
two hours from now (launches stop 15 minutes before the end), only when a person
starts it. It uses the same daily ledger, attempt limit, lock, PAUSED/COMPLETE
markers and allowlist, and refuses a window that would cross midnight.
From 11 November each catch-up has its own ledger and log names
(`<date>-catch-up-<n>`), so several make-up sessions can run on one day without
spending the scheduled day's attempts. A session that stops early on purpose creates
`STOP-<ledger name>`; the runner also stops relaunching after any attempt that
ends within five minutes.
**Network and login failures (14 November 2026).** On 11 November a session lost
the network; on 13 November the task fired in standby and the login refresh failed,
which failed the whole day in four seconds. The runner now waits for
`api.anthropic.com` to resolve before every launch (until the launch cutoff), retries
a session that ends on an API error up to three times after a two-minute wait
without failing the day, and holds a system-awake request while it runs. Optional
config `gh_user` passes that account's `gh` token to the session as `GH_TOKEN`, so
the globally active `gh` account cannot break PR edits; no token is stored.
**Installs allowed (owner decision, 14 November 2026).** The allowlist now includes
`uv pip install/add/sync/lock/venv`, `npm install/ci/run`, `npx playwright install`,
`winget install` and `git clone`. The prompt sets the rules: open-source only,
pinned versions committed, licences and hashes recorded, weights and corpora under
`D:/AVA-Data`, nothing that needs administrator rights. Merge, force push, reset,
clean and web tools stay denied.
Sessions also enable Claude Code's PowerShell tool, and every Bash allow/deny rule is
mirrored onto it, because this host's Git Bash is missing `usr\bin\bash.exe`.

Runs the approved backlog in `docs/engineering/daily-development-prompt.md` on
`feat/4-daily-development`, noon–2 PM Pakistan (UTC+05:00). No new invocation starts
at or after 13:45. Each run is cut off at 14:00 even if the trigger was late. A
Windows job object terminates the owned child process tree when the runner exits.
The runner itself never stages, commits, pushes, merges or cleans the worktree.
Those authorized development actions are governed by the agent prompt and tests;
the process boundary is not a security sandbox for generated code.

## Configuration

Keep the machine-specific JSON outside Git, for example
`.local/automation/config.json` in the dedicated worktree:

```json
{
  "codex_path": "C:/absolute/path/to/codex.exe",
  "python_path": "C:/absolute/path/to/python.exe",
  "worktree": "D:/Repositories/ava-daily-development",
  "state_dir": "D:/Repositories/ava-daily-development/.local/automation",
  "branch": "feat/4-daily-development",
  "remote_url": "https://github.com/mIBRAHIM707/Audio-Veracity-Analyzer.git",
  "max_invocations": 6
}
```

`max_invocations` bounds model sessions, not commits. A session may produce no
commit when QA fails. Account limits/authentication/network failures stop the day;
the runner does not purchase credits or fall back to a paid API. Default Codex
model/configuration applies; the prompt requires actual Astra test-design provenance.
No credentials are copied into config. Keep Codex signed in as the task's user.
The state directory is fixed to `worktree/.local/automation`, so a second config
cannot bypass the same worktree's lock or daily ledger. Both fetch and push URL
lists must contain exactly the expected GitHub URL. On Windows the child starts
suspended, is assigned to the kill-on-close job, then resumed before receiving input.

## Setup and operation

1. Run `python scripts/automation/daily_runner.py --config <absolute-config> --dry-run`.
   This reads config/Git/prompt only and does not call the model or write state.
2. Register from PowerShell with
   `scripts/automation/register-task.ps1 -ConfigPath <absolute-config>`.
   Registration refuses to replace an existing task. It uses the current logged-in
   user, no stored password and no elevation. Keep that Windows account logged in,
   machine on/online and scheduled wake permitted. Locking the screen is fine;
   signing out or shutting down prevents this interactive-user task from running.
3. Inspect with the same script plus `-Inspect`; remove with `-Remove`.
   The task defaults to `AVA-Daily-Development`; its UTC trigger is 07:00 daily.
   Missed runs are not started later. Task Scheduler ignores overlapping starts;
   the runner also holds an OS file lock and limits attempts across restarts.
4. Logs are `<date>-<attempt>.jsonl`, final responses `.md`, and a daily JSON ledger
   in `state_dir`. Treat logs as private, since tool output can include local details.
   Inspect failed attempts before resuming. A failed ledger blocks retries that day.
5. Create `state_dir/PAUSED` to stop an active invocation within approximately two
   seconds and prevent subsequent invocations. Remove that file only
   after the blocker is resolved. `COMPLETE` stops future model launches after the
   approved backlog is done. COMPLETE is checked between invocations so a finishing
   agent can save its report. To stop immediately, stop the scheduled task (job
   cleanup kills its tree). A pause-interrupted attempt records exit 125 and stops
   retries for that day, even if the marker is later removed.

`--max-seconds N` is a smoke-test time cap and only shortens the in-window run;
it cannot bypass the noon start or 14:00 cutoff. Logs/attempts are reserved before
launch. Unfinished changes are preserved for the next session—never automatically
reset them. Never let another client write this automation worktree.

The daily cutoff is a last-resort process stop, not a promise that arbitrary code
can gracefully finish at precisely 14:00. The prompt must checkpoint and finish
tests well before it. No automatic merging is configured by this runner or task.
