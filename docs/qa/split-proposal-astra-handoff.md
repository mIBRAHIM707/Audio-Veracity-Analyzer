# Split proposals: test-design handoff for GPT-6 Astra

**Provenance of this file.** Prepared by Claude (claude-fable-5-1, Claude Code) on
2026-10-04 as a handoff request. It is **not** an Astra-authored case catalogue and
contains no Astra output. The substantive case design is requested from the Codex
`ava_test_designer` role (`.codex/agents/ava-test-designer.toml`, `model = "gpt-6-astra"`)
following `docs/agents/test-designer.md`. If `gpt-6-astra` cannot be run, the role
must report that limitation rather than substitute another designer; Claude may then
only implement and execute cases that Astra has actually designed, recording the
real provenance of each part.

## 1. Task, revision and ownership

- Task: todo 9 in `docs/context/todo.md`, "reproducible speaker- and lineage-disjoint
  pilot splits; separately document missing identity/lineage that prevents final
  training-set assignment".
- Spec: `_bmad-output/implementation-artifacts/spec-split-proposals.md` (status
  `in-progress`, route `oneshot`). The frozen intent block is human-owned.
- Source revision: branch `feat/11-pilot-audio-preparation`, last commit
  `ad13029fa283183c23e84d736ac424b777e775c3` ("build balanced CSALT genuine
  inventory"), plus a large uncommitted working tree. Astra must run
  `git status --short --branch` and `git rev-parse HEAD` at intake and record both.
- Implementation state at the time of writing: `data/manifest/` does not exist and
  `tests/unit/test_split_proposals.py` is not present. Both are being written in
  parallel by Claude. Astra must inspect the actual files when they land; the
  contracts in section 3 are the spec's expectations, not observed behaviour.
- Ownership: Astra owns its new catalogue under `docs/qa/` and any new test module it
  creates. Astra must not edit `tests/unit/test_split_proposals.py`,
  `data/manifest/*.py`, the PRD, the architecture document or `current-state.md`;
  propose changes to those through the coordinator.
- No commits, pushes or PRs are authorised. No real manifests, audio or
  `.local/data-location.json` contents may be read into the catalogue or fixtures.

## 2. Requirement IDs and what each must be shown to hold

| ID | Canonical text (abridged) | What the tests must show for this task | Status for this task |
|---|---|---|---|
| FR-D8 (`prd.md` 5.1) | Speaker-disjoint 70/10/20 train/dev/test with a manifest; a script asserts zero speaker overlap and that each speaker's genuine and synthetic clips share a split. | Zero overlap on speaker, source recording, source hash and clip hash across every proposed partition; deterministic allocation close to 70/10/20 by speaker count with realised shares reported; an executable validator that fails nonzero on any overlap. | Partial: pilot proposals only. Test partitions (b) held-out generator, (c) held-out dialect and (d) real calls are out of scope; the spec excludes FR-D8 completion. |
| FR-D11 (`prd.md` 5.1) | Same-speaker rule: every synthetic clip's target voice is a consented speaker who also contributes genuine clips; recording-only consenters go to test only. | The grouping key co-locates genuine and synthetic rows that share `(source_id, speaker_id)`; the output states that cross-source identity is not merged and that no consent or reference mapping is known. The CSALT synthetic benchmark never enters train. | Pending product case: no consented participant data exists, so no test can establish FR-D11. Only the mechanism and the explicit limitation are testable now. |
| A4 (`prd.md` assumptions) | Speaker-disjoint splits are mandatory and each speaker's genuine and synthetic clips land in the same split. | Same evidence as FR-D8 plus speaker atomicity under adversarial row orders and across multiple input reports. | Testable with fictional rows. |
| D11 (`prd.md` decisions) | Fakes come from the same speakers as genuine clips and both classes pass through one processing script. | Only the placement half is in scope: a fictional speaker with both `label: genuine` and `label: synthetic` rows must receive one partition. Pipeline parity is covered elsewhere (`docs/qa/audio-conditioning-test-cases.md`). | Partial, as FR-D11. |
| ADR-14 (`architecture.md`) | Same-speaker fakes, one pipeline, leakage probes. | Identity must carry no label information across partitions: tests should show that a benchmark-only synthetic speaker ID can never be placed in train and that a genuine/synthetic pair for one speaker cannot straddle train and test. | Leakage probes (silence-only, duration-only classifiers) are not part of this task. |
| FR-E3 (supporting) | Speaker-level manifests live outside any repository. | CLI refuses an output root inside the repository for real runs; fixtures use temporary directories; no proposal content is written into Git. | Testable. |

Terminology to keep straight in every expected outcome: a **proposal** is a
reviewable placement with `status: proposal`; it is never an accepted train, dev or
test set, never a training-eligibility decision and never a consent record.

## 3. Module contracts the implementation is expected to expose

Derived from the spec's Implementation Notes. Astra should verify every name and
signature against the actual code and report mismatches as findings, not adapt
expected outcomes silently.

### 3.1 `data/manifest/propose_splits.py`

- Input: one or more processed `report.json` files as written by
  `data/prepare/prepare_audio.py` (`schema_version: 1`, keys `summary`, `sources`,
  `clips`), optionally plus raw pilot or balanced manifests for cross-checking.
  Each clip row carries at least `path`, `speaker_id`, `source_path`,
  `source_sha256`, `sha256`, `start_sample`, `end_sample`, `duration_seconds`,
  `review_status` and the `PROVENANCE_FIELDS` from `prepare_audio.py`, which
  include `source_id`, `source_version`, `source_recording_id`, `language`,
  `label`, `training_eligible`, `benchmark_only`, `publisher_split`,
  `original_split`, `use_scope`, `consent_status`, `cloning_status`.
- Bounded reads following existing conventions: 8 MB manifest limit (`MAX_MANIFEST_BYTES`)
  and 10,000-row limit as in `prepare_audio.preflight`; refuse an input directory
  containing an `INCOMPLETE` marker.
- Pure functions, no I/O:
  - group-key construction: `(source_id, speaker_id)` per clip, with
    `source_recording_id` as a secondary atomicity check;
  - partition assignment with rule precedence (1) `benchmark_only: true`, or
    `training_eligible: false` with a benchmark `use_scope`, goes to `benchmark_only`;
    (2) `publisher_split` or `original_split` in `{dev, test}` goes to
    `held_out_publisher_<split>` and is never reassigned; (3) everything else gets
    a deterministic 70/10/20 by speaker using `sha256(seed + source_id + speaker_id)`
    ordering within each source;
  - largest-remainder allocation on speaker counts per source, with at least one
    speaker per partition when the source has at least three speakers, never
    dropping a speaker;
  - validation (shared with the validator below).
- Output, written to a **new external directory** that must not already exist:
  `proposal.json` (original clip fields plus `proposed_split`, `proposal_id`,
  `proposal_seed`, `assignment_rule` in `{benchmark_only, publisher_held_out,
  hashed_speaker}`, `group_key`, `status: proposal`) and `proposal_report.json`
  (disjointness proofs with zero counts for speaker, recording, source-hash and
  clip-hash overlap; realised clip and minute shares per partition, per source,
  per language and per label; counts; a non-empty `limitations` list naming
  cross-source identity, consent and reference mapping as unknown; a statement that
  `training_eligible: false` placements are proposals, not approval).
- No overwrite: existing output root rejected before any write; staged write with
  an `INCOMPLETE` marker so an interrupted run never leaves a usable proposal.
- No audio is opened, moved or modified.

### 3.2 `data/manifest/validate_splits.py`

- Importable function that takes a loaded proposal (and ideally the report) and
  returns structured findings or raises; CLI wrapper that prints findings and exits
  nonzero on any failure, zero only when every check passes.
- Must reject: cross-partition overlap on speaker, recording, source hash or clip
  hash; any benchmark row outside `benchmark_only`; any publisher dev/test row with
  a different partition; any `status` other than `proposal` (no acceptance record
  exists yet, so `accepted` must also fail); any missing lineage field; and
  malformed or oversize input.
- Must not modify the proposal file.

## 4. Fixture constraints

- Fictional rows only. Use obviously invented identifiers such as
  `fx_source_alpha`, `fx_spk_01`, `fx_rec_01_a`. Do not reuse publisher labels such
  as CSALT folder names or Meta `pnb_Arab` speaker labels, and do not copy any value
  from a real external `report.json`.
- Hashes: compute `sha256` of fictional literal strings inside the test (for example
  `hashlib.sha256(b"fx_clip_01").hexdigest()`); never paste digests from real
  reports. Duplicate-hash cases should construct the collision deliberately.
- No audio: the proposer reads only JSON, so no WAV files are needed. If a case must
  prove that audio is not touched, create an empty sentinel file under `clips/` and
  assert its bytes and mtime are unchanged.
- Temporary directories only (`tempfile.TemporaryDirectory`), cleaned up on exit.
  Nothing under the repository tree or `.local/`. Fixture report files should stay
  small (kilobytes) except for the one deliberate oversize case, which can be
  generated in memory and written once.
- Stdlib only, so that the cases run in lightweight CI without
  `requirements-audio.txt`.
- No network, no reads of `.local/data-location.json`, no environment-specific paths
  in assertions.

## 5. Risk areas Astra should target

Design from the spec and these risks, not from the implementation's happy path.
Where the spec is silent, record a specification gap with the candidate
interpretations rather than inventing an expected value.

1. **Speaker atomicity under adversarial order.** Same fictional speaker's clips
   reversed, shuffled with a fixed seed, interleaved between two sources, and split
   across two input `report.json` files with the same `source_id`. Expected: one
   partition per `(source_id, speaker_id)` regardless of order and identical
   `proposed_split` values across all orderings for the same seed.
2. **Recording atomicity and lineage conflicts.** Clips sharing a
   `source_recording_id` but carrying two different `speaker_id` values (malformed
   lineage). Expected per spec: a recording can never straddle partitions; whether the
   proposer rejects the input or the validator rejects the output is a specification
   gap to record. Silent acceptance is a defect.
3. **Benchmark leakage.** `benchmark_only: true` rows always land in
   `benchmark_only` even when `publisher_split` is `train` and even when the same
   speaker ID also appears on genuine rows (rule 1 precedes rule 2 and 3).
   `training_eligible: false` with a non-benchmark scope must *not* be forced into
   `benchmark_only`. A benchmark clip whose `source_sha256` equals a genuine clip's
   hash in another partition must be reported as overlap.
4. **Publisher-split preservation and its interaction with speaker atomicity.**
   Rows with `publisher_split: dev` or `test` keep `held_out_publisher_dev/test`.
   Gap to probe: the same speaker appears in publisher `train` and publisher `dev`
   rows of one source. The spec forbids both reassigning the dev rows and splitting
   the speaker; the valid outcomes are "train rows follow the held-out partition" or
   "reject with a clear message". The validator must at least detect the overlap.
5. **Largest-remainder allocation edge cases at 1, 2, 3, 6 and 17 speakers.**
   Quotas at 70/10/20: 1 speaker gives 0.7/0.1/0.2; 2 gives 1.4/0.2/0.4; 3 gives
   2.1/0.3/0.6; 6 gives 4.2/0.6/1.2; 17 gives 11.9/1.7/3.4. Under plain Hamilton
   largest remainder these resolve to 1/0/0, 1/0/1, 2/0/1, 4/1/1 and 12/2/3, and the
   spec's minimum-one rule then overrides the 3-speaker case to 1/1/1. Astra should
   derive these independently rather than accept the figures here, and should also
   add 4 and 5 speakers, where remainders tie (0.8/0.4/0.8 and 0.5/0.5/0.0) and the
   spec names no tie-break order, which is a gap. Which partition receives a single
   or pair of speakers is unspecified and must be stated in the report. Every speaker
   must appear exactly once; sum of partition counts equals speaker count.
6. **Hash-seed stability across Python versions and processes.** Assignment must
   depend only on SHA-256 of UTF-8 text, never on `hash()`, `set` iteration order or
   `random`. Run the same fixture under at least two `PYTHONHASHSEED` values and,
   where available, Python 3.11 and 3.12, and assert byte-identical `proposal.json`
   minus timestamps if any. Also probe concatenation ambiguity: with no delimiter,
   `seed="1", source_id="2x"` and `seed="12", source_id="x"` produce the same digest.
   The spec writes `sha256(seed + source_id + speaker_id)`; whether a delimiter or
   structured encoding is used is a gap worth a finding. Changing the seed must change
   at least one hashed assignment for a fixture large enough to make that
   overwhelmingly likely, while benchmark and publisher rows stay fixed.
7. **Duplicate source hashes across sources.** Identical `source_sha256` or clip
   `sha256` under two different `source_id` values with different speakers. Expected:
   overlap reported and, if the partitions differ, rejection. Within-partition
   duplicates are not forbidden by the spec; record that as a gap and assert they are
   at least counted in the report.
8. **Missing or malformed lineage fields.** Absent, `null`, empty-string, wrong-type
   or uppercase-hex values for `speaker_id`, `source_recording_id`, `source_id`,
   `source_sha256`, `sha256`, `benchmark_only`, `training_eligible`,
   `publisher_split`/`original_split`, and `label`; non-finite or negative
   `duration_seconds` (minute shares must not become NaN); `benchmark_only` as the
   string `"true"`. Expected: proposer refuses before writing anything; validator
   rejects a proposal that lacks any of these.
9. **Oversize and hostile inputs.** Report file one byte over the 8 MB bound,
   10,001 clip rows, more input files than a reasonable limit, deeply nested JSON,
   non-object clip rows, empty `clips` list. Expected: fast failure, no output
   directory created, no partial `proposal.json`.
10. **`INCOMPLETE` markers and no-overwrite behaviour.** Input directory with an
    `INCOMPLETE` marker refused; pre-existing output root refused without modifying
    its contents (verify a sentinel file is untouched); output root inside the input
    directory or inside the repository refused for real runs; a fault injected after
    the output directory is reserved but before the final rename leaves the
    `INCOMPLETE` marker and no readable `proposal.json`; two sequential runs to the
    same root yield exactly one success. A concurrency case (two processes racing
    for `mkdir` on the same root) is in scope if it can be made deterministic;
    otherwise mark it pending.
11. **Validator exit codes and independence.** Exit 0 only for a clean proposal;
    nonzero for each failure class above, for an unreadable or oversize file and for
    a mismatch between `assignment_rule` and the row's flags (for example a
    `benchmark_only` row labelled `hashed_speaker`); usage errors distinguishable
    from validation failures; findings printed with row identifiers; the proposal
    file's bytes unchanged after validation. The validator must not import the
    proposer's assignment function to recompute the answer it is checking; it must
    check the stated invariants directly.
