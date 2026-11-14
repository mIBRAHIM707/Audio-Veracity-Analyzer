"""Bounded daily Claude Code runner. All agent publication is governed by the prompt.

Switched from Codex to the Claude Code CLI on 8 November 2026 (Codex/Astra access
ended). The runner launches `claude -p` headless with the daily prompt on stdin.

The Windows job object contains every launched descendant and kills it on runner
exit, including unexpected runner termination. No shell-built commands are used.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
from ctypes import wintypes
from datetime import datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time as clock

PKT = timezone(timedelta(hours=5), "Asia/Karachi")
REMOTE = "https://github.com/mIBRAHIM707/Audio-Veracity-Analyzer.git"
BRANCH = "feat/4-daily-development"
MIN_PRODUCTIVE_SECONDS = 300  # 10 Nov 2026: four idle relaunches in a row each wrote only a report.
API_HOST = "api.anthropic.com"
MAX_TRANSIENT_FAILURES = 3   # Network/login failures retried per session before the day is failed.
TRANSIENT_RETRY_SECONDS = 120
MODELS = {"opus", "sonnet", "claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5"}


def session_window(now: datetime, hours: int = 2) -> tuple[datetime, datetime, datetime]:
    """Noon start; `hours` long (owner decision 14 Nov 2026: configurable, 2 to 10); launches stop 15 min early."""
    if now.tzinfo is None:
        raise ValueError("Timezone-aware time required")
    if type(hours) is not int or not 2 <= hours <= 10:
        raise ValueError("window_hours must be an integer from 2 to 10")
    start = datetime.combine(now.astimezone(PKT).date(), time(12), PKT)
    deadline = start + timedelta(hours=hours)
    return start, deadline - timedelta(minutes=15), deadline


def catch_up_window(now: datetime) -> tuple[datetime, datetime, datetime]:
    """Manual two-hour make-up session starting now (user-invoked only, never scheduled)."""
    if now.tzinfo is None:
        raise ValueError("Timezone-aware time required")
    start = now.astimezone(PKT)
    deadline = start + timedelta(hours=2)
    if deadline.date() != start.date():
        raise ValueError("Catch-up session must finish before midnight Asia/Karachi")
    return start, deadline - timedelta(minutes=15), deadline


def load_config(path: Path) -> dict:
    if path.stat().st_size > 32_768:
        raise ValueError("Config too large")
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("Config must be an object")
    for name in ("claude_path", "python_path", "worktree", "state_dir"):
        value = config.get(name)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError(f"{name} must be an absolute path")
        config[name] = str(Path(value).resolve())
    model = config.get("model", "opus")
    if model not in MODELS:
        raise ValueError(f"model must be one of {sorted(MODELS)}")
    config["model"] = model
    gh_user = config.get("gh_user")
    if gh_user is not None and (not isinstance(gh_user, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", gh_user)):
        raise ValueError("gh_user must be a GitHub login")
    for name in ("claude_path", "python_path"):
        if not Path(config[name]).is_file():
            raise ValueError(f"Missing executable: {name}")
    if not Path(config["worktree"]).is_dir():
        raise ValueError("Missing worktree")
    if config.get("branch") != BRANCH:
        raise ValueError(f"Only the dedicated {BRANCH} branch is allowed")
    if config.get("remote_url", REMOTE) != REMOTE:
        raise ValueError("Unexpected remote URL")
    count = config.get("max_invocations", 6)
    if type(count) is not int or not 1 <= count <= 12:
        raise ValueError("max_invocations must be an integer from 1 to 12")
    config["max_invocations"] = count
    hours = config.get("window_hours", 2)
    if type(hours) is not int or not 2 <= hours <= 10:
        raise ValueError("window_hours must be an integer from 2 to 10")
    config["window_hours"] = hours
    config["remote_url"] = REMOTE
    state = Path(config["state_dir"])
    worktree = Path(config["worktree"])
    if state != worktree / ".local" / "automation":
        raise ValueError("State directory must be worktree/.local/automation")
    return config


def git(config: dict, *args: str) -> str:
    return subprocess.check_output(["git", "-C", config["worktree"], *args],
                                   text=True, timeout=20).strip()


def validate_workspace(config: dict) -> dict:
    actual = Path(git(config, "rev-parse", "--show-toplevel")).resolve()
    if actual != Path(config["worktree"]):
        raise ValueError("Git root does not match configured worktree")
    if git(config, "branch", "--show-current") != config["branch"]:
        raise ValueError("Unexpected branch; refusing to run")
    if git(config, "remote", "get-url", "--all", "origin").splitlines() != [config["remote_url"]]:
        raise ValueError("Unexpected origin; refusing to run")
    if git(config, "remote", "get-url", "--push", "--all", "origin").splitlines() != [config["remote_url"]]:
        raise ValueError("Unexpected push URL; refusing to run")
    prompt = Path(config["worktree"]) / "docs/engineering/daily-development-prompt.md"
    if not prompt.is_file() or prompt.stat().st_size > 64_000:
        raise ValueError("Missing or oversized daily prompt")
    return {"head": git(config, "rev-parse", "HEAD"),
            "status": git(config, "status", "--short"), "prompt": str(prompt)}


class FileLock:
    """Kernel-held lock: abandoned processes cannot leave a stale ownership lock."""
    def __init__(self, path: Path):
        self.path, self.handle = path, None

    def __enter__(self):
        self.handle = open(self.path, "a+b")
        self.handle.seek(0, 2)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise RuntimeError("Another daily runner already owns the worktree")
        return self

    def __exit__(self, *exc):
        if self.handle:
            self.handle.close()
            self.handle = None


class WindowsJob:
    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t),
                        ("max_ws", ctypes.c_size_t), ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, proc):
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(proc._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


# Unattended sessions never bypass permissions. File edits inside the worktree are
# auto-accepted; shell use is limited to this allowlist; anything else is denied
# because nobody is present to approve it.
ALLOWED_TOOLS = (
    "Read", "Edit", "Write", "Glob", "Grep", "TodoWrite", "Task",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
    "Bash(git add:*)", "Bash(git commit:*)", "Bash(git fetch:*)", "Bash(git rev-parse:*)",
    "Bash(git branch --show-current)", "Bash(git ls-remote:*)",
    "Bash(git push origin HEAD:refs/heads/feat/4-daily-development)",
    "Bash(uv run:*)", "Bash(npm run test:*)", "Bash(npx playwright test:*)",
    # Owner decision, 14 Nov 2026: sessions may install the software the backlog needs.
    "Bash(uv pip install:*)", "Bash(uv pip list:*)", "Bash(uv pip show:*)", "Bash(uv add:*)",
    "Bash(uv sync:*)", "Bash(uv lock:*)", "Bash(uv venv:*)", "Bash(uv tool install:*)",
    "Bash(npm install:*)", "Bash(npm ci:*)", "Bash(npm run:*)", "Bash(npx playwright install:*)",
    "Bash(winget install:*)", "Bash(winget list:*)", "Bash(winget search:*)", "Bash(winget show:*)",
    "Bash(git clone:*)", "Bash(git check-ignore:*)",
    # 14 Nov 2026 (later): sessions must see CI results; owner allowed web research and Docker.
    "Bash(gh run list:*)", "Bash(gh run view:*)", "Bash(gh pr checks:*)",
    "Bash(docker:*)", "Bash(wsl --status)", "Bash(wsl --list:*)",
    "WebFetch", "WebSearch",
    "Bash(gh pr view:*)", "Bash(gh pr list:*)", "Bash(gh pr edit:*)",
    "Bash(gh issue view:*)", "Bash(gh issue list:*)", "Bash(gh issue create:*)",
)
DENIED_TOOLS = (
    "Bash(gh pr merge:*)", "Bash(git push --force:*)", "Bash(git push -f:*)",
    "Bash(git reset --hard:*)", "Bash(git clean:*)", "Bash(git checkout main:*)",
    "Bash(gh run rerun:*)", "Bash(gh run cancel:*)", "Bash(gh workflow:*)",
    "Bash(docker login:*)", "Bash(docker push:*)", "Bash(docker system prune:*)",
)


def _with_powershell(rules: tuple[str, ...]) -> tuple[str, ...]:
    """Mirror every Bash rule onto the PowerShell tool (Git Bash is broken on the host, 9 Nov 2026)."""
    return rules + tuple("PowerShell" + rule[4:] for rule in rules if rule.startswith("Bash("))


ALLOWED_TOOLS = _with_powershell(ALLOWED_TOOLS)
DENIED_TOOLS = _with_powershell(DENIED_TOOLS)
CHILD_ENV = {"CLAUDE_CODE_USE_POWERSHELL_TOOL": "1"}


def build_command(config: dict) -> list[str]:
    """Headless Claude Code session; prompt on stdin, cwd is the worktree."""
    return [config["claude_path"], "-p", "--output-format", "stream-json", "--verbose",
            "--model", config["model"], "--permission-mode", "acceptEdits",
            "--allowedTools", ",".join(ALLOWED_TOOLS),
            "--disallowedTools", ",".join(DENIED_TOOLS)]


def write_final_message(log_path: Path, output_path: Path) -> bool:
    """Copy the session's final `result` text from the stream-json log, if any."""
    final = None
    try:
        with log_path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "result":
                    final = event.get("result")
    except OSError:
        return False
    if not isinstance(final, str):
        return False
    output_path.write_text(final, encoding="utf-8")
    return True


