"""Validate split proposals: lineage completeness, disjoint partitions, kept exclusions.

Importable pure checks plus a CLI that exits nonzero on any failure. The checks
read proposal structure only: no audio is opened and no acceptance is granted.
Python 3.11+ standard library only. Diagnostics use row positions and counts,
never speaker identities. The validator checks stated invariants directly and
does not recompute assignments from the proposer.
"""

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import stat
import sys

LINEAGE_FIELDS = ("source_id", "speaker_id", "source_recording_id", "source_sha256",
                  "sha256", "path", "label", "language", "duration_seconds")
PROPOSAL_FIELDS = ("proposed_split", "proposal_id", "proposal_seed", "assignment_rule",
                   "group_key", "status")
# Exact hashes link lineage across rows; they catch byte copies, not near-duplicates.
HASH_FIELDS = ("source_sha256", "sha256", "canonical_pcm_sha256", "original_audio_sha256")
PARTITIONS = ("train", "dev", "test", "benchmark_only",
              "held_out_publisher_dev", "held_out_publisher_test")
RULES = ("benchmark_only", "publisher_held_out", "hashed_speaker")
MAX_PROPOSAL_BYTES = 32_000_000
MAX_ERRORS = 200
HEX64 = re.compile(r"[0-9a-fA-F]{64}")


def is_benchmark(row):
    """Rule 1 input: benchmark-only rows never enter train, dev or test."""
    scope = str(row.get("use_scope", ""))
    return row.get("benchmark_only") is True or (
        row.get("training_eligible") is False and "benchmark" in scope)


def publisher_partitions(row):
    """Rule 2 input: publisher dev/test labels on the row, sorted and deduplicated."""
    values = {row.get(field) for field in ("publisher_split", "original_split")}
    return tuple(sorted(values & {"dev", "test"}))


def expected_rule(split):
    if split == "benchmark_only":
        return "benchmark_only"
    return "publisher_held_out" if split.startswith("held_out_publisher_") else "hashed_speaker"


def field_errors(row, fields):
    if not isinstance(row, dict):
        return ["row is not an object."]
    problems = []
    for field in fields:
        value = row.get(field)
        if field == "duration_seconds":
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or value <= 0:
                problems.append("duration_seconds must be finite and positive.")
        elif not isinstance(value, str) or not value.strip():
            problems.append(f"missing or empty field {field}.")
        elif field in HASH_FIELDS and not HEX64.fullmatch(value):
            problems.append(f"{field} must be 64 hexadecimal characters.")
    for field in ("benchmark_only", "training_eligible"):
        if field in row and not isinstance(row[field], bool):
            problems.append(f"{field} must be a boolean when present.")
    for field in ("publisher_split", "original_split"):
        if row.get(field) is not None and not isinstance(row[field], str):
            problems.append(f"{field} must be a string when present.")
    return problems


def lineage_errors(rows):
    """Input-side checks shared by proposer preflight and validator."""
    errors, owners = [], {}
    for index, row in enumerate(rows):
        problems = field_errors(row, LINEAGE_FIELDS)
        if problems:
            errors.extend(f"Row {index}: {problem}" for problem in problems)
            continue
        owners.setdefault((row["source_id"], row["source_recording_id"]), set()).add(row["speaker_id"])
    conflicts = sum(len(speakers) > 1 for speakers in owners.values())
    if conflicts:
        errors.append(f"{conflicts} recording(s) map to more than one speaker_id within a source.")
    return errors


