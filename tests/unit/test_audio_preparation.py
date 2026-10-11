"""Fictional PCM fixtures only; no real audio or main-repository writes."""

import array
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave


MODULE = Path(__file__).resolve().parents[2] / "data/prepare/prepare_audio.py"
SPEC = importlib.util.spec_from_file_location("prepare_audio", MODULE)
preparation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preparation)
RATE = 16_000


class AudioPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.sources = self.root / "sources"
        self.sources.mkdir()
        self.manifest = self.root / "fictional-manifest.json"
        self.output = self.root / "prepared"

    def wav(self, name="Speaker_02/fictional.wav", seconds=4, amplitude=2000,
            pause=None, rate=RATE, channels=1, width=2):
        path = self.sources / name
        path.parent.mkdir(parents=True, exist_ok=True)
        frames = int(seconds * rate)
        if width == 2:
            samples = array.array("h", (
                0 if pause and pause[0] <= index / rate < pause[1]
                else amplitude * (1 if index % 40 < 20 else -1)
                for index in range(frames) for _ in range(channels)
            ))
            if sys.byteorder != "little":
                samples.byteswap()
            payload = samples.tobytes()
        else:
            payload = bytes([128]) * frames * channels * width
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(channels)
            stream.setsampwidth(width)
            stream.setframerate(rate)
            stream.writeframes(payload)
        return {"path": name, "speaker_id": "Speaker_02",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def write_manifest(self, entries):
        self.manifest.write_text(json.dumps({"revision": "fictional-revision", "files": entries}),
                                 encoding="utf-8")

    def process(self, entries, output=None):
        self.write_manifest(entries)
        output = output or self.output
        summary = preparation.process_manifest(self.manifest, self.sources, output)
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(summary, report["summary"])
        return report

    def assert_coverage(self, source, clips, total_samples):
        ranges = [(clip["start_sample"], clip["end_sample"]) for clip in clips]
        ranges += [(gap["start_sample"], gap["end_sample"])
                   for gap in source["uncovered_ranges"]]
        cursor = 0
        for start, end in sorted(ranges):
            self.assertEqual(start, cursor, "Coverage must neither overlap nor silently lose samples")
            self.assertGreater(end, start)
            cursor = end
        self.assertEqual(cursor, total_samples)

    def test_pause_chunks_preserve_exact_frames_hashes_and_originals(self):
        entry = self.wav(seconds=7.2, pause=(3.4, 3.8))
        source_path = self.sources / entry["path"]
        before = source_path.read_bytes()
        report = self.process([entry])
        self.assertEqual(source_path.read_bytes(), before)
        self.assertEqual(report["summary"]["errors"], 0)
        self.assertEqual(report["summary"]["sources"], 1)
        self.assertEqual(len(report["clips"]), 2)
        with wave.open(str(source_path), "rb") as stream:
            original_frames = stream.readframes(stream.getnframes())
        for clip in report["clips"]:
            start, end = clip["start_sample"], clip["end_sample"]
            self.assertGreaterEqual(end - start, 3 * RATE)
            self.assertLessEqual(end - start, 6 * RATE)
            for boundary in (start, end):
                if boundary not in (0, int(7.2 * RATE)):
                    self.assertTrue(int(3.4 * RATE) <= boundary <= int(3.8 * RATE))
            self.assertEqual(clip["speaker_id"], entry["speaker_id"])
            self.assertEqual(clip["source_path"], entry["path"])
            self.assertEqual(clip["source_sha256"], entry["sha256"])
            self.assertEqual(clip["duration_seconds"], (end - start) / RATE)
            self.assertEqual(clip["review_status"], "pending_listening")
            path = self.output / clip["path"]
            self.assertTrue(path.resolve().is_relative_to(self.output.resolve()))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), clip["sha256"])
            with wave.open(str(path), "rb") as stream:
                self.assertEqual((stream.getframerate(), stream.getnchannels(), stream.getsampwidth()),
                                 (RATE, 1, 2))
                self.assertEqual(stream.readframes(stream.getnframes()), original_frames[start * 2:end * 2])
        self.assert_coverage(report["sources"][0], report["clips"], int(7.2 * RATE))

    def test_three_and_six_second_continuous_sources_need_no_internal_cut(self):
        for seconds in (3, 6):
            with self.subTest(seconds=seconds):
                entry = self.wav(seconds=seconds)
                report = self.process([entry], self.root / f"prepared-{seconds}")
                self.assertEqual(len(report["clips"]), 1)
                self.assertEqual(report["clips"][0]["start_sample"], 0)
                self.assertEqual(report["clips"][0]["end_sample"], seconds * RATE)
                self.assert_coverage(report["sources"][0], report["clips"], seconds * RATE)

    def test_short_and_long_without_pause_are_retained_as_uncovered(self):
        for seconds in (2, 7):
            with self.subTest(seconds=seconds):
                report = self.process([self.wav(seconds=seconds)], self.root / f"prepared-{seconds}")
                self.assertEqual(report["clips"], [])
                self.assertEqual(report["sources"][0]["status"], "processed")
                self.assert_coverage(report["sources"][0], [], seconds * RATE)

    def test_signal_flags_require_review_and_never_claim_acceptance(self):
        for label, amplitude in (("silence", 0), ("quiet", 500), ("full-scale", 32767)):
            with self.subTest(label=label):
                report = self.process([self.wav(amplitude=amplitude)], self.root / label)
                self.assertTrue(report["sources"][0]["metrics"]["review_flags"])
                if label == "silence":
                    self.assertEqual(report["clips"], [])
                    self.assert_coverage(report["sources"][0], [], 4 * RATE)
                    continue
                self.assertEqual(len(report["clips"]), 1)
                clip = report["clips"][0]
                self.assertTrue(clip["review_flags"])
                self.assertEqual(clip["review_status"], "pending_listening")

    def test_pause_shorter_than_120ms_does_not_authorize_a_cut(self):
        report = self.process([self.wav(seconds=7, pause=(3.5, 3.58))])
        self.assertEqual(report["clips"], [])
        self.assert_coverage(report["sources"][0], [], 7 * RATE)

    def test_output_write_failure_does_not_publish_partial_folder(self):
        entry = self.wav()
        original = (self.sources / entry["path"]).read_bytes()
        self.write_manifest([entry])
        real_wave_open = wave.open

        def fail_output(file, mode=None):
            if mode == "wb":
                raise OSError("fictional output failure")
            return real_wave_open(file, mode)

        with patch.object(preparation.wave, "open", side_effect=fail_output):
            with self.assertRaisesRegex(OSError, "fictional output failure"):
                preparation.process_manifest(self.manifest, self.sources, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob("ava-prep-*")), [])
        self.assertEqual((self.sources / entry["path"]).read_bytes(), original)

    def test_hash_mismatch_quarantines_source_and_continues_valid_input(self):
        bad = self.wav("Speaker_02/bad-hash.wav")
        bad["sha256"] = "0" * 64
        good = self.wav("Speaker_02/good.wav")
        report = self.process([bad, good])
        self.assertEqual(report["summary"]["errors"], 1)
        statuses = {source["path"]: source for source in report["sources"]}
        self.assertEqual(statuses[bad["path"]]["status"], "quarantined")
        self.assertTrue(statuses[bad["path"]]["error"])
        self.assertEqual(statuses[good["path"]]["status"], "processed")
        self.assertEqual([clip["source_path"] for clip in report["clips"]], [good["path"]])

    def test_corrupt_and_unsupported_audio_are_quarantined(self):
        for label, options in (("stereo", {"channels": 2}), ("8khz", {"rate": 8000}),
                               ("8bit", {"width": 1}), ("corrupt", {}), ("truncated", {})):
            with self.subTest(label=label):
                entry = self.wav(**options)
                path = self.sources / entry["path"]
                if label == "corrupt":
                    path.write_bytes(b"not a WAV")
                elif label == "truncated":
                    path.write_bytes(path.read_bytes()[:-100])
                entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                report = self.process([entry], self.root / label)
                self.assertEqual(report["summary"]["errors"], 1)
                self.assertEqual(report["clips"], [])
                self.assertEqual(report["sources"][0]["status"], "quarantined")

    def test_malformed_manifest_aborts_before_output_even_after_valid_entry(self):
        good = self.wav()
        malformed = ("not JSON", "[]", '{"files": {}}',
                     json.dumps({"files": [good, {"path": "missing-fields.wav"}]}),
                     json.dumps({"files": [{**good, "sha256": "invalid"}]}),
                     json.dumps({"files": [{**good, "speaker_id": "../unsafe"}]}))
        for index, contents in enumerate(malformed):
            with self.subTest(contents=contents):
                self.manifest.write_text(contents, encoding="utf-8")
                output = self.root / f"invalid-{index}"
                with self.assertRaises(ValueError):
                    preparation.process_manifest(self.manifest, self.sources, output)
                self.assertFalse(output.exists())

    def test_unsafe_manifest_paths_fail_before_output(self):
        good = self.wav()
        for index, name in enumerate(("../outside.wav", "/absolute.wav", "C:/absolute.wav",
                                      "Speaker_02/../../outside.wav", "Speaker_02\\file.wav")):
            with self.subTest(path=name):
                self.write_manifest([{**good, "path": name}])
                output = self.root / f"unsafe-{index}"
                with self.assertRaises(ValueError):
                    preparation.process_manifest(self.manifest, self.sources, output)
                self.assertFalse(output.exists())

    def test_source_symlink_is_quarantined_without_reading_external_audio(self):
        original = self.wav()
        outside = self.root / "outside.wav"
        outside.write_bytes((self.sources / original["path"]).read_bytes())
        link = self.sources / "Speaker_02/link.wav"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Host cannot create symlinks: {type(error).__name__}")
        report = self.process([{**original, "path": "Speaker_02/link.wav"}])
        self.assertEqual(report["clips"], [])
        self.assertEqual(report["sources"][0]["status"], "quarantined")
        self.assertEqual(report["summary"]["errors"], 1)

    def test_existing_output_and_overlapping_roots_are_refused(self):
        self.write_manifest([self.wav()])
        self.output.mkdir()
        sentinel = self.output / "retain.txt"
        sentinel.write_text("do not overwrite", encoding="utf-8")
        for target in (self.output, self.sources, self.sources / "new-output", self.root):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    preparation.process_manifest(self.manifest, self.sources, target)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not overwrite")
        self.assertFalse((self.sources / "new-output").exists())

    def test_bounded_manifest_and_source_reads_fail_conservatively(self):
        entry = self.wav()
        self.write_manifest([entry])
        with patch.object(preparation, "MAX_MANIFEST_BYTES", 16):
            with self.assertRaises(ValueError):
                preparation.process_manifest(self.manifest, self.sources, self.output)
        self.assertFalse(self.output.exists())
        with patch.object(preparation, "MAX_SOURCE_BYTES", 16):
            report = self.process([entry])
        self.assertEqual(report["summary"]["errors"], 1)
        self.assertEqual(report["clips"], [])
        self.assertEqual(report["sources"][0]["status"], "quarantined")

    def test_directory_source_is_quarantined_and_zero_clip_reason_is_explicit(self):
        entry = self.wav(seconds=2)
        folder = self.sources / "Speaker_02/directory.wav"
        folder.mkdir()
        report = self.process([{**entry, "path": "Speaker_02/directory.wav"}, entry])
        self.assertEqual(report["sources"][0]["status"], "quarantined")
        self.assertTrue(report["sources"][0]["error"])
        self.assertTrue(report["sources"][1]["segmentation_outcome"])
        self.assertEqual(report["sources"][1]["status"], "processed")
        self.assertEqual(report["clips"], [])

    def test_output_clip_bytes_and_lineage_are_deterministic(self):
        entry = self.wav(seconds=7.2, pause=(3.4, 3.8))
        first = self.process([entry], self.root / "first")
        second = self.process([entry], self.root / "second")
        self.assertEqual(first["summary"], second["summary"])
        self.assertEqual(first["clips"], second["clips"])
        self.assertEqual(first["sources"], second["sources"])
        for clip in first["clips"]:
            self.assertEqual((self.root / "first" / clip["path"]).read_bytes(),
                             (self.root / "second" / clip["path"]).read_bytes())

    def test_cli_prepares_external_fixture_and_reports_invalid_input(self):
        self.write_manifest([self.wav()])
        command = [sys.executable, str(MODULE), "--manifest", str(self.manifest),
                   "--source-root", str(self.sources), "--output-root", str(self.output)]
        process = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue((self.output / "report.json").is_file())
        repeat = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
        self.assertNotEqual(repeat.returncode, 0)
        self.assertNotIn("Traceback", repeat.stderr)


if __name__ == "__main__":
    unittest.main()
