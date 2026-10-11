# Task handoff: Punjabi pilot import (todo 7b)

- Task and issue: Todo 7b, import a small Pakistani Punjabi `pnb_Arab` pilot from
  one verified Meta train shard. Related issues: #6 (data access), #11 (intake;
  its Common Voice pa-PK wording is superseded by PRD v0.2).
- Owner and actual model used: coordinator role, Claude (Claude Code). Test cases
  were designed earlier by GPT-6 Astra; Claude ran them and did not author them.
- Worktree, branch and base commit: this checkout, `feat/11-pilot-audio-preparation`,
  HEAD `ad13029fa283183c23e84d736ac424b777e775c3` with the pre-existing dirty tree
  preserved. No commits, pushes, PRs, merges or discards.
- Owned files: this handoff; `docs/context/todo.md` and `current-state.md` updates;
  `.local/verify_pnb_pilot.py` and `.local/data-location.json` (machine-local,
  ignored). No importer or test code was changed.
- Requirement and ADR IDs: FR-D1 (source intake), FR-D8 (lineage), FR-D13
  (decode and signal checks), FR-E3 and FR-E6 (external storage, nothing private
  in Git); PRD D13 and ADR-14 context.

## Implemented changes

No code changes. The already-tested importer `data/collect/meta_pnb_pilot.py` was
run for the first time against real data, and an independent verifier was written
and run. Approval status: Tughral reported institutional approval on 4 October
2026; the reference and scope are not yet recorded in the repository. This import
is public-dataset intake under the publisher's CC BY 4.0 terms and does not
depend on that approval; it grants no cloning consent.

## Exact validation commands and results

1. `uv run --no-project --python 3.11 --with-requirements requirements-pnb.txt python -m unittest discover -s tests/pnb -v`
   : 13 tests passed, no skips, 7.0 s.
2. `uv run --no-project --python 3.11 python scripts/check_foundation.py`
   : passed (16 adapters, 29 skills / 214 files, 8 roles).
3. `uv run --no-project --python 3.11 --with-requirements requirements-pnb.txt python data/collect/meta_pnb_pilot.py --source-manifest <archive>/source.json --archive-root <archive> --raw-root <new raw root> --output-root <new manifest root> --per-speaker 2 --max-sources 30`
   : exit 0, `pilot_complete: true`, 12 sources, 6 publisher speakers with 2 each
   (spk01, spk04, spk06, spk09, spk10, spk11), 13.0646 minutes, 210 metadata rows
   scanned, 0 errors, 52 s wall time. Outputs: `flac/`, `wav/`, `transcripts/`
   (12 files each) in the raw root; `inventory.json`, `report.json`, `pilot.json`
   in the manifest root; no `INCOMPLETE` markers.
4. `uv run --no-project --python 3.11 --with-requirements requirements-pnb.txt python .local/verify_pnb_pilot.py`
   : exit 0, 13 aggregate checks passed; wrote `verification.json` beside
   `report.json`. Independently of the importer it recomputed the archive SHA-256
   (464,239,672 bytes, matches the publisher LFS hash before and after), re-read
   all 210 metadata rows, recomputed the deterministic per-speaker selection and
   found the identical 12 row indices, re-read the 12 embedded FLAC payloads
   straight from the Parquet shard and compared them byte-for-byte to the stored
   originals, decoded each with soundfile and compared int16 samples to the stored
   WAV frames, checked WAV and FLAC hashes against the rows, checked durations
   against declared metadata, checked transcript files against the shard's
   `raw_text` and `prompt`, checked speaker, prompt, segment and glottocode fields,
   the `meta_pnb_` namespacing, recording-ID and clip-ID derivation, and every
   policy field. All 12 originals are 48 kHz mono PCM16 FLAC of about 58 to 80 s
   (measured 57.98 to 80.02 s); decoded WAVs are 5.57 to 7.68 MB, under the 10 MB bound.

External locations are recorded in `.local/data-location.json` on this machine.

## Test environment and fixture versus product scope

Windows 11, Python 3.11 via uv with `requirements-pnb.txt` (pyarrow, soundfile,
numpy, soxr). The 13 Astra tests use fictional Parquet and FLAC fixtures; the
real run and the verifier cover the one real shard. CPU only; no network during
import or verification. This is source intake evidence, not product acceptance.

## Failures, limitations and untested behavior

- No failures. The importer's own pre- and post-extraction archive checks and the
  verifier's checks agree.
- Not established: dialect (Majhi or otherwise), speaker identity beyond publisher
  labels, individual consent, cloning permission, audible quality, language
  correctness by ear, or training eligibility. `training_eligible` is false and
  `split` is unassigned on every row.
- Source transcripts apply to whole recordings only; no alignment to future cuts.
- Six publisher speakers from one shard is a pilot, not Punjabi coverage. The
  second train shard, dev and test were not fetched.
- Only `review_flags` present on every row are `requires_format_processing` (48 kHz)
  and `requires_duration_processing` (about one minute each); no low-RMS, quiet or
  clipping flags were raised by the whole-file screen.
