# Balanced CSALT pilot test cases

Designed by **gpt-6-astra** on 2026-10-03 against working-tree changes based on `1bc01f616a88349bd1df064f1a918a6bae6c7dd1`. Actual provenance applies to this design session. Scope: `data/collect/csalt_pilot.py` and fictional tests in `tests/unit/test_csalt_pilot.py`.

FR-D1 motivates genuine-source intake; FR-D8 motivates source/speaker/hash lineage and unassigned splits; FR-D13 motivates integrity and signal checks; FR-E3/FR-E6 keep real audio and manifests outside Git. User-authorized noncommercial research intake accepts publisher genuine/Urdu labels as source metadata. The importer must not claim independent listening, verified linguistic identity, cloning permission or final training acceptance. Balancing source folders is not proof of demographic balance or cross-source speaker identity.

Fixtures synthesize PCM WAV bytes in temporary directories; downloads are injected/mock responses, with no live network or real corpus files. Tests must retain originals and existing extra files. All temporary fixtures are cleaned automatically. The browser layer is not applicable. Test results establish importer behavior, not actual corpus acceptance.

| Case | Priority / requirement | Preconditions and inputs | Expected result |
|---|---|---|---|
| CP-01 | P0; FR-D1, FR-D8 | Multiple official-style speaker folders with excess entries; target two per speaker; reverse catalog order | Deterministic equal selection per source speaker; order of catalog does not change selected set; no random or split assumptions |
| CP-02 | P0; FR-D1, FR-E3 | Some selected sources already exist with publisher-matching bytes; downloader records calls | Reuse verified originals without fetching/replacing them; download only missing selections |
| CP-03 | P0; FR-D8 | Local first speaker has extra published originals beyond target | Preserve extras byte-for-byte; full-local CSV includes them with `selected_for_pilot=false`; balanced JSON excludes them |
| CP-04 | P0; FR-E3 | Existing selected source is corrupt or publisher hash mismatched | Never overwrite or silently replace; failure reported in diagnostic CSV/report; `balanced.json` absent when any speaker target is incomplete |
| CP-05 | P0; FR-D1, FR-D13 | Mock fetched bytes fail publisher SHA or size; corrupt/non-WAV bytes with matching publisher metadata | Reject invalid source; never treat it as valid local intake; failure is explicit |
| CP-06 | P0; FR-E3 | Catalog includes traversal, absolute/non-Bonafide path, malformed speaker/hash metadata or duplicate entries | Fail before publication; no unsafe download/write; preserve existing source tree |
| CP-07 | P0; FR-D8 | Successful mixed reuse/download run | Full-local CSV and balanced preparation JSON agree on selected path, speaker and verified hash; all originals immutable; splits stay unassigned |
| CP-08 | P0; FR-D13 | Structurally valid low-amplitude WAV with published genuine/Urdu labels | Keep valid selection and report signal flags; flag does not become a rejection or claim of failed perceptual quality |
| CP-09 | P0; FR-D1 | Insufficient entries for requested balance or invalid target count | Clear failure without pretending balanced completion |
| CP-10 | P0; FR-E3 | Existing or overlapping output root | Refuse overwrite/overlap before mutation; existing sentinel and audio preserved |
| CP-11 | P0; FR-D8 | Mock downloader changes catalog file after initial read | Report digest binds original catalog bytes and revision, not altered file |
| CP-12 | P0; FR-D1 | Expected speaker set omits/adds/substitutes catalog speakers; then exact two-speaker fixture set | Mismatches fail before output/download/source-directory mutation; exact set succeeds; production caller requires all 17 |
| CP-13 | P0; FR-E3 | Inject `os.link` publication failure after staging verified download | Final source absent, staged files removed, diagnostic failure retained and balanced manifest withheld |
| CP-14 | P0; FR-D1, FR-D13 | Existing four-second 20 kHz mono PCM16 original with matching publisher hash | Reuse unchanged; record native rate and correct duration; flag `requires_format_processing`, without false duration flag or rejection |
| CP-15 | P0; FR-D1, FR-D13 | Downloaded four-second 48 kHz stereo PCM16 original | Verify complete frame bytes across both channels; preserve bytes/rate/channels; duration stays four seconds; flag later format processing |

## Executable mapping

`select_entries` receives a pinned 40-hex revision and catalog entries with relative paths, sizes and publisher LFS SHA-256. Existing paths receive priority; remaining ties use stable revision/path hashing. `run_pilot` receives an injected downloader in every test, so no network is used. Fictional nested paths such as `Bonafide/Speaker_01/Part 1/1.wav` verify supported publisher layout.

