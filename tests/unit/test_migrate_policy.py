"""Claude-authored tests (9 November 2026) for data/manifest/migrate_policy.py, backlog 1.4 / todo 9e.

Authored by Claude (Claude Code), not by GPT-6 Astra. Fictional rows only, stdlib only.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

MANIFEST_DIR = Path(__file__).resolve().parents[2] / "data" / "manifest"
sys.path.insert(0, str(MANIFEST_DIR))
import migrate_policy  # noqa: E402


def genuine(source="csalt_urdu", count=2, **extra):
    return [{"source_id": source, "label": "genuine", "path": f"fx/{i}.wav", "speaker_id": f"fx_{i}",
             "cloning_status": "not_authorized", "consent_status": "not_individually_verified",
             "validation_status": "verified", **extra} for i in range(count)]


def synthetic(count=2, **extra):
    return [{"source_id": "csalt_urdu", "label": "synthetic", "path": f"fx/s{i}.wav", "speaker_id": f"fx_{i}",
             "benchmark_only": True, "training_eligible": False, "cloning_status": "not_authorized",
             "validation_status": "verified", "use_scope": "noncommercial_benchmark_only", **extra}
            for i in range(count)]


class MigrationRuleTests(unittest.TestCase):
    def test_genuine_rows_become_trainable_and_cloneable_with_history(self):
        for kind, rows in (("csalt_genuine", genuine()),
                           ("meta_pnb", genuine("meta_omnilingual_pnb", training_eligible=False))):
            with self.subTest(kind=kind):
                original = copy.deepcopy(rows)
                migrated = migrate_policy.migrate_rows(rows, kind)
                self.assertEqual(rows, original, "input rows must not be mutated")
                for before, after in zip(rows, migrated):
                    self.assertIs(after["training_eligible"], True)
                    self.assertEqual(after["cloning_status"], "permitted_public_corpus")
                    self.assertEqual(after["consent_status"], before["consent_status"])
                    self.assertEqual(after["policy_version"], migrate_policy.POLICY_VERSION)
                    self.assertIn("Not individual consent", after["policy_basis"])
                    self.assertEqual(after["previous_policy"], {"training_eligible": before.get("training_eligible"),
                                                                "cloning_status": "not_authorized",
                                                                "consent_status": "not_individually_verified"})
                    self.assertEqual({k: v for k, v in after.items() if k in before and k not in
                                      ("training_eligible", "cloning_status")},
                                     {k: v for k, v in before.items() if k not in ("training_eligible", "cloning_status")})

    def test_missing_consent_becomes_explicitly_unverified_never_upgraded(self):
        rows = genuine(count=1)
        del rows[0]["consent_status"]
        migrated = migrate_policy.migrate_rows(rows, "csalt_genuine")[0]
        self.assertEqual(migrated["consent_status"], "not_individually_verified")
        self.assertIsNone(migrated["previous_policy"]["consent_status"])
        kept = migrate_policy.migrate_rows(genuine(count=1, consent_status="fx_other"), "csalt_genuine")[0]
        self.assertEqual(kept["consent_status"], "fx_other")

    def test_licences_differ_by_corpus(self):
        csalt = migrate_policy.migrate_rows(genuine(), "csalt_genuine")[0]["policy_licence"]
        meta = migrate_policy.migrate_rows(genuine("meta_omnilingual_pnb"), "meta_pnb")[0]["policy_licence"]
        self.assertIn("noncommercial", csalt)
        self.assertIn("attribution", meta)

    def test_benchmark_rows_stay_excluded_from_training(self):
        rows = synthetic() + synthetic(1, training_eligible=True, benchmark_only="true")
        migrated = migrate_policy.migrate_rows(rows, "csalt_synthetic_benchmark")
        for row in migrated:
            self.assertEqual((row["benchmark_only"], row["training_eligible"]), (True, False))
            self.assertEqual(row["cloning_status"], "not_authorized")
            self.assertEqual(row["policy_version"], migrate_policy.POLICY_VERSION)
            self.assertIn("Retained benchmark-only", row["policy_basis"])
            self.assertNotIn("may be used for training", row["policy_basis"])

    def test_mismatched_unsupported_or_repeated_input_is_refused(self):
        cases = (("csalt_genuine", synthetic()), ("csalt_synthetic_benchmark", genuine()),
                 ("meta_pnb", genuine()), ("common_voice", genuine("common_voice_ur")),
                 ("csalt_genuine", genuine(benchmark_only=True)),
                 ("csalt_genuine", [{**genuine()[0], "label": "synthetic"}]),
                 ("csalt_genuine", genuine(validation_status="failed")),
                 ("csalt_genuine", [{k: v for k, v in genuine()[0].items() if k != "validation_status"}]),
                 ("csalt_genuine", genuine(use_scope="noncommercial_benchmark_only")),
                 ("csalt_genuine", genuine(count=1) + synthetic(1)),
                 ("csalt_genuine", migrate_policy.migrate_rows(genuine(), "csalt_genuine")),
                 ("csalt_genuine", []), ("csalt_genuine", ["row"]), ("csalt_genuine", None))
        for kind, rows in cases:
            with self.subTest(kind=kind, rows=str(rows)[:60]):
                with self.assertRaises(ValueError):
                    migrate_policy.migrate_rows(rows, kind)


class MigrationPublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.manifest = self.root / "input" / "balanced.json"
        self.manifest.parent.mkdir()
        self.document = {"revision": "fx-rev", "balanced_complete": True, "files": genuine(count=3)}
        self.manifest.write_text(json.dumps(self.document), encoding="utf-8")

    def test_writes_new_versioned_copy_and_leaves_input_untouched(self):
        before = self.manifest.read_bytes()
        summary = migrate_policy.run(self.manifest, self.root / "v3", "csalt_genuine")
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(summary, {"kind": "csalt_genuine", "rows": 3, "policy_version": migrate_policy.POLICY_VERSION,
                                   "training_eligible_rows": 3, "benchmark_rows": 0})
        output = json.loads((self.root / "v3" / "balanced.json").read_text(encoding="utf-8"))
        self.assertEqual(output["migrated_from"]["manifest"], "balanced.json")
        self.assertEqual(len(output["migrated_from"]["sha256"]), 64)
        self.assertTrue(output["balanced_complete"])
        self.assertEqual(sorted(p.name for p in (self.root / "v3").iterdir()), ["balanced.json"])

    def test_write_failure_leaves_incomplete_marker(self):
        real_write = Path.write_text

        def failing(path, *args, **kwargs):
            if path.name == "balanced.json":
                raise OSError("fictional disk full")
            return real_write(path, *args, **kwargs)
        with patch.object(Path, "write_text", failing), self.assertRaises(OSError):
            migrate_policy.run(self.manifest, self.root / "broken", "csalt_genuine")
        self.assertTrue((self.root / "broken" / "INCOMPLETE").exists())
        self.assertFalse((self.root / "broken" / "balanced.json").exists())

    def test_refuses_existing_output_incomplete_input_partial_manifest_and_repo_output(self):
        (self.root / "exists").mkdir()
        with self.assertRaises(ValueError):
            migrate_policy.run(self.manifest, self.root / "exists", "csalt_genuine")
        with self.assertRaises(ValueError):
            migrate_policy.run(self.manifest, self.manifest.parent / "nested", "csalt_genuine")
        with self.assertRaises(ValueError):
            migrate_policy.run(self.manifest, MANIFEST_DIR / "fx-never-created", "csalt_genuine")
        self.assertFalse((MANIFEST_DIR / "fx-never-created").exists())
        partial = self.root / "partial" / "balanced.json"
        partial.parent.mkdir()
        partial.write_text(json.dumps({**self.document, "balanced_complete": False}), encoding="utf-8")
        with self.assertRaises(ValueError):
            migrate_policy.run(partial, self.root / "out-partial", "csalt_genuine")
        (self.manifest.parent / "INCOMPLETE").write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            migrate_policy.run(self.manifest, self.root / "out-incomplete", "csalt_genuine")
        self.assertFalse((self.root / "out-partial").exists())
        self.assertFalse((self.root / "out-incomplete").exists())


if __name__ == "__main__":
    unittest.main()