12. **Status and acceptance.** `status: accepted`, missing `status`, or a
    `proposed_split` outside the allowed set are rejected. Nothing in the pipeline
    may turn a proposal into an accepted set.
13. **Report honesty and privacy boundary.** `limitations` is non-empty and names
    cross-source identity, consent mapping and reference mapping; the report states
    that `training_eligible: false` rows are placements only; `proposal.json`
    contains no absolute machine paths, user names or values from `.local/`;
    per-source, per-language and per-label shares are present and sum to the totals.
14. **Multiple-report integration.** Three fictional reports standing in for CSALT
    genuine, CSALT synthetic benchmark and Meta `pnb` pilot, with a shared speaker
    label string across two sources (for example `fx_spk_01` in both). Expected:
    treated as two distinct speakers because keys are source-namespaced, and the
    report says identities are not merged.

## 6. Baseline tests already being written by Claude

`tests/unit/test_split_proposals.py` is a Claude-authored baseline covering, per the
spec: order independence, seed determinism, speaker and recording atomicity,
benchmark and publisher-split preservation, allocation with 3, 6 and 17 fictional
speakers, rejection of overlapping or mislabelled proposals, and no-overwrite
output. Astra should read that module first, cite which baseline test (if any)
already covers each risk, and **extend rather than duplicate**: deeper adversarial
variants, the specification gaps in section 5, the 1, 2, 4, 5-speaker boundaries,
`PYTHONHASHSEED` and version stability, oversize and fault-injection paths, and the
validator exit-code matrix. To respect file ownership, put new executable cases in
a separate module (suggested `tests/unit/test_split_proposals_astra.py`) unless the
coordinator assigns the baseline file to Astra. Label each case with its actual
author.

