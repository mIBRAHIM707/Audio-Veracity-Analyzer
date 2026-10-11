"""Claude-authored baseline tests for split proposals (claude-fable-5-1, Claude Code).

Fictional fixtures only: invented source, speaker and recording labels with hashes
of fictional literals. No real manifests, audio, external paths or `.local` values.
These are infrastructure tests of placement rules, not dataset or product acceptance.
Not authored by GPT-6 Astra; an Astra extension module is a separate deliverable.
"""

import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

MANIFEST_DIR = Path(__file__).resolve().parents[2] / "data" / "manifest"
sys.path.insert(0, str(MANIFEST_DIR))
import propose_splits  # noqa: E402
import validate_splits  # noqa: E402

SEED = "fx-seed-alpha"


def sha(text):
    return hashlib.sha256(f"fx:{text}".encode("utf-8")).hexdigest()


def fx_row(source="fx_source_alpha", speaker="fx_spk_01", recording=None, clip=0, **extra):
    recording = recording or f"{speaker}_rec_a"
    row = {"source_id": source, "source_version": "fx_v1", "speaker_id": speaker,
           "source_recording_id": recording, "source_sha256": sha(f"{source}/{recording}"),
           "sha256": sha(f"{source}/{recording}/{clip}"), "path": f"clips/{recording}_{clip}.wav",
           "label": "genuine", "language": "fx", "duration_seconds": 4.0, "split": "unassigned"}
    row.update(extra)
    return row


def speakers(count, source="fx_source_alpha", **extra):
    return [fx_row(source, f"fx_spk_{index:02d}", f"fx_spk_{index:02d}_rec_{letter}", clip, **extra)
            for index in range(1, count + 1) for letter in "ab" for clip in range(2)]


def partition_of(rows):
    """Map (source_id, speaker_id) to the set of partitions its rows received."""
    table = {}
    for row in rows:
        table.setdefault((row["source_id"], row["speaker_id"]), set()).add(row["proposed_split"])
    return table