def transient_failure(log_path: Path) -> bool:
    """True when the session ended on an API/network/login error, not on its own work (13 Nov 2026)."""
    final = None
    try:
        with log_path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "result":
                    final = event
    except OSError:
        return False
    return isinstance(final, dict) and final.get("terminal_reason") == "api_error"


def network_ready(host: str = API_HOST) -> bool:
    try:
        socket.getaddrinfo(host, 443)
        return True
    except OSError:
        return False


def wait_for_network(until: datetime, stop_path: Path, poll: float = 15.0) -> bool:
    """Block until the API host resolves; False at the launch cutoff or on PAUSED."""
    while datetime.now(PKT) < until and not stop_path.exists():
        if network_ready():
            return True
        clock.sleep(poll)
    return False


def keep_awake(enabled: bool) -> None:
    """Stop idle standby from freezing a running session (Modern Standby host)."""
    if os.name == "nt":
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if enabled else 0x80000000)


@contextlib.contextmanager
def staying_awake():
    keep_awake(True)
    try:
        yield
    finally:
        keep_awake(False)


def github_env(config: dict) -> dict:
    """Token for the configured gh account, so the global active account cannot break PR edits."""
    user, gh = config.get("gh_user"), shutil.which("gh")
    if not user or not gh:
        return {}
    try:
        done = subprocess.run([gh, "auth", "token", "--user", user], capture_output=True,
                              text=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.SubprocessError):
        return {}
    token = done.stdout.strip()
    return {"GH_TOKEN": token} if done.returncode == 0 and token else {}


