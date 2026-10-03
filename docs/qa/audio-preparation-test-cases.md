# Audio preparation regression cases

Designed and implemented on 2026-10-03 by the delegated **gpt-6-astra** agent. Base commit: `09541b596cb7dae3d85c61028276d59161552098`; tests target the uncommitted pilot implementation layered over that revision. Actual provenance describes this session, not future automatic model selection.

Scope: [prepare_audio.py](../../data/prepare/prepare_audio.py), tested through `process_manifest(manifest_path, source_root, output_root)` and its CLI. [Executable tests](../../tests/unit/test_audio_preparation.py) create deterministic synthetic PCM WAVs only in temporary external directories. No actual speech, real pilot manifest, identity, source audio or generated WAV is committed. All temporary inputs/outputs are removed after each test.

Requirements: FR-D8 (partial lineage), FR-D12 (partial length/pipeline preparation), FR-D13 (partial signal screening), FR-E3/FR-E6 (data handling), and FR-W2 (Astra test design). Preparation is CPU-only and stdlib-only. It emits **review candidates**, not approved training clips. Energy-defined quiet windows are not linguistic word/sentence boundaries; language, intelligibility and audible artifacts still require listening. Normalization, resampling, codecs, synthesis, consent validation and split assignment are not established by this pilot.

The agreed manifest contains a `files` list with relative POSIX `path`, safe `speaker_id` and lowercase SHA-256; `revision` is optional. The report records source status, source hashes, sample offsets, uncovered intervals, candidate hashes, screening flags and `pending_listening` status. Unsupported/corrupt source entries are quarantined while other valid entries continue. Structurally malformed manifests fail before output creation.

All cases are P0 regression checks for this pilot. The layer is Python unit/filesystem integration except AP-12, which uses a subprocess CLI. Browser applicability is none. Evidence is the exact test command/status and assertion diagnostics; fixtures clean up via `TemporaryDirectory`. AP-14 additionally injects a write error to verify staging cleanup. Synthetic square-wave fixtures make energy and sample boundaries unambiguous without suggesting real speech quality.

| Case | Requirement | Preconditions / input | Expected result | Automated test |
|---|---|---|---|---|
| AP-01 | FR-D8, FR-D12, FR-E3 | 7.2 s mono/16 kHz/PCM16 source with 400 ms silence around 3.6 s | Two 3–6 s clips; internal boundary in quiet range; byte-exact source frame slices; valid hashes/speaker/offsets; original unchanged; all samples covered or explicitly reported | `test_pause_chunks_preserve_exact_frames_hashes_and_originals` |
| AP-02 | FR-D12 | Continuous signals exactly 3 s and 6 s | One whole-source candidate each; source edges allowed; no internal cut needed | `test_three_and_six_second_continuous_sources_need_no_internal_cut` |
| AP-03 | FR-D12, FR-D13 | Continuous 2 s and 7 s signals without valid internal pause | No candidates; full source reported uncovered; processed status does not mean accepted | `test_short_and_long_without_pause_are_retained_as_uncovered` |
| AP-04 | FR-D13 | Four-second silence, low-RMS nonquiet signal, near-full-scale signal | Source screening flags exist; silence yields no clip/full uncovered; other candidates stay `pending_listening` with review flags | `test_signal_flags_require_review_and_never_claim_acceptance` |
| AP-05 | FR-D8, FR-E3 | First source hash mismatched, second valid | One quarantine/error; valid source still produces candidate; no candidate from mismatched source | `test_hash_mismatch_quarantines_source_and_continues_valid_input` |
| AP-06 | FR-D13 | Stereo, 8 kHz, PCM8, non-WAV bytes, physically truncated PCM16; declared hash matches supplied bytes | Every unsupported/corrupt source quarantined; no candidate; no silent conversion | `test_corrupt_and_unsupported_audio_are_quarantined` |
| AP-07 | FR-D8, FR-E3 | Invalid JSON/top-level shape, non-list files, valid entry followed by incomplete entry, bad hash syntax, unsafe speaker | `ValueError` before output folder exists, including when first entry was valid | `test_malformed_manifest_aborts_before_output_even_after_valid_entry` |
| AP-08 | FR-E3 | Traversal, absolute POSIX/Windows paths, nested parent traversal, backslash path | Whole manifest fails preflight; no output folder | `test_unsafe_manifest_paths_fail_before_output` |
| AP-09 | FR-E3 | Source file symlink points outside root | Source quarantined with no clips; host unable to create symlink explicitly skips test | `test_source_symlink_is_quarantined_without_reading_external_audio` |
| AP-10 | FR-E3 | Existing output with sentinel; output equals source, nested beneath source or contains source | Refuse operation; sentinel preserved; new nested output absent | `test_existing_output_and_overlapping_roots_are_refused` |
| AP-11 | FR-D8, FR-D12 | Identical source and manifest processed twice into distinct new output roots | Equal source/clip records and summary; byte-identical candidate WAVs | `test_output_clip_bytes_and_lineage_are_deterministic` |
| AP-12 | FR-W2; CLI contract | Valid external fixture CLI invocation; repeat against same output | First exit zero and report exists; repeated invocation fails without traceback or overwriting | `test_cli_prepares_external_fixture_and_reports_invalid_input` |
| AP-13 | FR-D12 | Seven-second signal with only 80 ms pause, below 120 ms policy | No forced internal cut; source remains fully uncovered | `test_pause_shorter_than_120ms_does_not_authorize_a_cut` |
| AP-14 | FR-E3; atomic publication contract | Valid source; inject `OSError` only when writing candidate WAV | Error propagates; final output absent; staging removed; original bytes unchanged | `test_output_write_failure_does_not_publish_partial_folder` |
| AP-15 | FR-E3; bounded input contract | Patch manifest and source byte caps to 16 bytes, below valid fixture size | Oversized manifest aborts before output; oversized source quarantines with no clips | `test_bounded_manifest_and_source_reads_fail_conservatively` |
| AP-16 | FR-D13, FR-E3 | Source path names a directory; second source is valid but two seconds long | Directory quarantines; second source processed with explicit segmentation outcome and no candidates | `test_directory_source_is_quarantined_and_zero_clip_reason_is_explicit` |

## Execution and limits

Executed on Windows with Python 3.11:

```text
uv run --no-project --python 3.11 python -m unittest discover -s tests/unit -p test_audio_preparation.py -v
```

Result on 2026-10-03 after bounded-read and directory-source regressions: **16 discovered, 15 passed, one skipped**, exit 0. AP-09 could not create a symlink on this host (`OSError`); symlink rejection is therefore not proven by this run. No failures remained. The initial test expectation for silence was corrected to the explicitly agreed conservative behavior (no candidate and full uncovered range); low-RMS fixture amplitude was selected above the quiet threshold to exercise a flagged candidate separately.

The tests verify exact conservation through nonoverlapping candidate plus uncovered intervals, not linguistic segmentation quality. They do not establish listening acceptance, source licence/consent, a balanced corpus, noise scoring, language recognition, synthetic lineage, speaker identity reconciliation or split safety. Per-source processing does not establish genuine/synthetic pipeline parity across a future model pipeline.

Untested hardening includes concurrent output publication, symlink/junction races, malformed RIFF variants beyond the fixtures, source files changing during read, exact production-limit boundary values, and prolonged memory use. AP-15 verifies rejection with reduced caps, not measured memory consumption. The one injected output-write failure establishes a specific prepublication cleanup path, not every filesystem failure; failures after final-folder reservation can leave a deliberately marked incomplete folder. Real external pilot results are maintained separately by the coordinator and are not inferred from fictional test passes.
