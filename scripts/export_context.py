"""Export only reviewed project instructions/docs, never arbitrary workspace files."""

import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "AGENTS.md", "CLAUDE.md", "README.md", "docs/context/current-state.md",
    "docs/context/todo.md",
    "docs/engineering/workflow.md", "docs/engineering/design-principles.md",
    "docs/agents/catalog.json", "docs/agents/common.md",
    "_bmad-output/planning-artifacts/prd.md",
    "_bmad-output/planning-artifacts/architecture.md",
]


def main():
    catalog = json.loads((ROOT / "docs/agents/catalog.json").read_text(encoding="utf-8"))
    roles = [ROOT / role["role_file"] for role in catalog]
    paths = list(dict.fromkeys([ROOT / name for name in FILES] + roles))
    missing = [str(path.relative_to(ROOT)) for path in paths if not path.is_file()]
    if missing:
        raise SystemExit("Missing required context: " + ", ".join(missing))
    for path in paths:
        if not path.resolve().is_relative_to(ROOT) or path.is_symlink():
            raise SystemExit("Refusing context reference outside repository.")
        if path in roles and (path.parent != ROOT / "docs/agents" or path.suffix != ".md"):
            raise SystemExit("Role context must be an explicit Markdown file in docs/agents.")
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    sections = ["# AVA portable context\n\n"
                f"Base commit: {commit}. Includes current working-tree documentation, "
                "which may be uncommitted. Review before sharing. No source code, "
                "private datasets, chat histories, browser reports or secrets are "
                "intentionally included. Documentation itself still needs review.\n"]
    for path in paths:
        sections.append(f"\n---\n\n## File {path.relative_to(ROOT).as_posix()}\n\n"
                        + path.read_text(encoding="utf-8"))
    output = ROOT / ".local/context/ava-context.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(sections), encoding="utf-8")
    print(f"Exported {len(paths)} allowlisted documents to {output.relative_to(ROOT)}")
    print("Review contents before sending to another model. Nothing was uploaded.")


if __name__ == "__main__":
    main()
