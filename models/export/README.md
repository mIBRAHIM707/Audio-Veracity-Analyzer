# ONNX export (backlog 10.1 partial; PRD FR-M7, architecture §2.3)

`onnx_export.py`:

- `export_onnx(model, path)`: FP32 ONNX, opset 17, input `waveform`
  `float32[batch, 64000]` (static length, dynamic batch), outputs `logits [batch, 2]`
  and `embedding [batch, D]`. Legacy TorchScript exporter (`dynamo=False`, torch
  2.8.0). Writes outside the repository only (the graph holds the weights), exports to
  `<name>.partial`, reads the opset back from the file (must be 17), then publishes
  with a hard link, which fails atomically if the target exists (so it never
  overwrites, POSIX or NTFS; the filesystem must support hard links), and removes the
  partial in every case. Returns path, SHA-256, size, opset, torch and onnx versions
  for the model card.
- `open_session(path, threads=, expected_sha256=)`: onnxruntime `CPUExecutionProvider`
  only, bounded intra-op threads, refuses a hash mismatch.
- `check_session(session)`: the I/O contract above, as a list of problems.
- `parity(model, session, windows)`: same float32 windows through PyTorch and ONNX;
  at least 100 windows (FR-M7); `passed` only when every output is finite and the
  maximum absolute logit difference is strictly below 1e-3. Also reports the embedding
  difference and class disagreements.
- `quantize_int8_dynamic(source, path, source_sha256=)`: onnxruntime dynamic INT8 of
  MatMul and Gemm only (architecture §4.3 step 4; the convolutional front end stays
  FP32, checked by a test), signed INT8 weights per tensor, activations as uint8 via
  `DynamicQuantizeLinear`; from a hash-pinned FP32 export; refuses a result with no
  quantized MatMul/Gemm node or a broken I/O contract; same no-overwrite/no-partial
  rules. Records source hash, op types, quantized node and activation-quantizer
  counts and the onnxruntime version.
- `quantize_int8_static(source, path, calibration_windows, source_sha256=)`: FR-M7's
  second step. QOperator format (`QLinearMatMul`), MatMul/Gemm only, MinMax calibration
  over 10-2,000 float32 windows fed one per batch, INT8 weights per tensor, uint8
  activations with fixed scales. Records the calibration count and the SHA-256 of its
  float32 bytes; refuses dynamic activation quantizers in the result. Tested: a
  window's output does not depend on its batch partners (< 1e-5 on the tiny model),
  unlike dynamic INT8. Which dev windows form the calibration set is an evaluation
  choice to record with the EER (not decided here); it must be disjoint from the
  windows that measure the FR-M7 static-INT8 EER and from test, and should cover the
  serving conditions (codecs, narrowband, noise), because activations outside the
  calibrated range are clipped. The calibration reader is duck-typed (so this module
  imports without onnxruntime); its protocol is guarded by a test.
- `compare_sessions(reference, candidate, windows)`: INT8-vs-FP32 output drift (max and
  mean logit difference, embedding difference, class disagreements). Informational:
  FR-M7 judges INT8 by dev EER, so there is no pass flag.
- **Finding (14 Nov, tiny model): dynamic INT8 output depends on batch partners.**
  `DynamicQuantizeLinear` takes one scale over the whole activation tensor, batch
  included; a window's logits moved by about 1e-3 to 3e-3 when other windows shared
  its batch (FP32: about 3e-8; onnxruntime 1.32.0, seed-0 tiny model, so MMS-300M may
  differ). Architecture §2.3 / ADR-3 micro-batch windows from different calls (the
  route to FR-I9's capacity), so with dynamic INT8 one call's score would depend on
  another call's audio, and file and live mode could score the same window
  differently. Options for the PRD/architecture: run INT8 at batch 1, prefer static
  INT8 (fixed scales), or accept and bound the drift. Pinned by a characterization
  test. Consequence for FR-M7: an INT8 dev EER depends on how evaluation windows were
  batched, so it must be measured under a stated batching policy (batch 1, or the
  serving micro-batch policy) recorded next to the EER before `choose_quantization`
  uses it.
- `choose_quantization(fp32_eer, int8_dynamic_eer, int8_static_eer=None)`: the FR-M7
  rule (dynamic INT8 if it loses at most 0.3 EER points, else static INT8, else FP32
  with a latency re-benchmark). Pure; EERs are measured elsewhere
  (`models/evaluation`).

`checkpoint_parity.py` runs the above on the real AntiDeepfake MMS-300M backbone: it
loads the hash-verified local checkpoint through `models/smoke/antideepfake.py` (no
download), truncates to `--kept-layers`, adds a seeded untrained SLS head (no
AVA-trained head exists yet), exports FP32 into a new directory outside the
repository, runs parity on 100 seeded noise windows and, with `--int8`, makes the
dynamic INT8 model and reports its drift. `report.json` records provenance, hashes,
parity, timings, versions and limitations; an `INCOMPLETE` marker stays in the
directory if the run fails (consume a report only without it). It checks graph numerics only, with no
accuracy or latency claim; the loader is injectable and tested with a tiny backbone.

Limits: the model's Python input checks (shape, finite samples) are traced as
constants, so the ONNX graph does not refuse NaN or wrong shapes by itself; serving
must validate windows before `session.run`. Other known limits: `open_session` and
both quantizers hash a file and onnxruntime then reopens it by path (a swap in
between is not detected);
EERs are fractions from 0 to 1, so a percentage below 1 (0.8 meaning 0.8 %) would be
misread; a graph of 2 GB or more makes torch write external-data files named after the
`.partial` file, which this export does not handle (MMS-300M FP32 is about 1.2 GB);
parity holds at most 2,000 windows and batches of at most 64 per call.

Real-checkpoint run (14 Nov, 8 layers, untrained head, noise windows; details in
`docs/context/daily-runs/2026-11-14.md`): FP32 parity max |logit diff| 4.95e-6;
dynamic INT8 drift max 0.024 logits. Not done: parity with the AVA-trained head on real
dev windows, INT8 latency on the reference CPU, and the dev-EER measurement. The unit
and model tests use a tiny random model on synthetic noise (infrastructure only);
FR-M7 parity is accepted only when measured with the trained head on real dev windows.

```text
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python -m unittest tests.unit.test_onnx_export_rules -v
uv run --no-project --python 3.11 --extra-index-url https://download.pytorch.org/whl/cpu --with-requirements requirements-model.txt --with-requirements requirements-export.txt python -m unittest tests.model.test_onnx_export -v
```
