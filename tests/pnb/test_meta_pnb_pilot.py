"""Tiny actual Parquet/FLAC fixtures; no real archive, speech or network."""

from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import soundfile as sf

from data.collect import meta_pnb_pilot as pnb


REVISION = "8648ba8946377697b427ae952076e49fc0e5e44d"
SHARD = "data/pnb_Arab/train-00000-of-00002.parquet"


class PnbPilotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.archives = self.root / "archives"
        self.archives.mkdir()
        self.raw = self.root / "raw"
        self.output = self.root / "output"
        self.manifest = self.root / "source.json"

    def flac(self, subtype="PCM_16", rate=20000):
        values = np.where(np.arange(rate * 4) % 40 < 20, 2000, -2000).astype(np.int16)
        buffer = io.BytesIO()
        sf.write(buffer, values, rate, format="FLAC", subtype=subtype)
        return buffer.getvalue(), values

    def rows(self, count=2, subtype="PCM_16", rate=20000):
        payload, _ = self.flac(subtype, rate)
        return [{"language": "pnb_Arab", "speaker_id": f"fictional-speaker-{speaker}",
                 "prompt_id": "reused-prompt", "prompt": "Fictional elicitation prompt",
                 "segment_id": "s01", "raw_text": "Fictional unaligned source transcript",
                 "iso_639_3": "pnb", "glottocode": "west2386", "iso_15924": "Arab",
                 "duration": 4., "audio": {"bytes": payload, "path": f"fictional-{speaker}-{index}.flac"}}
                for speaker in (1, 2) for index in range(count)]

    def write_archive(self, rows):
        archive = self.archives / Path(SHARD).name
        pq.write_table(pa.Table.from_pylist(rows), archive, row_group_size=2)
        payload = archive.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        source = {"repo": "facebook/omnilingual-asr-corpus", "revision": REVISION,
                  "language": "pnb_Arab", "licence": "CC-BY-4.0", "files": [{
                      "path": SHARD, "local_name": archive.name, "size": len(payload),
                      "sha256": digest, "publisher_lfs_sha256": digest}]}
        self.manifest.write_text(json.dumps(source), encoding="utf-8")
        return archive, source

    def run_pilot(self, **kwargs):
        result = pnb.run_pilot(self.manifest, self.archives, self.raw, self.output, **kwargs)
        report = json.loads((self.output / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(result, report["summary"])
        inventory = json.loads((self.output / "inventory.json").read_text(encoding="utf-8"))["files"]
        if result["pilot_complete"]:
            pilot = json.loads((self.output / "pilot.json").read_text(encoding="utf-8"))
        else:
            self.assertFalse((self.output / "pilot.json").exists())
            pilot = None
        return result, inventory, pilot, report

    def assert_preflight_rejects(self):
        with self.assertRaises((ValueError, OSError)):
            pnb.run_pilot(self.manifest, self.archives, self.raw, self.output)
        self.assertFalse(self.raw.exists())
        self.assertFalse(self.output.exists())

    def test_real_speaker_groups_not_repeated_segment_or_prompt_ids(self):
        archive, _ = self.write_archive(self.rows())
        before = archive.read_bytes()
        summary, inventory, pilot, _ = self.run_pilot()
        self.assertTrue(summary["pilot_complete"])
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(len(pilot["files"]), 4)
        counts = Counter(row["speaker_id"] for row in pilot["files"])
        self.assertEqual(len(counts), 2)
        self.assertEqual(set(counts.values()), {2})
        self.assertEqual(len({row["source_recording_id"] for row in pilot["files"]}), 4)
        self.assertEqual({row["source_segment_id"] for row in pilot["files"]}, {"s01"})
        self.assertEqual({row["source_prompt_id"] for row in pilot["files"]}, {"reused-prompt"})
        self.assertEqual(archive.read_bytes(), before)
        self.assertEqual(len(inventory), 4)

    def test_selection_is_order_independent_balanced_and_respects_total_cap(self):
        rows = [{"speaker_id": f"speaker-{speaker}", "source_shard": SHARD,
                 "source_row_index": speaker * 10 + index}
                for speaker in range(2) for index in range(3)]
        selected = pnb.select_rows(rows, REVISION, per_speaker=2, max_sources=30)
        reversed_selection = pnb.select_rows(rows[::-1], REVISION, per_speaker=2, max_sources=30)
        identity = lambda row: (row["source_shard"], row["source_row_index"])
        self.assertEqual({identity(row) for row in selected}, {identity(row) for row in reversed_selection})
        self.assertEqual(Counter(row["speaker_id"] for row in selected), {"speaker-0": 2, "speaker-1": 2})
        for per_speaker, cap in ((2, 3), (4, 30), (True, 30), (2, 31), (0, 30)):
            with self.subTest(per_speaker=per_speaker, cap=cap):
                with self.assertRaises(ValueError):
                    pnb.select_rows(rows, REVISION, per_speaker=per_speaker, max_sources=cap)

    def test_original_flac_and_decoded_pcm16_native_samples_are_exact(self):
        rows = self.rows()
        archive, source = self.write_archive(rows)
        original_archive = archive.read_bytes()
        _, _, pilot, _ = self.run_pilot()
        for record in pilot["files"]:
            expected = rows[record["source_row_index"]]["audio"]["bytes"]
            publisher_speaker = rows[record["source_row_index"]]["speaker_id"]
            self.assertEqual(record["source_speaker_id"], publisher_speaker)
            self.assertEqual(record["speaker_id"], "meta_pnb_" + publisher_speaker)
            original = (self.raw / record["original_audio_path"]).read_bytes()
            self.assertEqual(original, expected)
            self.assertEqual(hashlib.sha256(original).hexdigest(), record["original_audio_sha256"])
            self.assertEqual(record["source_shard"], SHARD)
            self.assertEqual(record["source_shard_sha256"], source["files"][0]["sha256"])
            decoded, rate = sf.read(io.BytesIO(original), dtype="int16")
            wav = self.raw / record["path"]
            self.assertEqual(hashlib.sha256(wav.read_bytes()).hexdigest(), record["sha256"])
            with wave.open(str(wav), "rb") as stream:
                self.assertEqual((stream.getframerate(), stream.getnchannels(), stream.getsampwidth()), (rate, 1, 2))
                self.assertEqual(stream.readframes(stream.getnframes()), decoded.astype("<i2").tobytes())
            self.assertEqual(record["publisher_split"], "train")
            self.assertEqual(record["dialect"], "unknown")
            self.assertEqual(record["cloning_status"], "not_authorized")
            self.assertEqual(record["listening_status"], "not_individually_reviewed")
            transcript = json.loads((self.raw / record["source_transcript_path"]).read_text(encoding="utf-8"))
            self.assertEqual(transcript["alignment"], "source_recording_only")
            self.assertEqual(transcript["raw_text"], rows[record["source_row_index"]]["raw_text"])
        self.assertEqual(archive.read_bytes(), original_archive)

    def test_invalid_row_language_missing_identity_and_nonfinite_duration_abort(self):
        for change in ({"language": "ur"}, {"speaker_id": ""}, {"speaker_id": None},
                       {"speaker_id": "s" * 72},
                       {"duration": float("nan")}, {"duration": float("inf")}, {"duration": 0.}):
            with self.subTest(change=change):
                rows = self.rows()
                rows[-1].update(change)
                self.write_archive(rows)
                self.assert_preflight_rejects()

    def test_publisher_archive_hash_size_and_truncation_fail_before_mutation(self):
        for case in ("hash", "size", "truncated"):
            with self.subTest(case=case):
                archive, source = self.write_archive(self.rows())
                if case == "hash":
                    source["files"][0]["sha256"] = "0" * 64
                    source["files"][0]["publisher_lfs_sha256"] = "0" * 64
                elif case == "size":
                    source["files"][0]["size"] += 1
                else:
                    archive.write_bytes(archive.read_bytes()[:-8])
                self.manifest.write_text(json.dumps(source), encoding="utf-8")
                self.assert_preflight_rejects()

    def test_unsafe_manifest_paths_or_nontraining_partition_rejected(self):
        for changes in ({"local_name": "../outside.parquet"}, {"local_name": "/absolute.parquet"},
                        {"path": "data/pnb_Arab/dev-00000-of-00001.parquet"},
                        {"path": "data/pnb_Arab/../train-00000-of-00002.parquet"}):
            with self.subTest(changes=changes):
                _, source = self.write_archive(self.rows())
                source["files"][0].update(changes)
                self.manifest.write_text(json.dumps(source), encoding="utf-8")
                self.assert_preflight_rejects()

    def test_pcm24_flac_fails_without_silent_lossy_conversion(self):
        archive, _ = self.write_archive(self.rows(subtype="PCM_24"))
        before = archive.read_bytes()
        summary, _, pilot, _ = self.run_pilot()
        self.assertFalse(summary["pilot_complete"])
        self.assertGreater(summary["errors"], 0)
        self.assertIsNone(pilot)
        self.assertTrue((self.output / "INCOMPLETE").exists())
        self.assertEqual(archive.read_bytes(), before)

    def test_corrupt_embedded_audio_keeps_diagnostics_and_withholds_pilot(self):
        rows = self.rows()
        rows[-1]["audio"]["bytes"] = b"fictional invalid FLAC"
        self.write_archive(rows)
        summary, _, pilot, report = self.run_pilot()
        self.assertFalse(summary["pilot_complete"])
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["verified_files"], 3)
        self.assertIsNone(pilot)
        self.assertTrue(report["errors"])
        self.assertTrue((self.output / "INCOMPLETE").exists())

    def test_existing_or_overlapping_destinations_preserve_sentinels(self):
        self.write_archive(self.rows())
        existing = self.root / "existing"
        existing.mkdir()
        sentinel = existing / "keep.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        cases = ((existing, self.output), (self.raw, existing), (self.archives / "raw", self.output),
                 (self.raw, self.raw / "output"), (self.root, self.output))
        for raw, output in cases:
            with self.subTest(raw=raw, output=output):
                with self.assertRaises(ValueError):
                    pnb.run_pilot(self.manifest, self.archives, raw, output)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
        self.assertFalse(self.raw.exists())
        self.assertFalse(self.output.exists())

    def test_publication_failure_cannot_leave_usable_complete_pilot(self):
        self.write_archive(self.rows())
        real_rename = Path.rename

        def fail_pilot_publication(path, target):
            if Path(target) == self.output / "pilot.json":
                raise OSError("fictional publication failure")
            return real_rename(path, target)

        with patch.object(Path, "rename", new=fail_pilot_publication):
            with self.assertRaises(OSError):
                pnb.run_pilot(self.manifest, self.archives, self.raw, self.output)
        self.assertTrue((self.output / "INCOMPLETE").exists())
        self.assertTrue((self.raw / "INCOMPLETE").exists())
        self.assertFalse((self.output / "pilot.json").exists())

    def test_archive_modified_during_decode_refuses_publication(self):
        archive, _ = self.write_archive(self.rows())
        real_decode = pnb._decode
        changed = False

        def mutate_archive_after_decode(*args, **kwargs):
            nonlocal changed
            result = real_decode(*args, **kwargs)
            if not changed:
                payload = archive.read_bytes()
                archive.write_bytes(payload + b"fictional concurrent mutation")
                changed = True
            return result

        with patch.object(pnb, "_decode", side_effect=mutate_archive_after_decode):
            with self.assertRaises(ValueError):
                pnb.run_pilot(self.manifest, self.archives, self.raw, self.output)
        self.assertTrue(changed)
        self.assertFalse(self.raw.exists())
        self.assertFalse(self.output.exists())

    def test_conditioning_preserves_archive_audio_row_lineage_without_aligned_transcript(self):
        from data.prepare.prepare_audio import process_manifest

        self.write_archive(self.rows())
        _, _, pilot, _ = self.run_pilot()
        originals = {path: path.read_bytes() for path in self.raw.rglob("*") if path.is_file()}
        destination = self.root / "prepared"
        summary = process_manifest(self.output / "pilot.json", self.raw, destination, condition=True)
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(summary["clips"], 4)
        report = json.loads((destination / "report.json").read_text(encoding="utf-8"))
        raw_rows = {record["path"]: record for record in pilot["files"]}
        fields = ("original_audio_sha256", "original_audio_path", "source_shard", "source_shard_sha256",
                  "source_row_index", "source_language", "publisher_split", "dialect", "source_prompt_id", "source_segment_id")
        for record in report["sources"] + report["clips"]:
            original = raw_rows[record.get("source_path", record.get("path"))]
            for field in fields:
                self.assertEqual(record[field], original[field])
            self.assertEqual(record["source_sha256"], original["sha256"])
            self.assertEqual(record["speaker_id"], original["speaker_id"])
        for clip in report["clips"]:
            for unaligned in ("prompt", "raw_text", "transcript", "source_transcript"):
                self.assertNotIn(unaligned, clip)
        for path, before in originals.items():
            self.assertEqual(path.read_bytes(), before)

    def test_preparation_refuses_incomplete_raw_root_before_output(self):
        from data.prepare.prepare_audio import process_manifest

        self.write_archive(self.rows())
        self.run_pilot()
        marker = self.raw / "INCOMPLETE"
        marker.write_text("fictional interrupted raw publication", encoding="utf-8")
        destination = self.root / "refused-preparation"
        with self.assertRaises(ValueError):
            process_manifest(self.output / "pilot.json", self.raw, destination, condition=True)
        self.assertFalse(destination.exists())
        self.assertEqual(marker.read_text(encoding="utf-8"), "fictional interrupted raw publication")


if __name__ == "__main__":
    unittest.main()
