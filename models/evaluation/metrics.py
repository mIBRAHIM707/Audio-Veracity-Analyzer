"""Detection metrics for the evaluation harness (backlog 9.3 partial; PRD FR-M5, FR-M6,
FR-M10). NumPy only; pure functions; no data access.

Score convention: `p_synthetic`, higher means more likely synthetic; the detector says
"synthetic" when score >= threshold. PRD glossary: FAR = genuine flagged as synthetic,
FRR = synthetic passed as genuine.

minDCF follows the ASVspoof form with bona fide as the target class: a "miss" is a
bona fide (genuine) clip rejected, which is AVA's FAR; a "false alarm" is a spoof
accepted, which is AVA's FRR. DCF(t) = c_miss (1 - pi_spoof) FAR(t) + c_fa pi_spoof
FRR(t), normalized by min(c_miss (1 - pi_spoof), c_fa pi_spoof). The cost parameters
are required arguments: the ASVspoof 5 DF-track values (recalled as c_miss=1, c_fa=10,
pi_spoof=0.05) could not be confirmed from the evaluation plan in this session and
must be checked and recorded in FR-M5 before any report uses them.

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


def dialect_reportable(speakers, genuine_clips):
    """FR-M10: a dialect is reported only with >= 10 speakers and >= 500 genuine clips;
    otherwise it is listed as 'insufficient data'."""
    for name, value in (('speakers', speakers), ('genuine_clips', genuine_clips)):
        if not _int(value) or value < 0:
            raise ValueError(f'{name} must be a non-negative integer')
    return speakers >= MIN_DIALECT_SPEAKERS and genuine_clips >= MIN_DIALECT_GENUINE_CLIPS
