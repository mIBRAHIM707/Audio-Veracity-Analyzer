"""Claude-authored regression tests for scripts/verify_pnb_pilot.py (todo 7d, review finding M2).

Builds a fictional pilot with the real importer from a tiny Parquet shard of
FLAC-encoded synthetic PCM, then checks that the repository verifier passes a
clean pilot and fails each tampered or misconfigured variant. No real audio,
no network. Authored by Claude (Claude Code), not by GPT-6 Astra.
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

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import soundfile as sf

from data.collect import meta_pnb_pilot as pnb
from scripts import verify_pnb_pilot as verifier

REVISION = "8648ba8946377697b427ae952076e49fc0e5e44d"
SHARD = "data/pnb_Arab/train-00000-of-00002.parquet"
SCRIPT = Path(verifier.__file__).resolve()
RATE = 20000


def flac_payload(period):
    """Four seconds of a fictional square wave; the period makes each row distinct."""
    values = np.where(np.arange(RATE * 4) % period < period // 2, 2000, -2000).astype(np.int16)
    buffer = io.BytesIO()
    sf.write(buffer, values, RATE, format="FLAC", subtype="PCM_16")
    return buffer.getvalue()


def fixture_rows(speakers=2, per_speaker=3):
    rows = []
    for speaker in range(1, speakers + 1):
        for index in range(per_speaker):
            rows.append({"language": "pnb_Arab", "speaker_id": f"fictional-speaker-{speaker}",
                         "prompt_id": "reused-prompt", "prompt": f"Fictional prompt {index}",
                         "segment_id": "s01", "raw_text": f"Fictional unaligned transcript {speaker}-{index}",
                         "iso_639_3": "pnb", "glottocode": "west2386", "iso_15924": "Arab",
                         "duration": 4., "audio": {"bytes": flac_payload(40 + 2 * len(rows)),
                                                   "path": f"fictional-{speaker}-{index}.flac"}})
    return rows


class VerifyPnbPilotTests(unittest.TestCase):
    """One fictional pilot is imported once; every test works on a private copy."""

    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        base = Path(cls.temporary.name).resolve()
        cls.archives = base / "archives"
        cls.archives.mkdir()
        archive = cls.archives / Path(SHARD).name
        pq.write_table(pa.Table.from_pylist(fixture_rows()), archive, row_group_size=2)
        payload = archive.read_bytes()
        cls.archive_sha = hashlib.sha256(payload).hexdigest()
        cls.source = cls.archives / "source.json"
        cls.source.write_text(json.dumps({
            "repo": "facebook/omnilingual-asr-corpus", "revision": REVISION, "language": "pnb_Arab",
            "licence": "CC-BY-4.0", "files": [{"path": SHARD, "local_name": archive.name, "size": len(payload),
                                               "sha256": cls.archive_sha, "publisher_lfs_sha256": cls.archive_sha}]}),
            encoding="utf-8")
        cls.pristine_raw, cls.pristine_manifest = base / "raw", base / "manifests"
        summary = pnb.run_pilot(cls.source, cls.archives, cls.pristine_raw, cls.pristine_manifest, per_speaker=2)
        if not summary["pilot_complete"]:
            raise RuntimeError("fixture import did not complete")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        private = tempfile.TemporaryDirectory()
        self.addCleanup(private.cleanup)
        root = Path(private.name).resolve()
        self.raw, self.manifest = root / "raw", root / "manifests"
        shutil.copytree(self.pristine_raw, self.raw)
        shutil.copytree(self.pristine_manifest, self.manifest)
        self.pilot = json.loads((self.manifest / "pilot.json").read_text(encoding="utf-8"))

    def argv(self, expected=None, **extra):
        argv = ["--source-manifest", str(self.source), "--archive-root", str(self.archives),
                "--raw-root", str(self.raw), "--manifest-root", str(self.manifest),
                "--expected-archive-sha256", expected or self.archive_sha, "--per-speaker", "2"]
        for name, value in extra.items():
            argv.extend(["--" + name.replace("_", "-")] + ([] if value is True else [str(value)]))
        return argv

    def run_verifier(self, *args, **kwargs):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = verifier.main(self.argv(*args, **kwargs))
        return code, output.getvalue().strip()

    def assert_fails_with(self, name, *args, **kwargs):
        code, output = self.run_verifier(*args, **kwargs)
        self.assertEqual(code, 1, output)
        self.assertTrue(output.startswith("FAIL"), output)
        self.assertIn(name, output)
        self.assertNotIn("PASS", output)
        return output

    def flip_last_byte(self, path):
        payload = bytearray(path.read_bytes())
        payload[-1] ^= 0x01
        path.write_bytes(bytes(payload))

    def test_clean_pilot_passes_with_aggregate_summary_only(self):
        code, output = self.run_verifier(expected_speakers=2)
        self.assertEqual(code, 0, output)
        self.assertEqual(output.count("\n"), 0, output)
        for fragment in ("PASS rows=4", "speakers=2", "per_speaker=2", "minutes=0.2667", f"native_formats={RATE}Hz/1ch:4"):
            self.assertIn(fragment, output)
        self.assertNotIn("fictional-speaker", output)
        self.assertNotIn("meta_pnb_", output)
        self.assertFalse((self.manifest / "verification.json").exists())
        self.assertEqual(len(self.pilot["files"]), 4)

    def test_recomputed_selection_matches_documented_formula(self):
        meta = {(SHARD, i): {"speaker_id": row["speaker_id"]} for i, row in enumerate(fixture_rows())}
        expected, speakers = verifier.expected_selection(meta, REVISION, 2)
        self.assertEqual(speakers, 2)
        self.assertEqual({index for _, index in expected}, {row["source_row_index"] for row in self.pilot["files"]})
        self.assertEqual(len(expected), 4)

    def test_wrong_expected_archive_hash_fails(self):
        self.assert_fails_with("archive_sha256_matches_expected", expected="0" * 64)
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                verifier.main(self.argv(expected="not-a-hash"))

    def test_wrong_expected_speaker_count_fails(self):
        self.assert_fails_with("speaker_count", expected_speakers=3)

    def test_tampered_wav_byte_fails(self):
        self.flip_last_byte(self.raw / self.pilot["files"][0]["path"])
        output = self.assert_fails_with("wav_sha256 x1")
        self.assertIn("wav_frames_differ x1", output)

    def test_tampered_flac_byte_fails(self):
        self.flip_last_byte(self.raw / self.pilot["files"][1]["original_audio_path"])
        self.assert_fails_with("flac_bytes_differ x1")

    def test_tampered_transcript_fails(self):
        path = self.raw / self.pilot["files"][2]["source_transcript_path"]
        transcript = json.loads(path.read_text(encoding="utf-8"))
        transcript["raw_text"] = transcript["raw_text"] + " altered"
        path.write_text(json.dumps(transcript), encoding="utf-8")
        self.assert_fails_with("transcript_text x1")

    def test_flipped_policy_field_fails(self):
        for field, value in (("training_eligible", True), ("cloning_status", "authorized"), ("split", "train")):
            with self.subTest(field=field):
                pilot = json.loads((self.pristine_manifest / "pilot.json").read_text(encoding="utf-8"))
                pilot["files"][3][field] = value
                (self.manifest / "pilot.json").write_text(json.dumps(pilot), encoding="utf-8")
                self.assert_fails_with("policy_fields x1")

    def test_incomplete_marker_fails(self):
        for root in (self.raw, self.manifest):
            with self.subTest(root=root.name):
                marker = root / "INCOMPLETE"
                marker.write_text("fictional interrupted publication", encoding="utf-8")
                self.assert_fails_with("no_incomplete_markers")
                marker.unlink()

    def test_missing_raw_file_fails_without_row_identifiers(self):
        (self.raw / self.pilot["files"][0]["path"]).unlink()
        output = self.assert_fails_with("row_unreadable:FileNotFoundError x1")
        self.assertIn("raw_file_counts", output)
        self.assertNotIn("meta_pnb_", output)

    def test_write_verification_writes_once_then_refuses(self):
        target = self.manifest / "verification.json"
        code, output = self.run_verifier(write_verification=True)
        self.assertEqual(code, 0, output)
        self.assertIn("transcript_hashes=recorded", output)
        self.assertNotIn("meta_pnb_", output)
        text = target.read_text(encoding="utf-8")
        written = json.loads(text)
        self.assertEqual(written["result"], "PASS")
        self.assertEqual((written["summary"]["rows"], written["summary"]["speakers"]), (4, 2))
        self.assertEqual(written["summary"]["archive"]["sha256"], self.archive_sha)
        self.assertEqual(written["environment"]["verifier_sha256"], hashlib.sha256(SCRIPT.read_bytes()).hexdigest())
        for package in ("pyarrow", "soundfile", "numpy"):
            self.assertRegex(written["environment"][package], r"^\d+\.\d+")
        expected = {row["source_transcript_path"]: hashlib.sha256((self.raw / row["source_transcript_path"]).read_bytes()).hexdigest()
                    for row in self.pilot["files"]}
        self.assertEqual(written["transcript_sha256"], expected)
        self.assertEqual(len(expected), 4)
        self.assertNotIn("Fictional unaligned transcript", text)
        self.assertNotIn("Fictional prompt", text)
        before = target.read_bytes()
        code, output = self.run_verifier(write_verification=True)
        self.assertEqual(code, 1, output)
        self.assertIn("already exists", output)
        self.assertEqual(target.read_bytes(), before)

    def test_prior_verification_transcript_hashes_are_compared(self):
        target = self.manifest / "verification.json"
        code, output = self.run_verifier(write_verification=True)
        self.assertEqual(code, 0, output)
        code, output = self.run_verifier()
        self.assertEqual(code, 0, output)
        self.assertIn("transcript_hashes=matched_prior", output)
        path = self.raw / self.pilot["files"][1]["source_transcript_path"]
        transcript = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(transcript, indent=None, separators=(",", ":")), encoding="utf-8")
        output = self.assert_fails_with("transcript_hashes_match_prior_verification x1")
        self.assertNotIn("transcript_text", output)
        target.write_text(json.dumps({"checks": {"legacy": {"pass": True}}}), encoding="utf-8")
        code, output = self.run_verifier()
        self.assertEqual(code, 0, output)
        self.assertIn("transcript_hashes=recorded", output)

    def test_failed_verification_writes_nothing(self):
        self.flip_last_byte(self.raw / self.pilot["files"][0]["path"])
        self.assert_fails_with("wav_sha256", write_verification=True)
        self.assertFalse((self.manifest / "verification.json").exists())

    def test_refuses_under_python_optimize_and_uses_no_bare_assert(self):
        result = subprocess.run([sys.executable, "-O", str(SCRIPT), *self.argv(write_verification=True)],
                                capture_output=True, text=True, timeout=120)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-O", result.stdout)
        self.assertFalse((self.manifest / "verification.json").exists())
        self.assertNotRegex(SCRIPT.read_text(encoding="utf-8"), re.compile(r"^\s*assert\b", re.MULTILINE))

    def test_cli_subprocess_passes_clean_pilot(self):
        result = subprocess.run([sys.executable, str(SCRIPT), *self.argv(expected_speakers=2)],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stdout.startswith("PASS rows=4"), result.stdout)


if __name__ == "__main__":
    unittest.main()