def check_rows(rows):
    """Return structured findings; 'ok' is true only with zero errors."""
    result = {"ok": False, "rows": 0, "partitions": {}, "overlaps": {},
              "within_partition_duplicates": {}, "warnings": [], "errors": []}
    if not isinstance(rows, list) or not rows:
        result["errors"].append("Proposal contains no rows.")
        return result
    errors = lineage_errors(rows)
    counts, ids, seeds, seen, holders = Counter(), set(), set(), set(), {}
    tables = {kind: {} for kind in ("speaker", "recording", *HASH_FIELDS)}
    for index, row in enumerate(rows):
        if field_errors(row, LINEAGE_FIELDS):
            continue  # already reported by lineage_errors
        problems = field_errors(row, PROPOSAL_FIELDS)
        if problems:
            errors.extend(f"Row {index}: {problem}" for problem in problems)
            continue
        split, rule = row["proposed_split"], row["assignment_rule"]
        counts[split] += 1
        ids.add(row["proposal_id"])
        seeds.add(row["proposal_seed"])
        if row["status"] != "proposal":
            errors.append(f"Row {index}: status must be proposal.")
        if split not in PARTITIONS:
            errors.append(f"Row {index}: unknown proposed_split.")
        elif rule != expected_rule(split):
            errors.append(f"Row {index}: assignment_rule does not match proposed_split.")
        if is_benchmark(row) and split != "benchmark_only":
            errors.append(f"Row {index}: benchmark-only row placed outside benchmark_only.")
        publisher = publisher_partitions(row)
        allowed = {"benchmark_only", "held_out_publisher_test"}
        if "test" not in publisher:
            allowed.add("held_out_publisher_dev")
        if publisher and split not in allowed:
            errors.append(f"Row {index}: publisher held-out row reassigned.")
        identity = (row["source_id"], row["path"])
        if identity in seen:
            errors.append(f"Row {index}: duplicate clip row.")
        seen.add(identity)
        keys = {"speaker": (row["source_id"], row["speaker_id"]),
                "recording": (row["source_id"], row["source_recording_id"])}
        for field in HASH_FIELDS:
            if isinstance(row.get(field), str) and row[field]:
                keys[field] = row[field].lower()
        for kind, key in keys.items():
            tables[kind].setdefault(key, set()).add(split)
            # Clips cut from one recording share its source hash by design; a duplicate
            # means one hash under several recordings, or one clip hash under several rows.
            holder = identity if kind == "sha256" else keys["recording"]
            holders.setdefault((kind, key, split), set()).add(holder)
    for kind, table in tables.items():
        crossing = sum(len(splits) > 1 for splits in table.values())
        result["overlaps"][kind] = {"keys": len(table), "crossing_partitions": crossing}
        if crossing:
            errors.append(f"{crossing} {kind} key(s) cross partitions.")
    for kind in HASH_FIELDS:
        repeated = {key for (k, key, _), owners in holders.items() if k == kind and len(owners) > 1}
        result["within_partition_duplicates"][kind] = len(repeated)
        if repeated:
            result["warnings"].append(f"{len(repeated)} {kind} value(s) repeat within one partition.")
    if len(ids) > 1:
        errors.append("Rows carry more than one proposal_id.")
    if len(seeds) > 1:
        errors.append("Rows carry more than one proposal_seed.")
    result.update(ok=not errors, rows=len(rows), partitions=dict(sorted(counts.items())),
                  errors=errors[:MAX_ERRORS])
    return result


def load_proposal(path):
    """Bounded read of proposal.json (or a directory containing it); never writes."""
    path = Path(path).resolve()
    if path.is_dir():
        path = path / "proposal.json"
    if (path.parent / "INCOMPLETE").exists():
        raise ValueError("Proposal publication is incomplete; do not consume it.")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Proposal must be a regular file.")
    with path.open("rb") as stream:
        document = stream.read(MAX_PROPOSAL_BYTES + 1)
    if len(document) > MAX_PROPOSAL_BYTES:
        raise ValueError("Proposal exceeds 32 MB limit.")
    proposal = json.loads(document)
    if not isinstance(proposal, dict) or not isinstance(proposal.get("rows"), list):
        raise ValueError("Proposal requires an object with a rows list.")
    return proposal


def check_proposal(proposal):
    result = check_rows(proposal["rows"])
    if proposal.get("status") != "proposal":
        result["errors"].append("Proposal document status must be proposal.")
    row_ids = {row.get("proposal_id") for row in proposal["rows"] if isinstance(row, dict)}
    if row_ids and row_ids != {proposal.get("proposal_id")}:
        result["errors"].append("Document proposal_id does not match its rows.")
    result["ok"] = not result["errors"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proposal", type=Path, help="proposal.json or its directory")
    args = parser.parse_args()
    try:
        result = check_proposal(load_proposal(args.proposal))
    except (OSError, ValueError, UnicodeDecodeError) as error:
        result = {"ok": False, "errors": [f"Cannot read proposal: {error}"]}
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
