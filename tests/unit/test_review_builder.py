"""Offline review builder fixtures contain generated PCM, never real speech."""

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import wave
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[2] / "data/review/build_review.py"
SPEC = importlib.util.spec_from_file_location("build_review", MODULE)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class ReviewBuilderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.clip = self.root / "clips/fictional.wav"
        self.clip.parent.mkdir()
        with wave.open(str(self.clip), "wb") as audio:
            audio.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\0\0" * 48_000)
        self.report_path = self.root / "report.json"
        self.record = {
            "path": "clips/fictional.wav", "speaker_id": "fictional-speaker",
            "duration_seconds": 3.0, "review_flags": ["mostly_quiet"],
            "sha256": hashlib.sha256(self.clip.read_bytes()).hexdigest(),
            "source_sha256": "a" * 64, "source_path": "fictional/source.wav",
            "start_sample": 0, "end_sample": 48_000,
            "review_status": "pending_listening",
        }

    def write_report(self, records=None):
        self.report_path.write_text(json.dumps({"schema_version": 1,
            "clips": [self.record] if records is None else records,
            "summary": {"clips": 1, "sources": 1, "errors": 0}}), encoding="utf-8")

    def assert_rejected(self):
        with self.assertRaises((ValueError, OSError)):
            builder.build_review(self.report_path)
        self.assertFalse((self.root / "review.html").exists())

    def test_valid_build_binds_report_inventory_and_preserves_inputs(self):
        self.write_report()
        before_report, before_audio = self.report_path.read_bytes(), self.clip.read_bytes()
        result = builder.build_review(self.report_path)
        self.assertEqual(result, self.root / "review.html")
        html = result.read_text(encoding="utf-8")
        self.assertIn(hashlib.sha256(before_report).hexdigest(), html)
        self.assertIn(self.record["sha256"], html)
        self.assertIn("clips/fictional.wav", html)
        self.assertEqual(self.report_path.read_bytes(), before_report)
        self.assertEqual(self.clip.read_bytes(), before_audio)

    def test_script_closing_payload_is_not_embedded_as_markup(self):
        payload = '</script><img src=x onerror="globalThis.fixtureAttack=true">'
        self.record["speaker_id"] = payload
        self.record["review_flags"] = [payload]
        self.record["source_path"] = payload
        self.write_report()
        result = builder.build_review(self.report_path)
        html = result.read_text(encoding="utf-8")
        self.assertNotIn(payload, html)
        self.assertIn(r"\u003c", html)
        # Browser tests separately establish that parsing/rendering stays inert.

    def test_malformed_or_empty_report_creates_no_page(self):
        for contents in ("not json", "[]", '{"clips": []}', '{"clips": {}}'):
            with self.subTest(contents=contents):
                self.report_path.write_text(contents, encoding="utf-8")
                self.assert_rejected()
        self.write_report([self.record.copy() for _ in range(201)])
        self.assert_rejected()

    def test_invalid_inventory_fields_reject_atomically(self):
        changes = ({"duration_seconds": float("nan")}, {"duration_seconds": float("inf")},
                   {"duration_seconds": 2.99}, {"duration_seconds": 6.01},
                   {"speaker_id": None}, {"review_flags": "quiet"},
                   {"review_flags": [3]}, {"sha256": "invalid"},
                   {"source_path": None}, {"start_sample": -1},
                   {"start_sample": True}, {"end_sample": 0}, {"end_sample": 48000.5},
                   {"end_sample": 64000})
        for change in changes:
            with self.subTest(change=change):
                self.write_report([{**self.record, **change}])
                self.assert_rejected()

    def test_unsafe_paths_and_duplicate_inventory_reject_without_page(self):
        paths = ("../outside.wav", "/absolute.wav", "C:/absolute.wav",
                 "clips/../outside.wav", "clips\\fictional.wav", "fictional.wav")
        for path in paths:
            with self.subTest(path=path):
                self.write_report([{**self.record, "path": path}])
                self.assert_rejected()
        self.write_report([self.record, self.record.copy()])
        self.assert_rejected()

    def test_hash_mismatch_and_incomplete_preparation_reject(self):
        self.write_report([{**self.record, "sha256": "0" * 64}])
        self.assert_rejected()
        self.write_report()
        (self.root / "INCOMPLETE").write_text("fixture marker", encoding="utf-8")
        self.assert_rejected()

    def test_existing_review_is_never_overwritten(self):
        self.write_report()
        output = self.root / "review.html"
        output.write_text("existing reviewer document", encoding="utf-8")
        with self.assertRaises((ValueError, OSError)):
            builder.build_review(self.report_path)
        self.assertEqual(output.read_text(encoding="utf-8"), "existing reviewer document")

    def test_report_size_limit_rejects_before_creating_page(self):
        self.write_report()
        with patch.object(builder, "MAX_BYTES", 16):
            self.assert_rejected()

    def test_symlink_clip_cannot_include_external_audio(self):
        self.write_report()
        outside = self.root / "outside.wav"
        outside.write_bytes(self.clip.read_bytes())
        self.clip.unlink()
        try:
            self.clip.symlink_to(outside)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Host cannot create symlinks: {type(error).__name__}")
        self.assert_rejected()


if __name__ == "__main__":
    unittest.main()
