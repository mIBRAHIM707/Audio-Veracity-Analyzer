"""Fictional existing-synthetic intake fixtures; no real speech or network."""

import array
from collections import Counter
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave


MODULE = Path(__file__).resolve().parents[2] / "data/collect/csalt_synthetic.py"
SPEC = importlib.util.spec_from_file_location("csalt_synthetic", MODULE)
synthetic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(synthetic)
REVISION = "a" * 40
FAMILIES = ("Spoofed_TTS", "Spoofed_Tacotron")


def fixture_wav(rate=16000, amplitude=2000):
    values = array.array("h", (amplitude if index % 40 < 20 else -amplitude
                               for index in range(rate * 4)))
    if sys.byteorder != "little":
        values.byteswap()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        stream.writeframes(values.tobytes())
    return buffer.getvalue()


class SyntheticBenchmarkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source_root = self.root / "sources"
        self.source_root.mkdir()
        self.output_root = self.root / "benchmark"
        self.catalog_path = self.root / "catalog.json"
        self.payloads = {}
        self.calls = []

    def entries(self, speakers=(1, 2), count=1, rate=16000, amplitude=2000):
        payload = fixture_wav(rate, amplitude)
        result = []
        for family in FAMILIES:
            for speaker in speakers:
                for number in range(1, count + 1):
                    name = f"{family}/Speaker_{speaker:02d}/Part 1/{number}.wav"
                    self.payloads[name] = payload
                    result.append({"path": name, "size": len(payload),
                                   "lfs": {"oid": hashlib.sha256(payload).hexdigest()}})
        return result

    def catalog(self, entries):
        return {"revision": REVISION, "files": entries}

    def local(self, entry, payload=None):
        path = self.source_root / entry["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.payloads[entry["path"]] if payload is None else payload)
        return path

    def fetch(self, entry, revision):
        self.assertEqual(revision, REVISION)
        self.calls.append(entry["path"])
        return self.payloads[entry["path"]]

    def run_benchmark(self, entries, per_group=1, fetch=None, output=None):
        self.catalog_path.write_text(json.dumps(self.catalog(entries)), encoding="utf-8")
        output = output or self.output_root
        summary = synthetic.run_benchmark(self.catalog_path, self.source_root, output,
                                          per_group=per_group, fetch_bytes=fetch or self.fetch)
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(summary, report["summary"])
        with (output / "inventory.csv").open(encoding="utf-8-sig", newline="") as stream:
            inventory = list(csv.DictReader(stream))
        manifest_path = output / "benchmark.json"
        if summary["benchmark_complete"]:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        else:
            self.assertFalse(manifest_path.exists())
            manifest = None
        return summary, inventory, manifest, report

    def test_selection_balances_two_per_family_speaker_independent_of_order(self):
        entries = self.entries(count=3)
        selected = synthetic.select_entries(self.catalog(entries), per_group=2)
        reversed_selection = synthetic.select_entries(self.catalog(entries[::-1]), per_group=2)
        paths = {entry["path"] for entry in selected}
        self.assertEqual(paths, {entry["path"] for entry in reversed_selection})
        self.assertEqual(len(paths), 8)
        counts = Counter("/".join(name.split("/")[:2]) for name in paths)
        self.assertEqual(len(counts), 4)
        self.assertEqual(set(counts.values()), {2})

    def test_missing_family_mismatched_speakers_and_insufficient_group_rejected(self):
        entries = self.entries()
        for invalid in (entries[:2], entries[:-1]):
            with self.subTest(paths=[entry["path"] for entry in invalid]):
                with self.assertRaises(ValueError):
                    synthetic.select_entries(self.catalog(invalid), per_group=1)
        for target in (0, -1, True, 1.5, 2):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    synthetic.select_entries(self.catalog(entries), per_group=target)

    def test_existing_valid_sources_are_reused_with_explicit_benchmark_provenance(self):
        entries = self.entries()
        originals = {self.local(entry): self.payloads[entry["path"]] for entry in entries}
        summary, inventory, manifest, _ = self.run_benchmark(entries)
        self.assertTrue(summary["benchmark_complete"])
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(inventory), 4)
        self.assertEqual({row["path"] for row in manifest["files"]}, {entry["path"] for entry in entries})
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)
        for row in inventory:
            self.assertEqual(row["label"], "synthetic")
            self.assertEqual(row["language"], "ur")
            self.assertEqual(row["label_provenance"], "publisher")
            self.assertEqual(row["split"], "unassigned")
            self.assertEqual(row["benchmark_only"], "true")
            self.assertEqual(row["training_eligible"], "false")
            self.assertEqual(row["use_scope"], "noncommercial_benchmark_only")
            self.assertEqual(row["cloning_status"], "not_authorized")
            self.assertEqual(row["speaker_identity_status"], "publisher_folder_only")
            self.assertEqual(row["target_reference_status"], "unknown")
            self.assertEqual(row["generator_version"], "unknown")
            partition = row["path"].split("/")[0]
            self.assertEqual(row["source_partition"], partition)
            self.assertEqual(row["generator_family"], "vits" if partition == "Spoofed_TTS" else "tacotron")
        for row in manifest["files"]:
            self.assertIs(row["benchmark_only"], True)
            self.assertIs(row["training_eligible"], False)
            self.assertEqual(hashlib.sha256((self.source_root / row["path"]).read_bytes()).hexdigest(), row["sha256"])

    def test_corrupt_existing_is_not_replaced_and_incomplete_manifest_is_withheld(self):
        entries = self.entries()
        corrupt = self.local(entries[0], b"fictional corrupt original")
        summary, inventory, manifest, _ = self.run_benchmark(entries)
        self.assertFalse(summary["benchmark_complete"])
        self.assertEqual(summary["errors"], 1)
        self.assertIsNone(manifest)
        self.assertEqual(corrupt.read_bytes(), b"fictional corrupt original")
        self.assertNotIn(entries[0]["path"], self.calls)
        self.assertEqual(sum(row["validation_status"] == "verified" for row in inventory), 3)

    def test_hash_decode_and_fetch_failures_keep_valid_siblings_but_no_manifest(self):
        for case in ("hash", "decode", "fetch"):
            with self.subTest(case=case):
                entries = self.entries()
                bad = entries[0]
                if case == "decode":
                    malformed = b"publisher-hashed but not a WAV"
                    bad.update(size=len(malformed), lfs={"oid": hashlib.sha256(malformed).hexdigest()})

                def fetch(entry, revision):
                    if entry["path"] != bad["path"]:
                        return self.fetch(entry, revision)
                    if case == "fetch":
                        raise OSError("fictional download interruption")
                    return malformed if case == "decode" else b"wrong publisher bytes"

                summary, inventory, manifest, _ = self.run_benchmark(entries, fetch=fetch, output=self.root / case)
                self.assertEqual(summary["errors"], 1)
                self.assertFalse(summary["benchmark_complete"])
                self.assertIsNone(manifest)
                self.assertFalse((self.source_root / bad["path"]).exists())
                self.assertEqual(sum(row["validation_status"] == "verified" for row in inventory), 3)

    def test_unsafe_catalog_and_metadata_fail_before_fetch_or_output(self):
        entries = self.entries()
        invalid_paths = ("../outside.wav", "/absolute.wav", "C:/outside.wav",
                         "Bonafide/Speaker_01/1.wav", "Spoofed_TTS/Speaker_01/../../outside.wav",
                         "Spoofed_TTS\\Speaker_01\\1.wav")
        catalogs = [self.catalog([{**entries[0], "path": path}, *entries[1:]]) for path in invalid_paths]
        catalogs.extend([{"revision": "main", "files": entries},
                         self.catalog([{**entries[0], "lfs": {"oid": "bad"}}, *entries[1:]]),
                         self.catalog([{**entries[0], "size": True}, *entries[1:]]),
                         self.catalog([*entries, entries[0]])])
        for index, catalog in enumerate(catalogs):
            with self.subTest(index=index):
                self.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
                output = self.root / f"unsafe-{index}"
                with self.assertRaises(ValueError):
                    synthetic.run_benchmark(self.catalog_path, self.source_root, output,
                                            per_group=1, fetch_bytes=self.fetch)
                self.assertFalse(output.exists())
                self.assertEqual(self.calls, [])

    def test_expected_speakers_and_output_safety_reject_before_network(self):
        entries = self.entries()
        self.catalog_path.write_text(json.dumps(self.catalog(entries)), encoding="utf-8")
        with self.assertRaises(ValueError):
            synthetic.run_benchmark(self.catalog_path, self.source_root, self.output_root,
                                    per_group=1, fetch_bytes=self.fetch, expected_speakers={"Speaker_01"})
        self.assertFalse(self.output_root.exists())
        self.output_root.mkdir()
        sentinel = self.output_root / "keep.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        for target in (self.output_root, self.source_root, self.source_root / "nested", self.root):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    synthetic.run_benchmark(self.catalog_path, self.source_root, target,
                                            per_group=1, fetch_bytes=self.fetch)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
        self.assertEqual(self.calls, [])

    def test_publication_failure_leaves_no_final_or_partial_audio(self):
        entries = self.entries()
        with patch.object(synthetic.os, "link", side_effect=OSError("fictional publication failure")):
            summary, inventory, manifest, report = self.run_benchmark(entries)
        self.assertEqual(summary["errors"], 4)
        self.assertIsNone(manifest)
        self.assertEqual([path for path in self.source_root.rglob("*") if path.is_file()], [])
        self.assertTrue(all(row["validation_status"] == "failed" for row in inventory))
        self.assertEqual(len(report["errors"]), 4)

    def test_native_low_volume_sources_are_preserved_without_listening_acceptance(self):
        entries = self.entries(rate=20000, amplitude=30)
        summary, inventory, manifest, _ = self.run_benchmark(entries)
        self.assertTrue(summary["benchmark_complete"])
        self.assertEqual(summary["errors"], 0)
        for row in inventory:
            self.assertEqual(int(row["sample_rate"]), 20000)
            flags = json.loads(row["review_flags"])
            self.assertIn("requires_format_processing", flags)
            self.assertIn("low_rms", flags)
            self.assertEqual(row["listening_status"], "not_individually_reviewed")
        self.assertEqual(len(manifest["files"]), 4)

    def test_shared_conditioning_retains_benchmark_exclusion_and_original_lineage(self):
        from data.prepare.prepare_audio import process_manifest

        entries = self.entries(rate=20000)
        _, _, manifest, _ = self.run_benchmark(entries)
        originals = {entry["path"]: (self.source_root / entry["path"]).read_bytes() for entry in entries}
        output = self.root / "prepared"
        summary = process_manifest(self.output_root / "benchmark.json", self.source_root, output, condition=True)
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(summary["clips"], 4)
        source_rows = {row["path"]: row for row in manifest["files"]}
        fields = ("generator_family", "generator_version", "source_partition", "speaker_identity_status",
                  "target_reference_status", "training_eligible", "benchmark_only", "use_scope", "label_provenance")
        for record in report["sources"] + report["clips"]:
            original_path = record.get("source_path", record.get("path"))
            original = source_rows[original_path]
            for field in fields:
                self.assertEqual(record[field], original[field])
            self.assertIs(record["training_eligible"], False)
            self.assertIs(record["benchmark_only"], True)
            self.assertEqual(record["source_clip_id"], original["clip_id"])
            self.assertEqual(record["source_sha256"], original["sha256"])
            self.assertEqual(record["speaker_id"], original["speaker_id"])
        for path, before in originals.items():
            self.assertEqual((self.source_root / path).read_bytes(), before)

    def test_preparation_refuses_manifest_from_incomplete_intake(self):
        from data.prepare.prepare_audio import process_manifest

        self.run_benchmark(self.entries(rate=16000))
        marker = self.output_root / "INCOMPLETE"
        marker.write_text("fictional interrupted publication", encoding="utf-8")
        for condition in (False, True):
            with self.subTest(condition=condition):
                destination = self.root / f"refused-preparation-{condition}"
                with self.assertRaises(ValueError):
                    process_manifest(self.output_root / "benchmark.json", self.source_root,
                                     destination, condition=condition)
                self.assertFalse(destination.exists())
        self.assertEqual(marker.read_text(encoding="utf-8"), "fictional interrupted publication")

    def test_default_preparation_retains_benchmark_exclusion_for_native_16khz(self):
        from data.prepare.prepare_audio import process_manifest

        entries = self.entries(rate=16000)
        _, _, manifest, _ = self.run_benchmark(entries)
        output = self.root / "default-prepared"
        summary = process_manifest(self.output_root / "benchmark.json", self.source_root, output)
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(summary["clips"], 4)
        originals = {row["path"]: row for row in manifest["files"]}
        fields = ("generator_family", "generator_version", "source_partition", "speaker_identity_status",
                  "target_reference_status", "use_scope", "label_provenance", "label", "language")
        for record in report["sources"] + report["clips"]:
            original = originals[record.get("source_path", record.get("path"))]
            self.assertIs(record["benchmark_only"], True)
            self.assertIs(record["training_eligible"], False)
            self.assertEqual(record["source_clip_id"], original["clip_id"])
            self.assertEqual(record["source_sha256"], original["sha256"])
            for field in fields:
                self.assertEqual(record[field], original[field])
        for clip in report["clips"]:
            with wave.open(str(self.source_root / clip["source_path"]), "rb") as source:
                source_pcm = source.readframes(source.getnframes())
            with wave.open(str(output / clip["path"]), "rb") as prepared:
                self.assertEqual(prepared.readframes(prepared.getnframes()), source_pcm)


if __name__ == "__main__":
    unittest.main()
