---
title: 'Offline listening review for prepared pilot candidates'
type: 'feature'
created: '2026-10-03'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
---

<frozen-after-approval reason="authorized continuation of candidate review">

## Intent

Make the completed audio pilot reviewable in an offline browser page alongside
its external data. A human reviewer can play candidates, prioritize signal flags,
record keep/reject/needs-recut decisions and notes, export them, and resume by
importing a report-bound review file. Every clip starts unreviewed. This supplies
the listening-review workflow; it does not claim the assistant heard the audio.
No upload, server, consent changes, training approval or original modification.

</frozen-after-approval>

## Implementation Notes

- Small standalone generator/template and fictional tests; no irreversible
  operations, existing caller contracts or unresolved user preference gaps.
- Continue on issue #11 branch with known prior dirty planning/foundation edits
  preserved. New task paths only are eligible for its commit.
- Parent owns build_review.py, README, spec and context integration. Browser agent
  owns template and Playwright tests. Actual GPT-6 Astra designs cases and builder
  tests; another independent reviewer checks the final change.
- Offline file:// playback; decisions exported as JSON. No invisible persistence;
  import validates exact report identity and candidate hashes before changing state.
- Requirement links: partial FR-D13 review support and FR-W2 evidence. Prepared
  candidates remain pending listening until humans supply their own decisions.
- Verification: 61 Python tests discovered, 59 passed, two source-symlink tests
  skipped because Windows denied creation. Browser suite: 17 passed, including
  seven listening-review fixtures. Actual generated page loaded all 66 candidates
  as unreviewed, decoded the first WAV, and made zero HTTP requests; no listening
  performed by the assistant. This is a local tool, not the AVA detector dashboard.
- Speaker linkage in exports uses the exact preparation-report hash plus clip
  path/hash. Speaker ID remains in the source report; it is not duplicated in
  each decision. The reviewer catalog documents this design explicitly.

## Review Triage Log

- Medium, patched: URL-significant filenames could resolve the wrong audio;
  each path component is now percent-encoded.
- Medium, patched: valid imports could overwrite unsaved edits; explicit dirty
  replacement confirmation now happens after validation and asynchronous reading.
- Medium, patched: out-of-order reads could overwrite newer imports; generation
  tokens discard stale imports, with regression coverage.
- Medium, patched: maximum exports could exceed import bounds; pilot builder now
  caps at 200 clips and 1000-character paths; notes are capped at 5000 characters.
  Export additionally checks the same 10 MiB bound as import.
- Medium, patched: multiple/hidden players could overlap; playing pauses other
  candidates and filtering pauses hidden players.
- Independent reviewer verified all five fixes with no unresolved findings.