def resume_child(proc):
    """Resume only after job assignment; suspended start closes descendant race."""
    if os.name == "nt":
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess.restype = ctypes.c_long
        status = ntdll.NtResumeProcess(wintypes.HANDLE(proc._handle))
        if status < 0:
            raise OSError(f"Cannot resume contained child; NTSTATUS={status:#x}")


def run_child(command: list[str], prompt: str, log_path: Path, timeout: float,
              stop_path: Path | None = None,
              wall_deadline: datetime | None = None,
              cwd: str | None = None, extra_env: dict | None = None) -> int:
    if timeout <= 0:
        return 124
    deadline = clock.monotonic() + timeout
    if wall_deadline is not None and wall_deadline.tzinfo is None:
        raise ValueError("Wall deadline must be timezone-aware")
    job = WindowsJob()
    proc = None
    try:
        with log_path.open("xb") as output:
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output,
                                    stderr=subprocess.STDOUT, cwd=cwd,
                                    env={**os.environ, **CHILD_ENV, **(extra_env or {})},
                                    creationflags=(subprocess.CREATE_NO_WINDOW | 0x4) if os.name == "nt" else 0,
                                    start_new_session=os.name != "nt")
            # CREATE_SUSPENDED prevents even startup hooks spawning outside the job.
            job.assign(proc)
            resume_child(proc)
            first = True
            while True:
                if stop_path is not None and stop_path.exists():
                    return 125
                remaining = deadline - clock.monotonic()
                if wall_deadline is not None:
                    remaining = min(remaining, (wall_deadline - datetime.now(PKT)).total_seconds())
                if remaining <= 0:
                    return 124
                try:
                    proc.communicate(prompt.encode("utf-8") if first else None,
                                     timeout=min(2.0, remaining))
                    return proc.returncode
                except subprocess.TimeoutExpired:
                    first = False
    finally:
        job.close()  # Also reaps descendants left behind after normal parent exit.
        if proc and os.name != "nt":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if proc and proc.poll() is None:
            if os.name == "nt":
                proc.kill()  # Fallback when assignment failed before the job owned it.
            proc.wait(timeout=10)


