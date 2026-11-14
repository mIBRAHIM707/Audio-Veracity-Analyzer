"""Detection metrics for the evaluation harness (backlog 9.3 partial; PRD FR-M5, FR-M6,
FR-M10). NumPy only; pure functions; no data access.

Score convention: `p_synthetic`, higher means more likely synthetic; the detector says
"synthetic" when score >= threshold. PRD glossary: FAR = genuine flagged as synthetic,
FRR = synthetic passed as genuine.

minDCF follows the ASVspoof form with bona fide as the target class: a "miss" is a
bona fide (genuine) clip rejected, which is AVA's FAR; a "false alarm" is a spoof
accepted, which is AVA's FRR. DCF(t) = c_miss (1 - pi_spoof) FAR(t) + c_fa pi_spoof
FRR(t), normalized by min(c_miss (1 - pi_spoof), c_fa pi_spoof). The cost parameters
are required arguments. `asvspoof5_track1_costs()` returns the ASVspoof 5 Track 1
(stand-alone deepfake detection) values, confirmed 14 Nov 2026 from the evaluation plan
(`ASVSPOOF5_TRACK1_SOURCE`): c_miss=1, c_fa=10, pi_spoof=0.05. With them the normalizer
is c_fa pi_spoof = 0.5 and DCF = beta FAR + FRR with beta = 1.9, the plan's form
(beta * P_miss + P_fa, P_miss = bona fide rejected). minDCF only: actDCF needs
log-likelihood-ratio scores and is not implemented.

Interpretation choices: EER by linear interpolation where FAR and FRR cross on the
empirical curve; FAR at FRR = 5 % at the highest threshold whose FRR <= 5 %; bootstrap
resamples each class independently (clip level, not speaker-clustered); ECE uses
equal-width bins over p_synthetic against the synthetic fraction.
"""
import math
import numbers

import numpy as np


def _int(value):
    """Integer including NumPy integers, not bool."""
    return isinstance(value, numbers.Integral) and not isinstance(value, bool)

MIN_DIALECT_SPEAKERS = 10  # PRD FR-M10
MIN_DIALECT_GENUINE_CLIPS = 500


def _scores(values, name):
    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 1 or array.size == 0:
        raise ValueError(f'{name} must be a nonempty 1-D sequence (insufficient data)')
    if not np.isfinite(array).all():
        raise ValueError(f'{name} must be finite')
    return array


def error_rates(genuine, synthetic):
    """(thresholds, far, frr) over every distinct score plus +inf, thresholds ascending.
    FAR falls and FRR rises as the threshold rises."""
    g = np.sort(_scores(genuine, 'genuine'))
    s = np.sort(_scores(synthetic, 'synthetic'))
    thresholds = np.append(np.unique(np.concatenate([g, s])), np.inf)
    far = (g.size - np.searchsorted(g, thresholds, side='left')) / g.size
    frr = np.searchsorted(s, thresholds, side='left') / s.size
    return thresholds, far, frr


def eer(genuine, synthetic):
    """Equal error rate: FAR and FRR interpolated linearly to their crossing."""
    _, far, frr = error_rates(genuine, synthetic)
    # First point where FRR has caught up; it exists (last FRR=1, FAR=0) and k >= 1
    # because the first threshold always has FAR=1 and FRR=0.
    k = int(np.argmax(frr >= far))
    if frr[k] == far[k]:
        return float((far[k] + frr[k]) / 2)
    d0, d1 = far[k - 1] - frr[k - 1], far[k] - frr[k]  # d0 > 0 > d1 or d1 == 0
    t = d0 / (d0 - d1)
    return float(far[k - 1] + t * (far[k] - far[k - 1]))


def far_at_frr(genuine, synthetic, target_frr=0.05):
    """(FAR, threshold) at the highest threshold whose FRR <= target_frr (FR-M5)."""
    if isinstance(target_frr, bool) or not isinstance(target_frr, numbers.Real) or not 0 <= target_frr < 1:
        raise ValueError('target_frr must be a number from 0 to below 1')
    thresholds, far, frr = error_rates(genuine, synthetic)
    k = int(np.flatnonzero(frr <= target_frr + 1e-12)[-1])  # index 0 always qualifies (FRR=0)
    return float(far[k]), float(thresholds[k])


