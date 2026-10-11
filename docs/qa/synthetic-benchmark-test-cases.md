# Existing-synthetic benchmark intake tests

Designed and implemented by **gpt-6-astra** on 2026-10-04 against working-tree changes based on commit `ad13029fa283183c23e84d736ac424b777e775c3`. Actual model provenance applies to this session. No commits or pushes were performed.

Scope: [synthetic importer](../../data/collect/csalt_synthetic.py), [executable tests](../../tests/unit/test_csalt_synthetic.py), and preservation of benchmark provenance through shared conditioned preparation. The [implementation spec](../../_bmad-output/implementation-artifacts/spec-synthetic-benchmark-intake.md) defines the bounded existing-synthetic benchmark. FR-D8 motivates lineage, FR-D13 integrity/screening, FR-E3/FR-E6 local handling, and FR-W2 honest test evidence. These tests do not satisfy FR-D2/FR-D11 consented same-speaker synthetic generation or final training eligibility.

Both `Spoofed_TTS` and `Spoofed_Tacotron` are required, with identical publisher speaker-folder inventories. Stable selection chooses two entries per family/speaker by default. Production expects 17 folders per family; tests use two fictional folders. Publisher descriptions supply VITS/Tacotron family names; exact versions and target/reference mappings remain unknown. A folder key is not independent evidence that the same person appears in both classes.

All WAVs are synthesized numeric fixtures in temporary external directories. Every download uses an injected function; no live network, corpus speech or voice generation is used. Temporary files clean up automatically. The integration test uses pinned NumPy/soxr dependencies. Browser applicability is none.

| Case | Priority / requirement / layer | Preconditions and inputs | Expected evidence | Executable test |
|---|---|---|---|---|
| SB-01 | P0; FR-D8 / selection | Two families × two speakers × three available; target two; reverse input order | Eight unique selections, two per group, unchanged selected set across catalog order | `test_selection_balances_two_per_family_speaker_independent_of_order` |
| SB-02 | P0; bounded benchmark contract / selection | Missing family, unequal speaker inventories, insufficient group, zero/negative/boolean/fractional target | Explicit failure, never pretend a balanced benchmark | `test_missing_family_mismatched_speakers_and_insufficient_group_rejected` |
| SB-03 | P0; FR-D8, FR-E3 / filesystem | All four selected originals already present with valid publisher hashes | No fetch; originals unchanged; inventory and manifest agree; publisher labels, unknown version/reference, folder-only identity, unassigned split, unauthorized cloning and benchmark-only/nontraining fields retained | `test_existing_valid_sources_are_reused_with_explicit_benchmark_provenance` |
| SB-04 | P0; FR-E3 / filesystem | One corrupt existing source plus three valid download siblings | Corrupt file not overwritten/refetched; failure row retained; siblings succeed; benchmark incomplete and `benchmark.json` absent | `test_corrupt_existing_is_not_replaced_and_incomplete_manifest_is_withheld` |
| SB-05 | P0; FR-D13 / injected download | Wrong publisher bytes; publisher-hashed undecodable bytes; download exception | Bad source never published; three valid siblings retained; diagnostic errors survive; no usable incomplete manifest | `test_hash_decode_and_fetch_failures_keep_valid_siblings_but_no_manifest` |
| SB-06 | P0; FR-E3 / preflight | Traversal/absolute/backslash/genuine-partition paths, moving revision, invalid hash/size, duplicates | Reject before download or output creation | `test_unsafe_catalog_and_metadata_fail_before_fetch_or_output` |
| SB-07 | P0; FR-E3 / preflight | Expected speaker mismatch; existing/overlapping output roots | Reject before network; preserve existing sentinel; no usable output on mismatch | `test_expected_speakers_and_output_safety_reject_before_network` |
| SB-08 | P0; FR-E3 / fault injection | Inject hard-link publication failure after verified downloads | Four failure rows; no final or partial audio files; no benchmark manifest | `test_publication_failure_leaves_no_final_or_partial_audio` |
| SB-09 | P0; FR-D13 / filesystem | Low-amplitude native 20 kHz originals | Structurally verified and selected with low-RMS/format flags; native metadata retained; no individual-listening claim | `test_native_low_volume_sources_are_preserved_without_listening_acceptance` |
| SB-10 | P0; FR-D8, FR-D12 / conditioning integration | Complete four-source 20 kHz benchmark passed through `condition=True` | Four candidates; original files unchanged; source/clip records retain generator family/version, partition, identity/reference uncertainty, boolean benchmark-only/training exclusion, original clip ID/hash and speaker | `test_shared_conditioning_retains_benchmark_exclusion_and_original_lineage` |
| SB-11 | P0; FR-E3 / preparation integration | Valid benchmark manifest beside `INCOMPLETE` intake marker; test both conditioning modes | Reject before prepared output exists; marker preserved; neither mode consumes unfinished intake | `test_preparation_refuses_manifest_from_incomplete_intake` |
| SB-12 | P0; FR-D8, FR-D12 / default-path integration | Complete four-source 16 kHz benchmark passed through default preparation without conditioning argument | Four byte-exact candidates; both source and clip records retain benchmark-only/nontraining booleans, generator/identity metadata and original clip ID/hash | `test_default_preparation_retains_benchmark_exclusion_for_native_16khz` |

CSV uses lowercase `true`/`false`; preparation JSON carries real booleans for `benchmark_only` and `training_eligible`. `benchmark_only=true`, `training_eligible=false` and `use_scope=noncommercial_benchmark_only` remain explicit after conditioning. Structural verification or normalized clip output must not silently promote these records into final AVA training data.

## Execution

```text
uv run --no-project --with-requirements requirements-audio.txt --python 3.11 python -m unittest discover -s tests/unit -p test_csalt_synthetic.py -v
```

Observed on Windows/Python 3.11 with the pinned audio dependencies after review regressions: **12 tests passed**, no skips, exit 0, in 5.097 seconds. SB-11 and SB-12 cover the review fixes for unfinished intake consumption and default-path provenance loss. Initial import exposed a module-path error before test execution; the implementation owner fixed package/importlib compatibility. The test-design agent executed the final run directly. Static foundation checks also passed earlier in this task: eight roles, 16 adapters and 29 BMAD skills/214 files. No commits or pushes were made.

The injected downloader does not establish real publisher availability, network retry behavior or source authenticity beyond the supplied pinned catalog. Other limits include symlink/junction races, source changes after validation, cross-process publication contention, exhaustive WAV variants and human listening consistency. Fault injection establishes the tested publication cleanup path, not immunity to every filesystem failure. Real benchmark counts and publisher verification belong in the coordinator's external report.
