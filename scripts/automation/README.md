# Daily development runner

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