ASVSPOOF5_TRACK1_SOURCE = (
    'ASVspoof 5 Evaluation Plan v0.6 (28 June 2024), Table 1, Track 1 min DCF: '
    'pi_spf=0.05, C_miss=1, C_fa=10; '
    'https://www.asvspoof.org/file/ASVspoof5___Evaluation_Plan_Phase2.pdf (accessed 2026-11-14); '
    'beta=1.90 form in Wang et al., ASVspoof 5, arXiv:2408.08739 eq. 1-2')


def asvspoof5_track1_costs():
    """A new dict of the ASVspoof 5 Track 1 minDCF costs (PRD FR-M5); pass it as
    `costs=` and record `ASVSPOOF5_TRACK1_SOURCE` as the report's costs status."""
    return {'c_miss': 1, 'c_fa': 10, 'pi_spoof': 0.05}


def min_dcf(genuine, synthetic, *, c_miss, c_fa, pi_spoof):
    """Minimum normalized DCF over all empirical thresholds (see module docstring)."""
    for name, value in (('c_miss', c_miss), ('c_fa', c_fa)):
        if not (isinstance(value, numbers.Real) and not isinstance(value, bool)
                and math.isfinite(value) and value > 0):
            raise ValueError(f'{name} must be a positive number')
    if not 0 < pi_spoof < 1:
        raise ValueError('pi_spoof must be strictly between 0 and 1')
    _, far, frr = error_rates(genuine, synthetic)
    dcf = c_miss * (1 - pi_spoof) * far + c_fa * pi_spoof * frr
    return float(dcf.min() / min(c_miss * (1 - pi_spoof), c_fa * pi_spoof))


def bootstrap_ci(metric, genuine, synthetic, *, seed, n_resamples=1000, confidence=0.95):
    """Percentile interval of metric(genuine*, synthetic*) with each class resampled
    with replacement, independently and at its own size. Returns (low, high)."""
    if not _int(seed) or seed < 0:
        raise ValueError('seed must be a non-negative integer')
    if not _int(n_resamples) or n_resamples < 100:
        raise ValueError('n_resamples must be an integer >= 100')
    if not 0 < confidence < 1:
        raise ValueError('confidence must be strictly between 0 and 1')
    g, s = _scores(genuine, 'genuine'), _scores(synthetic, 'synthetic')
    rng = np.random.default_rng(seed)
    values = np.array([metric(rng.choice(g, g.size), rng.choice(s, s.size)) for _ in range(n_resamples)])
    tail = (1 - confidence) / 2
    return float(np.quantile(values, tail)), float(np.quantile(values, 1 - tail))


def ece(p_synthetic, is_synthetic, n_bins=15):
    """Expected calibration error with equal-width bins on p_synthetic: the
    count-weighted mean of |synthetic fraction - mean p_synthetic| per nonempty bin."""
    p = _scores(p_synthetic, 'p_synthetic')
    y = np.asarray(is_synthetic)
    if y.shape != p.shape or not np.isin(y, (0, 1)).all() or y.dtype == object:
        raise ValueError('is_synthetic must be 0/1 labels matching p_synthetic')
    if ((p < 0) | (p > 1)).any():
        raise ValueError('p_synthetic must be from 0 to 1')
    if not _int(n_bins) or n_bins < 1:
        raise ValueError('n_bins must be a positive integer')
    bins = np.minimum((p * n_bins).astype(int), n_bins - 1)
    total = 0.0
    for b in np.unique(bins):
        members = bins == b
        total += members.sum() * abs(y[members].mean() - p[members].mean())
    return float(total / p.size)


def fit_temperature(logits, is_synthetic, *, low=0.05, high=20.0, iterations=100):
    """Temperature T > 0 minimizing the binary NLL of sigmoid(logit / T) (FR-M6),
    by golden-section search on log T within [low, high]."""
    z = _scores(logits, 'logits')
    raw = np.asarray(is_synthetic)
    if raw.dtype.kind not in 'biuf' or raw.dtype.kind == 'b':
        raise ValueError('is_synthetic must be numeric 0/1 labels')
    y = raw.astype(np.float64)
    if y.shape != z.shape or not np.isin(y, (0.0, 1.0)).all():
        raise ValueError('is_synthetic must be 0/1 labels matching logits')
    if y.min() == y.max():
        raise ValueError('temperature needs both classes (insufficient data)')
    if not 0 < low < high:
        raise ValueError('need 0 < low < high')

    def nll(log_t):
        x = z / math.exp(log_t)
        return float(np.mean(np.logaddexp(0, x) - y * x))  # -log sigmoid terms, stable

    a, b = math.log(low), math.log(high)
    ratio = (math.sqrt(5) - 1) / 2
    c, d = b - ratio * (b - a), a + ratio * (b - a)
    for _ in range(iterations):
        if nll(c) < nll(d):
            b, d = d, c
            c = b - ratio * (b - a)
        else:
            a, c = c, d
            d = a + ratio * (b - a)
    log_t = (a + b) / 2
    if min(log_t - math.log(low), math.log(high) - log_t) < 1e-3:
        # Inverted or uninformative logits drive the search to a bound; a bound is not a fit.
        raise ValueError('temperature fit reached a search bound; logits may be inverted '
                         'or uninformative')
    return math.exp(log_t)