- Independent review (ava-reviewer role, Claude, 4 October): **PASS, no blocking
  findings.** The reviewer re-ran the 13 tests (13 passed) and its own read-only
  recomputation by a different path (row-group iteration, FLAC decoded from disk):
  20 of 20 checks passed, including the archive hash against the preflight-published
  constant, the same 12 row indices, exact FLAC/WAV hashes and frames, transcript
  key sets, no stray files or temp directories, and all policy fields.
  Findings to act on: M1, the verifier used bare `assert` and would pass vacuously
  under `python -O` (fixed the same day: `__debug__` guard, explicit failure exit,
  PASS printed only when every check passed); M2, the verifier is machine-local
  and unparameterised, so the evidence is not reproducible from the repository
  (recorded as todo 7d: move a parameterised verifier to `scripts/` with a
  fictional-fixture test and reuse it for later shards); L1, the verifier could
  overwrite `verification.json` (fixed: refuses if present); L2, expected archive
  hash came only from `source.json` (fixed: pinned constant); L3, the importer
  feeds its own digest to the shared inspector's publisher-hash branch, which is
  vacuous there, and stores `review_flags` as a JSON string; L4, declared-duration
  bound (625 s) exceeds what the 10 MB WAV bound permits (about 104 s at 48 kHz),
  so a long row could withhold a pilot with no deterministic recourse, not
  triggered here; L6, `use_status: approved` needed its basis stated (done in
  `PNB_README.md`); L7, CLI exit codes and 48 kHz or stereo fixtures untested;
  L8, archive root not required to be outside the repository. L3, L4, L7 and L8
  are importer follow-ups under todo 7d.

## Proposed PRD, architecture or context updates

- `current-state.md`: record 7b complete with the numbers above and the limits.
- No PRD change. FR-D1's Meta Omnilingual pnb_Arab source is now exercised.
- PRD Q1 (Meta licence and derivative synthesis) remains open; this import is
  genuine-only detection intake.

## Todo 7c: conditioned preparation (same task, same day)

Command: `uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python data/prepare/prepare_audio.py --condition --manifest <manifest root>/pilot.json --source-root <raw root> --output-root <new processed root>`
: exit 0, 12 sources, 187 clips, 762.9929 s retained, 20.884 s uncovered,
10 review-flagged clips, outcomes 7 full coverage / 5 partial coverage / 0 too
short / 0 no valid pause / 0 all quiet, 0 errors, 66 s wall time. No `INCOMPLETE`.

Independent verifier `.local/verify_pnb_prepared.py` (Claude, today): exit 0.
For every source it rehashed the original FLAC and decoded WAV against the pilot
rows, recomputed the canonical 16 kHz PCM (mono mean, soxr HQ, round and clip)
and matched `canonical_pcm_sha256`, sample counts and saturation counts; for
every clip it sliced the canonical signal at the recorded offsets, applied the
−3 dBFS peak gain, and matched the output bytes, hash, format, frame count, peak
and recorded gain; it checked 31 lineage and policy fields identical across pilot
row, source row and clip row, that no transcript text was copied onto clip rows,
that retained plus uncovered ranges tile each source without gaps, that output
hashes are unique, and that the report summary equals the recomputed totals.
Clips per speaker: spk01 26, spk04 31, spk06 27, spk09 34, spk10 39, spk11 30.
Wrote `verification.json` beside the processed `report.json`.

Regression: `uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python -m unittest discover -s tests/unit -v`
: 104 tests, 102 passed, 2 skipped (Windows symlink creation denied, pre-existing).

Limits: candidates only (`review_status: unreviewed`); the pause heuristic is
not speech detection; all 10 flags are `mostly_quiet` (quiet fraction 0.51 to
0.90; no low-RMS, clipping or saturation flags anywhere) and request targeted
listening, three of them being leading-edge clips that pack initial quiet into
the first candidate; no telephone-band simulation, codec augmentation, VAD or
split assignment; dialect, consent, identity and eligibility unchanged from 7b.

Independent review of 7c (ava-reviewer role, Claude, 4 October): **PASS, no
blocking findings.** The reviewer recomputed all 12 sources and all 187 clips
from the FLAC originals (not the WAV path the verifier used), re-implemented the
pause-boundary segmentation and retained-sample selection independently and
obtained identical ranges and outcomes, recomputed every metric and flag, checked
report values for any transcript text or Arabic-script code point (none), and
confirmed root contents, hashes, lineage and policy fields. Findings: M1, the
verifier checked tiling rather than recomputing segmentation and counted rather
than recomputed flags, scanned keys not values, and read WAV not FLAC (covered by
the review for this run; folded into todo 7d for the parameterised verifier);
L1, one of two full unit-suite runs hit an intermittent Windows temporary-
directory cleanup error (`WinError 145`) after assertions passed, so the 104/102/2
figure is reproducible but not deterministic; L2, the hardened verifier was not
re-run and its hash is not recorded in `verification.json` (immaterial here, both
guards only add refusals; fix in 7d); L3, the flag wording above, corrected; L4,
no per-source `uncovered_seconds` field, only ranges (derivable); L5, transcript
files are shown unchanged by mtime and key set only, since 7b recorded no
transcript hashes (7d records them); L6, informational, edge-quiet packing.

## Next action and receiving owner

Todo 8: collection preparation. Reconcile `docs/data-collection/approval-pack.md`
with the approved protocol once Tughral supplies its reference and scope; until
then build the collection kit (session script, prompt allocation placeholders,
intake flow into `data/collect/intake_template.csv`, per-member allocation for
Tughral, Ibrahim, Ilsa and Ayesha) without inferring approved details. Then
todo 9: reproducible speaker/lineage-disjoint split proposals. Receiving owner:
coordinator (Claude) in this checkout.
