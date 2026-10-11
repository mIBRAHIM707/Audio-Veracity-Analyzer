# Task handoff: reproducible split proposals (todo 9)

- Task and issue: todo 9, reproducible speaker- and lineage-disjoint split
  proposals over the three prepared pilot reports. Spec:
  `_bmad-output/implementation-artifacts/spec-split-proposals.md` including the six
  coordinator decisions of 4 October. Related issue #11 context.
- Owner and actual model used: ava-data-engineer role run by Claude
  (claude-fable-5-1, Claude Code). Tests are Claude-authored; no GPT-6 Astra output
  exists for this task yet (`docs/qa/split-proposal-astra-handoff.md` is a request).
- Worktree, branch and base commit: main checkout, `feat/11-pilot-audio-preparation`,
  HEAD `ad13029fa283183c23e84d736ac424b777e775c3`, dirty tree preserved; nothing
  committed, pushed, stashed or reset.
- Owned files: `data/manifest/__init__.py`, `data/manifest/propose_splits.py`,
  `data/manifest/validate_splits.py`, `data/manifest/README.md`,
  `tests/unit/test_split_proposals.py`, this file. No other file edited.
- Requirement and ADR IDs: FR-D8 and A4 (partial: pilot proposals only), FR-D11
  placement mechanism only, FR-E3 (external storage), ADR-14 lineage coherence.
  Not FR-D8 completion; no acceptance workflow.

## Implemented changes

Stdlib-only proposer and validator with pure rules and I/O at the edges.
Validator encodes the invariants once (`is_benchmark`, `publisher_partitions`,
`lineage_errors`, `check_rows`); the proposer imports them and refuses to publish
anything its own output fails. The validator never recomputes assignments.

- Unit of assignment `(source_id, speaker_id)`; groups linked by an exact
  `source_sha256`, clip `sha256`, `canonical_pcm_sha256` or `original_audio_sha256`
  are merged (union-find, order independent) so no byte copy can straddle partitions.
- Rules per group in order: any benchmark row -> `benchmark_only`; any publisher
  dev/test row -> `held_out_publisher_<split>` with test over dev and
  `publisher_split_conflicts` listed (decision 1); remaining groups ordered by
  `sha256("\x1f".join([seed, source_id, speaker_id]))` (decision 3; merged groups
  use their smallest member hash), largest remainder 70/10/20 on integer percents,
  one group -> test, two -> train and test, three or more keep one per partition,
  `allocation_degenerate` flag below three (decision 2). Remainder ties resolve
  train, dev, test; I read "break by that same hash order" as the fill order in
  which hash-sorted speakers are consumed. Say so if a different reading was meant.
- Within-partition exact duplicates are warnings with counts (decision 4), counted
  per recording for source-level hashes and per row for clip hashes, so clips cut
  from one recording are not miscounted as duplicates. Cross-partition duplicates
  fail. A recording owned by two speaker IDs fails before output and in the
  validator (decision 5). Bounds: 20 reports, 8 MB each, 50,000 rows total, seed
  at most 200 characters, all enforced before the output directory exists (decision 6).
- Output rows keep all original fields plus `proposed_split`, `proposal_id`
  (SHA-256 of seed plus sorted canonical rows), `proposal_seed`, `assignment_rule`,
  `group_key`, `status: proposal`; rows sorted by lineage keys, so output is
  identical under any input order. Report: inputs (names and hashes, no absolute
  paths), counts, allocation, shares by partition overall and by source, language,
  label and rule, disjointness proofs for six key kinds, duplicate warnings and an
  eight-item `limitations` list. Publication reserves the directory with `mkdir`,
  writes `INCOMPLETE`, writes both JSON files through temporary names, removes the
  marker; a write failure leaves the marker; existing output roots, roots inside the
  repository and roots inside an input directory are refused.
- Line count: propose 315, validate 199 (514 together, above the suggested 400;
  the six added decisions, typed field checks and report tables account for it).

## Exact validation commands and results

```text
uv run --no-project --python 3.11 python -m unittest discover -s tests/unit -p test_split_proposals.py -v
  -> Ran 19 tests in 2.332s, OK (first run had two fixture-authoring failures, fixed; no assertion weakened)
uv run --no-project --python 3.11 python data/manifest/propose_splits.py --report D:/AVA-Data/processed/csalt-balanced-conditioned-v2/report.json --report D:/AVA-Data/processed/csalt-synthetic-benchmark-v1/report.json --report D:/AVA-Data/processed/meta-pnb-pilot-v1/report.json --seed ava-split-proposal-2026-10-04 --output-root D:/AVA-Data/manifests/split-proposal-v1
  -> exit 0; 687 rows, 23 speakers, 326 recordings, 22 groups
uv run --no-project --python 3.11 python data/manifest/validate_splits.py D:/AVA-Data/manifests/split-proposal-v1
  -> exit 0; ok true; zero crossing_partitions for speaker (23 keys), recording (326), source_sha256 (324), sha256 (683), canonical_pcm_sha256 (324), original_audio_sha256 (12); 3 duplicate warnings
uv run --no-project --python 3.11 python data/manifest/propose_splits.py --report ...csalt-balanced-conditioned-v2... --report ...meta-pnb-pilot-v1... --seed ava-split-proposal-2026-10-04 --output-root D:/AVA-Data/manifests/split-proposal-v1-genuine-only
  -> exit 0; 637 rows; validator exit 0, all crossing_partitions zero
uv run --no-project --python 3.11 python scripts/check_foundation.py   -> passed (8 roles, 16 adapters, 29 skills)
uv run --no-project --python 3.11 python scripts/repository_guard.py   -> passed (579 paths)
Determinism: two extra runs with PYTHONHASHSEED=0 and 31337 and the three reports in reversed order produced byte-identical proposal.json files whose rows and proposal_id equal v1; the scratch copies were deleted afterwards.
```

