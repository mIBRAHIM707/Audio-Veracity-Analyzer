"""Balanced importer regression tests with fictional WAVs and injected downloads."""

import array
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


MODULE = Path(__file__).resolve().parents[2] / "data/collect/csalt_pilot.py"
SPEC = importlib.util.spec_from_file_location("csalt_pilot", MODULE)
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)
REVISION = "a" * 40


def fictional_wav(amplitude=2000, rate=16_000, channels=1, seconds=4):
    import wave
    samples = array.array("h", (amplitude if index % 40 < 20 else -amplitude
                               for index in range(int(rate * seconds)) for _ in range(channels)))
    if sys.byteorder != "little":
        samples.byteswap()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        stream.writeframes(samples.tobytes())
    return buffer.getvalue()


class CsaltPilotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.sources = self.root / "sources"
        self.sources.mkdir()
        self.output = self.root / "inventory"
        self.catalog_path = self.root / "fictional-catalog.json"
        self.payloads = {}
        self.calls = []

    def entry(self, speaker, number, amplitude=2000):
        path = f"Bonafide/Speaker_{speaker:02d}/Part 1/{number}.wav"
        payload = fictional_wav(amplitude)
        self.payloads[path] = payload
        return {"path": path, "size": len(payload),
                "lfs": {"oid": hashlib.sha256(payload).hexdigest()}}

    def catalog(self, entries):
        return {"revision": REVISION, "files": entries}

    def save_local(self, entry, payload=None):
        path = self.sources / entry["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.payloads[entry["path"]] if payload is None else payload)
        return path

    def fetch(self, entry, revision):
        self.assertEqual(revision, REVISION)
        self.calls.append(entry["path"])
        return self.payloads[entry["path"]]

    def run_pilot(self, entries, per_speaker=2, fetch=None, output=None):
        self.catalog_path.write_text(json.dumps(self.catalog(entries)), encoding="utf-8")
        output = output or self.output
        summary = pilot.run_pilot(self.catalog_path, self.sources, output,
                                  per_speaker=per_speaker, fetch_bytes=fetch or self.fetch)
        with (output / "inventory.csv").open(encoding="utf-8-sig", newline="") as stream:
            inventory = list(csv.DictReader(stream))
        if summary["balanced_complete"]:
            balanced = json.loads((output / "balanced.json").read_text(encoding="utf-8"))
        else:
            self.assertFalse((output / "balanced.json").exists())
            balanced = None
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(summary, report["summary"])
        return summary, inventory, balanced, report

    def test_selection_balances_is_order_independent_and_prefers_existing(self):
        entries = [self.entry(speaker, number) for speaker in (1, 2) for number in range(1, 5)]
        existing = {entries[2]["path"], entries[6]["path"]}
        selected = pilot.select_entries(self.catalog(entries), existing, 2)
        reverse = pilot.select_entries(self.catalog(list(reversed(entries))), existing, 2)
        paths = {entry["path"] for entry in selected}
        self.assertEqual(paths, {entry["path"] for entry in reverse})
        self.assertTrue(existing <= paths)
        for speaker in ("Speaker_01", "Speaker_02"):
            self.assertEqual(sum(path.split("/")[1] == speaker for path in paths), 2)

    def test_invalid_target_or_insufficient_catalog_refuses_balance(self):
        catalog = self.catalog([self.entry(1, 1), self.entry(2, 1)])
        for target in (0, -1, True, 1.5, 2):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    pilot.select_entries(catalog, set(), target)

    def test_twenty_per_each_of_seventeen_folders_selects_340_entries(self):
        prototype = self.entry(1, 1)
        entries = [{**prototype, "path": f"Bonafide/Speaker_{speaker:02d}/Part 1/{number}.wav"}
                   for speaker in range(1, 18) for number in range(1, 22)]
        selected = pilot.select_entries(self.catalog(entries), set(), 20)
        self.assertEqual(len(selected), 340)
        self.assertEqual(len({entry["path"] for entry in selected}), 340)
        for speaker in range(1, 18):
            self.assertEqual(sum(entry["path"].split("/")[1] == f"Speaker_{speaker:02d}"
                                 for entry in selected), 20)

    def test_reuses_originals_preserves_extras_and_manifests_agree(self):
        entries = [self.entry(1, number) for number in range(1, 5)]
        entries += [self.entry(2, number) for number in range(1, 4)]
        existing = entries[:4] + [entries[4]]
        snapshots = {entry["path"]: self.save_local(entry).read_bytes() for entry in existing}
        summary, inventory, balanced, _ = self.run_pilot(entries)
        self.assertEqual(summary["errors"], 0)
        self.assertTrue(summary["balanced_complete"])
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(set(snapshots) & set(self.calls))
        for path, before in snapshots.items():
            self.assertEqual((self.sources / path).read_bytes(), before)
        self.assertEqual(len(inventory), 6)
        selected = {row["path"]: row for row in inventory if row["selected_for_pilot"] == "true"}
        self.assertEqual(len(selected), 4)
        self.assertEqual(len([row for row in inventory if row["selected_for_pilot"] == "false"]), 2)
        self.assertEqual(balanced["revision"], REVISION)
        self.assertEqual({row["path"] for row in balanced["files"]}, set(selected))
        for row in balanced["files"]:
            for key in ("speaker_id", "sha256"):
                self.assertEqual(row[key], selected[row["path"]][key])
        for row in inventory:
            self.assertEqual(row["validation_status"], "verified")
            self.assertEqual(row["split"], "unassigned")
            self.assertEqual(row["label"], "genuine")
            self.assertEqual(row["language"], "ur")
            self.assertEqual(row["label_provenance"], "publisher")
            self.assertEqual(row["listening_status"], "not_individually_reviewed")
            self.assertEqual(row["cloning_status"], "not_authorized")
            self.assertEqual(row["use_scope"], "noncommercial_detection_research")
            self.assertEqual(row["use_status"], "approved")
            self.assertEqual(hashlib.sha256((self.sources / row["path"]).read_bytes()).hexdigest(),
                             row["sha256"])

    def test_corrupt_existing_is_not_replaced_and_failed_row_remains(self):
        bad, good = self.entry(1, 1), self.entry(2, 1)
        damaged = b"fictional corrupt existing bytes"
        existing = self.save_local(bad, damaged)
        summary, inventory, balanced, _ = self.run_pilot([bad, good], per_speaker=1)
        self.assertGreater(summary["errors"], 0)
        self.assertFalse(summary["balanced_complete"])
        self.assertEqual(existing.read_bytes(), damaged)
        self.assertNotIn(bad["path"], self.calls)
        rows = {row["path"]: row for row in inventory}
        self.assertEqual(rows[bad["path"]]["validation_status"], "failed")
        self.assertNotEqual(rows[bad["path"]]["use_status"], "approved")
        self.assertIsNone(balanced)
        self.assertEqual(rows[good["path"]]["validation_status"], "verified")

    def test_invalid_download_hash_size_and_wav_never_publish_source(self):
        for case in ("hash", "size", "wav"):
            with self.subTest(case=case):
                entry = self.entry(1, 1)
                if case == "hash":
                    returned = bytes([self.payloads[entry["path"]][0] ^ 1]) + self.payloads[entry["path"]][1:]
                elif case == "size":
                    returned = self.payloads[entry["path"]] + b"extra"
                else:
                    returned = b"fictional non-WAV bytes"
                    entry["size"] = len(returned)
                    entry["lfs"]["oid"] = hashlib.sha256(returned).hexdigest()
                summary, inventory, balanced, _ = self.run_pilot(
                    [entry], per_speaker=1, fetch=lambda *_: returned, output=self.root / case)
                self.assertGreater(summary["errors"], 0)
                self.assertFalse(summary["balanced_complete"])
                self.assertFalse((self.sources / entry["path"]).exists())
                self.assertIsNone(balanced)
                self.assertFalse(any(row["validation_status"] == "verified" for row in inventory))

    def test_low_volume_flags_do_not_reject_publisher_labelled_clip(self):
        entry = self.entry(1, 1, amplitude=30)
        summary, inventory, balanced, _ = self.run_pilot([entry], per_speaker=1)
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(len(balanced["files"]), 1)
        self.assertEqual(inventory[0]["validation_status"], "verified")
        self.assertTrue(json.loads(inventory[0]["review_flags"]))
        self.assertEqual(inventory[0]["listening_status"], "not_individually_reviewed")

    def test_native_20khz_mono_is_preserved_and_flagged_for_later_conversion(self):
        entry = self.entry(17, 1)
        payload = fictional_wav(rate=20_000)
        self.payloads[entry["path"]] = payload
        entry.update(size=len(payload), lfs={"oid": hashlib.sha256(payload).hexdigest()})
        existing = self.save_local(entry)
        summary, inventory, balanced, _ = self.run_pilot([entry], per_speaker=1)
        self.assertTrue(summary["balanced_complete"])
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(existing.read_bytes(), payload)
        row = inventory[0]
        self.assertEqual(int(row["sample_rate"]), 20_000)
        self.assertEqual(int(row["channels"]), 1)
        self.assertEqual(float(row["duration_s"]), 4.0)
        self.assertIn("requires_format_processing", json.loads(row["review_flags"]))
        self.assertNotIn("requires_duration_processing", json.loads(row["review_flags"]))
        self.assertEqual(balanced["files"][0]["sha256"], entry["lfs"]["oid"])

    def test_native_stereo_frame_count_duration_and_bytes_are_preserved(self):
        entry = self.entry(1, 1)
        payload = fictional_wav(rate=48_000, channels=2)
        self.payloads[entry["path"]] = payload
        entry.update(size=len(payload), lfs={"oid": hashlib.sha256(payload).hexdigest()})
        summary, inventory, balanced, _ = self.run_pilot([entry], per_speaker=1)
        self.assertTrue(summary["balanced_complete"])
        self.assertEqual(summary["errors"], 0)
        self.assertEqual((self.sources / entry["path"]).read_bytes(), payload)
        row = inventory[0]
        self.assertEqual(int(row["channels"]), 2)
        self.assertEqual(int(row["sample_rate"]), 48_000)
        self.assertEqual(float(row["duration_s"]), 4.0)
        self.assertIn("requires_format_processing", json.loads(row["review_flags"]))
        self.assertNotIn("requires_duration_processing", json.loads(row["review_flags"]))
        self.assertEqual(balanced["files"][0]["sha256"], entry["lfs"]["oid"])

    def test_unsafe_catalog_aborts_before_download_or_output(self):
        valid = self.entry(1, 1)
        paths = ("../outside.wav", "/absolute.wav", "C:/absolute.wav",
                 "Bonafide/Speaker_01/../../outside.wav", "Synthetic/Speaker_01/1.wav",
                 "Bonafide/not-a-speaker/1.wav", "Bonafide\\Speaker_01\\1.wav")
        for index, path in enumerate(paths):
            with self.subTest(path=path):
                self.catalog_path.write_text(json.dumps(self.catalog([{**valid, "path": path}])), encoding="utf-8")
                target = self.root / f"unsafe-{index}"
                with self.assertRaises(ValueError):
                    pilot.run_pilot(self.catalog_path, self.sources, target, per_speaker=1, fetch_bytes=self.fetch)
                self.assertFalse(target.exists())
                self.assertEqual(self.calls, [])

    def test_malformed_catalog_revision_digest_size_duplicates_rejected(self):
        valid = self.entry(1, 1)
        catalogs = [self.catalog([{**valid, "lfs": {"oid": "bad"}}]),
                    self.catalog([{**valid, "size": 0}]),
                    self.catalog([{**valid, "size": True}]),
                    self.catalog([valid, valid.copy()]),
                    {"revision": "moving-main", "files": [valid]}, {"revision": REVISION, "files": []}]
        for catalog in catalogs:
            with self.subTest(catalog=catalog):
                with self.assertRaises(ValueError):
                    pilot.select_entries(catalog, set(), 1)

    def test_existing_or_overlapping_output_is_refused_without_fetch(self):
        self.catalog_path.write_text(json.dumps(self.catalog([self.entry(1, 1)])), encoding="utf-8")
        self.output.mkdir()
        sentinel = self.output / "keep.txt"
        sentinel.write_text("existing result", encoding="utf-8")
        for target in (self.output, self.sources, self.sources / "nested", self.root):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    pilot.run_pilot(self.catalog_path, self.sources, target, per_speaker=1, fetch_bytes=self.fetch)
        self.assertEqual(self.calls, [])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "existing result")
        self.assertFalse((self.sources / "nested").exists())

    def test_report_digest_binds_initial_catalog_if_file_changes_during_fetch(self):
        entry = self.entry(1, 1)
        initial_digest = None

        def mutate_catalog(download_entry, revision):
            nonlocal initial_digest
            initial_digest = hashlib.sha256(self.catalog_path.read_bytes()).hexdigest()
            self.catalog_path.write_text('{"changed_during_download": true}', encoding="utf-8")
            return self.fetch(download_entry, revision)

        summary, _, balanced, report = self.run_pilot([entry], per_speaker=1, fetch=mutate_catalog)
        self.assertTrue(summary["balanced_complete"])
        self.assertEqual(report["catalog_sha256"], initial_digest)
        self.assertNotEqual(report["catalog_sha256"], hashlib.sha256(self.catalog_path.read_bytes()).hexdigest())
        self.assertEqual(balanced["revision"], REVISION)

    def test_expected_speaker_inventory_mismatch_rejects_before_mutation(self):
        entries = [self.entry(1, 1), self.entry(2, 1)]
        self.catalog_path.write_text(json.dumps(self.catalog(entries)), encoding="utf-8")
        for index, expected in enumerate(({"Speaker_01"}, {"Speaker_01", "Speaker_03"},
                                          {f"Speaker_{i:02d}" for i in range(1, 18)})):
            with self.subTest(expected=expected):
                output = self.root / f"speaker-mismatch-{index}"
                with self.assertRaises(ValueError):
                    pilot.run_pilot(self.catalog_path, self.sources, output, per_speaker=1,
                                    fetch_bytes=self.fetch, expected_speakers=expected)
                self.assertFalse(output.exists())
                self.assertEqual(list(self.sources.iterdir()), [])
                self.assertEqual(self.calls, [])
        summary = pilot.run_pilot(self.catalog_path, self.sources, self.output, per_speaker=1,
                                  fetch_bytes=self.fetch, expected_speakers={"Speaker_01", "Speaker_02"})
        self.assertTrue(summary["balanced_complete"])

    def test_publication_failure_removes_staged_bytes_and_withholds_balanced_manifest(self):
        entry = self.entry(1, 1)
        with patch.object(pilot.os, "link", side_effect=OSError("fictional publication failure")):
            summary, inventory, balanced, report = self.run_pilot([entry], per_speaker=1)
        self.assertEqual(summary["errors"], 1)
        self.assertFalse(summary["balanced_complete"])
        self.assertIsNone(balanced)
        self.assertFalse((self.sources / entry["path"]).exists())
        self.assertEqual([path for path in self.sources.rglob("*") if path.is_file()], [])
        self.assertEqual(inventory[0]["validation_status"], "failed")
        self.assertIn("fictional publication failure", inventory[0]["error"])
        self.assertEqual(len(report["errors"]), 1)


if __name__ == "__main__":
    unittest.main()
