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
  20 Nov: the RawNet2 port in clovaai/aasist (`models/RawNet2Spoof.py`, commit
  a04c986) agrees on both points: `conv1` reads the block input, and every one of
  the `out_channels` filters is built (none left zero). That is a port, not the
  asvspoof-challenge/2021 original, so check the original before reporting numbers.
- `aasist.py`: AASIST (Jung et al., ICASSP 2022) written against the reference
  implementation, clovaai/aasist (MIT, NAVER Corp.) at commit
  `a04c9863f63d44471dde8a6abcb3b082b07cd1d1`. The module carries the upstream notice.
  `REFERENCE_CONFIG` (`config/AASIST.conf`, 297,866 parameters) and `LIGHT_CONFIG`
  (AASIST-L, 85,306). Parameter names follow upstream under `backbone.`/`head.`, and
  `load_reference_state_dict` loads an upstream state dict strictly. Reference quirks
  are kept on purpose: the unused residual pre-activation, `att_weight12` for both
  cross-type attention blocks, and `pool_ratios[3]` and `temperatures[3]` unused.
  Adaptations: 64,000-sample
  windows (29 temporal nodes, as for 64,600); the embedding is the 160-value readout
  before the 0.5 dropout; the upstream 23 spectral positions become `filts[0] // 3`;
  no frequency-mask augmentation (off in the reference configuration).
  `AASIST_TRAIN_CONFIG` copies the reference learning rate, decay value and class
  weights onto the shared harness. The decay value is the same, but its strength is
  not: the reference adds it to the gradient (Adam with coupled L2), and the harness's
  AdamW shrinks weights by lr x 1e-4 per step. `REFERENCE_TRAINING` records this and
  what the harness does not do: a per-step cosine schedule to 5e-6, batch 24, 100
  epochs and SWA.
- RawBoost for the XLS-R + AASIST baseline: `models/augmentation/rawboost.py`.
- Not yet implemented: XLS-R + AASIST (XLS-R weights are not provisioned).

Random-weight structure tests only; no baseline has been trained or evaluated here.
The published AASIST weights (trained on ASVspoof 2019 LA) are used only as a parity
fixture. They are not an AVA baseline result and are never shipped.

```text
uv run --offline --no-project --python 3.11 --extra-index-url https://download.pytorch.org/whl/cpu --with-requirements requirements-model.txt python -m unittest tests.model.test_rawnet2 tests.model.test_aasist -v
```

AASIST parity with the reference: clone it outside this repository at the pinned
commit, then point `AVA_AASIST_UPSTREAM` at the clone (for example through
`uv run --env-file`, with the file kept in the ignored `.local/`). A clone at another
commit fails. Without the variable those six cases skip; add `AVA_REQUIRE_UPSTREAM=1`
to make that a failure when the run is meant as parity evidence.

```text
git clone https://github.com/clovaai/aasist.git D:/AVA-Data/vendor/aasist
git -C D:/AVA-Data/vendor/aasist checkout a04c9863f63d44471dde8a6abcb3b082b07cd1d1
# .local/aasist-upstream.env:  AVA_AASIST_UPSTREAM=D:/AVA-Data/vendor/aasist
#                              AVA_REQUIRE_UPSTREAM=1
uv run --offline --no-project --python 3.11 --env-file .local/aasist-upstream.env --extra-index-url https://download.pytorch.org/whl/cpu --with-requirements requirements-model.txt python -m unittest tests.model.test_aasist -v
```

Checked on 20 Nov at that commit: the sinc taps are bit-identical, and the names,
shapes and parameter counts match. Logits agree within 1e-6 in eval mode (random
weights, both configurations). They also agree in training mode with the same seed
(both configurations), so the dropout order and the BatchNorm statistics match, `bn1`
included. The published
`AASIST.pth`/`AASIST-L.pth` load and score identically.
