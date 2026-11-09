"""Claude-authored split-proposal cases (9 November 2026), backlog 1.3.

Authored by Claude (Claude Code, Opus 5.5), not by GPT-6 Astra: since 8 November
Claude designs test cases. These extend the Claude-authored baseline in
test_split_proposals.py along the risk list of docs/qa/split-proposal-astra-handoff.md
(section 5) and spec decisions 1-8; the case catalogue is
docs/qa/split-proposal-test-cases.md. Fictional rows only; stdlib only.
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

MANIFEST_DIR = Path(__file__).resolve().parents[2] / "data" / "manifest"
sys.path.insert(0, str(MANIFEST_DIR))
import propose_splits  # noqa: E402
import validate_splits  # noqa: E402

SEED = "fx-seed-cases"


def sha(text):
    return hashlib.sha256(f"fx-case:{text}".encode("utf-8")).hexdigest()


def row(source="fx_source_alpha", speaker="fx_spk_01", recording=None, clip=0, **extra):
    recording = recording or f"{speaker}_rec_a"
    value = {"source_id": source, "speaker_id": speaker, "source_recording_id": recording,
             "source_sha256": sha(f"{source}/{recording}"), "sha256": sha(f"{source}/{recording}/{clip}"),
             "path": f"clips/{source}/{recording}_{clip}.wav", "label": "genuine", "language": "fx",
             "duration_seconds": 4.0}
    value.update(extra)
    return value


def population(count, source="fx_source_alpha", **extra):
    return [row(source, f"fx_spk_{i:02d}", f"fx_spk_{i:02d}_rec_{r}", c, **extra)
            for i in range(1, count + 1) for r in "ab" for c in range(2)]


def placements(rows):
    table = {}
    for item in rows:
        table.setdefault((item["source_id"], item["speaker_id"]), set()).add(item["proposed_split"])
    return table


class AtomicityAndDeterminismCases(unittest.TestCase):
    def test_c01_speaker_atomic_across_orders_and_interleaved_sources(self):
        rows = population(9) + population(5, "fx_source_beta")
        orders = [rows, rows[::-1], sorted(rows, key=lambda r: r["sha256"])]
        for seed in range(3):
            shuffled = list(rows)
            random.Random(seed).shuffle(shuffled)
            orders.append(shuffled)
        outputs = [propose_splits.propose(order, SEED)[1] for order in orders]
        for output in outputs:
            self.assertTrue(all(len(s) == 1 for s in placements(output).values()))
            self.assertEqual(output, outputs[0])

    def test_c02_byte_identical_proposal_under_two_interpreter_hash_seeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "inputs" / "report.json"
            report.parent.mkdir()
            report.write_text(json.dumps({"clips": population(7)}), encoding="utf-8")
            documents = []
            for hash_seed in ("0", "31337"):
                output = root / f"out-{hash_seed}"
                process = subprocess.run(
                    [sys.executable, str(MANIFEST_DIR / "propose_splits.py"), "--report", str(report),
                     "--seed", SEED, "--output-root", str(output)],
                    capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": hash_seed}, timeout=60)
                self.assertEqual(process.returncode, 0, process.stderr)
                documents.append((output / "proposal.json").read_bytes())
            self.assertEqual(documents[0], documents[1])

    def test_c03_seed_change_moves_hashed_speakers_only(self):
        rows = population(17) + population(2, "fx_source_pub", publisher_split="test")
        first, second = (propose_splits.propose(rows, seed)[1] for seed in ("fx-a", "fx-b"))
        held = lambda out: {k: v for k, v in placements(out).items() if k[0] == "fx_source_pub"}
        self.assertEqual(held(first), held(second))
        self.assertNotEqual(placements(first), placements(second))

    def test_c04_allocation_four_and_five_speakers_resolve_ties_train_first(self):
        for count, expected in ((4, (2, 1, 1)), (5, (3, 1, 1))):
            with self.subTest(count=count):
                table = placements(propose_splits.propose(population(count), SEED)[1])
                realised = tuple(sum(s == {name} for s in table.values()) for name in ("train", "dev", "test"))
                self.assertEqual(realised, expected)

    def test_c05_allocation_follows_ascending_speaker_hash_order(self):
        table = placements(propose_splits.propose(population(6), SEED)[1])
        ordered = sorted(table, key=lambda key: propose_splits.speaker_hash(SEED, *key))
        self.assertEqual([next(iter(table[key])) for key in ordered],
                         ["train"] * 4 + ["dev"] + ["test"])


class BenchmarkDecisionSevenCases(unittest.TestCase):
    def benchmark(self, speaker, **extra):
        return [row(speaker=speaker, recording=f"{speaker}_rec_fake", clip=c, label="synthetic",
                    benchmark_only=True, training_eligible=False, **extra) for c in range(2)]

    def test_c06_benchmark_twin_of_every_genuine_speaker_never_empties_training(self):
        genuine = population(6, training_eligible=True)
        fakes = [item for i in range(1, 7) for item in self.benchmark(f"fx_spk_{i:02d}")]
        _, output, _ = propose_splits.propose(genuine + fakes, SEED)
        result = validate_splits.check_rows(output)
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["trainable_rows_by_partition"].get("train"), 16)
        self.assertEqual(sum(result["benchmark_rows_by_partition"].values()), 12)
        for key, splits in placements(output).items():
            self.assertEqual(len(splits), 1, key)

    def test_c07_benchmark_row_in_publisher_held_out_group_stays_held_out(self):
        rows = population(1, "fx_source_pub", publisher_split="dev")
        rows += [row("fx_source_pub", "fx_spk_01", "fx_spk_01_rec_fake", 0, label="synthetic",
                     benchmark_only=True, training_eligible=False)]
        _, output, _ = propose_splits.propose(rows, SEED)
        fake = next(r for r in output if r["source_recording_id"] == "fx_spk_01_rec_fake")
        self.assertEqual((fake["proposed_split"], fake["assignment_rule"]),
                         ("held_out_publisher_dev", "benchmark_follows_speaker"))
        self.assertTrue(validate_splits.check_rows(output)["ok"])

    def test_c08_validator_rejects_benchmark_flag_or_rule_tampering(self):
        rows = population(4, training_eligible=True) + self.benchmark("fx_spk_01")
        _, output, _ = propose_splits.propose(rows, SEED)
        target = next(i for i, r in enumerate(output) if r.get("benchmark_only") is True)
        cases = (("training_eligible", True), ("assignment_rule", "hashed_speaker"),
                 ("proposed_split", "benchmark_only"))
        for field, value in cases:
            with self.subTest(field=field):
                tampered = [dict(r) for r in output]
                tampered[target][field] = value
                self.assertFalse(validate_splits.check_rows(tampered)["ok"])

    def test_c08b_benchmark_row_moved_alone_or_flag_dropped_is_rejected(self):
        rows = population(4, training_eligible=True) + self.benchmark("fx_spk_01")
        _, output, _ = propose_splits.propose(rows, SEED)
        target = next(i for i, r in enumerate(output) if r.get("benchmark_only") is True)
        other = next(s for s in ("train", "dev", "test") if s != output[target]["proposed_split"])
        moved = [dict(r) for r in output]
        moved[target]["proposed_split"] = other
        result = validate_splits.check_rows(moved)
        self.assertFalse(result["ok"])
        self.assertEqual(result["overlaps"]["speaker"]["crossing_partitions"], 1)
        for change in ({"benchmark_only": False}, "drop"):
            with self.subTest(change=change):
                tampered = [dict(r) for r in output]
                if change == "drop":
                    del tampered[target]["benchmark_only"]
                else:
                    tampered[target].update(change)
                self.assertFalse(validate_splits.check_rows(tampered)["ok"])

    def test_c08c_coordinated_relabel_of_benchmark_row_breaks_content_digest(self):
        rows = population(4, training_eligible=True) + self.benchmark("fx_spk_01")
        proposal_id, output, _ = propose_splits.propose(rows, SEED)
        document = {"schema_version": 2, "status": "proposal", "proposal_id": proposal_id,
                    "proposal_seed": SEED, "rows": [dict(r) for r in output]}
        self.assertTrue(validate_splits.check_proposal(document)["ok"])
        target = next(r for r in document["rows"] if r.get("benchmark_only") is True)
        del target["benchmark_only"]
        target.update(training_eligible=True, assignment_rule="hashed_speaker")
        self.assertTrue(validate_splits.check_rows(document["rows"])["ok"], "row checks alone cannot see it")
        result = validate_splits.check_proposal(document)
        self.assertFalse(result["ok"])
        self.assertTrue(any("digest mismatch" in error for error in result["errors"]))
        self.assertFalse(validate_splits.check_proposal({**document, "schema_version": 1})["ok"])

    def test_c09_trainable_counts_need_explicit_true(self):
        rows = population(3) + population(3, "fx_source_beta", training_eligible=True)
        _, output, _ = propose_splits.propose(rows, SEED)
        result = validate_splits.check_rows(output)
        self.assertEqual(sum(result["trainable_rows_by_partition"].values()), 12)


class LineageAndInputCases(unittest.TestCase):
    def test_c10_malformed_lineage_matrix_fails_before_output(self):
        bad_values = {"speaker_id": (None, "", 7), "source_recording_id": (None, " "),
                      "source_id": ("",), "source_sha256": ("abc", None), "sha256": ("z" * 64,),
                      "label": (None,), "duration_seconds": (0, -1.0, float("inf"), True, "4"),
                      "benchmark_only": ("true", 1), "training_eligible": ("false",),
                      "publisher_split": (3,)}
        for field, values in bad_values.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    rows = population(3)
                    rows[0][field] = value
                    with self.assertRaises(ValueError):
                        propose_splits.propose(rows, SEED)

    def test_c11_uppercase_hash_links_with_lowercase_twin(self):
        rows = population(5)
        twin = row(speaker="fx_spk_04", recording="fx_spk_04_rec_dup", clip=9,
                   source_sha256=rows[0]["source_sha256"].upper())
        _, output, detail = propose_splits.propose(rows + [twin], SEED)
        table = placements(output)
        self.assertEqual(table[("fx_source_alpha", "fx_spk_01")], table[("fx_source_alpha", "fx_spk_04")])
        self.assertEqual(len(detail["hash_linked_speaker_groups"]), 1)

    def test_c12_cross_source_duplicate_original_links_speakers_across_sources(self):
        rows = population(3) + population(3, "fx_source_beta")
        rows[0]["original_audio_sha256"] = sha("shared-flac")
        rows[-1]["original_audio_sha256"] = sha("shared-flac")
        _, output, detail = propose_splits.propose(rows, SEED)
        result = validate_splits.check_rows(output)
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["overlaps"]["original_audio_sha256"]["crossing_partitions"], 0)
        linked = [g for g in detail["hash_linked_speaker_groups"] if g["speakers"] == 2]
        self.assertEqual(len(linked), 1)


class ValidatorExitCases(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        report = self.root / "inputs" / "report.json"
        report.parent.mkdir()
        report.write_text(json.dumps({"clips": population(4)}), encoding="utf-8")
        self.output = self.root / "proposal"
        propose_splits.run([report], SEED, self.output)

    def validate(self, target=None):
        return subprocess.run([sys.executable, str(MANIFEST_DIR / "validate_splits.py"),
                               str(target or self.output)], capture_output=True, text=True, timeout=60)

    def test_c13_exit_matrix_and_no_speaker_identities_in_findings(self):
        self.assertEqual(self.validate().returncode, 0)
        path = self.output / "proposal.json"
        clean = json.loads(path.read_text(encoding="utf-8"))
        mutations = {"document_status": lambda d: d.update(status="accepted"),
                     "document_id": lambda d: d.update(proposal_id="split-proposal-other"),
                     "row_split": lambda d: d["rows"][0].update(proposed_split="holdout"),
                     "mixed_seed": lambda d: d["rows"][0].update(proposal_seed="fx-other")}
        for name, change in mutations.items():
            with self.subTest(name=name):
                document = json.loads(json.dumps(clean))
                change(document)
                path.write_text(json.dumps(document), encoding="utf-8")
                process = self.validate()
                self.assertEqual(process.returncode, 1)
                self.assertNotIn("fx_spk_", process.stdout)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.validate().returncode, 1)
        usage = subprocess.run([sys.executable, str(MANIFEST_DIR / "validate_splits.py")],
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(usage.returncode, 2, "usage errors are distinguishable from validation failures")

    def test_c14_report_states_scope_and_schema_version_two(self):
        report = json.loads((self.output / "proposal_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["schema_version"], 2)
        self.assertIn("benchmark_rows_by_partition", report)
        self.assertIn("trainable_rows_by_partition", report)
        text = " ".join(report["limitations"])
        self.assertIn("follow their speaker", text)
        self.assertNotIn("benchmark_only", report["summary"]["partitions"])


if __name__ == "__main__":
    unittest.main()
