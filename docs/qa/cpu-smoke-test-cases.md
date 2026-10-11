# CPU checkpoint smoke-test cases

Designed by **gpt-6-astra** on 2026-10-04 against working-tree changes based on commit `ad13029fa283183c23e84d736ac424b777e775c3`. This records actual test-design model provenance. No commits or pushes are authorized for this task.

Scope: the [CPU detector smoke spec](../../_bmad-output/implementation-artifacts/spec-cpu-detector-smoke.md), adapter in `models/smoke/antideepfake.py`, and optional tests in `tests/model/test_antideepfake_smoke.py`. FR-M1/FR-M2 model direction, CPU deployment goals, FR-D8 lineage and FR-W2 test evidence motivate these checks. They do not establish the AVA SLS head, calibrated scores, model accuracy, telephony robustness, production latency or cross-framework numerical parity.

Tiny fictional CPU tensors and temporary mathematical audio fixtures replace expensive checkpoint data in unit tests. Filesystem/backend tests use an explicitly injected fake detector and must label that scope; no actual model-quality claim can follow. The full checkpoint run is separate evidence. Tests must not download models, deserialize pickle or upload audio. Preserve every original and refuse existing output; clean temporary fixtures after tests.

| Case | Priority / layer | Inputs and preconditions | Required result |
|---|---|---|---|
| CPU-01 | P0 / tensor unit | Tiny matching backbone/head checkpoint | Exact expected trainable weights loaded; original two-class head retained; loading coverage reports actual keys; no random fallback |
| CPU-02 | P0 / tensor unit | Missing feature/head tensor, extra inference tensor, mismatched head shape | Hard failure before inference; no permissive silent load or newly initialized classifier |
| CPU-03 | P0 / tensor unit | Input waveform with nonzero mean/scale; constant waveform | Official whole-waveform layer normalization applied before backbone; finite constant-input behavior; eval/inference mode and CPU |
| CPU-04 | P0 / numeric unit | Known two-class logits, NaN/Infinity and invalid output shapes | Finite probability pair summing to one; fake index 0 / real index 1; reject malformed/nonfinite outputs |
| CPU-05 | P0 / manifest integration | Explicit valid inventory supplied in a known order | Preserve explicit input order and identities; bound count; do not select using labels or predicted scores. Balanced family sampling is the caller's responsibility, outside this runner |
| CPU-06 | P0 / filesystem integration | Unsafe path, bad source hash, incomplete source directory, oversized/malformed manifest, unsupported/short/long/corrupt WAV | Fail entire preflight before backend construction or output publication; never silently score a valid prefix |
| CPU-07 | P0 / filesystem integration | Explicit local model metadata file list with matching or mismatched size/hash | Verify all declared model artifacts before constructing backend; no inferred remote download; reject mismatch or unsafe reference |
| CPU-08 | P0 / fake-backend integration | Valid fictional files and deterministic fake logits | Report preserves input/model lineage and class order; warmup separate from measured inference; finite nonnegative durations; memory metric labelled sampled process RSS, not model-only or exact peak |
| CPU-09 | P0 / filesystem integration | Existing/overlapping output; backend or publication failure | No overwrite; failure cannot leave an apparently complete successful report; incomplete marker/publication behavior documented |
| CPU-10 | P1 / reproducibility | Repeat same inputs/settings with fake backend | Same selected identities/classes and probabilities; variable timings/RSS excluded from deterministic equality |
| CPU-11 | P0 / checkpoint integrity | Replace checkpoint between preflight and constructor; separately mutate path after snapshot verification | Changed pre-constructor bytes rejected before prediction; later path mutation cannot alter already verified bytes deserialized in memory |
| CPU-12 | P0 / converter contract | Converter drops a tensor or maps two keys to one; legacy position-convolution weight-normalization names | Drop/collision hard-fails; supported legacy names load exact values into current parametrizations |
| CPU-13 | P0 / precise audio boundary | 47,999, 96,000 and 96,001 frames at 16 kHz | Exactly six seconds accepted; one frame below three or above six rejected before backend/output |
| CPU-14 | P0 / metadata integrity | NaN, positive/negative Infinity, and literal exponent overflow `1e999`/`-1e999` in manifest or model metadata | JSON preflight rejects before backend/output rather than failing late during report serialization |

## Execution plan and limits

Optional model dependencies are isolated from lightweight data CI. Intended command:

```text
uv run --no-project --extra-index-url https://download.pytorch.org/whl/cpu --with-requirements requirements-model.txt --python 3.11 python -m unittest discover -s tests/model -v
```

Observed final targeted run after independent-review regressions, including JSON exponent overflow, on Windows, Python 3.11 and pinned optional model requirements: **20 tests passed**, no skips, exit 0, in 1.061 seconds. No full checkpoint was loaded by this suite. No commits or pushes were made.

## Executable coverage

