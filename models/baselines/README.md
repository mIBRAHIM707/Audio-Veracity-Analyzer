# Baseline detectors (PRD FR-M4, backlog 9.2)

Baselines share AVA's window contract (`float32[B, 64000]` -> `(logits[B, 2],
embedding)`, class order synthetic, genuine), the window loader
(`models/ava/windows.py`) and the training harness (`models/ava/train.py`), so they
train on the same manifests, splits and augmentation as AVA.

- `rawnet2.py`: RawNet2 in the ASVspoof 2021 baseline layout (fixed mel sinc front
  end, six residual blocks with filter-wise feature-map scaling, 3-layer 1024 GRU,
  FC 1024, FC 2), with `RAWNET2_TRAIN_CONFIG` for the reference training setup. Written
  from recollection without the upstream source available offline; two reviewers
  agreed on the residual-block quirk (pre-activation computed but unused, kept by
  default) and disagreed on whether the last sinc filter is zero (not reproduced).
  Check against the upstream commit before reporting baseline numbers.
- RawBoost for the XLS-R + AASIST baseline: `models/augmentation/rawboost.py`.
- Not yet implemented: AASIST and XLS-R + AASIST (XLS-R weights are not provisioned).

Random-weight structure tests only; no baseline has been trained or evaluated.

```text
uv run --offline --no-project --python 3.11 --extra-index-url https://download.pytorch.org/whl/cpu --with-requirements requirements-model.txt python -m unittest tests.model.test_rawnet2 -v
```
