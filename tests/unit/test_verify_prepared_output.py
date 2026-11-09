"""Claude-authored tests (9 November 2026) for scripts/verify_prepared_output.py, backlog task 1.1.

Authored by Claude (Claude Code), not by GPT-6 Astra. Fictional synthetic PCM only;
no corpus audio, GPU or network. One conditioned and one exact-mode output are
prepared by the real data/prepare/prepare_audio.py; every test verifies a private
copy, tampers with one property and expects the named check to fail.
"""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave

import numpy as np

from data.prepare.prepare_audio import process_manifest
from scripts import verify_prepared_output as verifier

SCRIPT = Path(verifier.__file__).resolve()
TRANSCRIPT = "Fictional unaligned transcript alpha"


def write_wav(path, values, rate, channels=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(np.asarray(values, dtype="<i2").tobytes())
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tone(rate, seconds, pauses=(), amplitude=6000):
    t = np.arange(round(rate * seconds)) / rate
    values = amplitude * np.sin(2 * np.pi * 220 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))
    for start, end in pauses:
        values[(t >= start) & (t < end)] = 0
    return np.rint(values).astype("<i2")


def build_sources(root, *, condition):
    """Fictional sources covering every segmentation outcome plus one quarantine."""
    sources, rows = root / "sources", []
    policy = {"training_eligible": False, "cloning_status": "not_authorized", "label": "genuine",
              "split": "unassigned", "use_scope": "noncommercial_detection_research"}
    native = 48000 if condition else 16000
    speech = tone(native, 9.6, pauses=((3.4, 3.7), (7.0, 7.3)))
    stereo = np.stack([speech, (speech // 2).astype("<i2")], axis=1).reshape(-1) if condition else speech
    specs = [("speaker_a/multi.wav", stereo, native, 2 if condition else 1),
             ("speaker_a/full.wav", tone(16000, 5.0), 16000, 1),
             ("speaker_b/silent.wav", np.zeros(16000 * 4, dtype="<i2"), 16000, 1),
             ("speaker_b/short.wav", tone(16000, 2.0), 16000, 1),
             ("speaker_b/long.wav", tone(16000, 7.0), 16000, 1),
             ("speaker_b/tampered.wav", tone(16000, 4.0), 16000, 1)]
    for index, (name, values, rate, channels) in enumerate(specs):
        digest = write_wav(sources / name, values, rate, channels)
        rows.append({"path": name, "speaker_id": name.split("/")[0], "sha256": digest,
                     "clip_id": f"fictional-{index}", **policy})
    rows[-1]["sha256"] = "0" * 64
    original = sources / "originals" / "multi.flac"
    original.parent.mkdir()
    original.write_bytes(b"fictional FLAC stand-in bytes")
    transcript = sources / "transcripts" / "multi.json"
    transcript.parent.mkdir()
    transcript.write_text(json.dumps({"raw_text": TRANSCRIPT, "prompt": "Fictional prompt beta",
                                      "alignment": "source_recording_only"}), encoding="utf-8")
    rows[0].update(original_audio_path="originals/multi.flac", source_transcript_path="transcripts/multi.json",
                   original_audio_sha256=hashlib.sha256(original.read_bytes()).hexdigest())
    manifest = root / "manifest" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"revision": "fictional-revision", "files": rows}), encoding="utf-8")
    process_manifest(manifest, sources, root / "prepared", condition=condition)
    return manifest, sources, root / "prepared"


class VerifyPreparedOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        base = Path(cls.temporary.name).resolve()
        cls.fixtures = {mode: build_sources(base / mode, condition=mode == "condition") for mode in ("condition", "exact")}

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name).resolve()

    def copy(self, mode="condition", name=None):
        manifest, sources, prepared = self.fixtures[mode]
        root = self.work / (name or mode)
        shutil.copytree(manifest.parent.parent, root)
        return root / "manifest" / "manifest.json", root / "sources", root / "prepared"

    def run_verifier(self, paths, mode="condition", extra=()):
        manifest, sources, prepared = paths
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = verifier.main(["--manifest", str(manifest), "--source-root", str(sources),
                                  "--output-root", str(prepared), "--mode", mode, *extra])
        return code, output.getvalue().strip()

    def edit_report(self, prepared, change):
        path = prepared / "report.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        change(report)
        path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    def assert_fails(self, paths, check, mode="condition", extra=()):
        code, line = self.run_verifier(paths, mode, extra)
        self.assertEqual(code, 1, line)
        self.assertTrue(line.startswith("FAIL "), line)
        self.assertIn(check, line)
        return line

    # Normal paths -------------------------------------------------------------
    def test_conditioned_fixture_passes_with_every_outcome_and_aggregates_only(self):
        paths = self.copy()
        report = json.loads((paths[2] / "report.json").read_text(encoding="utf-8"))
        outcomes = report["summary"]["segmentation_outcomes"]
        self.assertEqual(outcomes, {"all_quiet": 1, "too_short": 1, "no_valid_pause_segments": 1,
                                    "partial_coverage": 1, "full_coverage": 1})
        self.assertEqual(report["summary"]["errors"], 1)
        self.assertGreaterEqual(report["summary"]["clips"], 3)
        code, line = self.run_verifier(paths)
        self.assertEqual(code, 0, line)
        self.assertRegex(line, r"^PASS mode=condition sources=6 processed=5 quarantined=1 clips=\d+ ")
        self.assertIn("originals_hashed=1 transcripts_scanned=2", line)
        for identifier in ("speaker_", "multi", "fictional", ".wav", "clips/"):
            self.assertNotIn(identifier, line)

    def test_exact_mode_fixture_passes(self):
        code, line = self.run_verifier(self.copy("exact"), mode="exact")
        self.assertEqual(code, 0, line)
        self.assertTrue(line.startswith("PASS mode=exact sources=6 processed=5 quarantined=1"), line)

    def test_cli_subprocess_passes_and_refuses_optimized_mode(self):
        manifest, sources, prepared = self.copy()
        command = [sys.executable, str(SCRIPT), "--manifest", str(manifest), "--source-root", str(sources),
                   "--output-root", str(prepared), "--mode", "condition"]
        root = str(SCRIPT.parents[1])
        normal = subprocess.run(command, capture_output=True, text=True, cwd=root, timeout=120)
        self.assertEqual(normal.returncode, 0, normal.stdout + normal.stderr)
        optimized = subprocess.run([sys.executable, "-O", *command[1:]], capture_output=True, text=True, cwd=root, timeout=120)
        self.assertEqual(optimized.returncode, 2)
        self.assertIn("refusing to run under python -O", optimized.stdout)
        self.assertNotRegex(SCRIPT.read_text(encoding="utf-8"), r"(?m)^\s*assert\b")

    # Mode, policy and manifest identity --------------------------------------
    def test_wrong_mode_fails_policy(self):
        self.assert_fails(self.copy(), "report_policy_matches_mode", mode="exact")

    def test_manifest_changed_after_preparation_fails(self):
        paths = self.copy()
        document = json.loads(paths[0].read_text(encoding="utf-8"))
        document["revision"] = "another-fictional-revision"
        paths[0].write_text(json.dumps(document), encoding="utf-8")
        line = self.assert_fails(paths, "manifest_sha256")
        self.assertIn("manifest_revision", line)

    # Audio, hashes and immutable sources -------------------------------------
    def test_clip_byte_tamper_fails_rebuild_and_hash(self):
        paths = self.copy()
        clip = sorted((paths[2] / "clips").iterdir())[0]
        payload = bytearray(clip.read_bytes())
        payload[-1] ^= 1
        clip.write_bytes(bytes(payload))
        line = self.assert_fails(paths, "clip_bytes_rebuilt")
        self.assertIn("clip_sha256", line)

    def test_source_changed_after_preparation_fails(self):
        paths = self.copy()
        source = paths[1] / "speaker_a" / "full.wav"
        payload = bytearray(source.read_bytes())
        payload[100] ^= 1
        source.write_bytes(bytes(payload))
        self.assert_fails(paths, "source_rejected_but_not_quarantined")

    def test_missing_source_fails(self):
        paths = self.copy()
        (paths[1] / "speaker_a" / "full.wav").unlink()
        self.assert_fails(paths, "source_rejected_but_not_quarantined")

    def test_quarantine_claimed_for_readable_source_fails(self):
        paths = self.copy()
        def change(report):
            source = next(s for s in report["sources"] if s["path"] == "speaker_b/silent.wav")
            source.update(status="quarantined", error="fictional")
        self.edit_report(paths[2], change)
        self.assert_fails(paths, "source_processed")

    def test_original_audio_tamper_fails(self):
        paths = self.copy()
        (paths[1] / "originals" / "multi.flac").write_bytes(b"fictional FLAC stand-in bytes, altered")
        self.assert_fails(paths, "original_audio_sha256")

    # Metrics, flags and segmentation -----------------------------------------
    def test_source_metric_tamper_fails(self):
        paths = self.copy()
        self.edit_report(paths[2], lambda r: r["sources"][1]["metrics"].update(rms_dbfs=r["sources"][1]["metrics"]["rms_dbfs"] + .001))
        self.assert_fails(paths, "source_metrics")

    def test_removed_clip_flag_fails(self):
        paths = self.copy()
        def change(report):
            clip = next(c for c in report["clips"] if c["source_path"] == "speaker_a/multi.wav")
            clip["review_flags"] = [] if clip["review_flags"] else ["low_rms"]
        self.edit_report(paths[2], change)
        self.assert_fails(paths, "clip_metrics_and_flags")

    def test_pre_normalization_metric_tamper_fails(self):
        paths = self.copy()
        self.edit_report(paths[2], lambda r: r["clips"][0]["pre_normalization_metrics"].update(quiet_fraction=0.99))
        self.assert_fails(paths, "clip_pre_normalization_metrics")

    def test_dropped_clip_fails_optimum_and_coverage(self):
        paths = self.copy()
        report = json.loads((paths[2] / "report.json").read_text(encoding="utf-8"))
        victims = [c for c in report["clips"] if c["source_path"] == "speaker_a/multi.wav"]
        self.assertGreaterEqual(len(victims), 2, "fixture must split the multi-pause source")
        victim = victims[-1]
        (paths[2] / victim["path"]).unlink()
        def change(r):
            r["clips"] = [c for c in r["clips"] if c["path"] != victim["path"]]
            r["summary"]["clips"] -= 1
        self.edit_report(paths[2], change)
        line = self.assert_fails(paths, "segmentation_retains_optimum")
        self.assertIn("uncovered_ranges", line)

    def test_span_off_pause_boundary_fails(self):
        paths = self.copy()
        def change(report):
            clip = next(c for c in report["clips"] if c["source_path"] == "speaker_a/full.wav")
            clip["end_sample"] -= 320
        self.edit_report(paths[2], change)
        self.assert_fails(paths, "segmentation_spans_on_pause_boundaries")

    def test_segmentation_outcome_tamper_fails(self):
        paths = self.copy()
        self.edit_report(paths[2], lambda r: next(s for s in r["sources"] if s["path"] == "speaker_b/short.wav").update(segmentation_outcome="all_quiet"))
        self.assert_fails(paths, "segmentation_outcome")

    def test_conditioning_record_tamper_fails(self):
        paths = self.copy()
        self.edit_report(paths[2], lambda r: r["sources"][0]["conditioning"].update(saturated_samples=1))
        self.assert_fails(paths, "source_conditioning")

    # Lineage, provenance and summary ------------------------------------------
    def test_flipped_training_eligibility_fails(self):
        paths = self.copy()
        self.edit_report(paths[2], lambda r: r["clips"][0].update(training_eligible=True))
        self.assert_fails(paths, "clip_lineage_and_provenance")

    def test_integer_typed_policy_flag_fails(self):
        paths = self.copy()
        self.edit_report(paths[2], lambda r: r["sources"][0].update(training_eligible=0))
        self.assert_fails(paths, "source_lineage")

    def test_unexpected_clip_field_fails(self):
        paths = self.copy()
        self.edit_report(paths[2], lambda r: r["clips"][0].update(listening_decision="accepted"))
        self.assert_fails(paths, "clip_has_no_unexpected_fields")

    def test_summary_tamper_fails(self):
        for key, value, check in (("review_flagged_clips", 999, "summary_totals"),
                                  ("retained_seconds", 1.0, "summary_seconds"),
                                  ("segmentation_outcomes", {}, "summary_outcomes")):
            with self.subTest(key=key):
                paths = self.copy(name=key)
                self.edit_report(paths[2], lambda r: r["summary"].update({key: value}))
                self.assert_fails(paths, check)

    # Privacy: transcript text and script scan ---------------------------------
    def test_transcript_text_in_report_fails(self):
        paths = self.copy()
        self.edit_report(paths[2], lambda r: r.update(note=f"copied: {TRANSCRIPT}"))
        self.assert_fails(paths, "no_transcript_text_in_report")

    def test_arabic_script_in_report_fails(self):
        paths = self.copy()
        self.edit_report(paths[2], lambda r: r["policy"].update(remark="آواز"))
        self.assert_fails(paths, "no_arabic_script_in_report")

    # Root contents and publication state --------------------------------------
    def test_extra_root_entry_fails_unless_allowed(self):
        paths = self.copy()
        (paths[2] / "verification.json").write_text("{}", encoding="utf-8")
        self.assert_fails(paths, "output_root_contents")
        code, line = self.run_verifier(paths, extra=("--allow-extra", "verification.json"))
        self.assertEqual(code, 0, line)

    def test_stray_clip_file_fails(self):
        paths = self.copy()
        (paths[2] / "clips" / "stray.wav").write_bytes(b"RIFF")
        self.assert_fails(paths, "clips_directory_contents")

    def test_incomplete_marker_fails(self):
        for target in ("prepared", "sources", "manifest"):
            with self.subTest(target=target):
                paths = self.copy(name=target)
                (self.work / target / target / "INCOMPLETE").write_text("fictional", encoding="utf-8")
                self.assert_fails(paths, "no_incomplete_markers")

    def test_allow_extra_rejects_paths(self):
        paths = self.copy()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_verifier(paths, extra=("--allow-extra", "../escape"))

    def test_unreadable_report_fails_cleanly(self):
        paths = self.copy()
        (paths[2] / "report.json").write_text("[]", encoding="utf-8")
        self.assert_fails(paths, "manifest_or_report_unreadable")
        (paths[2] / "report.json").write_text("{not json", encoding="utf-8")
        self.assert_fails(paths, "setup:JSONDecodeError")

    # Added after independent review (same day) --------------------------------
    def test_report_tampers_found_by_exact_field_sets(self):
        cases = (
            ("source_extra", lambda r: r["sources"][1].update(consent_status="individually_verified"),
             "source_has_no_unexpected_fields"),
            ("quarantine_forged", lambda r: next(s for s in r["sources"] if s["status"] == "quarantined").update(
                metrics={"rms_dbfs": -20.0}), "quarantined_source_record"),
            ("top_level_note", lambda r: r.update(note="accepted"), "report_has_no_unexpected_fields"),
            ("summary_extra", lambda r: r["summary"].update(accepted_clips=3), "summary_has_no_unexpected_fields"),
            ("library_version", lambda r: r["sources"][0]["conditioning"].update(soxr_version="0.0.0"),
             "conditioning_library_versions"),
            ("float_start", lambda r: r["clips"][0].update(start_sample=float(r["clips"][0]["start_sample"])),
             "segmentation_spans_on_pause_boundaries"),
            ("clips_reordered", lambda r: r.update(clips=r["clips"][::-1]), "clips_in_source_order"),
            ("sources_reordered", lambda r: r.update(sources=r["sources"][::-1]), "source_rows_in_manifest_order"),
            ("bool_count", lambda r: r["summary"].update(errors=True), "summary_totals"),
        )
        for name, change, check in cases:
            with self.subTest(name=name):
                paths = self.copy(name=name)
                self.edit_report(paths[2], change)
                line = self.assert_fails(paths, check)
                for identifier in ("speaker_", ".wav", "clips/", str(self.work)):
                    self.assertNotIn(identifier, line)

    def test_clip_claimed_for_quarantined_source_fails(self):
        paths = self.copy()
        def change(report):
            clip = dict(report["clips"][0], source_path="speaker_b/tampered.wav")
            report["clips"].append(clip)
        self.edit_report(paths[2], change)
        self.assert_fails(paths, "source_rejected_but_not_quarantined")

    def test_malformed_nested_values_fail_without_traceback(self):
        cases = (("conditioning_list", lambda r: r["sources"][0].update(conditioning=[1]), "source_conditioning"),
                 ("summary_list", lambda r: r.update(summary=[]), "summary_totals"),
                 ("metrics_string", lambda r: r["sources"][1].update(metrics="fine"), "source_metrics"),
                 ("source_path_list", lambda r: r["clips"][0].update(source_path=[]), "manifest_or_report_unreadable"))
        for name, change, check in cases:
            with self.subTest(name=name):
                paths = self.copy(name=name)
                self.edit_report(paths[2], change)
                self.assert_fails(paths, check)
        paths = self.copy(name="deep_nesting")
        (paths[2] / "report.json").write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
        self.assert_fails(paths, "setup:RecursionError")

    def test_manifest_row_preflight_would_reject_fails(self):
        for name, change in (("no_speaker", lambda row: row.pop("speaker_id")),
                             ("bad_hash", lambda row: row.update(sha256="XYZ")),
                             ("traversal", lambda row: row.update(path="../outside.wav"))):
            with self.subTest(name=name):
                paths = self.copy(name=name)
                document = json.loads(paths[0].read_text(encoding="utf-8"))
                change(document["files"][1])
                paths[0].write_text(json.dumps(document), encoding="utf-8")
                self.assert_fails(paths, "manifest_rows_not_preparable")

    def test_transcript_variant_with_case_and_spacing_changes_is_found(self):
        paths = self.copy()
        variant = "  " + TRANSCRIPT.upper().replace(" ", "   ") + " "
        self.edit_report(paths[2], lambda r: r["clips"][0].update(source_id=variant))
        self.assert_fails(paths, "no_transcript_text_in_report")

    def test_missing_original_audio_fails(self):
        paths = self.copy()
        (paths[1] / "originals" / "multi.flac").unlink()
        self.assert_fails(paths, "lineage_file_unreadable")

    def test_exact_mode_clip_tamper_fails(self):
        paths = self.copy("exact")
        clip = sorted((paths[2] / "clips").iterdir())[0]
        payload = bytearray(clip.read_bytes())
        payload[60] ^= 1
        clip.write_bytes(bytes(payload))
        self.assert_fails(paths, "clip_bytes_rebuilt", mode="exact")

    def test_verifier_does_not_import_the_preparation_code(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(?m)^\s*(from|import)\s+(data|prepare_audio|conditioning)\b")

    def test_equally_optimal_alternative_segmentation_is_accepted(self):
        boundaries = [0, 48000, 96000, 144000]
        optimum = verifier.best_retained(boundaries)
        for spans in ([(0, 48000), (48000, 144000)], [(0, 96000), (96000, 144000)]):
            with self.subTest(spans=spans):
                self.assertTrue(verifier.spans_valid(spans, boundaries))
                self.assertEqual(sum(end - start for start, end in spans), optimum)
        self.assertFalse(verifier.spans_valid([(0, 96000), (48000, 144000)], boundaries), "overlap")
        self.assertFalse(verifier.spans_valid([(0, 40000)], [0, 40000]), "shorter than 3 s")

    def test_strict_equal_distinguishes_bool_from_int_at_depth(self):
        self.assertTrue(verifier.strict_equal({"a": [1, 2.5]}, {"a": [1, 2.5]}))
        self.assertFalse(verifier.strict_equal([1], [True]))
        self.assertFalse(verifier.strict_equal({"a": 1}, {"a": 1.0}))

    # Pure helpers --------------------------------------------------------------
    def test_pause_boundaries_ignore_edge_runs_and_short_pauses(self):
        quiet = [True] * 10 + [False] * 200 + [True] * 5 + [False] * 200 + [True] * 6 + [False] * 10 + [True] * 3
        boundaries = verifier.pause_boundaries(quiet, len(quiet) * 320)
        self.assertEqual(boundaries, [0, (2 * 415 + 6) * 320 // 2, len(quiet) * 320])

    def test_best_retained_maximizes_samples_within_three_to_six_seconds(self):
        self.assertEqual(verifier.best_retained([0, 50000, 96000, 150000]), 150000)
        self.assertEqual(verifier.best_retained([0, 50000, 60000, 110000]), 110000)
        self.assertEqual(verifier.best_retained([0, 97000]), 0)
        self.assertEqual(verifier.best_retained([0, 40000]), 0)
        self.assertEqual(verifier.best_retained([0, 48000]), 48000)


if __name__ == "__main__":
    unittest.main()
