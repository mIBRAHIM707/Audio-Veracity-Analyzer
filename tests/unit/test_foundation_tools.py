"""Offline tooling regressions; all mutations and Git indexes are temporary."""

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FoundationToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def write(self, name, contents="fictional fixture\n"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def run_main(self, module, *arguments):
        output = io.StringIO()
        with patch.object(module, "ROOT", self.root), patch(
                "sys.argv", [module.__name__, *arguments]), redirect_stdout(output):
            result = module.main()
        return result, output.getvalue()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True,
                              capture_output=True, text=True, timeout=10)

    def agent_fixture(self):
        role = {"id": "test-designer", "description": 'Test "quoted" role',
                "role_file": "docs/agents/test-designer.md", "model": "gpt-6-astra"}
        self.write("docs/agents/catalog.json", json.dumps([role]))
        self.write("docs/agents/common.md", "Shared fictional instructions.\n")
        self.write(role["role_file"], "Fictional role instructions.\n")
        return role

    def test_guard_forbidden_paths_and_exact_size_boundary(self):
        guard = load_script("repository_guard")
        blocked = ["audio.WAV", "weights.safetensors", "secret.KEY", ".env",
                   ".env.production", "private/notes.txt", "Private/notes.txt",
                   "data/RAW/notes.txt", "consents/form.txt", "participants.csv",
                   "nested/manifest.jsonl"]
        for name in blocked:
            with self.subTest(name=name):
                self.assertIsNotNone(guard.reason(name, 1))
        for name in (".env.example", "docs/manifest-format.md", "docs/readme.md"):
            with self.subTest(name=name):
                self.assertIsNone(guard.reason(name, 1_000_000))
                self.assertEqual(guard.reason(name, 1_000_001), "file exceeds 1 MB")

    def test_staged_guard_reads_index_blob_not_smaller_working_copy(self):
        guard = load_script("repository_guard")
        self.git("init", "--quiet")
        path = self.write("large.txt", "x" * 1_000_001)
        self.git("add", "large.txt")
        path.write_text("small working copy", encoding="utf-8")
        status, output = self.run_main(guard, "--staged")
        self.assertEqual(status, 1)
        self.assertIn("file exceeds 1 MB", output)
        self.assertEqual(self.run_main(guard)[0], 0)

    def test_staged_guard_ignores_unstaged_large_edit_and_deleted_paths(self):
        guard = load_script("repository_guard")
        self.git("init", "--quiet")
        path = self.write("small.txt", "small staged copy")
        self.git("add", "small.txt")
        path.write_text("x" * 1_000_001, encoding="utf-8")
        self.assertEqual(self.run_main(guard, "--staged")[0], 0)
        self.assertEqual(self.run_main(guard)[0], 1)
        self.git("rm", "--cached", "--force", "small.txt")
        path.unlink()
        self.assertEqual(self.run_main(guard, "--staged")[0], 0)

    def test_agent_generation_is_idempotent_and_preserves_model_boundary(self):
        sync = load_script("sync_agents")
        self.agent_fixture()
        self.assertEqual(self.run_main(sync)[0], 0)
        codex = self.root / ".codex/agents/ava-test-designer.toml"
        claude = self.root / ".claude/agents/ava-test-designer.md"
        original = (codex.read_bytes(), claude.read_bytes())
        self.assertEqual(self.run_main(sync)[0], 0)
        self.assertEqual((codex.read_bytes(), claude.read_bytes()), original)
        config = tomllib.loads(codex.read_text(encoding="utf-8"))
        self.assertEqual(config["model"], "gpt-6-astra")
        self.assertIn("Shared fictional instructions", config["developer_instructions"])
        self.assertIn("model: inherit", claude.read_text(encoding="utf-8"))
        self.assertIn("cannot run GPT-6 Astra", claude.read_text(encoding="utf-8"))
        self.assertEqual(self.run_main(sync, "--check")[0], 0)

    def test_agent_check_reports_missing_and_edited_adapters_without_writing(self):
        sync = load_script("sync_agents")
        self.agent_fixture()
        self.assertEqual(self.run_main(sync, "--check")[0], 1)
        self.assertFalse((self.root / ".codex").exists())
        self.run_main(sync)
        path = self.write(".codex/agents/ava-test-designer.toml", "deliberate drift")
        status, output = self.run_main(sync, "--check")
        self.assertEqual(status, 1)
        self.assertIn("ava-test-designer.toml", output)
        self.assertEqual(path.read_text(encoding="utf-8"), "deliberate drift")

    def test_bmad_mirror_copies_resources_ignores_cache_and_reports_stale(self):
        sync = load_script("sync_bmad")
        source = self.root / ".claude/skills"
        destination = self.root / ".agents/skills"
        self.write("_bmad/_config/manifest.yaml")
        self.write(".claude/skills/bmad-example/SKILL.md")
        self.write(".claude/skills/bmad-example/resources/prompt.txt", "resource contents")
        self.write(".claude/skills/bmad-example/__pycache__/ignored.pyc")
        self.write(".claude/skills/other-skill/SKILL.md")
        with patch.object(sync, "SOURCE", source), patch.object(sync, "DEST", destination):
            self.assertEqual(self.run_main(sync)[0], 0)
            self.assertEqual(self.run_main(sync)[0], 0)
            self.assertEqual(self.run_main(sync, "--check")[0], 0)
            self.assertEqual((destination / "bmad-example/resources/prompt.txt").read_text(),
                             "resource contents")
            self.assertFalse((destination / "bmad-example/__pycache__").exists())
            self.assertFalse((destination / "other-skill").exists())
            stale = self.write(".agents/skills/bmad-example/stale.txt")
            self.assertEqual(self.run_main(sync)[0], 1)
            self.assertTrue(stale.exists(), "Stale resources require manual review, not deletion")
            stale.unlink()
            changed = self.write(".agents/skills/bmad-example/resources/prompt.txt", "drift")
            self.assertEqual(self.run_main(sync, "--check")[0], 1)
            self.assertEqual(changed.read_text(), "drift")

    def test_bmad_missing_installation_does_not_create_mirror(self):
        sync = load_script("sync_bmad")
        with patch.object(sync, "SOURCE", self.root / ".claude/skills"), patch.object(
                sync, "DEST", self.root / ".agents/skills"):
            self.assertEqual(self.run_main(sync)[0], 1)
            self.assertFalse((self.root / ".agents").exists())

    def export_fixture(self, exporter):
        for name in exporter.FILES:
            self.write(name)
        self.agent_fixture()

    def test_context_exports_only_explicit_files_and_catalog_roles(self):
        exporter = load_script("export_context")
        self.export_fixture(exporter)
        self.write("docs/agents/private-notes.md", "UNAPPROVED_PRIVATE_MARKER")
        self.write("private/participants.csv", "UNAPPROVED_PARTICIPANTS_MARKER")
        self.write(".env", "UNAPPROVED_SECRET_MARKER")
        with patch.object(exporter.subprocess, "check_output", return_value="fictional-commit\n"):
            self.run_main(exporter)
        output = (self.root / ".local/context/ava-context.md").read_text(encoding="utf-8")
        self.assertIn("Base commit: fictional-commit", output)
        self.assertIn("Fictional role instructions", output)
        self.assertNotIn("UNAPPROVED_", output)
        self.assertNotIn("private-notes.md", output)

    def test_context_missing_required_file_fails_without_export(self):
        exporter = load_script("export_context")
        self.export_fixture(exporter)
        (self.root / "AGENTS.md").unlink()
        with self.assertRaisesRegex(SystemExit, "Missing required context"):
            self.run_main(exporter)
        self.assertFalse((self.root / ".local/context/ava-context.md").exists())

    def test_foundation_rejects_wrong_astra_model_and_reports_static_scope(self):
        sync = load_script("sync_agents")
        checker = load_script("check_foundation")
        self.agent_fixture()
        self.run_main(sync)
        for name in ("AGENTS.md", "CLAUDE.md", "README.md", "prd.md", "architecture.md",
                     "docs/context/current-state.md", "docs/engineering/workflow.md",
                     "docs/engineering/design-principles.md", ".python-version"):
            self.write(name)
        self.write("_bmad-output/planning-artifacts/prd.md", "FR-W1 FR-W4")
        # Subprocess checks have dedicated integration cases above. This case tests
        # the checker's own config validation; it does not prove client discovery.
        with patch.object(checker.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            status, output = self.run_main(checker)
            self.assertEqual(status, 0)
            self.assertIn("Static validation only", output)
            path = self.root / ".codex/agents/ava-test-designer.toml"
            path.write_text(path.read_text(encoding="utf-8").replace(
                'model = "gpt-6-astra"', 'model = "wrong-model"'), encoding="utf-8")
            status, output = self.run_main(checker)
            self.assertEqual(status, 1)
            self.assertIn("Test designer must select gpt-6-astra", output)


if __name__ == "__main__":
    unittest.main()
