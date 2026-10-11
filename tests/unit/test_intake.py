import csv
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[2] / "data/collect/validate_intake.py"
SPEC = importlib.util.spec_from_file_location("validate_intake", MODULE)
intake = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(intake)


class IntakeTests(unittest.TestCase):
    def row(self, **changes):
        record = dict(zip(intake.FIELDS, (
            "fictional-clip-1", "test-source", "v1", "fictional-speaker-1",
            "fictional-recording-1", "ur", "genuine", "a" * 64, "4.2",
            "fictional-rights-review", "approved", "unassigned",
        )))
        record.update(changes)
        return record

    def check_rows(self, rows):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fictional.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=intake.FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            return intake.validate(path)

    def check_text(self, contents):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fictional.csv"
            path.write_text(contents, encoding="utf-8")
            return intake.validate(path)

    def run_cli(self, path):
        return subprocess.run(
            [sys.executable, str(MODULE), str(path)],
            capture_output=True, text=True, timeout=10, check=False,
        )

    def test_valid_unassigned_pilot(self):
        self.assertTrue(self.check_rows([self.row()])["ok"])

    def test_both_languages_and_aggregate_counts(self):
        result = self.check_rows([self.row(), self.row(
            clip_id="fictional-clip-2", language="pnb_Arab")])
        self.assertTrue(result["ok"])
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["language_counts"], {"ur": 1, "pnb_Arab": 1})

    def test_supported_splits(self):
        for split in ("unassigned", "train", "dev", "test"):
            with self.subTest(split=split):
                self.assertTrue(self.check_rows([self.row(split=split)])["ok"])

    def test_whitespace_is_normalized_before_duplicate_detection(self):
        rows = [self.row(), self.row(clip_id="  fictional-clip-1  ")]
        self.assertIn("duplicate clip_id", " ".join(self.check_rows(rows)["errors"]))

    def test_each_required_value_must_be_populated(self):
        for field in intake.FIELDS:
            with self.subTest(field=field):
                result = self.check_rows([self.row(**{field: " \t "})])
                self.assertFalse(result["ok"])
                self.assertEqual(result["errors"], ["Row 2: required value missing."])

    def test_empty_template_is_not_dataset(self):
        self.assertFalse(self.check_rows([])["ok"])

    def test_pending_permissions_rejected(self):
        self.assertFalse(self.check_rows([self.row(use_status="pending")])["ok"])

    def test_nonfinite_and_invalid_durations(self):
        for duration in ("nan", "inf", "-inf", "1e309", "0", "-1", "unknown"):
            with self.subTest(duration=duration):
                self.assertFalse(self.check_rows([self.row(duration_s=duration)])["ok"])

    def test_intake_does_not_claim_processed_audio_duration_limits(self):
        for duration in ("0.1", "600"):
            with self.subTest(duration=duration):
                self.assertTrue(self.check_rows([self.row(duration_s=duration)])["ok"])

    def test_speaker_leakage(self):
        rows = [self.row(split="train"), self.row(
            clip_id="fictional-clip-2", source_recording_id="other",
            sha256="b" * 64, split="test")]
        self.assertIn("speaker crosses", " ".join(self.check_rows(rows)["errors"]))

    def test_identical_audio_across_sources(self):
        rows = [self.row(split="train"), self.row(
            clip_id="fictional-clip-2", source_id="other-source", split="test")]
        self.assertIn("sha256 crosses", " ".join(self.check_rows(rows)["errors"]))

    def test_hash_leakage_is_case_insensitive(self):
        rows = [self.row(split="train"), self.row(
            clip_id="fictional-clip-2", source_id="other-source",
            sha256="A" * 64, split="test")]
        self.assertEqual(self.check_rows(rows)["errors"],
                         ["Row 3: sha256 crosses split assignments."])

    def test_source_scopes_speaker_and_recording_ids(self):
        rows = [self.row(split="train"), self.row(
            clip_id="fictional-clip-2", source_id="other-source",
            sha256="b" * 64, split="test")]
        self.assertTrue(self.check_rows(rows)["ok"])

    def test_source_version_does_not_hide_speaker_leakage(self):
        rows = [self.row(split="train"), self.row(
            clip_id="fictional-clip-2", source_version="v2",
            source_recording_id="other", sha256="b" * 64, split="test")]
        self.assertEqual(self.check_rows(rows)["errors"],
                         ["Row 3: speaker crosses split assignments."])

    def test_partial_assignment_is_rejected_in_both_orders(self):
        rows = [self.row(), self.row(
            clip_id="fictional-clip-2", source_recording_id="other",
            sha256="b" * 64, split="train")]
        for ordered in (rows, rows[::-1]):
            with self.subTest(first_split=ordered[0]["split"]):
                self.assertIn("speaker crosses", " ".join(
                    self.check_rows(ordered)["errors"]))

    def test_repeated_group_in_one_split_is_allowed(self):
        rows = [self.row(split="train"), self.row(
            clip_id="fictional-clip-2", split="train")]
        self.assertTrue(self.check_rows(rows)["ok"])

    def test_recording_leakage_even_with_different_speaker_label(self):
        rows = [self.row(split="train"), self.row(
            clip_id="fictional-clip-2", speaker_id="other-speaker",
            sha256="b" * 64, split="dev")]
        self.assertIn("recording crosses", " ".join(self.check_rows(rows)["errors"]))

    def test_duplicate_ids(self):
        self.assertFalse(self.check_rows([self.row(), self.row()])["ok"])

    def test_synthetic_needs_future_lineage_validator(self):
        self.assertFalse(self.check_rows([self.row(label="synthetic")])["ok"])

    def test_invalid_hash_language_and_split(self):
        for changes in ({"sha256": "x" * 64}, {"language": "unknown"},
                        {"split": "validation"}, {"speaker_id": ""}):
            with self.subTest(changes=changes):
                self.assertFalse(self.check_rows([self.row(**changes)])["ok"])

    def test_missing_header_and_malformed_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fictional.csv"
            path.write_text("clip_id\nx\n", encoding="utf-8")
            self.assertFalse(intake.validate(path)["ok"])
            path.write_text(",".join(intake.FIELDS) + "\nx,y\n", encoding="utf-8")
            self.assertFalse(intake.validate(path)["ok"])

    def test_duplicate_header_and_extra_cells_are_rejected(self):
        header = ",".join(intake.FIELDS)
        values = ",".join(self.row().values())
        for contents in (header + ",clip_id\n" + values + ",extra\n",
                         header + "\n" + values + ",extra\n"):
            with self.subTest(contents=contents):
                self.assertFalse(self.check_text(contents)["ok"])

    def test_utf8_bom_quoted_values_and_additional_metadata(self):
        row = self.row(licence_ref="fictional,rights-review")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fictional.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=(*intake.FIELDS, "note"))
                writer.writeheader()
                writer.writerow({**row, "note": "fictional note"})
            self.assertTrue(intake.validate(path)["ok"])

    def test_counts_are_not_accepted_dataset_totals(self):
        result = self.check_rows([self.row(use_status="pending"),
                                  self.row(clip_id="fictional-clip-2", speaker_id="")])
        self.assertFalse(result["ok"])
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["language_counts"], {"ur": 1})

    def test_cli_success_and_failure_emit_json_with_matching_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fictional.csv"
            header = ",".join(intake.FIELDS) + "\n"
            for rows, expected_code in (("", 1), (",".join(self.row().values()), 0)):
                path.write_text(header + rows, encoding="utf-8")
                process = self.run_cli(path)
                self.assertEqual(process.returncode, expected_code)
                self.assertEqual(json.loads(process.stdout)["ok"], expected_code == 0)
                self.assertEqual(process.stderr, "")

    def test_cli_unreadable_input_does_not_echo_paths_or_tracebacks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fictional-sensitive-name.csv"
            for contents in (None, b"\xff\xfe", (
                    ",".join(intake.FIELDS) + "\n" +
                    ",".join(list(self.row().values())[:-1]) + ',"unassigned'
                    ).encode("utf-8")):
                with self.subTest(contents=contents):
                    if contents is not None:
                        path.write_bytes(contents)
                    process = self.run_cli(path)
                    self.assertEqual(process.returncode, 1)
                    self.assertEqual(json.loads(process.stdout), {
                        "ok": False, "errors": ["Cannot read manifest as UTF-8 CSV."]})
                    self.assertEqual(process.stderr, "")
                    self.assertNotIn(path.name, process.stdout)


if __name__ == "__main__":
    unittest.main()
