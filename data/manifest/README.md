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

1. Benchmark rows follow their speaker (spec decision 7, schema version 2).
   `benchmark_only` is a usage scope, not a partition: a row with
   `benchmark_only: true` takes its group's partition with `assignment_rule:
   benchmark_follows_speaker` and must keep `benchmark_only: true` and
   `training_eligible: false`. A benchmark `use_scope` without both flags fails
   before output (decision 9). The report adds `benchmark_rows_by_partition` and
   `trainable_rows_by_partition`; only rows with explicit `training_eligible: true`
   that are not benchmark rows count as trainable.
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
known partitions, rule matching partition, benchmark rows using
`benchmark_follows_speaker` with both flags kept, publisher held-out rows never
in train/dev/test, no duplicate
clip rows, no recording owned by two speakers, and zero cross-partition overlap.
For a whole proposal it also requires `schema_version` 2 and recomputes the
`proposal_id` content digest over the rows without proposal fields, so edits made
after proposal (including coordinated flag and rule changes) are detected.
It exits 1 on any failure or unreadable input and never modifies the proposal.

## Known limits and open items

- Identity is source-namespaced; the same person may appear under two sources.
- No consent record or synthetic target-reference mapping exists; nothing here
  asserts either. Same-speaker synthetic lineage from consented participants and
  codec copies do not exist yet.
- Exact hashes catch byte copies only, not near-duplicates or reused voices.
- Schema version 1 (before decision 7) moved every speaker with a benchmark row
  into a `benchmark_only` partition; the CSALT synthetic benchmark shares all 17
  speakers with CSALT genuine, so v1 left no Urdu genuine clips for train/dev/test.
  The external v1 proposals are kept as evidence; the schema-2 rerun (backlog 1.4,
  9 Nov) is in `docs/context/handoffs/policy-migration.md`.
- Benchmark rows now share a partition with genuine rows of the same speaker, so a
  model trained on that partition must exclude them by flag; consumers must read
  `training_eligible`/`benchmark_only`, not the partition name.
- Small speaker counts make 70/10/20 approximate; read realised shares.

## Policy migration (FR-D11)

`migrate_policy.py --manifest <published intake manifest> --output-root <new dir>
--kind csalt_genuine|csalt_synthetic_benchmark|meta_pnb` writes a policy-versioned
copy (`fr-d11-2026-10-05`): genuine CSALT and Meta rows become trainable with
`cloning_status: permitted_public_corpus`; benchmark rows stay excluded; consent is
unchanged; previous values are kept in `previous_policy`. Re-prepare the copy with
`prepare_audio.py --condition` (clips carry `policy_version`), verify with
`scripts/verify_prepared_output.py`, then propose. See
`docs/context/handoffs/policy-migration.md`.

## Verification

```text
uv run --no-project --python 3.11 python -m unittest discover -s tests/unit -p "test_split_proposals*.py" -v
```

Cases: Claude-authored baseline `test_split_proposals.py` and Claude-authored
cases `test_split_proposals_cases.py` (catalogue `docs/qa/split-proposal-test-cases.md`).

Tests use fictional rows in temporary directories. No real proposal, manifest or
audio belongs in Git; this machine's data is located by `.local/data-location.json`.