EVIDENCE_LENGTHS = (1, 2, 3, 4, 6)  # PRD FR-M9


def _alpha(alpha):
    if isinstance(alpha, bool) or not isinstance(alpha, numbers.Real) or not 0 < alpha <= 1:
        raise ValueError('alpha must be a number in (0, 1]')
    return float(alpha)


def ema_first(scores, n, alpha):
    """Live-scorer EMA (first window seeds it, as `services/scorer/session.py`) over the
    first n window scores; a parity test ties the two together."""
    alpha = _alpha(alpha)
    if not _int(n) or n < 1 or len(scores) < n:
        raise ValueError('n must be an integer from 1 to the number of scores')
    value = float(scores[0])
    for score in scores[1:n]:
        value = alpha * score + (1 - alpha) * value
    return float(value)


def evidence_curve(genuine_clips, synthetic_clips, *, alpha=0.4, lengths=EVIDENCE_LENGTHS,
                   cohort='fixed', min_clips_per_class=10, codec=None, seed=0,
                   n_resamples=1000, target_frr=0.05):
    """FR-M9 / RQ-A: EER and FAR at FRR = target_frr against evidence length. Each clip
    is a sequence of per-window p_synthetic (0-1) in time order, aggregated at length n
    with `ema_first` (the live EMA). This measures evidence quality at exactly n
    windows, not the alert rule (2 consecutive windows, hysteresis).

    `cohort='fixed'` (default) uses only clips with at least max(lengths) windows at
    every length, so each point has the same clips; `cohort='available'` uses every clip
    with at least n windows, which mixes populations (long clips only at large n) and
    can bias the curve. A length with fewer than `min_clips_per_class` clips in either
    class is 'insufficient data'. Intervals are clip-level (too narrow when few speakers
    or calls contribute many clips; clustering is an FR-M5 question), and the FAR
    interval re-picks the threshold in each resample (it describes the method, not a
    fixed deployed threshold). FR-M11: evaluate only the live-call campaign and
    concatenated clips of at least 8 s; that selection, and running once per codec
    (recorded via `codec`), are the caller's job."""
    alpha = _alpha(alpha)
    if not lengths or any(not _int(n) or n < 1 for n in lengths):
        raise ValueError('lengths must be positive integers')
    lengths = sorted(set(int(n) for n in lengths))
    if cohort not in ('fixed', 'available'):
        raise ValueError("cohort must be 'fixed' or 'available'")
    if not _int(min_clips_per_class) or min_clips_per_class < 1:
        raise ValueError('min_clips_per_class must be a positive integer')
    clips = {}
    for name, group in (('genuine', genuine_clips), ('synthetic', synthetic_clips)):
        clips[name] = [_scores(clip, f'{name} clip') for clip in group]
        if any(((c < 0) | (c > 1)).any() for c in clips[name]):
            raise ValueError(f'{name} window scores must be from 0 to 1')
    needed = max(lengths) if cohort == 'fixed' else None
    rows = []
    for n in lengths:
        floor = needed or n
        g = [ema_first(c, n, alpha) for c in clips['genuine'] if c.size >= floor]
        s = [ema_first(c, n, alpha) for c in clips['synthetic'] if c.size >= floor]
        row = {'windows': n, 'cohort': cohort, 'codec': codec,
               'genuine_clips': len(g), 'synthetic_clips': len(s)}
        if min(len(g), len(s)) < min_clips_per_class:
            row['status'] = 'insufficient data'
        else:
            far, _ = far_at_frr(g, s, target_frr)
            row.update(status='ok', eer=eer(g, s), far_at_frr=far,
                       eer_ci=bootstrap_ci(eer, g, s, seed=seed, n_resamples=n_resamples),
                       far_ci=bootstrap_ci(lambda a, b: far_at_frr(a, b, target_frr)[0], g, s,
                                           seed=seed, n_resamples=n_resamples))
        rows.append(row)
    return rows


LABELS = ('genuine', 'synthetic')


