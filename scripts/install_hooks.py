"""Install this clone's repository guard without replacing unrelated hook setup."""

from pathlib import Path
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    result = subprocess.run(["git", "config", "--get", "core.hooksPath"], cwd=ROOT,
                            text=True, capture_output=True)
    existing = result.stdout.strip()
    if existing and existing != ".githooks":
        raise SystemExit("Existing hooksPath detected; integrate the AVA guard manually: " + existing)
    hook = ROOT / ".githooks/pre-commit"
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    subprocess.run(["git", "config", "--local", "core.hooksPath", ".githooks"], cwd=ROOT, check=True)
    print("Installed local pre-commit guard at .githooks/pre-commit")


if __name__ == "__main__":
    main()