| Executable test | Cases / actual evidence |
|---|---|
| `test_strict_load_retains_checkpoint_head_and_lists_only_allowed_omissions` | CPU-01: tiny backbone plus two-class 1024-input head; exact loaded values; explicit omitted pretraining key and strict coverage |
| `test_missing_extra_misshaped_or_nonfinite_weights_never_get_random_fallback` | CPU-02: missing backbone/head weight/bias, unknown root/backbone key, wrong head dimensions, NaN and wrong dtype all rejected |
| `test_detector_uses_verified_snapshot_nonzero_head_mean_pooling_and_inference` | CPU-01/03/11: actual tiny safetensors bytes with nonzero head weights and unequal temporal feature vectors; analytic expected logits `[3,27]` require mean pooling plus saved weights/bias; CPU/eval/inference mode and normalization observed; path mutation after snapshot capture does not affect deserialized bytes |
| `test_converter_dropped_keys_and_alias_collisions_are_rejected` | CPU-02/12: synthetic converter omissions and alias collisions both hard-fail |
| `test_legacy_position_weight_names_load_into_current_parametrizations` | CPU-12: real tiny weight-normalized convolution accepts adapted legacy weight-g/weight-v names with exact saved tensor values |
| `test_waveform_normalization_is_whole_waveform_and_finite_for_constant` | CPU-03: known normalized three-value waveform and finite zero output for constant waveform |
| `test_class_order_probability_and_nonfinite_output_rejection` | CPU-04: fake index 0 / real index 1, finite normalized scores, malformed logit length and NaN/Infinity rejected |
| `test_fake_backend_preserves_manifest_order_lineage_and_separates_warmup` | CPU-05/08: explicit order, hashes/labels/source clip IDs preserved, warmup adds one call, fake score not derived from publisher label; nonnegative latency, sampled RSS value/count/scope and fixture loading coverage |
| `test_bad_audio_hash_path_or_format_fails_all_before_backend_and_output` | CPU-06: valid first entry followed by bad hash, traversal/absolute path, 8 kHz, too short/long or truncated WAV; no backend/output |
| `test_model_explicit_file_list_hashes_paths_and_class_order_are_enforced` | CPU-07: checkpoint mismatch, unsafe listed file, wrong declared card size, actual same-size card corruption, wrong or omitted mandatory class order/rate rejected before backend/output |
| `test_missing_checkpoint_and_malformed_manifest_fail_before_backend` | CPU-06/07: invalid JSON and missing checkpoint fail preflight |
| `test_incomplete_input_or_model_marker_refuses_preflight` | CPU-06/07: `INCOMPLETE` in manifest directory, model root or audio root rejects before backend/output |
| `test_manifest_count_and_byte_bounds_reject_before_backend` | CPU-06: 101 entries or document over 2 MB rejected before backend/output |
| `test_existing_output_is_refused_without_changing_sentinel` | CPU-09: existing output and input/output containment rejected; sentinel unchanged |
| `test_backend_failure_cannot_publish_a_complete_report` | CPU-09: loader failure leaves no report and requires an incomplete marker if output exists |
| `test_repeated_fake_run_preserves_inputs_scores_but_not_timing_assumptions` | CPU-10: identity/class/logit/probability fields equal across runs; timing/memory intentionally not compared |
| `test_checkpoint_replacement_after_preflight_fails_before_prediction` | CPU-11: actual tiny checkpoint overwritten with same-size changed bytes inside loader; constructor rejects before backbone forward; no report, incomplete marker retained |
| `test_exact_audio_frame_boundaries_accept_six_seconds_and_reject_one_outside` | CPU-13: precise frame-count boundary cases, not approximate durations |
| `test_nonfinite_manifest_and_model_metadata_fail_before_loader` | CPU-14: NaN/Infinity/-Infinity plus literal `1e999`/`-1e999` in each metadata location fail before loading/publication |
| `test_report_write_failure_keeps_incomplete_marker` | CPU-09: injected report write error after completed fake inference leaves no report and retains `INCOMPLETE` |

The initial regression run exposed missing `INCOMPLETE` preflight checks; the implementation owner added them and the tests passed. The actual full-checkpoint attempt separately exposed an undeclared `packaging` dependency, now explicitly pinned by the coordinator. The final tiny checkpoint case exercises real safetensors serialization and `load(bytes)` deserialization, including the dependency path without loading large weights. The same immutable bytes are checked against the pinned size/hash and then deserialized; the tests distinguish replacement before verification from harmless path mutation after snapshot capture.

Strict-loading tests patch the fairseq key converter to identity or deliberately faulty mappings for tiny backbones. They establish rejection, supported weight-name adaptation, exact-head retention and temporal pooling mechanics, **not** full fairseq conversion correctness or numerical parity. Filesystem tests patch official checkpoint size/hash constants to the tiny fixture and inject a fake detector; their reports explicitly identify fixture-only coverage. They do not establish the real artifact's hash, complete backbone coverage, inference quality or real-model latency/memory. Full-checkpoint coverage, actual source/model revisions, scores and CPU measurements belong in the separate external run report.

Remaining gaps include automatic sample balancing (not implemented in this runner), cross-framework parity, actual checkpoint-level prediction fixtures, filesystem races beyond the explicit checkpoint replacement cases, partial disk writes beyond the injected failure, memory-sampler failure, long-duration performance and calibrated accuracy. Sampled process RSS includes loading/warmup/trials and can miss short peaks; it is neither isolated model memory nor an exact peak. No browser acceptance or AVA training eligibility is implied.
