"""Versioned policy migration of public-corpus intake manifests (PRD D11/FR-D11, todo 9e).

Applies the 5 October 2026 project-owner decision to a *copy* of an existing intake
manifest (CSALT balanced.json, CSALT benchmark.json or Meta pnb pilot.json): genuine
CSALT Urdu and Meta Omnilingual pnb_Arab rows become `training_eligible: true` with
`cloning_status: permitted_public_corpus`; CSALT existing-synthetic benchmark rows
stay `benchmark_only: true`, `training_eligible: false`. Every row records its
previous values, the policy version and its basis. Consent status is unchanged:
public-corpus permission is not individual consent. The input manifest is never
modified; output goes to a new directory outside the repository. Common Voice and
any other source are refused. Python 3.11+ standard library only; no audio is read.
"""
import argparse
import hashlib
import json
from pathlib import Path
import stat
import sys

POLICY_VERSION = "fr-d11-2026-10-05"
POLICY_BASIS = ("Project-owner decision 5 Oct 2026 (PRD D11, FR-D11) under the university approval "
                "reported 4 Oct 2026 (reference pending): public corpora whose licences permit "
                "derivative works may be used for training and voice cloning in this research. "
                "No cloned voice is released (D14). Not individual consent.")
BENCHMARK_BASIS = ("Retained benchmark-only under the same decision: publisher synthetic clips have "
                   "unknown generator version, prompt and reference audio (FR-D2), so they are neither "
                   "training nor cloning sources. Only AVA-generated synthetic clips can satisfy FR-D11.")
KINDS = {
    "csalt_genuine": {"source_id": "csalt_urdu", "label": "genuine", "benchmark": False,
                      "licence": "CC BY-NC 4.0, noncommercial use only", "basis": POLICY_BASIS},
    "csalt_synthetic_benchmark": {"source_id": "csalt_urdu", "label": "synthetic", "benchmark": True,
                                  "licence": "CC BY-NC 4.0, noncommercial use only", "basis": BENCHMARK_BASIS},
    "meta_pnb": {"source_id": "meta_omnilingual_pnb", "label": "genuine", "benchmark": False,
                 "licence": "CC BY 4.0, attribution required", "basis": POLICY_BASIS},
}
UNVERIFIED_CONSENT = "not_individually_verified"
COMPLETE_FLAGS = ("balanced_complete", "benchmark_complete", "pilot_complete")
MAX_BYTES, MAX_ROWS = 8_000_000, 10_000


def migrate_rows(rows, kind):
    """Pure: returns migrated copies of rows or raises ValueError naming the first problem."""
    if kind not in KINDS:
        raise ValueError("Unsupported source kind; only CSALT and Meta pnb public corpora are covered")
    spec = KINDS[kind]
    if not isinstance(rows, list) or not 0 < len(rows) <= MAX_ROWS:
        raise ValueError(f"Manifest needs 1 to {MAX_ROWS} file rows")
    result = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"Row {index} is not an object")
        if row.get("policy_version") is not None:
            raise ValueError(f"Row {index} is already migrated")
        if row.get("source_id") != spec["source_id"] or row.get("label") != spec["label"]:
            raise ValueError(f"Row {index} does not belong to {kind}")
        benchmark = row.get("benchmark_only") in (True, "true")
        if benchmark != spec["benchmark"] or (not benchmark and "benchmark" in str(row.get("use_scope", ""))):
            raise ValueError(f"Row {index} benchmark scope does not match {kind}")
        if row.get("validation_status") != "verified":
            raise ValueError(f"Row {index} is not a verified intake row")
        previous = {key: row.get(key) for key in ("training_eligible", "cloning_status", "consent_status")}
        updates = ({"training_eligible": False, "benchmark_only": True} if spec["benchmark"] else
                   {"training_eligible": True, "cloning_status": "permitted_public_corpus"})
        # Corpus permission is not consent: make the unverified status explicit, never upgrade it.
        updates["consent_status"] = row.get("consent_status") or UNVERIFIED_CONSENT
        result.append({**row, **updates, "policy_version": POLICY_VERSION, "policy_basis": spec["basis"],
                       "policy_licence": spec["licence"], "previous_policy": previous})
    return result


def migrate_document(document, kind, source_name, source_sha256):
    if not isinstance(document, dict) or not any(document.get(flag) is True for flag in COMPLETE_FLAGS):
        raise ValueError("Input must be a complete published intake manifest")
    return {**document, "files": migrate_rows(document.get("files"), kind),
            "policy_version": POLICY_VERSION, "policy_basis": POLICY_BASIS,
            "migrated_from": {"manifest": source_name, "sha256": source_sha256, "kind": kind}}


def run(manifest, output_root, kind):
    manifest, output_root = Path(manifest).resolve(), Path(output_root).resolve()
    if (manifest.parent / "INCOMPLETE").exists():
        raise ValueError("Input publication is incomplete")
    if not stat.S_ISREG(manifest.stat().st_mode):
        raise ValueError("Manifest must be a regular file")
    if output_root.exists():
        raise ValueError("Output must be a new directory; existing outputs are never overwritten")
    if output_root.is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError("Real manifests must stay outside the repository")
    if output_root.is_relative_to(manifest.parent):
        raise ValueError("Output must not be inside the input directory")
    with manifest.open("rb") as stream:
        payload = stream.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ValueError("Manifest exceeds 8 MB limit")
    migrated = migrate_document(json.loads(payload), kind, manifest.name, hashlib.sha256(payload).hexdigest())
    output_root.parent.mkdir(parents=True, exist_ok=True)
    output_root.mkdir()
    marker = output_root / "INCOMPLETE"
    marker.write_text("Publication incomplete; do not consume this directory.", encoding="utf-8")
    (output_root / manifest.name).write_text(json.dumps(migrated, indent=2), encoding="utf-8")
    marker.unlink()
    rows = migrated["files"]
    return {"kind": kind, "rows": len(rows), "policy_version": POLICY_VERSION,
            "training_eligible_rows": sum(r["training_eligible"] is True for r in rows),
            "benchmark_rows": sum(r.get("benchmark_only") is True for r in rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--kind", required=True, choices=sorted(KINDS))
    args = parser.parse_args()
    try:
        summary = run(args.manifest, args.output_root, args.kind)
    except (OSError, ValueError) as error:
        print(f"Migration failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
