# Offline listening-review test cases

Designed by **gpt-6-astra** on 2026-10-03, against working-tree changes based on commit `1a78550cbf044b4547fb8e7c61136dd98601dd65`. This records actual model provenance for the design session. Builder tests were implemented by the same agent; browser execution evidence is attributed separately below.

Scope: [builder](../../data/review/build_review.py), its offline HTML template, [builder tests](../../tests/unit/test_review_builder.py), and the browser agent's fictional review fixtures. FR-D13 motivates listening QA; FR-D8 supports exact clip/source lineage; FR-E3/FR-E6 motivate local data handling; FR-W2 requires appropriate test layers and honest evidence. A `keep` decision describes perceived clip quality only. It does not authorize training, cloning, consent, rights or dataset acceptance.

All test WAVs are synthesized temporary PCM fixtures with fictional identifiers. No real candidate audio or review decisions enter the repository. Unit fixtures and browser servers/contexts/files are cleaned after each test. Retain test status and diagnostics; any browser artifacts stay in ignored output directories. The shared precondition is a complete preparation directory with `report.json` and referenced clips. Builder output is an exclusive sibling `review.html`; all validation must finish before page creation.

## Builder cases

All are P0 Python filesystem/unit tests with browser applicability none. Tests exercise the actual generator and file hashes, not mocked validation.

| Case | Requirement | Inputs / preconditions | Expected result | Automated test |
|---|---|---|---|---|
| LB-01 | FR-D8, FR-E3 | Valid three-second WAV and matching report | Page binds exact report/clip hashes and relative clip path; report and audio bytes unchanged | `test_valid_build_binds_report_inventory_and_preserves_inputs` |
| LB-02 | FR-E3; safe rendering | Closing-script/image-onerror payload in speaker, source path and flags | Embedded document contains escaped text, not raw injected markup; browser case separately verifies inert rendering | `test_script_closing_payload_is_not_embedded_as_markup` |
| LB-03 | FR-D8 | Invalid JSON, wrong shape, empty/non-list inventory, 201 candidates beyond pilot cap of 200 | Reject with no page | `test_malformed_or_empty_report_creates_no_page` |
| LB-04 | FR-D8, FR-D13 | NaN/infinite/out-of-range duration; invalid field types; negative, boolean, fractional or duration-inconsistent sample interval | Reject atomically before output | `test_invalid_inventory_fields_reject_atomically` |
| LB-05 | FR-E3 | Traversal, absolute paths, backslash paths, path outside `clips/`, duplicate entries | Reject with no page | `test_unsafe_paths_and_duplicate_inventory_reject_without_page` |
| LB-06 | FR-D8, FR-E3 | Clip hash mismatch; then valid report with `INCOMPLETE` marker | Reject both without page | `test_hash_mismatch_and_incomplete_preparation_reject` |
| LB-07 | FR-E3 | Existing review page with sentinel content | Refuse overwrite; sentinel unchanged | `test_existing_review_is_never_overwritten` |
| LB-08 | FR-E3 | Report exceeds patched 16-byte cap | Reject before output; tests bounded-input policy without generating large files | `test_report_size_limit_rejects_before_creating_page` |
| LB-09 | FR-E3 | Candidate symlink points outside clip directory | Reject; test explicitly skips if host cannot create symlink | `test_symlink_clip_cannot_include_external_audio` |

## Browser acceptance design

These are bounded local-fixture browser cases. The test must load the generated-style page through `file://` where supported and use fictional playable WAVs. Requests to external HTTP(S) origins must not occur. Browser interception is additional evidence, not system-wide network isolation proof. Mark designs implemented only when mapped to executable tests.