## 7. Exact commands Astra should run

Run from the repository root. PowerShell shown first, bash in comments where it differs.

```powershell
git status --short --branch
git rev-parse HEAD

# Baseline and Astra unit modules (stdlib only; no audio requirements needed)
uv run --no-project --python 3.11 python -m unittest tests.unit.test_split_proposals -v
uv run --no-project --python 3.11 python -m unittest tests.unit.test_split_proposals_astra -v

# Hash-seed stability: same tests under two interpreter hash seeds
$env:PYTHONHASHSEED = '0';     uv run --no-project --python 3.11 python -m unittest tests.unit.test_split_proposals_astra -v
$env:PYTHONHASHSEED = '31337'; uv run --no-project --python 3.11 python -m unittest tests.unit.test_split_proposals_astra -v
Remove-Item Env:PYTHONHASHSEED
# bash: PYTHONHASHSEED=0 uv run ... ; PYTHONHASHSEED=31337 uv run ...

# Optional cross-version check if a 3.12 interpreter is available (repo pins 3.11)
uv run --no-project --python 3.12 python -m unittest tests.unit.test_split_proposals_astra -v

# Full unit suite as in README (expect the two known Windows symlink skips)
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python -m unittest discover -s tests/unit -v

# Fixture-driven CLI round trip in a temporary directory (confirm flag names from the actual argparse)
$tmp = Join-Path $env:TEMP ("ava-split-" + [guid]::NewGuid())
uv run --no-project --python 3.11 python data/manifest/propose_splits.py --report "$tmp/report.json" --seed fx-seed-1 --output-root "$tmp/proposal"
uv run --no-project --python 3.11 python data/manifest/validate_splits.py "$tmp/proposal/proposal.json"; "exit=$LASTEXITCODE"
# bash: ...; echo "exit=$?"

# Repository hygiene before handoff
uv run --no-project --python 3.11 python scripts/check_foundation.py
uv run --no-project --python 3.11 python scripts/repository_guard.py
```