class AssignmentRuleTests(unittest.TestCase):
    def test_hash_vector_and_delimiter(self):
        expected = hashlib.sha256(b"s\x1fsrc\x1fspk").hexdigest()
        self.assertEqual(propose_splits.speaker_hash("s", "src", "spk"), expected)
        self.assertNotEqual(propose_splits.speaker_hash("1", "2x", "s"),
                            propose_splits.speaker_hash("12", "x", "s"))

    def test_allocation_counts(self):
        expected = {0: (0, 0, 0), 1: (0, 0, 1), 2: (1, 0, 1), 3: (1, 1, 1), 4: (2, 1, 1),
                    5: (3, 1, 1), 6: (4, 1, 1), 17: (12, 2, 3), 100: (70, 10, 20)}
        for count, (train, dev, test) in expected.items():
            counts = propose_splits.allocate(count)
            self.assertEqual(counts, {"train": train, "dev": dev, "test": test}, count)
            self.assertEqual(sum(counts.values()), count)
        with self.assertRaises(ValueError):
            propose_splits.allocate(-1)

    def test_order_independence_and_seed_determinism(self):
        rows = speakers(17) + speakers(2, "fx_source_beta", benchmark_only=True, label="synthetic")
        rows += speakers(3, "fx_source_gamma", publisher_split="dev")
        first = propose_splits.propose(rows, SEED)
        shuffled = list(rows)
        random.Random(7).shuffle(shuffled)
        self.assertEqual(first, propose_splits.propose(shuffled, SEED))
        self.assertEqual(first, propose_splits.propose(list(reversed(rows)), SEED))
        other = propose_splits.propose(rows, "fx-seed-beta")
        self.assertNotEqual(first[0], other[0])
        hashed = lambda result: {r["group_key"]: r["proposed_split"] for r in result[1]
                                 if r["assignment_rule"] == "hashed_speaker"}
        self.assertNotEqual(hashed(first), hashed(other), "a new seed should move some speaker")
        fixed = lambda result: {r["group_key"]: r["proposed_split"] for r in result[1]
                                if r["assignment_rule"] != "hashed_speaker"}
        self.assertEqual(fixed(first), fixed(other))

    def test_seventeen_speakers_allocation_and_atomicity(self):
        rows = speakers(17)
        _, output, detail = propose_splits.propose(rows, SEED)
        table = partition_of(output)
        self.assertEqual(len(table), 17)
        self.assertTrue(all(len(splits) == 1 for splits in table.values()))
        counts = {name: sum(splits == {name} for splits in table.values())
                  for name in ("train", "dev", "test")}
        self.assertEqual(counts, {"train": 12, "dev": 2, "test": 3})
        recordings = {}
        for row in output:
            recordings.setdefault(row["source_recording_id"], set()).add(row["proposed_split"])
        self.assertTrue(all(len(splits) == 1 for splits in recordings.values()))
        self.assertEqual(detail["allocation"]["fx_source_alpha"]["allocation_degenerate"], False)
        self.assertTrue(all(row["status"] == "proposal" for row in output))

    def test_six_and_three_speakers(self):
        for count, expected in ((6, (4, 1, 1)), (3, (1, 1, 1))):
            table = partition_of(propose_splits.propose(speakers(count), SEED)[1])
            counts = tuple(sum(s == {name} for s in table.values()) for name in ("train", "dev", "test"))
            self.assertEqual(counts, expected)

    def test_one_and_two_speaker_sources_are_degenerate(self):
        _, output, detail = propose_splits.propose(speakers(1), SEED)
        self.assertEqual(set(partition_of(output)[("fx_source_alpha", "fx_spk_01")]), {"test"})
        self.assertTrue(detail["allocation"]["fx_source_alpha"]["allocation_degenerate"])
        _, output, detail = propose_splits.propose(speakers(2), SEED)
        table = partition_of(output)
        hashes = {key: propose_splits.speaker_hash(SEED, *key) for key in table}
        first, second = sorted(table, key=hashes.get)
        self.assertEqual((table[first], table[second]), ({"train"}, {"test"}))
        self.assertTrue(detail["allocation"]["fx_source_alpha"]["allocation_degenerate"])

    def test_same_label_in_two_sources_stays_separate(self):
        rows = speakers(4) + speakers(4, "fx_source_beta")
        _, output, _ = propose_splits.propose(rows, SEED)
        keys = {row["group_key"] for row in output if row["speaker_id"] == "fx_spk_01"}
        self.assertEqual(len(keys), 2)

    def test_hash_linked_speakers_share_a_partition(self):
        rows = speakers(6)
        twin = fx_row(speaker="fx_spk_02", recording="fx_spk_02_rec_dup", clip=0,
                      source_sha256=rows[0]["source_sha256"])
        _, output, detail = propose_splits.propose(rows + [twin], SEED)
        table = partition_of(output)
        self.assertEqual(table[("fx_source_alpha", "fx_spk_01")], table[("fx_source_alpha", "fx_spk_02")])
        self.assertEqual(len(detail["hash_linked_speaker_groups"]), 1)
        self.assertEqual(detail["hash_linked_speaker_groups"][0]["speakers"], 2)
        result = validate_splits.check_rows(output)
        self.assertTrue(result["ok"])
        self.assertEqual(result["within_partition_duplicates"]["source_sha256"], 1)
        self.assertEqual(result["within_partition_duplicates"]["sha256"], 0)

    def test_benchmark_rules(self):
        rows = speakers(5, publisher_split="train")
        benchmark = speakers(2, "fx_source_bench", benchmark_only=True, label="synthetic",
                             publisher_split="train")
        scoped = speakers(1, "fx_source_scope", training_eligible=False,
                          use_scope="noncommercial_benchmark_only")
        pending = speakers(3, "fx_source_pend", training_eligible=False,
                           use_scope="noncommercial_detection_research")
        coupled = [fx_row(speaker="fx_spk_01", recording="fx_spk_01_rec_fake", label="synthetic",
                          benchmark_only=True, training_eligible=False)]
        _, output, detail = propose_splits.propose(rows + benchmark + scoped + pending + coupled, SEED)
        by_source = {}
        for row in output:
            by_source.setdefault(row["source_id"], set()).add(row["proposed_split"])
        self.assertEqual(by_source["fx_source_bench"], {"benchmark_only"})
        self.assertEqual(by_source["fx_source_scope"], {"benchmark_only"})
        self.assertEqual(by_source["fx_source_pend"], {"train", "dev", "test"})
        table = partition_of(output)
        self.assertEqual(table[("fx_source_alpha", "fx_spk_01")], {"benchmark_only"})
        self.assertEqual(detail["benchmark_coupled_rows"], 4)
        rules = {row["assignment_rule"] for row in output if row["proposed_split"] == "benchmark_only"}
        self.assertEqual(rules, {"benchmark_only"})

    def test_publisher_rules_and_conflicts(self):
        dev = speakers(2, publisher_split="dev")
        test = speakers(2, "fx_source_beta", original_split="test")
        mixed = [fx_row("fx_source_gamma", "fx_spk_01", "fx_spk_01_rec_a", 0, publisher_split="train"),
                 fx_row("fx_source_gamma", "fx_spk_01", "fx_spk_01_rec_b", 0, publisher_split="dev"),
                 fx_row("fx_source_gamma", "fx_spk_02", "fx_spk_02_rec_a", 0, publisher_split="dev"),
                 fx_row("fx_source_gamma", "fx_spk_02", "fx_spk_02_rec_b", 0, publisher_split="test")]
        _, output, detail = propose_splits.propose(dev + test + mixed, SEED)
        table = partition_of(output)
        self.assertEqual(table[("fx_source_alpha", "fx_spk_01")], {"held_out_publisher_dev"})
        self.assertEqual(table[("fx_source_beta", "fx_spk_01")], {"held_out_publisher_test"})
        self.assertEqual(table[("fx_source_gamma", "fx_spk_01")], {"held_out_publisher_dev"})
        self.assertEqual(table[("fx_source_gamma", "fx_spk_02")], {"held_out_publisher_test"})
        self.assertEqual(len(detail["publisher_split_conflicts"]), 2)
        self.assertEqual(validate_splits.check_rows(output)["ok"], True)

    def test_recording_with_two_speakers_fails_before_output(self):
        rows = speakers(3) + [fx_row(speaker="fx_spk_09", recording="fx_spk_01_rec_a", clip=5)]
        with self.assertRaises(ValueError):
            propose_splits.propose(rows, SEED)
        _, good, _ = propose_splits.propose(speakers(3), SEED)
        bad = good + [{**good[0], "speaker_id": "fx_spk_09", "path": "clips/other.wav",
                       "sha256": sha("other")}]
        self.assertFalse(validate_splits.check_rows(bad)["ok"])

    def test_seed_and_row_bounds(self):
        with self.assertRaises(ValueError):
            propose_splits.propose(speakers(1), "")
        with self.assertRaises(ValueError):
            propose_splits.propose(speakers(1), "x" * 201)
        with patch.object(propose_splits, "MAX_ROWS", 3):
            with self.assertRaises(ValueError):
                propose_splits.propose(speakers(1), SEED)


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        rows = speakers(6) + speakers(1, "fx_source_bench", benchmark_only=True, label="synthetic")
        rows += speakers(1, "fx_source_pub", publisher_split="dev")
        _, self.rows, _ = propose_splits.propose(rows, SEED)
        self.assertTrue(validate_splits.check_rows(self.rows)["ok"])

    def mutate(self, predicate, **changes):
        rows = [dict(row) for row in self.rows]
        for row in rows:
            if predicate(row):
                row.update(changes)
                break
        return validate_splits.check_rows(rows)

    def test_rejections(self):
        hashed = lambda row: row["assignment_rule"] == "hashed_speaker"
        other = lambda row: "dev" if row["proposed_split"] != "dev" else "test"
        moved = self.mutate(hashed, proposed_split=other(next(filter(hashed, self.rows))))
        self.assertFalse(moved["ok"])
        for kind in ("speaker", "recording", "source_sha256"):
            self.assertEqual(moved["overlaps"][kind]["crossing_partitions"], 1, kind)
        bench = lambda row: row["source_id"] == "fx_source_bench"
        self.assertFalse(self.mutate(bench, proposed_split="train", assignment_rule="hashed_speaker")["ok"])
        pub = lambda row: row["source_id"] == "fx_source_pub"
        self.assertFalse(self.mutate(pub, proposed_split="train", assignment_rule="hashed_speaker")["ok"])
        self.assertFalse(self.mutate(hashed, status="accepted")["ok"])
        self.assertFalse(self.mutate(hashed, source_recording_id="")["ok"])
        self.assertFalse(self.mutate(hashed, assignment_rule="benchmark_only")["ok"])
        self.assertFalse(self.mutate(hashed, proposed_split="holdout")["ok"])
        self.assertFalse(self.mutate(hashed, duration_seconds=float("nan"))["ok"])
        self.assertFalse(self.mutate(hashed, benchmark_only="true")["ok"])
        self.assertFalse(self.mutate(hashed, sha256="g" * 64)["ok"])
        self.assertFalse(validate_splits.check_rows([])["ok"])
        self.assertFalse(validate_splits.check_rows([1])["ok"])

    def test_within_partition_duplicates_warn_only(self):
        rows = [dict(row) for row in self.rows]
        first = next(row for row in rows if row["assignment_rule"] == "hashed_speaker")
        twin = next(row for row in rows if row["speaker_id"] == first["speaker_id"] and row is not first)
        twin["sha256"] = first["sha256"]
        result = validate_splits.check_rows(rows)
        self.assertTrue(result["ok"])
        self.assertEqual(result["within_partition_duplicates"]["sha256"], 1)
        self.assertEqual(len(result["warnings"]), 1)

    def test_cross_source_hash_overlap_rejected(self):
        rows = [dict(row) for row in self.rows]
        bench = next(row for row in rows if row["source_id"] == "fx_source_bench")
        hashed = next(row for row in rows if row["assignment_rule"] == "hashed_speaker")
        bench["source_sha256"] = hashed["source_sha256"]
        result = validate_splits.check_rows(rows)
        self.assertFalse(result["ok"])
        self.assertEqual(result["overlaps"]["source_sha256"]["crossing_partitions"], 1)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.output = self.root / "proposal-v1"

    def report(self, name, rows, **extra):
        path = self.inputs / name / "report.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"schema_version": 1, "source_revision": "fx_rev",
                                    "source_manifest_sha256": sha(name), "clips": rows, **extra}),
                        encoding="utf-8")
        return path

    def test_run_publishes_valid_proposal_and_report(self):
        genuine = self.report("genuine", speakers(17))
        bench = self.report("bench", speakers(2, "fx_source_bench", benchmark_only=True,
                                                label="synthetic", language="fy"))
        pilot_rows = speakers(6, "fx_source_pilot", publisher_split="train",
                              training_eligible=False, language="fz")
        for row in pilot_rows:
            row["original_audio_sha256"] = sha("flac/" + row["source_recording_id"])
        pilot = self.report("pilot", pilot_rows)
        sentinel = self.inputs / "genuine" / "clips" / "fx.wav"
        sentinel.parent.mkdir()
        sentinel.write_bytes(b"not audio")
        before = (sentinel.read_bytes(), sentinel.stat().st_mtime_ns)
        summary = propose_splits.run([genuine, bench, pilot], SEED, self.output)
        self.assertEqual(summary["rows"], 100)
        self.assertEqual(summary["partitions"]["benchmark_only"], 8)
        self.assertEqual(summary["training_ineligible_placements"], 24)
        self.assertEqual((sentinel.read_bytes(), sentinel.stat().st_mtime_ns), before)
        self.assertFalse((self.output / "INCOMPLETE").exists())
        proposal = json.loads((self.output / "proposal.json").read_text(encoding="utf-8"))
        report = json.loads((self.output / "proposal_report.json").read_text(encoding="utf-8"))
        self.assertEqual(proposal["status"], "proposal")
        self.assertNotIn(str(self.root), json.dumps(proposal) + json.dumps(report))
        self.assertTrue(all(v["crossing_partitions"] == 0 for v in report["disjointness"].values()))
        self.assertEqual(report["disjointness"]["original_audio_sha256"]["keys"], 12)
        text = " ".join(report["limitations"]).lower()
        for phrase in ("cross-source identity", "consent", "reference", "not training approval"):
            self.assertIn(phrase, text)
        totals = report["shares"]["overall"]
        for scope in ("by_source", "by_language", "by_label", "by_assignment_rule"):
            clips = sum(v["clips"] for table in report["shares"][scope].values() for v in table.values())
            self.assertEqual(clips, 100, scope)
            for table in report["shares"][scope].values():
                self.assertAlmostEqual(sum(v["clip_share"] for v in table.values()), 1.0, places=2)
        self.assertEqual(sum(v["clips"] for v in totals.values()), 100)
        result = validate_splits.check_proposal(validate_splits.load_proposal(self.output))
        self.assertTrue(result["ok"], result["errors"])
        with self.assertRaises(ValueError):
            propose_splits.run([genuine], SEED, self.output)

    def test_no_overwrite_marker_and_boundaries(self):
        genuine = self.report("genuine", speakers(3))
        self.output.mkdir()
        keep = self.output / "keep.txt"
        keep.write_text("sentinel", encoding="utf-8")
        with self.assertRaises(ValueError):
            propose_splits.run([genuine], SEED, self.output)
        self.assertEqual(keep.read_text(encoding="utf-8"), "sentinel")
        with self.assertRaises(ValueError):
            propose_splits.run([genuine], SEED, self.inputs / "genuine" / "nested")
        with self.assertRaises(ValueError):
            propose_splits.run([genuine], SEED, MANIFEST_DIR / "fx-never-created")
        self.assertFalse((MANIFEST_DIR / "fx-never-created").exists())
        failing = self.root / "failing"
        calls = []

        def broken(path, payload):
            calls.append(path.name)
            if len(calls) == 2:
                raise OSError("fx disk full")
            propose_splits.write_json.__wrapped__(path, payload)
        broken.__wrapped__ = propose_splits.write_json
        with patch.object(propose_splits, "write_json", broken):
            with self.assertRaises(OSError):
                propose_splits.run([genuine], SEED, failing)
        self.assertTrue((failing / "INCOMPLETE").exists())
        self.assertFalse((failing / "proposal_report.json").exists())
        with self.assertRaises(ValueError):
            validate_splits.load_proposal(failing)
        with self.assertRaises(ValueError):
            propose_splits.run([genuine], SEED, failing)

    def test_input_bounds_fail_before_output(self):
        genuine = self.report("genuine", speakers(3))
        with self.assertRaises(ValueError):
            propose_splits.run([genuine] * 21, SEED, self.output)
        with patch.object(propose_splits, "MAX_ROWS", 10):
            with self.assertRaises(ValueError):
                propose_splits.run([genuine], SEED, self.output)
        big = self.report("big", speakers(1), padding="x" * propose_splits.MAX_REPORT_BYTES)
        with self.assertRaises(ValueError):
            propose_splits.run([big], SEED, self.output)
        empty = self.report("empty", [])
        with self.assertRaises(ValueError):
            propose_splits.run([empty], SEED, self.output)
        scalar = self.report("scalar", [1, 2])
        with self.assertRaises(ValueError):
            propose_splits.run([scalar], SEED, self.output)
        incomplete = self.report("incomplete", speakers(1))
        (incomplete.parent / "INCOMPLETE").write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            propose_splits.run([incomplete], SEED, self.output)
        with self.assertRaises(ValueError):
            propose_splits.run([genuine, genuine], SEED, self.output)  # duplicate clip rows
        self.assertFalse(self.output.exists())

    def test_cli_exit_codes(self):
        genuine = self.report("genuine", speakers(4))
        command = [sys.executable, str(MANIFEST_DIR / "propose_splits.py"), "--report", str(genuine),
                   "--seed", SEED, "--output-root", str(self.output)]
        env = {**os.environ, "PYTHONHASHSEED": "31337"}
        self.assertEqual(subprocess.run(command, capture_output=True, env=env).returncode, 0)
        self.assertEqual(subprocess.run(command, capture_output=True, env=env).returncode, 1)
        validator = [sys.executable, str(MANIFEST_DIR / "validate_splits.py"), str(self.output)]
        proposal_path = self.output / "proposal.json"
        before = proposal_path.read_bytes()
        env["PYTHONHASHSEED"] = "0"
        self.assertEqual(subprocess.run(validator, capture_output=True, env=env).returncode, 0)
        self.assertEqual(proposal_path.read_bytes(), before)
        in_process = propose_splits.propose(json.loads(genuine.read_text(encoding="utf-8"))["clips"], SEED)
        self.assertEqual(json.loads(before)["rows"], in_process[1])
        document = json.loads(before)
        document["rows"][0]["status"] = "accepted"
        proposal_path.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(subprocess.run(validator, capture_output=True, env=env).returncode, 1)
        self.assertEqual(subprocess.run(validator[:-1] + [str(self.root / "missing")],
                                        capture_output=True, env=env).returncode, 1)


if __name__ == "__main__":
    unittest.main()
