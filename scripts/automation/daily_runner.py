"""Bounded daily Codex runner. All agent publication is governed by the prompt.

The Windows job object contains every launched descendant and kills it on runner
exit, including unexpected runner termination. No shell-built commands are used.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time as clock

PKT = timezone(timedelta(hours=5), "Asia/Karachi")
REMOTE = "https://github.com/mIBRAHIM707/Audio-Veracity-Analyzer.git"
BRANCH = "feat/4-daily-development"


def session_window(now: datetime) -> tuple[datetime, datetime, datetime]:
    if now.tzinfo is None:
        raise ValueError("Timezone-aware time required")
    day = now.astimezone(PKT).date()
    return tuple(datetime.combine(day, value, PKT) for value in
                 (time(12), time(13, 45), time(14)))


def load_config(path: Path) -> dict:
    if path.stat().st_size > 32_768:
        raise ValueError("Config too large")
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("Config must be an object")
    for name in ("codex_path", "python_path", "worktree", "state_dir"):
        value = config.get(name)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError(f"{name} must be an absolute path")
        config[name] = str(Path(value).resolve())
    for name in ("codex_path", "python_path"):
        if not Path(config[name]).is_file():
            raise ValueError(f"Missing executable: {name}")
    if not Path(config["worktree"]).is_dir():
        raise ValueError("Missing worktree")
    if config.get("branch") != BRANCH:
        raise ValueError(f"Only the dedicated {BRANCH} branch is allowed")
    if config.get("remote_url", REMOTE) != REMOTE:
        raise ValueError("Unexpected remote URL")
    count = config.get("max_invocations", 6)
    if type(count) is not int or not 1 <= count <= 6:
        raise ValueError("max_invocations must be an integer from 1 to 6")
    config["max_invocations"] = count
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


def build_command(config: dict, output_path: Path) -> list[str]:
    return [config["codex_path"], "exec", "-c", "approval_policy=\"never\"",
            "--sandbox", "danger-full-access", "--json", "--color", "never",
            "--output-last-message", str(output_path), "-C", config["worktree"], "-"]


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
              wall_deadline: datetime | None = None) -> int:
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
                                    stderr=subprocess.STDOUT,
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
    args = parser.parse_args(argv)
    try:
        if args.max_seconds is not None and (not 0 < args.max_seconds <= 7200):
            raise ValueError("max-seconds must be > 0 and <= 7200")
        config = load_config(args.config)
        workspace = validate_workspace(config)
        now = datetime.now(PKT)
        start, soft, deadline = session_window(now)
        if args.dry_run:
            print(json.dumps({"dry_run": True, "workspace": workspace,
                              "start": start.isoformat(), "soft_stop": soft.isoformat(),
                              "deadline": deadline.isoformat(), "in_window": start <= now < soft,
                              "command": build_command(config, Path(config["state_dir"]) / "last.md")}, indent=2))
            return 0
        if not start <= now < soft:
            print("Skipped: outside launch window 12:00–13:45 Asia/Karachi")
            return 0
        state = Path(config["state_dir"])
        state.mkdir(parents=True, exist_ok=True)
        with FileLock(state / "runner.lock"):
            day = now.date().isoformat()
            ledger_path = state / f"{day}.json"
            if ledger_path.exists() and ledger_path.stat().st_size > 131_072:
                raise ValueError("Daily ledger too large; manual inspection required")
            ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {"date": day, "attempts": 0}
            if not isinstance(ledger, dict) or ledger.get("date") != day:
                raise ValueError("Invalid daily ledger; manual inspection required")
            if ledger.get("failed"):
                print("Skipped: previous run failed today; inspect the daily ledger")
                return 0
            if type(ledger.get("attempts")) is not int or not 0 <= ledger["attempts"] <= 6:
                raise ValueError("Invalid daily ledger; manual inspection required")
            monotonic_deadline = clock.monotonic() + min((deadline - datetime.now(PKT)).total_seconds(), args.max_seconds or 7200)
            while ledger["attempts"] < config["max_invocations"]:
                if (state / "PAUSED").exists() or (state / "COMPLETE").exists():
                    print("Skipped: PAUSED or COMPLETE flag present")
                    return 0
                current = datetime.now(PKT)
                remaining = min((deadline - current).total_seconds(), monotonic_deadline - clock.monotonic())
                if current >= soft or remaining <= 0:
                    break
                workspace = validate_workspace(config)
                current = datetime.now(PKT)
                remaining = min((deadline - current).total_seconds(), monotonic_deadline - clock.monotonic())
                if current >= soft or remaining <= 0:
                    break
                ledger["attempts"] += 1
                ledger.update({"last_started": current.isoformat(), "before": workspace})
                atomic_json(ledger_path, ledger)  # Reserve attempt before any launch.
                stem = f"{day}-{ledger['attempts']:02d}"
                prompt = Path(workspace["prompt"]).read_text(encoding="utf-8")
                prompt += (f"\n\nRUN CONTEXT: date={day}; attempt={ledger['attempts']}; "
                           f"soft stop={soft.isoformat()}; absolute deadline={deadline.isoformat()}; remaining seconds={remaining:.0f}. "
                           "Finish and checkpoint before the remaining budget expires. "
                           f"State directory: {state}. Create COMPLETE only when the agreed backlog is finished; "
                           "create PAUSED when only external blockers remain. Never merge a PR or push main.\n")
                try:
                    current = datetime.now(PKT)
                    remaining = min((deadline - current).total_seconds(), monotonic_deadline - clock.monotonic())
                    if current >= soft or remaining <= 0:
                        ledger["stop_reason"] = "launch cutoff reached during preparation"
                        atomic_json(ledger_path, ledger)
                        break
                    code = run_child(build_command(config, state / f"{stem}.md"), prompt,
                                     state / f"{stem}.jsonl", remaining, state / "PAUSED", deadline)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    ledger.update({"last_exit": 1, "last_finished": datetime.now(PKT).isoformat(),
                                   "failed": True, "error": str(exc)})
                    atomic_json(ledger_path, ledger)
                    raise
                ledger.update({"last_exit": code, "last_finished": datetime.now(PKT).isoformat(),
                               "failed": code != 0})
                atomic_json(ledger_path, ledger)
                if code:
                    return code
            print("Daily session finished; see state directory for logs and handoffs")
            return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Daily runner stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