Record every command, its exit code, counts of passed, failed and skipped tests, and
any command that could not be run (for example no Python 3.12). Do not retry a
failing test to obtain a pass.

## 8. Deliverables requested from Astra

1. `docs/qa/split-proposal-test-cases.md`: catalogue in the format of
   `docs/qa/pnb-pilot-test-cases.md` (provenance line with model, date and
   revision; scope paragraph; fixture paragraph; table with case ID, requirement
   and layer, inputs and expected evidence, executable test name; execution
   section with actual results; limitations paragraph). Each case needs ID, FR/ADR
   reference, priority, layer, preconditions, action, precise expected outcome,
   evidence to retain, cleanup, automation status and known limitation, per
   `docs/agents/test-designer.md`.
2. New executable cases in a separate test module as described in section 6.
3. `docs/context/handoffs/split-proposal-test-design.md` using
   `docs/context/handoffs/TEMPLATE.md`, listing findings with severity and file
   references, specification gaps separated from implementation bugs, and proposed
   spec or PRD wording for the coordinator.

## 9. What passing tests would and would not establish

Would establish, for fictional fixtures only:

- The proposer places every fictional speaker, recording and derived clip in exactly
  one partition, deterministically from the seed, independent of row order and
  interpreter hash seed.
- Benchmark-only and publisher dev/test rows are never moved into a training
  partition.
