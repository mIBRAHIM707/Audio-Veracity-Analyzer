"""Reject obvious data/secret artifacts and files over 1 MB before commit or in CI.

This is a filename/size safeguard, not a content privacy scanner.
"""

import argparse
from pathlib import Path, PurePosixPath
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BLOCKED = {".wav", ".mp3", ".amr", ".gsm", ".flac", ".ogg", ".m4a", ".aac",
           ".onnx", ".pt", ".pth", ".ckpt", ".safetensors", ".pem", ".key"}


def reason(name, size):
    path = PurePosixPath(name)
    lower = path.name.lower()
    if path.suffix.lower() in BLOCKED:
        return "audio, model or key artifact"
    if lower == ".env" or lower.startswith(".env.") and lower != ".env.example":
        return "environment secret file"
    if any(part.lower() in {"raw", "processed", "private", "consents"} for part in path.parts):
        return "private/data directory"
    if lower in {"speakers.csv", "participants.csv", "consent.csv", "manifest.csv", "manifest.jsonl"}:
        return "potential private participant manifest"
    if size > 1_000_000:
        return "file exceeds 1 MB"
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args()
    command = (["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"]
               if args.staged else ["git", "ls-files", "-c", "-o", "--exclude-standard", "-z"])
    names = set(subprocess.check_output(command, cwd=ROOT).decode("utf-8").split("\0")) - {""}
    failures = []
    for name in sorted(names):
        if args.staged:
            size = int(subprocess.check_output(["git", "cat-file", "-s", ":" + name], cwd=ROOT))
        else:
            path = ROOT / name
            if not path.is_file():
                continue
            size = path.stat().st_size
        problem = reason(name, size)
        if problem:
            failures.append(f"{name}: {problem}")
    if failures:
        print("Repository guard rejected:\n" + "\n".join(failures))
        return 1
    print(f"Repository guard passed ({len(names)} paths checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
