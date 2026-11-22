# AVA detector: truncated backbone, SLS head and smoke harness (backlog 9.1)

Implements the model structure of PRD FR-M1/FR-M2/FR-M3 (uncalibrated) and the
FR-M8 ablation head, following architecture §2.3 and §4.1–§4.2. It is a harness:
no AVA training data, no chosen layer cut and no accuracy or capacity evidence.

- `backbone.py`: `truncate_state` drops transformer layers `>= kept` from a converted
  torchaudio state and refuses states whose layer indices are not exactly
  `0..total-1`; `truncate_backbone(full_model, kept)` strictly loads a new truncated
  model (CNN feature encoder, projection, positional convolution and the first
  `kept` layers). `TruncatedBackbone` returns every retained layer's hidden states,
  `[L, B, T, D]`, before the encoder's final layer norm. `SWEEP_LAYERS` is the PRD
  task 2.4 set (4, 6, 7, 8, 10, 12). The real checkpoint path is: load it strictly with
  `models.smoke.antideepfake.CpuDetector`, then `truncate_backbone(detector.backbone,
  kept)`. That path has not been run here (tests use tiny random models).
- `sls.py`: `SLSHead` (softmax layer weighting starting uniform, projection to 256,
  attentive statistics pooling, linear to the 256-d embedding, 2-class output) and
  `MeanPoolHead` (last retained layer, temporal mean, ReLU MLP to 256, 2-class output).
  Class order is (`synthetic`, `genuine`), as in the AntiDeepfake checkpoint. The
  architecture names no activation between SLS stages, so none is added.
- `model.py`: `AvaDetector` takes `float32[B, 64000]`, applies per-window layer
  normalization (as in the AntiDeepfake card), and returns `(logits[B, 2],
  embedding[B, 256])`. Non-finite, wrong-shaped or non-float32 input is refused.
- `train.py`: `TrainConfig` (defaults: backbone LR 2e-5, head LR 1e-3, feature encoder
  frozen for 1 epoch, class-weighted cross-entropy; every invalid field listed),
  AdamW with backbone/head parameter groups (biases, norm weights and SLS layer
  logits are never weight-decayed), `train_epoch`, `evaluate` (weighted loss over the
  whole set), `run_training` (leaves the encoder trainable on return) and
  `layer_sweep` (one fresh model per cut from the same seed; per-cut losses,
  parameter count, learned layer weights and the median of 5 warm one-window
  forwards).

- `windows.py`: `load_windows(root, split, purpose)` reads a conditioned
  `prepare_audio.py --condition` root, or an `augment.py` root together with its
  `parent_root` (verified with `verify_augmented` first), and returns one 64,000-sample
  int16 window per clip (FR-M11: zero-padded at the end; clips over 4 s keep their first
  64,000 samples, an interpretation to confirm). `purpose='train'` reads only split
  `train`, only explicitly trainable rows (the `validate_splits.is_trainable` rule), and
  refuses if a speaker or source-audio hash of those rows also appears in another split
  of the same report. Every selected clip is hash-checked and decoded; any failure,
  duplicate, quarantined source or refused copy refuses the load (sources errors only
  with `allow_source_errors=True`). The summary reports per-label padding/truncation
  (a length shortcut check), exclusions, review-flagged windows and report hashes.
  `batches(windows, size, seed)` yields `float32` PCM/32768 and `int64` targets.
  Leakage across separately published roots still needs `validate_splits.py`.

Open, not decided here: RawBoost and OC-Softmax (architecture §4.2), the sweep's selection rule
(accuracy versus CPU cost, PRD task 2.4), checkpointing and calibration (FR-M6).
Specification questions for the model owner: (1) the FR-M8 mean-pool head uses the
last retained layer, so the ablation changes both layer weighting and pooling; a
layer-averaged variant would separate them. (2) With no activation, embedding and
classifier compose into one linear map; architecture §4.2 reads that way. (3) The
weighting is one learned softmax vector per architecture §4.2, whereas the cited SLS
paper (Zhang et al. 2024) derives layer weights from the input.

## Verification

```text
uv run --offline --no-project --python 3.11 --extra-index-url https://download.pytorch.org/whl/cpu --with-requirements requirements-model.txt python -m unittest tests.model.test_ava_sls tests.model.test_ava_windows -v
```

Claude-authored tests (12 Nov 2026) use tiny random wav2vec 2.0 configurations and
synthetic tone/noise windows. They check contracts, truncation equality with the full
model's first layers, layer-weighting and pooling known answers, frozen-encoder
epochs, the weighted loss, determinism and refusals. A falling smoke loss shows the
training step works; it is not detection accuracy.
