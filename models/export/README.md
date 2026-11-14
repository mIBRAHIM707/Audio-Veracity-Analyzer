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
- `choose_quantization(fp32_eer, int8_dynamic_eer, int8_static_eer=None)`: the FR-M7
  rule (dynamic INT8 if it loses at most 0.3 EER points, else static INT8, else FP32
  with a latency re-benchmark). Pure; EERs are measured elsewhere
  (`models/evaluation`).

Limits: the model's Python input checks (shape, finite samples) are traced as
constants, so the ONNX graph does not refuse NaN or wrong shapes by itself; serving
must validate windows before `session.run`. Other known limits: `open_session` hashes
the file and onnxruntime then reopens it by path (a swap in between is not detected);
EERs are fractions from 0 to 1, so a percentage below 1 (0.8 meaning 0.8 %) would be
misread; a graph of 2 GB or more makes torch write external-data files named after the
`.partial` file, which this export does not handle (MMS-300M FP32 is about 1.2 GB);
parity holds at most 2,000 windows and batches of at most 64 per call.

INT8 quantization, the real-checkpoint export and the dev-EER measurement are not done
yet. Evidence so far is a tiny random model on synthetic noise (infrastructure only):
FR-M7 parity stays unmet until it is measured on the real truncated MMS-300M graph with
real dev windows.

```text
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python -m unittest tests.unit.test_onnx_export_rules -v
uv run --no-project --python 3.11 --extra-index-url https://download.pytorch.org/whl/cpu --with-requirements requirements-model.txt --with-requirements requirements-export.txt python -m unittest tests.model.test_onnx_export -v
```
