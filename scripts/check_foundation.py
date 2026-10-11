"""Validate shared agent configuration and required project context offline."""

import json
from pathlib import Path
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def main():
    required = ["AGENTS.md", "CLAUDE.md", "README.md", "prd.md", "architecture.md",
                "docs/context/current-state.md", "docs/engineering/workflow.md",
                "docs/engineering/design-principles.md", ".python-version"]
    errors = [f"Missing {name}" for name in required if not (ROOT / name).is_file()]
    catalog = json.loads((ROOT / "docs/agents/catalog.json").read_text(encoding="utf-8"))
    names = set()
    for role in catalog:
        path = ROOT / f'.codex/agents/ava-{role["id"]}.toml'
        try:
            config = tomllib.loads(path.read_text(encoding="utf-8"))
            for key in ("name", "description", "developer_instructions"):
                if not isinstance(config.get(key), str) or not config[key].strip():
                    errors.append(f"{path.name}: missing {key}")
            if config.get("name") in names:
                errors.append(f"{path.name}: duplicate name")
            names.add(config.get("name"))
            if role["id"] == "test-designer" and config.get("model") != "gpt-6-astra":
                errors.append("Test designer must select gpt-6-astra")
        except (OSError, tomllib.TOMLDecodeError) as error:
            errors.append(f"{path.name}: {type(error).__name__}")
    for script in ("sync_agents.py", "sync_bmad.py"):
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / script), "--check"], cwd=ROOT)
        if result.returncode:
            errors.append(f"{script} drift check failed")
    prd = (ROOT / "_bmad-output/planning-artifacts/prd.md").read_text(encoding="utf-8")
    if "FR-W1" not in prd or "FR-W4" not in prd:
        errors.append("PRD construction workflow requirements missing")
    if errors:
        print("\n".join(errors))
        return 1
    print(f"Foundation checks passed: {len(catalog)} shared roles; canonical context present.")
    print("Static validation only; client agent discovery is a separate check.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
