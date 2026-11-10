# Evaluation metrics (backlog 9.3 partial; PRD FR-M5, FR-M6, FR-M10)

`metrics.py` holds pure NumPy functions over `p_synthetic` scores (higher means more
likely synthetic; "synthetic" when score >= threshold). FAR = genuine flagged as
synthetic, FRR = synthetic passed as genuine (PRD glossary).

- `error_rates`, `eer` (linear interpolation at the FAR/FRR crossing),
  `far_at_frr(target_frr=0.05)` (highest threshold with FRR <= 5 %, FR-M5);
- `min_dcf(..., c_miss=, c_fa=, pi_spoof=)`: ASVspoof form with bona fide as target,
  so the ASVspoof "miss" is AVA's FAR. **The cost parameters are required:** the
  ASVspoof 5 DF-track values (recalled as 1, 10 and 0.05, normalizer 0.5) were not
  confirmed from the evaluation plan in this session (no web access). Confirm them,
  cite the source in FR-M5, then add them as named defaults;
- `bootstrap_ci(metric, genuine, synthetic, seed=)`: percentile interval, each class
  resampled independently at clip level (speaker-clustered resampling is a proposed
  refinement, since clips from one speaker are correlated);
- `ece` (equal-width bins over p_synthetic vs synthetic fraction) and
  `fit_temperature` (golden-section NLL minimization on log T, FR-M6);
- `dialect_reportable(speakers, genuine_clips)`: FR-M10's 10 speakers and 500 genuine
  clips rule.

Empty, non-1-D or non-finite inputs raise ValueError ("insufficient data").
`fit_temperature` also refuses single-class labels and fits that end at a search bound
(inverted or uninformative logits) instead of returning the bound.

Open for FR-M5 before any number is reported: whether intervals must be
speaker-clustered (clip-level intervals are too narrow when one speaker contributes
many clips; a clustered variant would take a `groups` array), and the EER method. This
module interpolates at the crossing; ASVspoof/BOSARIS tools use nearest-point or
convex-hull (ROCCH) EER, which can differ slightly on small sets, so record the method
or cross-check against the reference implementation when comparing with published
numbers.

Not done: subset slicing over manifests (FR-M5 a-g), evidence-length curves (FR-M9),
reliability-diagram output, the report generator, and any metric on real AVA scores.
These are known-answer arithmetic tests, not model results.

```text
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python -m unittest tests.unit.test_eval_metrics -v
```
