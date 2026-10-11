# Task handoff: reproducible Punjabi pilot verifier (todo 7d, review finding M2)

- Task and issue: Todo 7d part M2 from the 7b review. Replace the machine-local
  `.local/verify_pnb_pilot.py` prototype with a parameterised, tested verifier in
  `scripts/` so the Punjabi pilot evidence can be reproduced from the repository.
  Related issues: #6 (data access), #11 (intake). Mid-task coordinator addition
  from the 7c review: record the verifier's own SHA-256, dependency versions and
  per-file transcript hashes in `verification.json`, and compare transcript hashes
  against a prior `verification.json` when one carries them.
- Owner and actual model used: ava-implementer role, Claude (Claude Code, model
  Fable 5.1). The test module is Claude-authored and says so in its docstring;
  it is not a GPT-6 Astra design. Astra was not consulted for this task.
- Worktree, branch and base commit: this checkout, `feat/11-pilot-audio-preparation`,
  HEAD `ad13029fa283183c23e84d736ac424b777e775c3`, pre-existing dirty tree preserved.
  No commits, pushes, stashes, resets or discards.
- Owned files: `scripts/verify_pnb_pilot.py` (new, 253 lines),
  `tests/pnb/test_verify_pnb_pilot.py` (new, 248 lines), this handoff. Inspected
  only: `data/collect/meta_pnb_pilot.py`, `data/collect/PNB_README.md`,
  `data/collect/csalt_pilot.py` (`inspect_audio` metric keys),
  `tests/pnb/test_meta_pnb_pilot.py`, `.local/verify_pnb_pilot.py`,
  `scripts/check_foundation.py`, `scripts/repository_guard.py`.
- Requirement and ADR IDs: FR-D1 (source intake), FR-D8 (lineage), FR-D13
  (decode and signal checks), FR-E3 and FR-E6 (external storage, nothing private
  in Git); PRD D13 and ADR-14 context, as for todo 7b.

## Implemented changes

`scripts/verify_pnb_pilot.py` is a read-only CLI that is independent of the
importer (it does not import `meta_pnb_pilot`; it re-implements the documented
selection formula `sha256(f"{revision}/{shard}#{row_index}")` and the clip-ID
derivation). Arguments: `--source-manifest`, `--archive-root`, `--raw-root`,
`--manifest-root`, `--expected-archive-sha256` (required, 64 hex; the publisher
LFS hash from the preflight so `source.json` cannot self-certify), `--per-speaker`
(default 2, bounded 1..30), `--expected-speakers` (optional), `--write-verification`
(writes `verification.json` into the manifest root with `open('x')`; refuses up
front if it already exists; writes nothing on failure).

Behaviour: refuses under `python -O` via `__debug__` (exit 2) and contains no
bare `assert` (a test greps the source). Failures are collected and printed on
one `FAIL` line as check names with counts, exit 1. Fifteen aggregate checks:
no `INCOMPLETE` markers; archive SHA-256 equals the expected hash; archive size
and `source.json` hash fields agree with the recomputed hash; revision consistent
across source, pilot and report; source-manifest hash; `pilot_complete`; row
counts across pilot, inventory and report with zero errors; metadata row count;
selection recomputed from all shard rows; speaker count (and `--expected-speakers`);
equal per-speaker allocation; every selected row re-read; minutes equal the
report; exact file counts in `flac/`, `wav/`, `transcripts/`; archive hash
unchanged after the re-read; and, only when a prior `verification.json` with
`transcript_sha256` exists, equality of the per-file transcript hashes.
Seventeen per-row checks in the pure `compare_row`: FLAC bytes equal the embedded
payload; FLAC hash; WAV hash; WAV params (rate, channels, 16-bit, frames); int16
frames byte-equal to an independent soundfile decode; row rate/channels/bits;
declared duration within 0.1 s; row `duration_s`/`duration_seconds`; transcript
`raw_text` and `prompt`; `alignment == source_recording_only` plus recording ID
and revision; `meta_pnb_` speaker namespacing; recording and clip-ID derivation;
derived row paths; prompt/segment/glottocode; shard language fields; archive
lineage; and the fifteen policy fields (`training_eligible` false as a bool,
`cloning_status not_authorized`, `consent_status not_individually_verified`,
`dialect unknown`, `split unassigned`, publisher/original/partition `train`,
`label genuine`, `label_provenance publisher`, `listening_status
not_individually_reviewed`, `validation_status verified`, language fields,
`use_scope`). Reads are bounded (JSON 8 MB, FLAC/WAV 10 MB, transcript 1 MB,
10,000 rows); only selected rows' audio is materialised, in batches of eight.
Stdout carries aggregates only; row-level identifiers appear nowhere on stdout.
`verification.json` (on request) holds `result`, `scope`, `environment`
(verifier file SHA-256, pyarrow/soundfile/numpy versions via `importlib.metadata`),
`summary`, and `transcript_sha256` keyed by `transcripts/<clip_id>.json` with no
transcript text.