def atomic_json(path: Path, value: dict):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-seconds", type=float, help="Only shortens this run; never bypasses the daily window")
    parser.add_argument("--catch-up", action="store_true",
                        help="Manual make-up session: two hours from now instead of noon-2 PM; same ledger and guards")
    args = parser.parse_args(argv)
    try:
        if args.max_seconds is not None and (not 0 < args.max_seconds <= 7200):
            raise ValueError("max-seconds must be > 0 and <= 7200")
        config = load_config(args.config)
        workspace = validate_workspace(config)
        now = datetime.now(PKT)
        start, soft, deadline = catch_up_window(now) if args.catch_up else session_window(now, config["window_hours"])
        if args.dry_run:
            print(json.dumps({"dry_run": True, "workspace": workspace,
                              "start": start.isoformat(), "soft_stop": soft.isoformat(),
                              "deadline": deadline.isoformat(), "in_window": start <= now < soft,
                              "command": build_command(config)}, indent=2))
            return 0
        if not start <= now < soft:
            print(f"Skipped: outside launch window {start:%H:%M}–{soft:%H:%M} Asia/Karachi")
            return 0
        state = Path(config["state_dir"])
        state.mkdir(parents=True, exist_ok=True)
        with FileLock(state / "runner.lock"), staying_awake():
            day = now.date().isoformat()
            label = day
            if args.catch_up:  # Each make-up session has its own ledger; the scheduled day is untouched.
                index = next((i for i in range(1, 10) if not (state / f"{day}-catch-up-{i}.json").exists()), None)
                if index is None:
                    raise ValueError("Too many catch-up sessions today; manual inspection required")
                label = f"{day}-catch-up-{index}"
            ledger_path = state / f"{label}.json"
            stop_marker = state / f"STOP-{label}"
            if ledger_path.exists() and ledger_path.stat().st_size > 131_072:
                raise ValueError("Daily ledger too large; manual inspection required")
            ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {"date": day, "attempts": 0}
            if not isinstance(ledger, dict) or ledger.get("date") != day:
                raise ValueError("Invalid daily ledger; manual inspection required")
            if ledger.get("failed"):
                print("Skipped: previous run failed today; inspect the daily ledger")
                return 0
            if type(ledger.get("attempts")) is not int or not 0 <= ledger["attempts"] <= 12:
                raise ValueError("Invalid daily ledger; manual inspection required")
            monotonic_deadline = clock.monotonic() + min((deadline - datetime.now(PKT)).total_seconds(), args.max_seconds or 36_000)
            while ledger["attempts"] < config["max_invocations"]:
                if (state / "PAUSED").exists() or (state / "COMPLETE").exists():
                    print("Skipped: PAUSED or COMPLETE flag present")
                    return 0
                if stop_marker.exists():
                    ledger["stop_reason"] = "agent stopped this session (no ready task fits the remaining time)"
                    atomic_json(ledger_path, ledger)
                    break
                current = datetime.now(PKT)
                remaining = min((deadline - current).total_seconds(), monotonic_deadline - clock.monotonic())
                if current >= soft or remaining <= 0:
                    break
                if not wait_for_network(soft, state / "PAUSED"):  # Wake from standby: network may lag.
                    ledger["stop_reason"] = "no network before the launch cutoff"
                    atomic_json(ledger_path, ledger)
                    break
                workspace = validate_workspace(config)
                current = datetime.now(PKT)
                remaining = min((deadline - current).total_seconds(), monotonic_deadline - clock.monotonic())
                if current >= soft or remaining <= 0:
                    break
                ledger["attempts"] += 1
                ledger.update({"last_started": current.isoformat(), "before": workspace})
                atomic_json(ledger_path, ledger)  # Reserve attempt before any launch.
                stem = f"{label}-{ledger['attempts']:02d}"
                prompt = Path(workspace["prompt"]).read_text(encoding="utf-8")
                prompt += (f"\n\nRUN CONTEXT: date={day}; attempt={ledger['attempts']}; "
                           f"soft stop={soft.isoformat()}; absolute deadline={deadline.isoformat()}; remaining seconds={remaining:.0f}. "
                           "Finish and checkpoint before the remaining budget expires. "
                           f"State directory: {state}. Create COMPLETE only when the agreed backlog is finished; "
                           "create PAUSED when only external blockers remain. "
                           f"If you stop early because no ready task can reach a reviewed checkpoint before the soft stop, "
                           f"create {stop_marker} so this session is not relaunched. Never merge a PR or push main.\n")
                try:
                    current = datetime.now(PKT)
                    remaining = min((deadline - current).total_seconds(), monotonic_deadline - clock.monotonic())
                    if current >= soft or remaining <= 0:
                        ledger["stop_reason"] = "launch cutoff reached during preparation"
                        atomic_json(ledger_path, ledger)
                        break
                    launched = clock.monotonic()
                    code = run_child(build_command(config), prompt,
                                     state / f"{stem}.jsonl", remaining, state / "PAUSED", deadline,
                                     cwd=config["worktree"], extra_env=github_env(config))
                    write_final_message(state / f"{stem}.jsonl", state / f"{stem}.md")
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    ledger.update({"last_exit": 1, "last_finished": datetime.now(PKT).isoformat(),
                                   "failed": True, "error": str(exc)})
                    atomic_json(ledger_path, ledger)
                    raise
                retry = (code not in (0, 124, 125) and transient_failure(state / f"{stem}.jsonl")
                         and ledger.get("transient_failures", 0) < MAX_TRANSIENT_FAILURES)
                if retry:  # Network/login error: unfinished work stays in the worktree for the relaunch.
                    ledger["transient_failures"] = ledger.get("transient_failures", 0) + 1
                ledger.update({"last_exit": code, "last_finished": datetime.now(PKT).isoformat(),
                               "failed": code != 0 and not retry})
                atomic_json(ledger_path, ledger)
                if retry:
                    clock.sleep(TRANSIENT_RETRY_SECONDS)
                    continue
                if code:
                    return code
                if clock.monotonic() - launched < MIN_PRODUCTIVE_SECONDS:
                    ledger["stop_reason"] = "attempt ended within minutes; not relaunching an idle session"
                    atomic_json(ledger_path, ledger)
                    break
            print("Daily session finished; see state directory for logs and handoffs")
            return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Daily runner stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