| Executable test | Catalog cases / evidence |
|---|---|
| `test_selection_balances_is_order_independent_and_prefers_existing` | CP-01, CP-02: order-independent equal selection; existing entries preferred |
| `test_twenty_per_each_of_seventeen_folders_selects_340_entries` | CP-01: 17 fictional folders, 21 available each, exactly 20 chosen each / 340 unique selections; no files downloaded for this pure selection test |
| `test_invalid_target_or_insufficient_catalog_refuses_balance` | CP-09: zero, negative, boolean and fractional target rejected; insufficient per-speaker catalog rejected |
| `test_reuses_originals_preserves_extras_and_manifests_agree` | CP-02, CP-03, CP-07: five existing files retained byte-for-byte, one fetch, six full-local rows, four selected rows; balanced JSON matches CSV; publisher labels, unassigned splits and limited use/listening/cloning statuses asserted |
| `test_corrupt_existing_is_not_replaced_and_failed_row_remains` | CP-04: corrupt file remains unchanged and unfetched; failed CSV row retained, valid peer retained, balanced completion false, error nonzero and `balanced.json` absent |
| `test_invalid_download_hash_size_and_wav_never_publish_source` | CP-05: separate size/hash mismatch and publisher-matching non-WAV payloads; no final source file or verified selection; incomplete balance reported |
| `test_low_volume_flags_do_not_reject_publisher_labelled_clip` | CP-08: quiet synthetic PCM remains verified/selected with signal flags and no individual listening claim |
| `test_unsafe_catalog_aborts_before_download_or_output` | CP-06: unsafe path variants fail before any fetch or output directory |
| `test_malformed_catalog_revision_digest_size_duplicates_rejected` | CP-06: moving revision, missing/invalid digest, zero/boolean size, duplicate path and empty list rejected |
| `test_existing_or_overlapping_output_is_refused_without_fetch` | CP-10: output/source containment and existing output rejected; sentinel unchanged; no downloader calls |
| `test_report_digest_binds_initial_catalog_if_file_changes_during_fetch` | CP-11: actual catalog file mutation during injected fetch cannot alter recorded initial digest/revision |
| `test_expected_speaker_inventory_mismatch_rejects_before_mutation` | CP-12: missing/substituted/expanded expected sets fail before side effects; exact fixture set succeeds |
| `test_publication_failure_removes_staged_bytes_and_withholds_balanced_manifest` | CP-13: actual staging with injected hard-link error leaves no source or partial files, while diagnostic rows/report survive |
| `test_native_20khz_mono_is_preserved_and_flagged_for_later_conversion` | CP-14: native 20 kHz source reused without fetch or conversion; unchanged hash/bytes and four-second duration |
| `test_native_stereo_frame_count_duration_and_bytes_are_preserved` | CP-15: native 48 kHz stereo accepted with correct frame-byte accounting and exact unchanged published bytes |

## Execution and limits

Executed on Windows with Python 3.11:

```text
uv run --no-project --python 3.11 python -m unittest discover -s tests/unit -p test_csalt_pilot.py -v
```

Result on 2026-10-03 after independent-review fixes and native-format regressions: **15 tests passed**, exit 0, no skips. Incomplete-balance expectations require absence of `balanced.json`, while retaining CSV/report diagnostics. Regressions cover pinned catalog bytes, expected speaker inventory, staged-publication failure, native 20 kHz mono and stereo originals. Tests use stdlib only. The injected downloader verifies the pinned revision argument and records calls; it does not exercise HTTP/TLS behavior, publisher availability, retries or remote redirects.

Raw intake accepts supported native 8–192 kHz mono/stereo PCM16 and flags formats requiring later processing; it performs no resampling or channel conversion. The separate preparation CLI still accepts **only 16 kHz mono PCM16**. A mixed-native-rate balanced manifest is therefore not directly compatible with that CLI until the later conversion step exists. These native-format tests do not change or establish downstream conversion behavior.

No test establishes sample quality by listening or that a source folder corresponds to a distinct person across datasets. Low-volume flags are tested as review signals, not automatic rejection. Publisher genuine/Urdu metadata is asserted as provenance, not independently recognized from a waveform. `approved` is limited to the user's noncommercial detection-research scope; cloning remains unauthorized.

Untested hardening includes filesystem symlink/junction races, partial disk writes before publication, process cancellation, corruption between verification and subsequent use, concurrent runs on shared source roots, metadata-size extremes, and authenticated catalog acquisition. The injected hard-link failure covers one cleanup path, not every disk failure. Unknown local paths outside the pinned catalog are separately reported by the importer, not silently granted publisher provenance. Production pilot counts, actual publisher verification and rights evidence belong in the coordinator's separate intake report; fictional test passes do not substitute for them.
