"""Propose reproducible speaker- and lineage-disjoint splits over prepared pilot reports.

Every output row keeps ``status: proposal``: the result is a reviewable placement,
never an accepted train, dev or test set and never training or consent approval.
No audio is read, moved or modified. Python 3.11+ standard library only.
"""

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import stat
import sys

try:
    from .validate_splits import (HASH_FIELDS, check_rows, is_benchmark, lineage_errors,
                                  publisher_partitions)
except ImportError:  # direct script execution or spec-based test import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from validate_splits import (HASH_FIELDS, check_rows, is_benchmark, lineage_errors,
                                 publisher_partitions)

SCHEMA_VERSION = 1
RATIOS = (("train", 70), ("dev", 10), ("test", 20))  # integer percents; exact arithmetic
MAX_REPORTS = 20
MAX_REPORT_BYTES = 8_000_000
MAX_ROWS = 50_000
MAX_SEED_CHARS = 200
LIMITATIONS = (
    "Proposal only: rows with status 'proposal' are not an accepted train, dev or test "
    "set; no acceptance workflow exists.",
    "Cross-source identity is unknown: speaker IDs are namespaced by source_id and are "
    "never merged across sources, so one person may appear under two sources.",
    "No consent or reference mapping exists: placements assert neither participant "
    "consent nor a synthetic clip's target-reference speaker.",
    "training_eligible false rows outside benchmark scope receive placements only; a "
    "placement is not training approval.",
    "Exact SHA-256 equality is the only duplicate signal; near-duplicates, re-encodes "
    "and reused voices are not detected.",
    "A speaker group containing any benchmark-only row is placed in benchmark_only in "
    "full, so its non-benchmark rows are unavailable to train, dev or test.",
    "Small per-source speaker counts make 70/10/20 approximate; realised clip and "
    "second shares are reported, not targets.",
    "Codec copies and same-speaker synthetic lineage from consented participants do not "
    "exist yet and are out of scope.",
)


def speaker_hash(seed, source_id, speaker_id):
    """Decision 3: SHA-256 of seed, source_id and speaker_id joined by U+001F."""
    return hashlib.sha256("\x1f".join([seed, source_id, speaker_id]).encode("utf-8")).hexdigest()


