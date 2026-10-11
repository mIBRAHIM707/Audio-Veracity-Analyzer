---
title: 'CPU smoke test of the official pretrained detector'
type: 'feature'
created: '2026-10-04'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent">

## Intent

Run a small, reproducible CPU-only genuine/synthetic inference smoke test using
the official AntiDeepfake MMS-300M checkpoint and its trained binary classifier.
Record source revision/hash, class order, loading coverage, input provenance,
scores, latency and process memory. This is baseline execution evidence, not an
AVA SLS model, a calibrated accuracy benchmark or validated telephone detector.
No commits, pushes, participant contact or external audio upload.

</frozen-after-approval>

## Implementation Notes

- Official model revision7928e119e09f616b2ce698892e00ca22dbaa40bf;
  1,269,622,944-byte safetensors checkpoint. Header contains original
  proj_fc.weight[2,1024]/bias[2]; model card specifies class0fake/class1real,
  16kHz input and whole-waveform layer normalization before inference.
- Official card uses fairseq. CPU adapter will use pinned torchaudio's official
  fairseq weight conversion and matching backbone configuration, strict state
  loading, original temporal-mean+linear head, no random classifier. Ignore only
  explicit pretraining-only tensors unused by inference; log every omitted key.
  Cross-framework numerical parity with original fairseq is not yet established
  and must not be implied by strict loading alone.
- Own new `models/smoke/antideepfake.py`, README, separate optional CPU dependency
  pins, Astra tests in `tests/model/`, source register and external reports.
- Select small deterministic genuine/synthetic sample with equal fake-generator
  representation; keep hashes and classes, no test-set metric claims. CPU4threads,
  inference mode, eval, warmup separate; sampled process RSS labelled accurately.
- Every source/model file verified from pinned local metadata before inference.
  No inference-time fetches, pickle loading, overwritten reports or silent random
  fallback. Bounds on manifest/audio counts/sizes and finite shape/score checks.
- Unit tests use tiny fictional tensors/models; actual full checkpoint execution
  separately verifies all feature/head weights loaded. Document any compatibility
  issues rather than weakening strict loading. No browser changes.
- Full CPU v3: 12 trials, 55.98275 seconds of audio, 19.38719 seconds measured
  inference (RTF0.3463), load9.7096s, warmup2.6785s, sampled peak RSS2,772,365,312B.
  Loaded421backbone+2head tensors strictly; omitted8known pretraining-only tensors.
  Python socket/DNS guard rejected connections; observed0attempts. Scores matched
  prior v2 within1e-6. Twenty Astra adapter tests passed, no skips.
- v1 failed on missing packaging dependency; added packaging24.1 from the CPU
  index. v2 ran successfully; v3 repeated after immutable snapshot hardening.
  Final exponent-overflow parser guard landed after v3 and is unit-verified;
  it does not alter valid-input inference. Exact run-code hash in verification JSON.

## Review Triage Log

- Medium/fixed: checkpoint could change between preflight hash and later reopen.
  Hash and deserialize the same immutable bounded byte snapshot; retain the exact
  official size/hash pin. Mutation regressions prove the bytes used remain verified.
- Medium/fixed: retained nonfinite metadata failed after inference. Reject NaN,
  Infinity and exponent overflow in both JSON parsers before any backend work.
- Medium evidence gap/fixed: zero classifier weights tested bias only. Nonzero
  weights plus unequal temporal features now verify analytical mean-pool logits.
- Medium evidence gap/fixed: added converter omissions/collisions and positional
  weight-name adaptation regressions.
- Medium evidence gap/fixed: added corrupt noncheckpoint artifact and size tests.
- Medium evidence gap/fixed: injected report publication failure preserves marker.
- Low evidence gap/fixed: exact six-second and adjacent-frame boundaries tested.
