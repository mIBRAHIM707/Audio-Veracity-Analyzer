# Evaluation metrics (backlog 9.3 partial; PRD FR-M5, FR-M6, FR-M10)

`metrics.py` holds pure NumPy functions over `p_synthetic` scores (higher means more
likely synthetic; "synthetic" when score >= threshold). FAR = genuine flagged as
synthetic, FRR = synthetic passed as genuine (PRD glossary).

- `error_rates`, `eer` (linear interpolation at the FAR/FRR crossing),
  `far_at_frr(target_frr=0.05)` (highest threshold with FRR <= 5 %, FR-M5);
- `min_dcf(..., c_miss=, c_fa=, pi_spoof=)`: ASVspoof form with bona fide as target,
  so the ASVspoof "miss" is AVA's FAR. The cost parameters are required arguments;
  `asvspoof5_track1_costs()` gives the ASVspoof 5 Track 1 values c_miss=1, c_fa=10,
  pi_spoof=0.05 (normalizer 0.5, so DCF = 1.9 FAR + FRR, the plan's beta form),
  confirmed 14 Nov 2026 from the ASVspoof 5 Evaluation Plan v0.6, Table 1
  (`ASVSPOOF5_TRACK1_SOURCE`, cited in FR-M5; pass it as `costs_status=`). ASVspoof 5
  has no "DF track": Track 1 is stand-alone deepfake detection. actDCF (Bayes threshold
  on log-likelihood ratios) is not implemented;
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

`evidence_curve` (FR-M9 / RQ-A) gives EER and FAR at FRR 5 % with intervals at 1, 2,
3, 4 and 6 windows, aggregating each clip with `ema_first`, the live scorer's EMA (a
parity test feeds the same scores through `DirectionSession`). Default
`cohort='fixed'` keeps the same clips (those with at least the longest length) at every
point; `cohort='available'` mixes populations and is labelled in each row. Points with
fewer than `min_clips_per_class` (default 10, a local choice) clips per class are
'insufficient data'. Rows carry the `codec` label; run once per codec.

`subset_report(records, fields, costs=)` builds the FR-M5 table: a full-set row and
one row per value of each string subset field (language, codec, generator partition,
code-switched, call set), each with EER, FAR at FRR 5 % (per-row threshold), minDCF and
intervals for all three, missing counts split by class, 'insufficient data' below 10
clips per class and 'no values' for a field no record carries. FR-M10 dialect FAR at the
deployed threshold is separate. `reliability_bins` gives the FR-M6 reliability-diagram
data on the same bins as `ece`.

`dialect_far(records, dialects=, threshold=)` gives FR-M10 / RQ-B FAR per listed
dialect at the fixed deployed threshold on genuine clips: every listed dialect gets a
row (no data is 'insufficient data'), unlisted dialects and non-genuine records are
refused, and rows name the interval method and flag zero-width intervals.

`report.py` (12 Nov) generates the report: `score_windows(model, windows,
temperature=)` scores `models/ava/windows.py` windows (binary logit `z = synthetic -
genuine`, `p_synthetic = sigmoid(z / T)`, model training state restored);
`build_report(scored, costs=, costs_status=, temperature_status=, model_id=,
windows_summary=, [dialects=, threshold=])` builds the subset table (language, codec,
dialect, generator family, source partition, source), ECE/reliability at the rows'
single temperature, and optional FR-M10 dialect FAR with unlisted dialect names and
speakerless rows counted, not crashed; `write_report` writes new JSON outside the
repository only. FR-M5 (e) code-switched and (g) real phone-call subsets have no
manifest field yet and are listed as not reported; (d) lacks a held-out marker.

Not done: plotting, and any metric on real AVA scores. These are known-answer and
mechanics tests, not model results.

```text
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python -m unittest tests.unit.test_eval_metrics -v
uv run --offline --no-project --python 3.11 --extra-index-url https://download.pytorch.org/whl/cpu --with-requirements requirements-model.txt python -m unittest tests.model.test_eval_report -v
```
