"""GPT-6 Astra scheduler cases, 2026-11-06; fictional/offline fixtures only."""
from contextlib import redirect_stdout, redirect_stderr
import ctypes
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/automation/daily_runner.py"
SPEC = importlib.util.spec_from_file_location("daily_runner", SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class FixedDateTime(datetime):
    value = datetime(2026, 11, 6, 12, tzinfo=runner.PKT)

    @classmethod
    def now(cls, tz=None):
        return cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)


def alive(pid):
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.work = self.root / "work"
        self.work.mkdir()
        self.state = self.work / ".local/automation"
        self.prompt = self.work / "docs/engineering/daily-development-prompt.md"
        self.prompt.parent.mkdir(parents=True)
        self.prompt.write_text("Fictional task. Never merge. No outside contacts.", encoding="utf-8")
        self.config = dict(claude_path=sys.executable, python_path=sys.executable,
                           worktree=str(self.work), state_dir=str(self.state),
                           branch=runner.BRANCH, remote_url=runner.REMOTE, max_invocations=2)
        self.config_path = self.root / "config.json"
        self.save_config()
        FixedDateTime.value = datetime(2026, 11, 6, 12, tzinfo=runner.PKT)

    def save_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def run_main(self, *args, child=None):
        workspace = {"head": "fictional-revision", "status": "?? sentinel.txt", "prompt": str(self.prompt)}
        with patch.object(runner, "datetime", FixedDateTime), \
                patch.object(runner, "validate_workspace", return_value=workspace), \
                patch.object(runner, "run_child", side_effect=child or (lambda *a, **k: 0)) as launch, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = runner.main(["--config", str(self.config_path), *args])
        return result, launch

    def ledger(self):
        return json.loads((self.state / "2026-11-06.json").read_text())

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.work), *args], text=True,
                                       stderr=subprocess.STDOUT, timeout=10).strip()

    def init_git(self):
        self.git("init", "-b", runner.BRANCH)
        self.git("-c", "user.name=Fictional Test", "-c", "user.email=fictional@example.invalid",
                 "commit", "--allow-empty", "-m", "fixture")
        self.git("remote", "add", "origin", runner.REMOTE)

    def test_window_uses_pakistan_date_across_utc_calendar_boundary(self):
        for value, date in [(datetime(2026, 12, 31, 20, tzinfo=timezone.utc), (2027, 1, 1)),
                            (datetime(2026, 11, 6, 6, 59, tzinfo=timezone.utc), (2026, 11, 6))]:
            with self.subTest(value=value):
                start, soft, end = runner.session_window(value)
                self.assertEqual(start, datetime(*date, 12, tzinfo=runner.PKT))
                self.assertEqual(soft, datetime(*date, 13, 45, tzinfo=runner.PKT))
                self.assertEqual(end, datetime(*date, 14, tzinfo=runner.PKT))
                self.assertEqual((end - start).total_seconds(), 7200)
        with self.assertRaises(ValueError):
            runner.session_window(datetime(2026, 11, 6, 12))

    # Claude-authored, 9 Nov 2026: manual catch-up window.
    def test_catch_up_window_is_two_hours_from_now_and_refuses_midnight_crossing(self):
        now = datetime(2026, 11, 9, 15, 8, tzinfo=runner.PKT)
        start, soft, end = runner.catch_up_window(now)
        self.assertEqual(start, now)
        self.assertEqual(soft, datetime(2026, 11, 9, 16, 53, tzinfo=runner.PKT))
        self.assertEqual((end - start).total_seconds(), 7200)
        runner.catch_up_window(datetime(2026, 11, 9, 21, 59, tzinfo=runner.PKT))
        for bad in (datetime(2026, 11, 9, 22, 0, 1, tzinfo=runner.PKT), datetime(2026, 11, 9, 15)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                runner.catch_up_window(bad)

    def test_catch_up_launches_outside_noon_window_with_two_hour_cap_and_same_ledger(self):
        FixedDateTime.value = datetime(2026, 11, 6, 15, 8, tzinfo=runner.PKT)
        result, launch = self.run_main()
        self.assertEqual(result, 0)
        launch.assert_not_called()
        result, launch = self.run_main("--catch-up")
        self.assertEqual(result, 0)
        self.assertGreater(launch.call_count, 0)
        for call in launch.call_args_list:
            self.assertGreater(call.args[3], 0)
            self.assertLessEqual(call.args[3], 7200)
        self.assertEqual(self.ledger()["date"], "2026-11-06")

    def test_no_launch_before_noon_or_at_soft_or_hard_cutoff(self):
        for hour, minute, second, microsecond in [(11, 59, 59, 999999), (13, 45, 0, 0),
                                                  (14, 0, 0, 0), (23, 59, 59, 0)]:
            with self.subTest(hour=hour, minute=minute):
                FixedDateTime.value = datetime(2026, 11, 6, hour, minute, second, microsecond, runner.PKT)
                result, launch = self.run_main()
                self.assertEqual(result, 0)
                launch.assert_not_called()
                self.assertFalse(self.state.exists())

    def test_noon_launches_and_daily_budget_survives_restart(self):
        result, launch = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(self.ledger()["attempts"], 2)
        self.assertFalse(self.ledger()["failed"])
        self.assertFalse((self.state / "COMPLETE").exists())
        result, launch = self.run_main()
        self.assertEqual(result, 0)
        launch.assert_not_called()

    def test_last_microsecond_before_soft_cutoff_can_start_but_is_hard_capped(self):
        FixedDateTime.value = datetime(2026, 11, 6, 13, 44, 59, 999999, runner.PKT)
        result, launch = self.run_main("--max-seconds", "7200")
        self.assertEqual(result, 0)
        self.assertGreater(launch.call_count, 0)
        for call in launch.call_args_list:
            self.assertGreater(call.args[3], 0)
            self.assertLessEqual(call.args[3], 900.000001)

    def test_max_seconds_only_shortens_remaining_budget(self):
        result, launch = self.run_main("--max-seconds", "1")
        self.assertEqual(result, 0)
        for call in launch.call_args_list:
            self.assertGreater(call.args[3], 0)
            self.assertLessEqual(call.args[3], 1)

    def test_invalid_duration_never_launches(self):
        for value in ("0", "-1", "nan", "inf", "-inf", "7201", "text"):
            with self.subTest(value=value):
                try:
                    result, launch = self.run_main("--max-seconds=" + value)
                except SystemExit as exc:
                    self.assertNotEqual(exc.code, 0)
                else:
                    self.assertNotEqual(result, 0)
                    launch.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_dry_run_preserves_real_git_and_creates_no_state(self):
        self.init_git()
        sentinel = self.work / "sentinel.txt"
        sentinel.write_text("preserve this", encoding="utf-8")
        before = (self.git("rev-parse", "HEAD"), self.git("status", "--porcelain"), sentinel.read_bytes())
        with patch.object(runner, "run_child") as launch, redirect_stdout(io.StringIO()):
            result = runner.main(["--config", str(self.config_path), "--dry-run"])
        self.assertEqual(result, 0)
        launch.assert_not_called()
        self.assertEqual(before, (self.git("rev-parse", "HEAD"), self.git("status", "--porcelain"), sentinel.read_bytes()))
        self.assertFalse(self.state.exists())

    def test_workspace_rejects_wrong_branch_main_master_and_detached(self):
        self.init_git()
        self.assertIn("head", runner.validate_workspace(self.config))
        for branch in ("main", "master", "other-feature"):
            self.git("checkout", "-b", branch)
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                runner.validate_workspace(self.config)
        self.git("checkout", "--detach")
        with self.assertRaises(ValueError):
            runner.validate_workspace(self.config)

    def test_workspace_rejects_subdirectory_and_unexpected_push_remote(self):
        self.init_git()
        nested = dict(self.config, worktree=str(self.prompt.parent))
        with self.assertRaises(ValueError):
            runner.validate_workspace(nested)
        self.git("remote", "set-url", "--push", "origin", "https://example.invalid/fictional.git")
        with self.assertRaises(ValueError):
            runner.validate_workspace(self.config)

    def test_workspace_rejects_additional_push_destination(self):
        self.init_git()
        self.git("remote", "set-url", "--push", "origin", runner.REMOTE)
        self.git("remote", "set-url", "--add", "--push", "origin", "https://example.invalid/unapproved.git")
        with self.assertRaises(ValueError):
            runner.validate_workspace(self.config)

    def test_claude_command_never_bypasses_permissions_and_denies_merge(self):
        # Claude-authored (8 Nov 2026) after the switch from Codex.
        config = runner.load_config(self.config_path)
        command = runner.build_command(config)
        self.assertEqual(command[:2], [config["claude_path"], "-p"])
        joined = " ".join(command)
        self.assertNotIn("bypassPermissions", joined)
        self.assertNotIn("dangerously", joined)
        self.assertEqual(command[command.index("--permission-mode") + 1], "acceptEdits")
        allowed = command[command.index("--allowedTools") + 1].split(",")
        denied = command[command.index("--disallowedTools") + 1].split(",")
        self.assertIn("Bash(git push origin HEAD:refs/heads/feat/4-daily-development)", allowed)
        self.assertFalse(any(tool.startswith("Bash(git push") and "feat/4-daily-development" not in tool
                             for tool in allowed))
        self.assertIn("Bash(gh pr merge:*)", denied)
        self.assertNotIn("Bash(*)", allowed)
        self.assertNotIn("Bash", allowed)
        # Claude-authored (9 Nov 2026): PowerShell tool mirrors the same rules, never unrestricted.
        for shell in ("Bash", "PowerShell"):
            self.assertIn(f"{shell}(gh pr merge:*)", denied)
            self.assertIn(f"{shell}(git push --force:*)", denied)
        self.assertIn("PowerShell(git push origin HEAD:refs/heads/feat/4-daily-development)", allowed)
        self.assertFalse(any(tool.startswith("PowerShell(git push") and "feat/4-daily-development" not in tool
                             for tool in allowed))
        self.assertNotIn("PowerShell", allowed)
        self.assertNotIn("PowerShell(*)", allowed)
        self.assertEqual(len([t for t in allowed if t.startswith("Bash(")]),
                         len([t for t in allowed if t.startswith("PowerShell(")]))
        self.assertEqual(runner.CHILD_ENV, {"CLAUDE_CODE_USE_POWERSHELL_TOOL": "1"})

    def test_final_message_extracted_from_stream_json_or_absent(self):
        log, out = self.root / "s.jsonl", self.root / "s.md"
        log.write_text('{"type":"system"}\nnot json\n{"type":"result","result":"done: 2 commits"}\n',
                       encoding="utf-8")
        self.assertTrue(runner.write_final_message(log, out))
        self.assertEqual(out.read_text(encoding="utf-8"), "done: 2 commits")
        log.write_text('{"type":"assistant"}\n', encoding="utf-8")
        out.unlink()
        self.assertFalse(runner.write_final_message(log, out))
        self.assertFalse(out.exists())
        self.assertFalse(runner.write_final_message(self.root / "missing.jsonl", out))

    def test_config_rejects_unsafe_or_malformed_values(self):
        base = self.config.copy()
        for field, value in [("branch", "main"), ("branch", "master"), ("max_invocations", True),
                             ("max_invocations", 0), ("max_invocations", 7), ("max_invocations", 1.5),
                             ("worktree", "relative"), ("claude_path", ""), ("model", "gpt-6-astra"),
                             ("remote_url", "https://example.invalid/repo"), ("state_dir", str(self.work)),
                             ("state_dir", str(self.root / "alternate-state")),
                             ("state_dir", str(self.work / ".local/alternate-state"))]:
            with self.subTest(field=field, value=value):
                self.config = dict(base, **{field: value})
                self.save_config()
                with self.assertRaises(ValueError):
                    runner.load_config(self.config_path)
        for raw in ("[]", "null", "{", " " * 32769):
            self.config_path.write_text(raw)
            with self.subTest(raw=raw[:10]), self.assertRaises(ValueError):
                runner.load_config(self.config_path)

    def test_pause_and_completion_prevent_launch_and_clearing_pause_allows_restart(self):
        self.state.mkdir(parents=True)
        for flag in ("PAUSED", "COMPLETE"):
            marker = self.state / flag
            marker.touch()
            result, launch = self.run_main()
            self.assertEqual(result, 0)
            launch.assert_not_called()
            marker.unlink()
        result, launch = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(launch.call_count, 2)

    def test_completion_created_by_child_prevents_next_iteration(self):
        def complete(*args, **kwargs):
            (self.state / "COMPLETE").touch()
            return 0
        result, launch = self.run_main(child=complete)
        self.assertEqual(result, 0)
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(self.ledger()["last_exit"], 0)

    def test_failure_is_recorded_and_not_retried_on_same_day(self):
        result, launch = self.run_main(child=lambda *a, **k: 7)
        self.assertEqual(result, 7)
        self.assertEqual(launch.call_count, 1)
        self.assertTrue(self.ledger()["failed"])
        self.assertEqual(self.ledger()["last_exit"], 7)
        result, launch = self.run_main()
        launch.assert_not_called()
        with runner.FileLock(self.state / "runner.lock"):
            pass

    def test_spawn_error_is_recorded_and_not_retried(self):
        def cannot_start(*args, **kwargs):
            raise FileNotFoundError("fictional missing executable")
        result, launch = self.run_main(child=cannot_start)
        self.assertNotEqual(result, 0)
        self.assertEqual(launch.call_count, 1)
        self.assertTrue(self.ledger()["failed"])
        _, launch = self.run_main()
        launch.assert_not_called()

    def test_corrupt_daily_ledger_refuses_before_launch(self):
        self.state.mkdir(parents=True)
        path = self.state / "2026-11-06.json"
        for raw in ('{"attempts": true}', '{"attempts": -1}', '{"attempts": 7}', '{', '[]', 'null'):
            path.write_text(raw)
            result, launch = self.run_main()
            self.assertNotEqual(result, 0)
            launch.assert_not_called()

    def test_slow_workspace_validation_cannot_launch_after_cutoff(self):
        workspace = {"head": "fictional", "status": "", "prompt": str(self.prompt)}
        validations = []
        def validate(config):
            validations.append(True)
            if len(validations) == 2:
                FixedDateTime.value = datetime(2026, 11, 6, 14, tzinfo=runner.PKT)
            return workspace
        with patch.object(runner, "datetime", FixedDateTime), \
                patch.object(runner, "validate_workspace", side_effect=validate), \
                patch.object(runner, "run_child") as launch, redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(["--config", str(self.config_path)]), 0)
        launch.assert_not_called()

    def test_lock_contends_across_processes_and_recovers_after_crash(self):
        lock = self.root / "runner.lock"
        code = ("import runpy,sys,time; from pathlib import Path; "
                "m=runpy.run_path(sys.argv[1]); "
                "lock=m['FileLock'](Path(sys.argv[2])); lock.__enter__(); "
                "print('LOCKED',flush=True); time.sleep(60)")
        child = subprocess.Popen([sys.executable, "-u", "-c", code, str(SCRIPT), str(lock)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
        self.addCleanup(cleanup)
        # Readiness uses a bounded communicate on a separate helper below, not a
        # sleep assumption; select is not portable for Windows pipe handles.
        import threading
        ready = []
        thread = threading.Thread(target=lambda: ready.append(child.stdout.readline()), daemon=True)
        thread.start()
        thread.join(timeout=10)
        self.assertEqual(ready, ["LOCKED\n"])
        with self.assertRaises(RuntimeError):
            with runner.FileLock(lock):
                self.fail("concurrent contender acquired lock")
        child.kill()
        child.wait(timeout=10)
        self.assertTrue(lock.exists())
        with runner.FileLock(lock):
            pass
        with runner.FileLock(lock):
            pass

    def test_harmless_child_exit_and_log(self):
        log = self.root / "child.jsonl"
        code = "import sys; print(sys.stdin.read()); sys.exit(7)"
        result = runner.run_child([sys.executable, "-c", code], "fictional input", log, 5)
        self.assertEqual(result, 7)
        self.assertIn("fictional input", log.read_text())

    def test_pause_created_during_child_stops_it_promptly(self):
        marker = self.root / "PAUSED"
        pid_path = self.root / "pid.txt"
        code = ("import sys,time,os; from pathlib import Path; sys.stdin.read(); "
                "Path(sys.argv[2]).write_text(str(os.getpid())); "
                "Path(sys.argv[1]).touch(); time.sleep(60)")
        started = time.monotonic()
        result = runner.run_child([sys.executable, "-c", code, str(marker), str(pid_path)],
                                  "fictional", self.root / "pause.log", 20, marker)
        self.assertEqual(result, 125)
        self.assertLess(time.monotonic() - started, 8)
        self.assertTrue(pid_path.exists())
        self.assertFalse(alive(int(pid_path.read_text())))

    @unittest.skipUnless(os.name == "nt", "Windows job-object crash acceptance case")
    def test_supervisor_crash_kills_child_and_releases_lock(self):
        pid_path = self.root / "pid.txt"
        lock = self.root / "crash.lock"
        child_code = ("import sys,time,os; from pathlib import Path; sys.stdin.read(); "
                      "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)")
        supervisor_code = ("import runpy,sys; from pathlib import Path; "
                           "m=runpy.run_path(sys.argv[1]); "
                           "lock=m['FileLock'](Path(sys.argv[2])); lock.__enter__(); "
                           "m['run_child']([sys.executable,'-c',sys.argv[3],sys.argv[4]], "
                           "'fictional',Path(sys.argv[5]),50)")
        proc = subprocess.Popen([sys.executable, "-c", supervisor_code, str(SCRIPT), str(lock),
                                 child_code, str(pid_path), str(self.root / "crash.log")],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=10)
            if pid_path.exists() and alive(int(pid_path.read_text())):
                subprocess.run(["taskkill", "/PID", pid_path.read_text(), "/T", "/F"], capture_output=True, timeout=10)
        self.addCleanup(cleanup)
        until = time.monotonic() + 10
        while not pid_path.exists() and proc.poll() is None and time.monotonic() < until:
            time.sleep(0.02)
        self.assertTrue(pid_path.exists(), "contained child never reached readiness")
        pid = int(pid_path.read_text())
        self.assertTrue(alive(pid))
        proc.kill()
        proc.wait(timeout=10)
        until = time.monotonic() + 5
        while alive(pid) and time.monotonic() < until:
            time.sleep(0.02)
        self.assertFalse(alive(pid), "child outlived abruptly terminated supervisor")
        with runner.FileLock(lock):
            pass

    @unittest.skipUnless(os.name == "nt", "Windows job-object acceptance case")
    def test_deadline_kills_owned_grandchild_but_not_unrelated_process(self):
        marker = self.root / "pids.json"
        code = ("import sys,subprocess,time,json,os; from pathlib import Path; sys.stdin.read(); "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                "Path(sys.argv[1]).write_text(json.dumps([os.getpid(),child.pid])); time.sleep(60)")
        control = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        def cleanup():
            if control.poll() is None:
                control.kill()
            control.wait(timeout=10)
            if marker.exists():
                for pid in json.loads(marker.read_text()):
                    if alive(pid):
                        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
        self.addCleanup(cleanup)
        started = time.monotonic()
        result = runner.run_child([sys.executable, "-c", code, str(marker)], "fictional", self.root / "tree.log", 1.5)
        self.assertEqual(result, 124)
        self.assertLess(time.monotonic() - started, 12)
        self.assertTrue(marker.exists(), "fixture did not establish a grandchild")
        pids = json.loads(marker.read_text())
        until = time.monotonic() + 5
        while any(alive(pid) for pid in pids) and time.monotonic() < until:
            time.sleep(0.02)
        self.assertFalse(any(alive(pid) for pid in pids), f"owned process survived: {pids}")
        self.assertIsNone(control.poll(), "unrelated process was terminated")

    def test_atomic_result_remains_valid_after_replacement(self):
        path = self.root / "last.json"
        runner.atomic_json(path, {"status": "before"})
        runner.atomic_json(path, {"status": "after", "exit": 7})
        self.assertEqual(json.loads(path.read_text()), {"status": "after", "exit": 7})
        self.assertFalse(path.with_suffix(".tmp").exists())


if __name__ == "__main__":
    unittest.main()