def subset_report(records, fields, *, costs, min_clips_per_class=10, seed=0, n_resamples=1000,
                  target_frr=0.05):
    """FR-M5 per-subset table (subsets such as 'language', 'codec',
    'generator_partition', 'code_switched', 'call_set'). `records` are dicts with 'label'
    ('genuine' or 'synthetic'), 'score' (p_synthetic, 0-1) and string subset fields;
    `costs` is exactly {c_miss, c_fa, pi_spoof} for minDCF (see module docstring).

    Returns one row for the full set (field None), then per field one row per value in
    sorted order, with counts, EER, FAR at each row's own FRR = target_frr threshold,
    minDCF and bootstrap intervals for all three, or 'insufficient data' when either
    class has fewer than `min_clips_per_class` records. `missing` is the whole-field
    count of records where the field is absent or None (split by class in
    `missing_genuine`/`missing_synthetic`), repeated on each of that field's rows; a field
    present on no record gets one row with status 'no values'.

    Not for FR-M10 dialects: those sets are genuine-only and need FAR at the fixed
    deployed threshold; use `dialect_far`."""
    if not isinstance(fields, (list, tuple)) or not all(isinstance(f, str) and f for f in fields):
        raise ValueError('fields must be a list of field names')
    if len(set(fields)) != len(fields) or set(fields) & {'label', 'score'}:
        raise ValueError("fields must be distinct and must not be 'label' or 'score'")
    if not _int(min_clips_per_class) or min_clips_per_class < 1:
        raise ValueError('min_clips_per_class must be a positive integer')
    if not _int(seed) or seed < 0 or not _int(n_resamples) or n_resamples < 100:
        raise ValueError('seed must be a non-negative integer and n_resamples an integer >= 100')
    if not isinstance(costs, dict) or set(costs) != {'c_miss', 'c_fa', 'pi_spoof'}:
        raise ValueError('costs must be a dict with exactly c_miss, c_fa and pi_spoof')
    min_dcf([0.0], [1.0], **costs)  # validate the cost values once, before any work
    clean = []
    for index, record in enumerate(records):
        if not isinstance(record, dict) or record.get('label') not in LABELS:
            raise ValueError(f'record {index} needs a label from {LABELS}')
        score = record.get('score')
        if isinstance(score, bool) or not isinstance(score, numbers.Real) \
                or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError(f'record {index} needs a score from 0 to 1')
        for field in fields:
            if record.get(field) is not None and not isinstance(record[field], str):
                raise ValueError(f'record {index} field {field!r} must be a string or None')
        clean.append(record)

    def row(field, value, group, absent=()):
        g = [r['score'] for r in group if r['label'] == 'genuine']
        s = [r['score'] for r in group if r['label'] == 'synthetic']
        out = {'field': field, 'value': value, 'genuine': len(g), 'synthetic': len(s),
               'missing': len(absent),
               'missing_genuine': sum(r['label'] == 'genuine' for r in absent),
               'missing_synthetic': sum(r['label'] == 'synthetic' for r in absent)}
        if min(len(g), len(s)) < min_clips_per_class:
            out['status'] = 'insufficient data'
            return out
        far, threshold = far_at_frr(g, s, target_frr)
        kw = dict(seed=seed, n_resamples=n_resamples)
        out.update(status='ok', eer=eer(g, s), far_at_frr=far, threshold=threshold,
                   min_dcf=min_dcf(g, s, **costs),
                   eer_ci=bootstrap_ci(eer, g, s, **kw),
                   far_ci=bootstrap_ci(lambda a, b: far_at_frr(a, b, target_frr)[0], g, s, **kw),
                   min_dcf_ci=bootstrap_ci(lambda a, b: min_dcf(a, b, **costs), g, s, **kw))
        return out

    rows = [row(None, None, clean)]
    for field in fields:
        present = [r for r in clean if r.get(field) is not None]
        absent = [r for r in clean if r.get(field) is None]
        values = sorted({r[field] for r in present})
        if not values:
            empty = row(field, None, [], absent)
            empty['status'] = 'no values'
            rows.append(empty)
        for value in values:
            rows.append(row(field, value, [r for r in present if r[field] == value], absent))
    return rows


