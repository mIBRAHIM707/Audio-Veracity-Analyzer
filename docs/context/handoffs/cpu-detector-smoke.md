# CPU detector smoke-test handoff

4 October 2026, todo6. No commits, pushes, uploads or participant contact.
Runner: `models/smoke/antideepfake.py`; optional dependencies:
`requirements-model.txt`; source provenance: `models/source_register.json`.

## Observed result

The final full run scored12 files:6genuine,3VITS,3Tacotron. All55.98275seconds of
audio produced finite two-class outputs on CPU with4threads. Measured inference
took19.38719seconds (aggregate RTF0.3463). Loading9.7096seconds and warmup2.6785seconds
are separate. Sampled maximum process RSS was2,772,365,312bytes, including load and
inference; this is not an exact operating-system peak or steady-state memory claim.
Host identifier: Intel64 Family6 Model140 Stepping1, Windows; details in the report.

Strictly loaded421backbone tensors and both original classifier tensors. Eight
explicitly enumerated pretraining-only tensors are unused. No missing/unexpected
inference parameters, random classifier or new SLS head. Original class order:
fake0,real1. Whole-waveform layer normalization follows the official model card.
Weights remained pinned to the publisher SHA256 recorded in the source register.

The independent check verified all12input hashes, probabilities, scope metadata
and loading coverage. Repeated logits matched the earlier v2run within1e-6.
The v3process rejected Python socket connection/DNS calls and observed no attempts;
this is not an OS firewall/native-network trace. Inference code has no download path.

## QA and fixes

Twenty actual GPT-6 Astra model tests passed, no skips. Tiny real safetensors
fixtures test loaded nonzero classifier weights, unequal temporal features,
mean pooling, layer normalization and immutable verified-byte loading. Other
cases cover strict tensor coverage, converter safeguards, artifact integrity,
input bounds, malformed/nonfinite metadata and publication failure.
All tests run in the separate model environment; they are not part of the
lightweight `tests/unit` suite. Independent review found seven issues/gaps; all
were fixed or covered by targeted regressions. See the BMAD spec triage log.

Earlier v1 stopped on safetensors' missing packaging dependency; it remains
incomplete and must not be consumed. v2 succeeded before snapshot hardening.
v3 is the final full-run evidence. The last parser guard for JSON exponent
overflow was added after v3 and verified by the20-test suite; it changes only
invalid metadata handling. External verification records the exact code hash
used for that real run, along with dependency-file hash.

## Reproduction and limits

See the runner README for offline preparation and CLI. `.local/data-location.json`
locates weights,12-file manifest and v3report, `verification.json` and
`offline-verification.json`. Machine-local evidence scripts are
`.local/run_cpu_offline.py` and `.local/check_cpu_run.py`. Inputs and weights are
never committed. A new output directory is required for each run.

This is a small execution smoke, not accuracy/calibration evidence. It uses
published corpus labels and a torchaudio adaptation whose numerical parity with
the original fairseq implementation is unmeasured. It does not establish ONNX
parity, SLS training, telephone robustness, multi-call capacity or deployment
readiness. Synthetic inputs remain benchmark-only and training-ineligible.

Next automatic step: small multi-speaker Punjabi public-source pilot.