The v1 run was the first and only write to `split-proposal-v1`; no v2 was needed.

### Aggregate results, `split-proposal-v1` (three reports, `split-proposal-79dd4349da7d3b68`)

| Partition | Clips | Minutes | Clip share | Second share | Speakers |
|---|---|---|---|---|---|
| benchmark_only | 500 | 36.604 | 0.728 | 0.742 | 17 (CSALT: 450 genuine + 50 synthetic) |
| train | 122 | 8.349 | 0.178 | 0.169 | 4 (pnb) |
| dev | 39 | 2.471 | 0.057 | 0.050 | 1 (pnb) |
| test | 26 | 1.896 | 0.038 | 0.038 | 1 (pnb) |

Per source: CSALT Urdu 100% benchmark_only; Meta pnb 65.2/20.9/13.9% of clips
(65.7/19.4/14.9% of seconds) over 4/1/1 speakers, `allocation_degenerate: false`.
`benchmark_coupled_rows: 450`; `training_ineligible_placements: 187` (all pnb rows,
placements only). One hash-linked group (2 speakers, 74 clips). No publisher conflicts.

### Aggregate results, `split-proposal-v1-genuine-only` (decision aid, `split-proposal-da23aaf0e01de8f3`)

| Partition | Clips | Minutes | Clip share | Second share | Speakers |
|---|---|---|---|---|---|
| train | 431 | 31.143 | 0.677 | 0.687 | 16 |
| dev | 94 | 6.394 | 0.148 | 0.141 | 3 |
| test | 112 | 7.821 | 0.176 | 0.172 | 4 |

CSALT Urdu: 12/2/3 speakers (11/2/3 groups; the merged pair sits in train),
68.7/12.2/19.1% of clips, 69.8/12.0/18.2% of seconds. Meta pnb identical to v1.

## Failures, limitations and untested behavior

1. **Identity collision between CSALT genuine and CSALT synthetic (high, decision
   needed).** The two CSALT reports share one `source_id` and all 17 `speaker_id`
   values (publisher folder names; benchmark rows carry
   `speaker_identity_status: publisher_folder_only`, `target_reference_status:
   unknown`). Under the spec's unit of assignment, every CSALT genuine speaker group
   contains benchmark rows, and the only assignment that keeps speaker overlap at
   zero across all partitions, `benchmark_only` included, places the 450 genuine
   clips in `benchmark_only` too. That is what v1 does, so v1 yields no Urdu train,
   dev or test data. A row-level reading of rule 1 would instead give 17 speaker
   overlaps between `benchmark_only` and train/dev/test, which the strict validator
   rejects. I did not tune seeds or weaken checks. Options for the coordinator:
   (a) accept v1 as "CSALT is a speaker-matched evaluation set, pnb supplies
   genuine train/dev/test" until participant data exists; (b) accept the
   genuine-only variant for CSALT training and record in the PRD that the CSALT
   synthetic benchmark is **not** speaker-disjoint from it; (c) an A4-style
   overlay where fakes follow their speaker's partition with `training_eligible:
   false` kept, which needs a spec change to rules 1 and the validator.
2. **Byte-identical originals under two speaker folders (medium).** Within the CSALT
   genuine selection, 2 `source_sha256` values each appear under two
   `source_recording_id`s belonging to two different `speaker_id`s, giving 4
   duplicated clip hashes. The merge keeps them together (one group of 2 speakers);
   the warnings `source_sha256: 2`, `sha256: 4`, `canonical_pcm_sha256: 2` record
   it. Whether this is a publisher filing error or one person under two folders is
   unknown; intake (todo 3/7d follow-up) should quarantine or annotate those
   originals rather than leave the resolution to split time.
3. Decision 2 tie-break interpretation stated above; 4- and 5-speaker allocations
   resolve to 2/1/1 and 3/1/1 and are unit-tested.
4. Not run: the full unit suite with `requirements-audio.txt`, Python 3.12, a
   process-race test on `mkdir`, and any Astra-authored cases. No listening,
   leakage probes, codec copies or consent mapping are involved. Exact hashes are
   the only duplicate signal. Fixture tests are infrastructure evidence, not
   dataset acceptance; `status: proposal` on every row.
5. Both external outputs hold speaker-level rows and live only under
   `D:/AVA-Data/manifests/`; nothing from them is in Git or this file beyond
   counts, dataset names and proposal IDs.

## Proposed PRD, architecture or context updates

- `docs/context/current-state.md`: todo 9 tooling complete; v1 and the genuine-only
  variant exist with the aggregates above; record finding 1 as an open decision and
  finding 2 as an intake follow-up. Add both report paths to
  `.local/data-location.json` (`split_proposal_v1_report`,
  `split_proposal_v1_genuine_only_report`).
- PRD FR-D8/A4 note: the existing-synthetic CSALT benchmark shares speakers with
  the CSALT genuine pilot; a speaker-disjoint evaluation against a CSALT-trained
  model is not possible with that benchmark, which strengthens the need for
  same-speaker synthetic lineage from consented participants (FR-D11).
- Spec: record the tie-break reading, the per-recording duplicate counting, and the
  chosen option for finding 1; update `docs/context/todo.md` accordingly.
- `CLAUDE.md`/README command list: add the two `data/manifest` commands if the
  coordinator wants them as entrypoints.

## Next action and receiving owner

Coordinator: choose among options (a), (b) and (c) for finding 1, then decide
whether v1 or the genuine-only variant is the proposal of record; queue the
duplicate-original intake follow-up; request Astra extension cases from the
prepared handoff; integrate `.local` pointers. No commits or pushes until Tughral
instructs.
