---
title: 'Reproducible speaker- and lineage-disjoint split proposals'
type: 'feature'
created: '2026-10-04'
status: 'in-progress'
route: 'oneshot'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent">

## Intent

Produce deterministic, reviewable split **proposals** over the prepared pilot
manifests (CSALT genuine conditioned v2, CSALT synthetic benchmark v1, Meta
pnb pilot v1) that keep every speaker, source recording and derived clip within
one partition, preserve publisher splits and benchmark-only / training-ineligible
exclusions, and record what is unknown (cross-source identity, consent and
reference mappings). A proposal must never silently become an accepted
training or test set. No audio is read, moved or modified. No commits or pushes.

</frozen-after-approval>

## Implementation Notes

- New `data/manifest/propose_splits.py` with pure functions and I/O at the edges:
  load one or more processed `report.json` files (and optional raw pilot/balanced
  manifests for cross-checking), build a grouping key per clip, assign partitions,
  validate, write `proposal.json` and `proposal_report.json` to a new external
  directory. Reuse the existing bounded-read and no-overwrite conventions.
- Grouping key: `(source_id, speaker_id)` is the unit of assignment. All clips
  whose `speaker_id` matches, across every source recording and every derived
  clip, land together. `source_recording_id` is a secondary check: a recording
  can never straddle partitions. Speaker IDs are source-namespaced; the proposal
  does not merge identities across sources and says so.
- Partition rules, in order: (1) `benchmark_only: true` or `training_eligible:
  false` with a benchmark scope stays in `benchmark_only`, never train; (2) rows
  whose `publisher_split` or `original_split` is `dev` or `test` keep that
  publisher partition as `held_out_publisher_<split>` and are not reassigned;
  (3) remaining eligible-or-pending rows (`training_eligible` false but not
  benchmark; currently all pilot rows) get a deterministic 70/10/20 assignment by
  speaker using `sha256(seed + source_id + speaker_id)` ordering within each
  source, so the proposal is reproducible from the seed and stable under row
  reordering; the output states that `training_eligible: false` rows are
  proposed placements only, not training approval.
- Minimum-group handling: with few speakers per source (17 CSALT, 6 pnb), exact
  70/10/20 by speaker count is impossible; use largest-remainder allocation on
  speaker counts with at least one speaker per partition where the source has at
  least three speakers, and report the realised clip and minute shares per
  partition, per source, per language and per label. Never drop a speaker.
- Output rows: original clip row fields plus `proposed_split`, `proposal_id`,
  `proposal_seed`, `assignment_rule` (`benchmark_only`, `publisher_held_out`,
  `hashed_speaker`), `group_key`, and `status: proposal`. The report includes
  disjointness proofs (zero speaker, recording, source-hash or clip-hash overlap
  across partitions), shares, counts, and an explicit `limitations` list.
- Validator `data/manifest/validate_splits.py` (also importable): rejects any
  proposal with cross-partition overlap on speaker, recording, source hash or
  clip hash, any benchmark row outside `benchmark_only`, any publisher dev/test
  row reassigned, any `status` other than `proposal` unless an explicit
  acceptance record is present (not implemented now), and any missing lineage
  field. Exit nonzero on failure.
- Tests `tests/unit/test_split_proposals.py` with fictional fixtures: order
  independence, seed determinism, speaker and recording atomicity, benchmark and
  publisher-split preservation, allocation with 3, 6 and 17 fictional speakers,
  rejection of overlapping or mislabelled proposals, no-overwrite output. These
  are Claude-authored; prepare an Astra handoff for substantive cases through the
  `ava-test-designer` role and record provenance accurately.
- Docs: `data/manifest/README.md` with the command, the limitation that
  proposals are not accepted sets, and the open items (cross-source identity,
  consent mapping, same-speaker synthetic lineage once participant data exists).
- Out of scope: codec copies (none exist yet), synthetic lineage from consented
  speakers, cross-source duplicate detection beyond exact hashes, acceptance
  workflow, PRD FR-D8 completion.

## Decisions on specification gaps (coordinator, 4 October 2026)

Raised by the Astra handoff preparation (`docs/qa/split-proposal-astra-handoff.md`).

1. **Speaker spanning publisher train and dev/test rows.** Atomicity wins. If any
   row of a `(source_id, speaker_id)` group carries a publisher `dev` or `test`
   split, the whole group is assigned `held_out_publisher_<split>` (test wins over
   dev if both appear) and the report lists the group under
   `publisher_split_conflicts`. Not expected in current data (Meta speakers are
   split-disjoint per preflight; CSALT rows carry no dev/test), but it must hold.
2. **Allocation ties and tiny sources.** Order speakers by their hash key
   (ascending) and fill partitions by largest remainder; ties on remainder break by
   that same hash order, so the result is deterministic. A source with one
   eligible speaker places it in `test`; a source with two places the first (by
   hash order) in `train` and the second in `test`; three or more follow the
   at-least-one-per-partition rule. The report states the realised shares and
   flags sources below three speakers as `allocation_degenerate`.
3. **Hash input.** `sha256("\x1f".join([seed, source_id, speaker_id]).encode("utf-8"))`,
   hex digest, compared as strings. Seed is a caller-supplied string recorded in
   the output. No dependence on `hash()` or `PYTHONHASHSEED`.
4. **Duplicate hashes within one partition.** Exact duplicate `source_sha256` or
   clip `sha256` within the same partition is reported under
   `within_partition_duplicates` as a warning with counts; it does not fail the
   proposal. Any duplicate across partitions remains a hard failure.
5. **Conflicting speaker IDs on one recording.** A `source_recording_id` that maps
   to more than one `speaker_id` within a source fails at proposal time, before
   any output directory is created, and the validator rejects it as well.
6. **Input bounds.** At most 20 report files, each at most 8 MB, at most 50,000
   clip rows in total; exceeding any bound fails before output creation.
7. **Benchmark rows follow their speaker (supersedes rule 1 above).** The first
   real run showed that the CSALT synthetic benchmark reuses the same source and
   all 17 speaker IDs as the CSALT genuine set, so a `benchmark_only` *partition*
   forces every Urdu genuine clip into the benchmark. `benchmark_only` is a usage
   scope, not a partition. Rule 1 is replaced: rows with `benchmark_only: true`
   are assigned the partition of their `(source_id, speaker_id)` group like any
   other row, with `assignment_rule: benchmark_follows_speaker`, and they keep
   `benchmark_only: true` and `training_eligible: false`. Exclusion from training
   is carried by those flags, not by placement. The validator must check that
   benchmark rows retain both flags and are never counted in any `trainable`
   total, and must report benchmark rows per partition; it no longer treats
   `benchmark_only` as a partition. The report carries `benchmark_rows_by_partition`
   and a `trainable_rows_by_partition` table that excludes every
   `training_eligible: false` row. Publisher held-out rule 2 and atomicity rule 1
   (decisions section) are unchanged. Groups linked by shared hashes still merge.
8. **Allocation tie-break reading confirmed.** Remainder ties resolve in the
   fixed order train, dev, test after ordering speakers by hash key; 4 speakers
   give 2/1/1 and 5 give 3/1/1. Within-partition duplicate warnings are counted
   per recording for source-level hashes and per row for clip hashes.
