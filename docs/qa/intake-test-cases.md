# Genuine-audio intake test cases

Designed and implemented on 2026-10-02 by the delegated **gpt-6-astra** agent, as requested by the user. This is actual model provenance for this session, not a claim that future runs automatically select that model.

Scope: [validator](../../data/collect/validate_intake.py), [unit tests](../../tests/unit/test_intake.py), [pilot contract](../../data/collect/README.md), and [BMAD PRD](../../_bmad-output/planning-artifacts/prd.md) sections 5.1–5.2. Requirement references below mean partial supporting evidence unless explicitly stated otherwise. Passing this suite does not complete FR-D8, FR-D13, or grant recording, cloning, training, or redistribution rights.

All current cases use temporary CSVs containing fictional identifiers. No real audio, consent forms, participant records, external service, or browser is involved. Base row: unique clip ID, source `test-source`, version `v1`, fictional speaker/recording IDs, language `ur`, label `genuine`, 64 hexadecimal hash characters, duration `4.2`, fictional licence reference, status `approved`, split `unassigned`. Each case modifies only the listed fields unless stated otherwise. P0 means required to trust this pilot; P1 means compatibility or boundary coverage. Priority here is test priority, not a change to PRD priorities.

| Case | Requirement / contract | Preconditions and inputs | Expected result | Priority | Applicability / automated test |
|---|---|---|---|---|---|
| IN-01 | FR-D1; pilot languages | Base row; second unique clip with `pnb_Arab` | Pass; two rows; language counts one each | P0 | Unit; `test_both_languages_and_aggregate_counts` |
| IN-02 | FR-D8; intake staging | Base row, independently with `unassigned`, `train`, `dev`, `test` | Each passes; `unassigned` does not claim a completed split | P0 | Unit; `test_supported_splits`, `test_valid_unassigned_pilot` |
| IN-03 | FR-D8; manifest schema | Empty/header-only manifest; missing required header; duplicate column; too few or too many row cells | Fail; empty template is not a dataset | P0 | Unit; `test_empty_template_is_not_dataset`, `test_missing_header_and_malformed_csv`, `test_duplicate_header_and_extra_cells_are_rejected` |
| IN-04 | FR-D8; required metadata | Set each required field to whitespace independently | Fail each input with row-number missing-value diagnostic | P0 | Unit; `test_each_required_value_must_be_populated` |
| IN-05 | FR-D8; unique clip IDs | Repeat ID exactly or with surrounding whitespace | Fail duplicate ID after normalization | P0 | Unit; `test_duplicate_ids`, `test_whitespace_is_normalized_before_duplicate_detection` |
| IN-06 | FR-E5; declared human use review | Base row with `use_status=pending` | Fail; parser never grants rights itself | P0 | Unit; `test_pending_permissions_rejected` |
| IN-07 | FR-D2, FR-D11; pilot scope | Base row with `label=synthetic` | Fail; require future consent/lineage validation | P0 | Unit; `test_synthetic_needs_future_lineage_validator` |
| IN-08 | FR-D1, FR-D8; value domains | Invalid hexadecimal hash, unknown language, invalid split independently | Fail corresponding domain check | P0 | Unit; `test_invalid_hash_language_and_split` |
| IN-09 | FR-D13; metadata sanity only | Durations `nan`, `inf`, `-inf`, `1e309`, `0`, `-1`, nonnumeric text | Fail every nonfinite/nonpositive/non-numeric duration | P0 | Unit; `test_nonfinite_and_invalid_durations` |
| IN-10 | FR-D12, FR-D13; stage boundary | Positive durations `0.1` and `600` in raw intake | Pass metadata check; no assertion that processed audio meets 3–6 s requirement | P1 | Unit; `test_intake_does_not_claim_processed_audio_duration_limits` |
| IN-11 | FR-D8; speaker-disjoint grouping | Same source/speaker across train/test; distinct recording/hash | Fail speaker crossing | P0 | Unit; `test_speaker_leakage` |
| IN-12 | FR-D8, FR-D12; recording grouping | Same source/recording across train/dev; distinct speaker/hash | Fail recording crossing | P0 | Unit; `test_recording_leakage_even_with_different_speaker_label` |
| IN-13 | FR-D8, FR-D12; identical audio | Same hash across different sources and splits, including uppercase/lowercase hex variants | Fail hash crossing globally | P0 | Unit; `test_identical_audio_across_sources`, `test_hash_leakage_is_case_insensitive` |
| IN-14 | FR-D8; partial assignment | Same source/speaker appears unassigned and train, in both orders | Fail in both orders; no partially assigned speaker | P0 | Unit; `test_partial_assignment_is_rejected_in_both_orders` |
| IN-15 | FR-D8; source-scoped identifiers | Same speaker/recording strings in different sources and splits, distinct hashes | Pass; cross-corpus identity reconciliation is outside pilot | P1 | Unit; `test_source_scopes_speaker_and_recording_ids` |
| IN-16 | FR-D8; version stability | Same source/speaker, different source versions and splits; distinct recording/hash | Fail; changing source version cannot hide speaker crossing | P0 | Unit; `test_source_version_does_not_hide_speaker_leakage` |
| IN-17 | FR-D8; avoid false positives | Distinct clip IDs with same speaker/recording/hash in one split | Pass; duplicate content within one split is not prohibited by this pilot | P1 | Unit; `test_repeated_group_in_one_split_is_allowed` |
| IN-18 | FR-D8; CSV interoperability | UTF-8 BOM, correctly quoted comma in licence reference, extra named metadata column | Pass without corrupting required fields | P1 | Unit; `test_utf8_bom_quoted_values_and_additional_metadata` |
| IN-19 | FR-D10; count interpretation | One populated row with pending approval plus one missing required value | Fail; rows=2, language count=1; counts are not accepted-dataset totals | P1 | Unit; `test_counts_are_not_accepted_dataset_totals` |
| IN-20 | CLI contract | Run CLI against valid row and empty template | JSON on stdout; exit 0/1 agrees with `ok`; stderr empty | P0 | Unit subprocess; `test_cli_success_and_failure_emit_json_with_matching_exit_status` |
| IN-21 | CLI contract; FR-E3 diagnostic minimization | Missing path, invalid UTF-8, unterminated quoted final cell | Exit 1 with generic UTF-8 CSV error; no traceback or supplied filename | P0 | Unit subprocess; `test_cli_unreadable_input_does_not_echo_paths_or_tracebacks` |