| Case | Priority / requirement | Actions / input | Expected result |
|---|---|---|---|
| LR-01 | P0; FR-D13 | Open page with flagged and unflagged clips | Flagged clips first; every decision initially `unreviewed`; no automatic keep or acceptance; quality-only limitation visible |
| LR-02 | P0; FR-E3, FR-D13 | Load/play fictional WAV from local clip reference; observe all requests | Audio metadata decodes with finite expected duration and local resource URL; no remote requests/uploads; no genuine speech fixture |
| LR-03 | P0; FR-D8, FR-D13 | Export with empty reviewer, then named reviewer and edits while a filter hides clips | Reviewer validation follows explicit UI policy; valid export includes every inventory entry's path/hash/decision/notes plus report hash and reviewer, including hidden entries; speaker linkage is through the exact report |
| LR-04 | P0; FR-D8 | Export edited decisions; reload; import saved JSON | Reload has no implicit persisted decisions; valid exact-bound export restores reviewer, notes and decisions; no localStorage needed |
| LR-05 | P0; FR-D8, FR-E3 | Import malformed JSON, wrong report hash, missing/extra/duplicate clips, wrong path/hash, invalid enum/type | Reject atomically and explain error; existing reviewer, notes and decisions remain unchanged; never partially apply valid prefix |
| LR-06 | P0; FR-E3 | Hostile speaker/flags/notes/reviewer text, including closing-script and image-onerror payloads | Payload displayed as text where appropriate; no executable element or attack flag; import does not introduce markup execution |
| LR-07 | P0; FR-D13 | Edit clip, change flagged/decision filters and return | State preserved; visible count/filter results correct; export still covers complete inventory |
| LR-08 | P1; FR-E3 | Modify decision/note/reviewer and attempt navigation; export and resume | Explicit export reminder and dirty-state warning; documented dirty clearing semantics; download initiation does not prove file saved |
| LR-09 | P0; FR-E3 | Begin async file import, edit reviewer/notes before read completes, dismiss overwrite confirmation | New edits remain; import cancellation reported; pending read cannot silently discard intervening work |
| LR-10 | P0; FR-E3 | Start two imports; finish newer read first, older read last | Newer successful import remains; stale read cannot overwrite it |
| LR-11 | P1; FR-D13 | Start one clip then another; filter hides playing clip | First playback pauses when second starts; hidden playback pauses to avoid mixed-source listening |

No browser test implies human listening occurred or that a browser can assess speech intelligibility. The page has no server persistence. Exported JSON is an explicit portable review record, not approval for training or cloning.

The coordinator explicitly accepted report-bound speaker linkage: export schema contains `schema_version`, `report_sha256`, `reviewer`, and `decisions` with `path`, `sha256`, `decision`, and `notes`. It does **not** duplicate `speaker_id` or validate a separate imported speaker field. Retain the exact preparation report beside the export to resolve speaker and source metadata; changing that report invalidates its hash binding.

## Execution evidence

```text
uv run --no-project --python 3.11 python -m unittest discover -s tests/unit -p test_review_builder.py -v
```

Builder result on Windows, 2026-10-03: **nine discovered, eight passed, one skipped**, exit 0. Symlink creation raised `OSError`, so LB-09 is not proven by this run. No unit failure remained.

The browser implementation agent reported **17 tests passed in 9.5 seconds**: ten existing crawler foundation tests plus seven listening-review tests in `tests/browser/listening-review.spec.cjs`. This is that agent's execution evidence, not a duplicate run by the test-design agent.

| Executable listening test | Design coverage / actual evidence |
|---|---|
| LR01/LR02/LR06 | Flagged-first ordering, unreviewed defaults, synthetic local WAV playback with advancing time, inert metadata, zero external requests during observed page/audio activity |
| LR03/LR04/LR07 | Reviewer requirement, export of full inventory despite filter, reload clears state, valid import restores reviewer/notes/decisions |
| LR05 | Twelve malformed/schema/inventory/value import variations reject without changing prior edits |
| LR06/LR08 | Imported hostile text remains inert; synthetic beforeunload event cancelled for dirty state; export keeps warning and asks reviewer to verify downloaded JSON |
| LR09 | Cancelled import preserves edits made during pending file read |
| LR10 | Stale asynchronous import cannot replace newer successful import |
| LR11 | Single active audio and pause when filtered out |

The browser fixture directly injects fictional template data and one-second synthesized WAVs to test UI behavior; builder validation separately enforces real preparation candidates of 3–6 seconds. Fixture hashes are identifiers for import testing, not asserted file checksums; LB-01/LB-06 test actual checksum enforcement. This separation does not establish a browser test of the full Python-to-HTML pipeline. The beforeunload test dispatches an event; it does not prove every browser displays a native leave-page prompt.

Outstanding hardening includes symlink/junction races, source mutation between page generation and playback, OS/browser-specific download durability, stale human judgement after source changes, and manual listening consistency. Exact hash binding protects import association; the static page does not continuously rehash audio at playback time.
