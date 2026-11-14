# Task handoff: prepared-output verifier (backlog 1.1, todo 7d)

- Task: backlog 1.1 / todo 7d second bullet, from the 7c review medium finding
  (the 7c verifier checked tiling and counted flags instead of recomputing them).
  Issue #11 (ingest and normalize). Requirements: FR-D12 (one conditioning path, partial),
  FR-D8 (lineage), FR-D13 (signal checks), FR-E3/FR-E6 (nothing private in Git).
- Author: Claude (Claude Code, Opus 5.5), scheduled session 9 November 2026.
  Tests are Claude-authored and labelled so; no Astra design was used.
- Owned files: `scripts/verify_prepared_output.py`,
  `tests/unit/test_verify_prepared_output.py`, `data/prepare/README.md`
  (verification paragraph), this handoff.

## Behaviour

Read-only CLI: `--manifest`, `--source-root`, `--output-root`, `--mode
condition|exact`, repeatable `--allow-extra NAME` for earlier evidence files in
the output root. It does not import `data/prepare`. Per source it re-reads and
hashes the WAV, re-derives the canonical 16 kHz PCM (mono mean, soxr HQ,
round-half-even, clip), the conditioning record, metrics and flags, the pause
boundaries and the maximum retained-sample total. Claimed clips must be ordered
spans between recomputed boundaries of 3–6 s whose total equals that optimum, so
the check does not rely on the preparer's tie-breaking. Each clip is rebuilt
(slice, peak normalization in condition mode, WAV header) and compared byte for
byte; its metrics, pre-normalization metrics, normalization record, flags,
lineage and provenance are compared with type-strict equality and no extra
fields. Rejected sources (hash change, unsupported format, missing) must be
quarantined without clips. Aggregate checks: no `INCOMPLETE` marker in source,
output or manifest directory; schema and mode policy; manifest SHA-256 and
revision; source order; summary counts, seconds and outcomes; output root holds
only `report.json`, `clips/` and allowed extras; `clips/` holds exactly the
reported files; original audio hashes (e.g. FLAC) when rows name them; no
transcript text (`raw_text`, `prompt`, …) and no Arabic-script code point in any
report key or value. Refuses under `python -O`; no bare asserts; bounded reads;
stdout is one aggregate line.

Metrics are compared for equality, not tolerance: PCM16 squares divided by 2^30
are dyadic rationals whose float64 partial sums are exact below 2^23 samples, so
summation order cannot change the result; longer sources are reported as
unreadable rather than compared approximately.

## Independent review

An ava-reviewer pass (read-only; its shell was unavailable, so it reviewed by
reading) returned a conditional pass: clip integrity sound, boundary and optimum
logic and the exact-sum argument confirmed. Fixed the same session:

- M1–M3: source records, quarantined records, `report.json`, `summary` and
  `conditioning` are now compared as exact key sets with deep type-strict
  equality (`strict_equal`), so injected fields or flipped policy flags fail;
  quarantined records must carry an `error` and only lineage fields; library
  versions are a separate `conditioning_library_versions` check.
- M4: malformed nested values (lists for dicts, unhashable `source_path`, deep
  nesting) give a named `FAIL` line instead of a traceback; manifest rows must
  pass preparation's preflight rules (`manifest_rows_not_preparable`).
- Lows fixed: exactness bound is now the integer sum of squares (< 2^53), so long
  8 kHz sources are not falsely rejected; symlink/junction components and
  intermediate `INCOMPLETE` markers are rejected like preparation does; summary
  seconds are recomputed with preparation's float operations in the same order
  and compared exactly; transcript scan walks nested keys and matches after NFKC,
  case and whitespace normalization; wider Arabic-script ranges without the BOM;
  `best_retained` prunes spans over 6 s; `original_audio_sha256` must equal
  `sha256` when the original path is the source path; `clips/` and `report.json`
  must not be links.
- Not changed: partial transcript excerpts are not detected (whole strings only);
  FLAC originals are hashed, not decoded (the import verifier already compares
  decoded FLAC with the WAV); Python 3.12 `wave` differences are out of scope.

## Evidence

- New tests: 40 Claude-authored cases, all pass (normal condition and exact
  outputs covering all five segmentation outcomes plus a quarantine, CLI and
  `-O` refusal, one tamper per check family, malformed values, preflight-invalid
  rows, an equally optimal alternative segmentation, no preparation import).
- Full unit suite before the review fixes: 153 tests, OK, 2 existing Windows
  symlink skips (rerun recorded in the daily report).
- Real read-only runs after the review fixes, against the external outputs
  located by `.local/data-location.json` (aggregates only):
  - Punjabi pilot (condition): `PASS sources=12 processed=12 quarantined=0
    clips=187 flagged=10 retained_seconds=762.993 uncovered_seconds=20.884
    originals_hashed=12 transcripts_scanned=24 aggregate_checks=19`.
  - CSALT balanced conditioned v2: `PASS sources=340 processed=340 clips=450
    flagged=154 retained_seconds=1958.442 uncovered_seconds=771.672
    aggregate_checks=18`.
  - CSALT synthetic benchmark v1: `PASS sources=68 processed=68 clips=50
    flagged=10 retained_seconds=237.827 uncovered_seconds=391.561
    aggregate_checks=18`.
  All match the totals recorded by earlier independent verification.
- The exact-mode CSALT pilot v2 was not re-verified: its input manifest is not
  on disk (the stored selection file has a different schema). Exact mode is
  covered by fixtures only.

## Limitations

- Same-formula re-derivation: the verifier implements the documented policy
  separately but uses the same numerical recipe and soxr version; it cannot detect
  a policy that is itself wrong, and a different soxr/numpy build may change
  resampled bytes (it then fails rather than passes).
- No listening, word-boundary, dialect, identity, consent or eligibility evidence.