Browser applicability is **none** for IN-01–IN-21: this repository has a Python intake CLI, not an intake web interface. Diagnostic checks cover the stated filename/error cases; they are not a complete privacy audit of arbitrary user-supplied metadata.

## Requirement gaps: future acceptance cases

These are designs, not implemented or passed tests. Use controlled storage and explicitly approved data when the corresponding pipeline exists.

| Case | Requirement IDs | Preconditions / inputs | Expected acceptance evidence | Priority | Applicability |
|---|---|---|---|---|---|
| GAP-01 | FR-D1, FR-E5 | Actual source adapter; known 31-clip Common Voice speaker; approved and pending source records | 30-clip cap; pending source rejected; licence evidence linked to intended use | P0 | Future unit/integration plus manual rights review; no current browser |
| GAP-02 | FR-D8, FR-D9, FR-D11 | Full genuine/synthetic manifest with target/reference/source ancestry and held-out generator | Speaker and all derivatives share split; held-out generator absent train/dev; ratio and test-partition reports; cross-corpus speaker reconciliation | P0 | Future unit/integration |
| GAP-03 | FR-D2, FR-D3, FR-D11, FR-E1, FR-E2 | Approved ethics protocol, separate cloning consent, withdrawal event, target voices | No generation without appropriate approval/opt-in; genuine-only consent uses test split; removal follows every derivative | P0 | Future integration plus manual governance review |
| GAP-04 | FR-D6, FR-D7, FR-D12, FR-D13 | Actual decoded audio and common processing pipeline; edge-duration, silent, clipped, noisy and wrong-language fixtures | 3–6 s processed clips; quality decisions; codec/SNR lineage; parity distributions and leakage probe evidence | P0 (FR-D7 P1) | Future audio unit/integration; manual listening remains required |
| GAP-05 | FR-D4, FR-D5, FR-D10, FR-D14, FR-D15 | Full corpus and smoke-test results, demographics, transcripts, source/version statistics | Required representation and code-switch fraction; 100-clip check; generator decisions; v0/v1 datasheets and disclosed gaps | P0 | Future aggregate validation plus manual review |
| GAP-06 | FR-E3, FR-E4, FR-E6 | Repository guards and controlled storage/release workflow | Prohibited audio/personal manifests/checkpoints blocked; private records kept outside Git; no default public release | P0 | Separate repository/security integration tests; not established by intake tests |

## Recorded execution

On 2026-10-02, Windows, Python 3.11 selected by uv:

```text
uv run --no-project --python 3.11 python -m unittest discover -s tests/unit -p test_intake.py -v
```

Result: **26 tests passed**, including subprocess CLI checks. uv emitted an informational warning that no Python project was found; unittest completed successfully. The unterminated-quote regression exposed Python CSV's permissive default; the validator now enables strict CSV parsing, and its existing CLI error handler returns the generic error. No real corpus acceptance or audio QA was performed.