- The validator detects the specified overlap, mislabelling, status and missing
  lineage failures and exits nonzero.
- Output is external, non-overwriting and leaves no usable artefact after an
  interrupted run.

Would not establish:

- That any real speaker ID refers to one person, or that the same person does not
  appear under two sources or two labels: no identity verification is performed and
  the proposal explicitly does not merge identities across sources.
- Consent, cloning authorisation or ethics coverage for any row; `consent_status`
  and `cloning_status` are carried through, not evaluated.
- Training eligibility: all current pilot rows are `training_eligible: false` and a
  proposed placement is not approval.
- FR-D11 or ADR-14 compliance for the eventual dataset: no consented speaker has
  both genuine and synthetic clips yet, no leakage probes run, and codec copies and
  synthetic lineage from participants do not exist.
- Near-duplicate or cross-source duplicate detection beyond exact SHA-256 equality.
- Anything about audio content, listening quality or model behaviour.

## 10. Unresolved questions for Astra to carry as specification gaps

- Rule 2 versus speaker atomicity when one speaker spans publisher `train` and `dev`
  rows (section 5, item 4).
- Tie-break order in largest-remainder allocation and the destination partition for
  sources with one or two speakers (item 5).
- Delimiter or encoding in `sha256(seed + source_id + speaker_id)` (item 6).
- Whether within-partition duplicate hashes are an error or a reported count (item 7).
- Whether a recording with conflicting speaker IDs is rejected at proposal time or
  validation time (item 2).
- The spec names no explicit limit on the number of input reports; the bounded-read
  convention suggests one is needed (item 9).

Astra should propose a resolution for each, mark the related cases pending until the
coordinator decides, and must not encode a guessed resolution as a passing assertion.
