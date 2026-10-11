"""Check a genuine-audio pilot manifest; never read or upload audio.

This is an intake check, not full FR-D8/FR-D13 acceptance or a rights decision.
Python 3.11+ standard library only. Diagnostics use row numbers, not identities.
"""

import argparse
import csv
import json
import math
from pathlib import Path
import re


FIELDS = (
    "clip_id", "source_id", "source_version", "speaker_id",
    "source_recording_id", "language", "label", "sha256", "duration_s",
    "licence_ref", "use_status", "split",
)


def validate(path: Path) -> dict:
    errors = []
    ids = set()
    groups = {name: {} for name in ("speaker", "recording", "sha256")}
    counts = {}
    rows = 0
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        header = reader.fieldnames or []
        missing = sorted(set(FIELDS) - set(header))
        if missing or len(header) != len(set(header)):
            return {"ok": False, "rows": 0, "errors": [
                "Invalid header: required fields missing or duplicate columns."]}
        for number, raw in enumerate(reader, start=2):
            rows += 1
            if None in raw or any(raw.get(field) is None for field in FIELDS):
                errors.append(f"Row {number}: malformed CSV row.")
                continue
            row = {field: raw[field].strip() for field in FIELDS}
            if any(not value for value in row.values()):
                errors.append(f"Row {number}: required value missing.")
                continue
            if row["clip_id"] in ids:
                errors.append(f"Row {number}: duplicate clip_id.")
            ids.add(row["clip_id"])
            if row["label"] != "genuine":
                errors.append(f"Row {number}: pilot supports genuine intake only.")
            if row["language"] not in {"ur", "pnb_Arab"}:
                errors.append(f"Row {number}: unsupported pilot language.")
            if row["use_status"] != "approved":
                errors.append(f"Row {number}: intended use review is not approved.")
            split = row["split"]
            if split not in {"unassigned", "train", "dev", "test"}:
                errors.append(f"Row {number}: invalid split.")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", row["sha256"]):
                errors.append(f"Row {number}: invalid SHA256.")
            try:
                duration = float(row["duration_s"])
                if not math.isfinite(duration) or duration <= 0:
                    raise ValueError
            except ValueError:
                errors.append(f"Row {number}: duration must be finite and positive.")
            # Include unassigned: partially assigning one speaker also needs repair.
            keys = {
                "speaker": (row["source_id"], row["speaker_id"]),
                "recording": (row["source_id"], row["source_recording_id"]),
                "sha256": row["sha256"].lower(),
            }
            for kind, key in keys.items():
                seen = groups[kind].setdefault(key, split)
                if seen != split:
                    errors.append(f"Row {number}: {kind} crosses split assignments.")
            counts[row["language"]] = counts.get(row["language"], 0) + 1
    if rows == 0:
        errors.append("Manifest contains no clips.")
    return {"ok": not errors, "rows": rows, "language_counts": counts,
            "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        result = validate(args.manifest)
    except (OSError, UnicodeError, csv.Error):
        result = {"ok": False, "errors": ["Cannot read manifest as UTF-8 CSV."]}
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