def reliability_bins(p_synthetic, is_synthetic, n_bins=15):
    """Reliability-diagram data (FR-M6) on the same bins as `ece`: per nonempty bin its
    nominal edges, count, mean p_synthetic and synthetic fraction. Edges are nominal:
    floating-point multiplication can put a value on an edge into the bin below, and
    the last bin includes 1.0 (both exactly as in `ece`)."""
    ece(p_synthetic, is_synthetic, n_bins)  # same validation
    p = np.asarray(p_synthetic, dtype=np.float64)
    y = np.asarray(is_synthetic)
    bins = np.minimum((p * n_bins).astype(int), n_bins - 1)
    return [{'low': b / n_bins, 'high': (b + 1) / n_bins, 'count': int((bins == b).sum()),
             'mean_p': float(p[bins == b].mean()), 'fraction_synthetic': float(y[bins == b].mean())}
            for b in np.unique(bins)]


def _unit_real(value):
    return (not isinstance(value, bool) and isinstance(value, numbers.Real)
            and math.isfinite(value))


def dialect_far(records, *, dialects, threshold, seed=0, n_resamples=1000, confidence=0.95):
    """FR-M10 / RQ-B: FAR per dialect at the fixed deployed `threshold` on genuine
    records (dicts with 'dialect', 'speaker', 'score' and, if present, label 'genuine').
    `dialects` lists every dialect to report (e.g. Majhi, Pothwari, Saraiki, Hindko,
    Standard Urdu, code-switched); each gets a row, and one with no or too little data
    (`dialect_reportable`: 10 speakers, 500 clips) says 'insufficient data'. Records for
    an unlisted dialect, or with a non-genuine label, are refused. Names are matched
    exactly. Supplying dialect-held-out sets is the caller's job.

    Intervals are a clip-level percentile bootstrap (`ci_method`), too narrow when false
    alarms cluster by speaker (FR-M5 clustering question); a zero-width interval, as at
    FAR 0 or 1, is flagged `ci_degenerate` because it does not mean certainty."""
    if not isinstance(dialects, (list, tuple)) or not dialects \
            or not all(isinstance(d, str) and d for d in dialects) or len(set(dialects)) != len(dialects):
        raise ValueError('dialects must be a nonempty list of distinct names')
    if not _unit_real(threshold) or not 0 <= threshold <= 1:
        raise ValueError('threshold must be the deployed operating point, from 0 to 1')
    if not _int(seed) or seed < 0 or not _int(n_resamples) or n_resamples < 100 \
            or not _unit_real(confidence) or not 0 < confidence < 1:
        raise ValueError('seed >= 0, n_resamples >= 100 and confidence in (0, 1) are required')
    groups = {d: [] for d in dialects}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f'record {index} must be a dict')
        if record.get('label', 'genuine') != 'genuine':
            raise ValueError(f'record {index} is not genuine; FR-M10 FAR uses genuine clips only')
        dialect, speaker, score = record.get('dialect'), record.get('speaker'), record.get('score')
        if not isinstance(dialect, str) or not dialect or not isinstance(speaker, str) or not speaker:
            raise ValueError(f'record {index} needs string dialect and speaker')
        if dialect not in groups:
            raise ValueError(f'record {index} has unlisted dialect {dialect!r}')
        if isinstance(score, bool) or not isinstance(score, numbers.Real) \
                or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError(f'record {index} needs a score from 0 to 1')
        groups[dialect].append((speaker, float(score)))
    rows = []
    for dialect in dialects:
        scores = np.array([s for _, s in groups[dialect]])
        speakers = len({sp for sp, _ in groups[dialect]})
        row = {'dialect': dialect, 'speakers': speakers, 'genuine_clips': int(scores.size),
               'threshold': float(threshold)}
        if not dialect_reportable(speakers, int(scores.size)):
            row['status'] = 'insufficient data'
        else:
            rng = np.random.default_rng(seed)
            draws = [(rng.choice(scores, scores.size) >= threshold).mean() for _ in range(n_resamples)]
            tail = (1 - confidence) / 2
            low, high = float(np.quantile(draws, tail)), float(np.quantile(draws, 1 - tail))
            row.update(status='ok', far=float((scores >= threshold).mean()), far_ci=(low, high),
                       ci_method='clip-level percentile bootstrap', ci_degenerate=low == high)
        rows.append(row)
    return rows


def dialect_reportable(speakers, genuine_clips):
    """FR-M10: a dialect is reported only with >= 10 speakers and >= 500 genuine clips;
    otherwise it is listed as 'insufficient data'."""
    for name, value in (('speakers', speakers), ('genuine_clips', genuine_clips)):
        if not _int(value) or value < 0:
            raise ValueError(f'{name} must be a non-negative integer')
    return speakers >= MIN_DIALECT_SPEAKERS and genuine_clips >= MIN_DIALECT_GENUINE_CLIPS
