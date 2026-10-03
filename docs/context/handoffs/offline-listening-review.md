# Offline listening review — 3 October 2026

User requested the next step after preparing 66 candidate clips. Direct audio
listening is unavailable in this session. Implemented a local browser review
workflow to make that pending human QA actionable, without fabricating decisions.

The stdlib builder verifies the preparation report and candidate hashes and writes
`review.html` beside the external report. The browser page plays local audio,
prioritizes flagged clips, records reviewer decisions/notes, exports JSON, and
resumes by importing a matching report-bound inventory. No server or remote data
transfer. Originals and the preparation report remain unchanged.

Files: `data/review/build_review.py`, `review_template.html`, README;
`tests/unit/test_review_builder.py`, `tests/browser/listening-review.spec.cjs`,
`docs/qa/listening-review-test-cases.md`. Parent owns generator and integration;
Playwright agent owns template/browser tests; actual GPT-6 Astra owns test design
and builder tests. Related scope: issue #11, partial FR-D13/FR-W2.

## Limits

There are no file-linked completed listening decisions in the tool yet. The user
reports having reviewed some clips successfully as a native Urdu speaker; their
filenames and whether they were raw or prepared clips have not been specified.
All real candidates therefore begin unreviewed in this page.
A human keep decision concerns audio quality only; consent, source-use rights,
splits and final training acceptance remain separate. Playback tests validate
decoding/UI behavior, not intelligibility or clean word boundaries.

The page does not save automatically. Export regularly, retain that JSON outside
Git, and import it to resume. The original report is never edited. Pilot pages
support at most 200 candidates and 5,000 characters per note. The browser does
not rehash a WAV on every playback; rerun preparation if the data changes.

## Review findings

Independent review identified filename URL encoding, overwriting unsaved edits,
asynchronous import races, incompatible export/import size bounds, and concurrent
or hidden playback. Fixes and their final test results are recorded in the spec
and QA catalog. No automated result counts as listening acceptance.

## Verification

61 Python tests discovered: 59 passed, two symlink tests skipped due to Windows
permissions. Full Playwright suite: 17 passed, including seven new listening
review fixtures. Generated the real 66-candidate page outside Git and confirmed
all decisions start unreviewed, the first local WAV decodes, no JavaScript errors
occurred and no HTTP requests were made. Playback is not listening evidence.