`tests/pnb/test_verify_pnb_pilot.py` builds one fictional pilot per class: a
six-row Parquet shard (two fictional speakers, three rows each, 4 s square-wave
FLAC at 20 kHz, distinct per row) imported by the real `run_pilot` with
`per_speaker=2`, so recomputed selection genuinely drops rows. Each test copies
the raw and manifest roots. Cases: clean pass with the aggregate line and no
identifiers; selection formula; wrong and malformed expected hash; wrong expected
speaker count; tampered WAV byte; tampered FLAC byte; tampered transcript text;
flipped policy fields (three subtests); `INCOMPLETE` in either root; missing raw
file; write once then refuse; failed run writes nothing; refuses under `-O` via
subprocess and no bare assert; CLI subprocess pass; prior transcript hashes
matched, then whitespace-only transcript re-serialisation fails only the prior-hash
check, then a prototype-format prior without hashes yields `recorded`.

## Exact validation commands and results

1. `uv run --no-project --python 3.11 --with-requirements requirements-pnb.txt python -m unittest discover -s tests/pnb -p test_verify_pnb_pilot.py -v`
   : 15 tests, all passed, 8.1 s.
2. `uv run --no-project --python 3.11 --with-requirements requirements-pnb.txt python -m unittest discover -s tests/pnb -v`
   : 28 tests (13 Astra importer tests plus 15 new), all passed, 13.8 s.
3. Real pilot, read-only (no `--write-verification`):
   `uv run --no-project --python 3.11 --with-requirements requirements-pnb.txt python scripts/verify_pnb_pilot.py --source-manifest D:/AVA-Data/source-archives/meta-pnb/source.json --archive-root D:/AVA-Data/source-archives/meta-pnb --raw-root D:/AVA-Data/raw/meta-pnb-pilot-v1 --manifest-root D:/AVA-Data/manifests/meta-pnb-pilot-v1 --expected-archive-sha256 d8ad5551672394f4f9995ffa3f71f3065f611a121f44ca1170a2780fde4f812d --expected-speakers 6`
   : exit 0. Aggregate line:
   `PASS rows=12 speakers=6 per_speaker=2 minutes=13.0646 native_formats=48000Hz/1ch:12 transcript_hashes=recorded aggregate_checks=15`
   The manifest and raw roots were unchanged afterwards (same four manifest files,
   `verification.json` still the prototype's from 18:52 local time, 12 files in
   each of `flac/`, `wav/`, `transcripts/`). An earlier run of the pre-addition
   script gave the same line without the `transcript_hashes` field.
4. `uv run --no-project --python 3.11 python scripts/check_foundation.py`
   : passed (16 adapters, 29 skills / 214 files, 8 roles).
5. `uv run --no-project --python 3.11 python scripts/repository_guard.py`
   : passed (576 paths checked).

## Test environment and fixture versus product scope

Windows 11, Python 3.11 via uv with `requirements-pnb.txt` (pyarrow 21.0.0,
soundfile 0.13.1, numpy 2.3.5, soxr). CPU only; no network. The 15 tests use
fictional Parquet/FLAC fixtures and are infrastructure evidence for the verifier.
The real run reproduces the 7b verification from the repository; it is source
intake evidence, not listening QA, dialect, identity, consent or training
eligibility evidence, and not product acceptance.

## Failures, limitations and untested behavior

- No failures.
- Length: 253 lines against the "about 220" guidance. The three coordinator
  additions account for roughly 12 lines; the remainder is explicit check names.
- Single archive only: `--expected-archive-sha256` takes one value, so the
  verifier requires exactly one `files` entry in `source.json` and fails with
  `single_archive_source_manifest_required` otherwise. A two-shard pilot would
  need a repeatable hash flag and per-shard metadata keys (the internal keys are
  already `(shard, index)`).
- On this machine the prior-hash comparison is inactive: the existing
  `verification.json` is the prototype's and has no `transcript_sha256`, so the
  run reports `recorded`. The comparison becomes live once a `verification.json`
  written by this script exists in a manifest root. Writing one here needs a
  decision on the prototype file (move aside or use a fresh manifest root); not
  done, since the task was read-only.
- `--write-verification` writes only on a full pass; failed runs leave no record
  file. Failure output is stdout only.
- Durations are compared with `abs(row - frames/rate) < 1e-9`, which assumes the
  importer stored the exact float; the real run confirms this for 12 rows.
- Not tested: two-channel or 48 kHz fixtures (the real run covers 48 kHz mono),
  oversized inputs hitting the byte bounds, Parquet with more than 10,000 rows.
- The `.local/verify_pnb_pilot.py` prototype was neither imported nor removed.

## Proposed PRD, architecture or context updates

- `docs/context/todo.md`: mark 7d part M2 done; L3, L4, L7, L8 importer
  follow-ups remain open.
- `docs/context/current-state.md`: replace the "machine-local verifier" wording
  under todo 7 with the repository command above and its aggregate line.
- `data/collect/PNB_README.md` (not owned here): add a one-paragraph pointer to
  `scripts/verify_pnb_pilot.py` and the publisher-hash argument.
- `README.md` command list: add the verifier test command.
- No PRD or architecture change.

## Next action and receiving owner

Coordinator (Claude) in this checkout: integrate the two new files and this
handoff, decide whether to write a fresh `verification.json` with the new script
(fresh manifest root or move the prototype's file aside) so the transcript-hash
comparison becomes active, and schedule the remaining 7d importer follow-ups.
