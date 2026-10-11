# Split proposals

Partial FR-D8 / A4 groundwork: reproducible, speaker- and lineage-disjoint
**proposals** over prepared pilot reports. Every row carries `status: proposal`.
A proposal is a reviewable placement; it is not an accepted train, dev or test
set, not a training-eligibility decision and not a consent record. No acceptance
workflow exists. Stdlib only; no audio is opened, moved or modified.

```text
uv run --no-project --python 3.11 python data/manifest/propose_splits.py --report D:/AVA-Data/processed/<genuine>/report.json --report D:/AVA-Data/processed/<benchmark>/report.json --seed <seed-string> --output-root D:/AVA-Data/manifests/<new-directory>
uv run --no-project --python 3.11 python data/manifest/validate_splits.py D:/AVA-Data/manifests/<new-directory>
```

Inputs are `report.json` files written by `data/prepare/prepare_audio.py`. Bounds:
at most 20 reports, 8 MB each, 50,000 clip rows in total; an input directory with
an `INCOMPLETE` marker is refused. The output root must be new, outside the
repository and outside every input directory; existing outputs are never
overwritten. Publication reserves the directory, writes an `INCOMPLETE` marker,
then `proposal.json` and `proposal_report.json`, then removes the marker. A
directory that still carries the marker must not be consumed and will not be
reused. All checks run before the directory is created.

## Rules

The unit of assignment is `(source_id, speaker_id)`. Speakers linked by an exact
`source_sha256`, clip `sha256`, `canonical_pcm_sha256` or `original_audio_sha256`
are merged into one group so that no recording or byte copy straddles partitions;
such groups are listed under `hash_linked_speaker_groups`. A recording that maps
to two speaker IDs fails before any output. Rules apply per group, in order:

1. Any row with `benchmark_only: true`, or `training_eligible: false` with a
   benchmark `use_scope`, places the whole group in `benchmark_only`. Non-benchmark
   rows carried along are counted as `benchmark_coupled_rows`.
2. Any row with publisher `dev` or `test` (`publisher_split` or `original_split`)
   places the whole group in `held_out_publisher_<split>`; test wins over dev.
   Groups whose rows disagree are listed under `publisher_split_conflicts`.
3. Remaining groups are ordered within each source by
   `sha256("\x1f".join([seed, source_id, speaker_id]))` (merged groups use their
   smallest member hash) and filled 70/10/20 by largest remainder with remainder
   ties resolved train, dev, test. Three or more groups keep at least one per
   partition; one group goes to test; two go to train and test. Sources below
   three groups are flagged `allocation_degenerate`.

Rows with `training_eligible: false` outside benchmark scope receive placements
under rule 3 and are counted as `training_ineligible_placements`; a placement is
not approval. Output rows keep every original field and add `proposed_split`,
`proposal_id`, `proposal_seed`, `assignment_rule`, `group_key` and `status`.
Output order and `proposal_id` depend only on the seed and the row contents.

## Report and validator

`proposal_report.json` records inputs (file name, parent directory name, document
hash, source manifest hash, revision), counts, per-source allocation, clip and
second shares per partition overall and by source, language, label and rule,
disjointness proofs (zero `crossing_partitions` for speaker, recording and each
hash field), within-partition exact duplicates as warnings, and an explicit
`limitations` list. The validator checks the stated invariants directly without
recomputing assignments: lineage fields present and typed, status `proposal`,
known partitions, rule matching partition, benchmark rows only in
`benchmark_only`, publisher held-out rows never in train/dev/test, no duplicate
clip rows, no recording owned by two speakers, and zero cross-partition overlap.
It exits 1 on any failure or unreadable input and never modifies the proposal.

## Known limits and open items

- Identity is source-namespaced; the same person may appear under two sources.
- No consent record or synthetic target-reference mapping exists; nothing here
  asserts either. Same-speaker synthetic lineage from consented participants and
  codec copies do not exist yet.
- Exact hashes catch byte copies only, not near-duplicates or reused voices.
- When a benchmark set shares `source_id` and `speaker_id` values with genuine
  rows, rule 1 moves those genuine speakers to `benchmark_only` as well. That is
  the only strictly disjoint outcome; it also removes them from train/dev/test.
- Small speaker counts make 70/10/20 approximate; read realised shares.

## Verification

```text
uv run --no-project --python 3.11 python -m unittest discover -s tests/unit -p test_split_proposals.py -v
```

Tests use fictional rows in temporary directories. No real proposal, manifest or
audio belongs in Git; this machine's data is located by `.local/data-location.json`.