def allocate(count):
    """Decision 2: largest remainder; one group goes to test, two to train and test."""
    if count < 0:
        raise ValueError("Group count cannot be negative")
    if count == 1:
        return {"train": 0, "dev": 0, "test": 1}
    if count == 2:
        return {"train": 1, "dev": 0, "test": 1}
    counts = {name: count * share // 100 for name, share in RATIOS}
    by_remainder = sorted(RATIOS, key=lambda item: -(count * item[1] % 100))  # stable: ties keep train, dev, test
    for name, _ in by_remainder[:count - sum(counts.values())]:
        counts[name] += 1
    for name in counts:
        if count >= 3 and counts[name] == 0:
            donor = max(counts, key=counts.get)  # first maximum in train, dev, test order
            counts[donor] -= 1
            counts[name] += 1
    return counts


def build_groups(rows):
    """Union (source_id, speaker_id) keys linked by exact hashes; result is order independent."""
    parent, owner = {}, {}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for row in rows:
        key = (row["source_id"], row["speaker_id"])
        parent.setdefault(key, key)
        for field in HASH_FIELDS:
            value = row.get(field)
            if isinstance(value, str) and value:
                first, second = find(owner.setdefault((field, value.lower()), key)), find(key)
                if first != second:
                    parent[max(first, second)] = min(first, second)
    groups = defaultdict(list)
    for row in rows:
        groups[find((row["source_id"], row["speaker_id"]))].append(row)
    return groups


def group_decision(members):
    """Rules in order: benchmark, publisher held-out (test over dev), else hashed."""
    if any(is_benchmark(row) for row in members):
        return "benchmark_only", "benchmark_only"
    publisher = {value for row in members for value in publisher_partitions(row)}
    if publisher:
        return "publisher_held_out", "held_out_publisher_" + ("test" if "test" in publisher else "dev")
    return "hashed_speaker", None


def propose(rows, seed):
    """Pure assignment: returns (proposal_id, output rows, detail) or raises ValueError."""
    if not isinstance(seed, str) or not seed.strip() or len(seed) > MAX_SEED_CHARS:
        raise ValueError("Seed must be a nonempty string of at most 200 characters")
    if not rows or len(rows) > MAX_ROWS:
        raise ValueError("Provide 1 to 50000 clip rows")
    errors = lineage_errors(rows)
    if errors:
        raise ValueError("Input rows failed lineage checks: " + "; ".join(errors[:5]))
    canonical = sorted(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows)
    proposal_id = "split-proposal-" + hashlib.sha256(
        "\n".join([seed, *canonical]).encode("utf-8")).hexdigest()[:16]
    groups = build_groups(rows)
    decided, pools = {}, defaultdict(list)
    detail = {"allocation": {}, "hash_linked_speaker_groups": [],
              "publisher_split_conflicts": [], "benchmark_coupled_rows": 0}
    for root, members in groups.items():
        keys = sorted({(row["source_id"], row["speaker_id"]) for row in members})
        label = "+".join(f"{source}|{speaker}" for source, speaker in keys)
        rule, partition = group_decision(members)
        if len(keys) > 1:
            detail["hash_linked_speaker_groups"].append(
                {"group_key": label, "speakers": len(keys), "clips": len(members), "rule": rule})
        if rule == "benchmark_only":
            detail["benchmark_coupled_rows"] += sum(not is_benchmark(row) for row in members)
        if rule == "publisher_held_out" and any(
                publisher_partitions(row) != (partition.rsplit("_", 1)[1],) for row in members):
            detail["publisher_split_conflicts"].append(
                {"group_key": label, "clips": len(members), "partition": partition})
        if partition:
            decided[root] = (rule, partition, label)
        else:
            order = min(speaker_hash(seed, source, speaker) for source, speaker in keys)
            pools["+".join(sorted({source for source, _ in keys}))].append((order, label, root, len(keys)))
    for scope, entries in sorted(pools.items()):
        entries.sort()
        counts, cursor = allocate(len(entries)), 0
        for name, _ in RATIOS:
            for _, label, root, _ in entries[cursor:cursor + counts[name]]:
                decided[root] = ("hashed_speaker", name, label)
            cursor += counts[name]
        detail["allocation"][scope] = {
            "eligible_groups": len(entries), "eligible_speakers": sum(e[3] for e in entries),
            "group_counts": counts, "allocation_degenerate": len(entries) < 3}
    output = []
    for root, members in groups.items():
        rule, partition, label = decided[root]
        output.extend({**row, "proposed_split": partition, "proposal_id": proposal_id,
                       "proposal_seed": seed, "assignment_rule": rule, "group_key": label,
                       "status": "proposal"} for row in members)
    output.sort(key=lambda r: (r["source_id"], r["speaker_id"], r["source_recording_id"],
                               r["path"], r["sha256"]))
    return proposal_id, output, detail


def shares(rows, key):
    """Clip and second shares per partition inside each scope; shares sum to one per scope."""
    scopes = defaultdict(lambda: defaultdict(list))
    for row in rows:
        scopes[key(row)][row["proposed_split"]].append(row)
    result = {}
    for scope, parts in sorted(scopes.items()):
        clips = sum(len(members) for members in parts.values())
        seconds = sum(row["duration_seconds"] for members in parts.values() for row in members)
        result[scope] = {}
        for partition, members in sorted(parts.items()):
            part_seconds = sum(row["duration_seconds"] for row in members)
            result[scope][partition] = {
                "clips": len(members), "clip_share": round(len(members) / clips, 4),
                "seconds": round(part_seconds, 3), "minutes": round(part_seconds / 60, 3),
                "second_share": round(part_seconds / seconds, 4),
                "speakers": len({(r["source_id"], r["speaker_id"]) for r in members}),
                "recordings": len({(r["source_id"], r["source_recording_id"]) for r in members})}
    return result


def build_report(proposal_id, seed, inputs, rows, detail, validation):
    return {
        "schema_version": SCHEMA_VERSION, "proposal_id": proposal_id, "proposal_seed": seed,
        "status": "proposal",
        "policy": {
            "unit": "(source_id, speaker_id); groups linked by exact hashes are merged",
            "ratios_percent": dict(RATIOS),
            "order": "sha256(U+001F-joined seed, source_id, speaker_id) hex ascending per "
                     "source; merged groups order by their smallest member hash",
            "rules": ["any benchmark-only row -> whole speaker group benchmark_only",
                      "any publisher dev/test row -> whole group held_out_publisher_<split>, "
                      "test over dev",
                      "remaining groups: largest remainder 70/10/20 per source, one per "
                      "partition at three or more groups, one group -> test, two -> train "
                      "and test, remainder ties resolved train, dev, test"]},
        "inputs": inputs,
        "summary": {
            "rows": len(rows),
            "speakers": len({(r["source_id"], r["speaker_id"]) for r in rows}),
            "recordings": len({(r["source_id"], r["source_recording_id"]) for r in rows}),
            "groups": len({r["group_key"] for r in rows}),
            "partitions": validation["partitions"],
            "benchmark_coupled_rows": detail["benchmark_coupled_rows"],
            "training_ineligible_placements": sum(
                r.get("training_eligible") is False and r["assignment_rule"] == "hashed_speaker"
                for r in rows)},
        "allocation": detail["allocation"],
        "hash_linked_speaker_groups": detail["hash_linked_speaker_groups"],
        "publisher_split_conflicts": detail["publisher_split_conflicts"],
        "shares": {"overall": shares(rows, lambda r: "all")["all"],
                   "by_source": shares(rows, lambda r: r["source_id"]),
                   "by_language": shares(rows, lambda r: r["language"]),
                   "by_label": shares(rows, lambda r: r["label"]),
                   "by_assignment_rule": shares(rows, lambda r: r["assignment_rule"])},
        "disjointness": validation["overlaps"],
        "within_partition_duplicates": validation["within_partition_duplicates"],
        "validation": {"ok": validation["ok"], "errors": validation["errors"],
                       "warnings": validation["warnings"]},
        "limitations": list(LIMITATIONS)}


def load_report(path):
    """Bounded read of one processed report.json; returns (metadata, clip rows)."""
    path = Path(path).resolve()
    if (path.parent / "INCOMPLETE").exists():
        raise ValueError("Input publication is incomplete")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Report must be a regular file")
    with path.open("rb") as stream:
        document = stream.read(MAX_REPORT_BYTES + 1)
    if len(document) > MAX_REPORT_BYTES:
        raise ValueError("Report exceeds 8 MB limit")
    report = json.loads(document)
    clips = report.get("clips") if isinstance(report, dict) else None
    if not isinstance(clips, list) or not clips or not all(isinstance(r, dict) for r in clips):
        raise ValueError("Report requires a nonempty clips list of objects")
    if len(clips) > MAX_ROWS:
        raise ValueError("Report exceeds 50000 clip rows")
    return {"report": path.name, "report_directory": path.parent.name,
            "document_sha256": hashlib.sha256(document).hexdigest(),
            "source_manifest_sha256": report.get("source_manifest_sha256"),
            "source_revision": report.get("source_revision"), "clips": len(clips)}, clips


def write_json(path, payload):
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def publish(output_root, proposal, report):
    """Reserve a new directory atomically; INCOMPLETE marker remains if a write fails."""
    output_root.parent.mkdir(parents=True, exist_ok=True)
    output_root.mkdir()
    marker = output_root / "INCOMPLETE"
    marker.write_text("Publication incomplete; do not consume this directory.", encoding="utf-8")
    write_json(output_root / "proposal.json", proposal)
    write_json(output_root / "proposal_report.json", report)
    marker.unlink()


def run(report_paths, seed, output_root):
    if not 1 <= len(report_paths) <= MAX_REPORTS:
        raise ValueError("Provide 1 to 20 report files")
    output_root = Path(output_root).resolve()
    if output_root.exists():
        raise ValueError("Output must be a new directory; existing outputs are never overwritten")
    if output_root.is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError("Proposal output must be outside the repository")
    inputs, rows = [], []
    for path in report_paths:
        if output_root.is_relative_to(Path(path).resolve().parent):
            raise ValueError("Output must not be inside an input directory")
        metadata, clips = load_report(path)
        inputs.append(metadata)
        rows.extend(clips)
        if len(rows) > MAX_ROWS:
            raise ValueError("Inputs exceed 50000 clip rows in total")
    proposal_id, output_rows, detail = propose(rows, seed)
    validation = check_rows(output_rows)
    if not validation["ok"]:
        raise ValueError("Generated proposal failed validation: " + "; ".join(validation["errors"][:5]))
    report = build_report(proposal_id, seed, inputs, output_rows, detail, validation)
    proposal = {"schema_version": SCHEMA_VERSION, "proposal_id": proposal_id,
                "proposal_seed": seed, "status": "proposal", "inputs": inputs,
                "rows": output_rows}
    publish(output_root, proposal, report)
    return {**report["summary"], "disjointness": report["disjointness"],
            "allocation": report["allocation"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="append", required=True, type=Path,
                        help="processed report.json; repeat the flag for each source")
    parser.add_argument("--seed", required=True, help="caller-supplied string recorded in output")
    parser.add_argument("--output-root", required=True, type=Path,
                        help="new directory outside the repository")
    args = parser.parse_args()
    try:
        summary = run(args.report, args.seed, args.output_root)
    except (OSError, ValueError, UnicodeDecodeError) as error:
        print(f"Proposal failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
