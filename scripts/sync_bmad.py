"""Mirror installed BMAD skill resources into Codex discovery without reinstalling."""

import argparse
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".claude/skills"
DEST = ROOT / ".agents/skills"


def files_under(root):
    return {p.relative_to(root): p for p in root.glob("bmad-*/**/*")
            if p.is_file() and "__pycache__" not in p.parts
            and p.suffix not in {".pyc", ".pyo"}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source = files_under(SOURCE)
    if not source or not (ROOT / "_bmad/_config/manifest.yaml").is_file():
        print("Installed BMAD source/manifest missing; no mirror created.")
        return 1
    drift = []
    for relative, path in source.items():
        target = DEST / relative
        if args.check:
            if not target.is_file() or target.read_bytes() != path.read_bytes():
                drift.append(str(relative))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    stale = set(files_under(DEST)) - set(source)
    if drift or stale:
        print("BMAD mirror differs; regenerate and review stale files manually:")
        print("\n".join(sorted(map(str, set(drift) | stale))))
        return 1
    skills = sum(path.name == "SKILL.md" for path in source.values())
    print(f"BMAD mirror {'checked' if args.check else 'generated'}: {skills} skills, {len(source)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
