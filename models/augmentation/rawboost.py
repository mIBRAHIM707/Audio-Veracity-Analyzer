"""RawBoost data boosting (Tak et al., ICASSP 2022) in NumPy, seeded per clip.

Training-time waveform augmentation for the XLS-R + AASIST baseline (PRD FR-M4) and
AVA training (architecture §4.2). Follows the published reference algorithms:

1. ``linear_nonlinear_convolutive``: sum of ``N_f`` higher-order powers of the
   signal, each through a cascade of random FIR band-stop (notch) filters; the
   gain bias applies from the second power on, as in the reference code.
2. ``impulsive_signal_dependent``: up to ``P`` % of samples get signal-dependent
   impulsive noise of scale ``g_sd``.
3. ``stationary_signal_independent``: white noise coloured by random notch filters,
   added at a uniform 10-40 dB SNR.

Differences from the reference, deliberate: scipy is not a project dependency, so
``firwin`` (Hamming window, band-stop, unit DC gain), ``freqz`` (512 points on
[0, pi)) and ``lfilter`` (FIR) are re-implemented with NumPy; randomness comes from a
``numpy.random.Generator`` derived from ``(master_seed, clip_id)`` rather than the
global RNG, so each clip's draw is reproducible and recorded. Inputs are finite
float waveforms at 16 kHz in [-1, 1]; outputs keep the input length and dtype
(arithmetic is float64; the reference runs in the input's float32). Parameters default to
the reference's published values (``ALGORITHM_DEFAULTS``).
"""
import hashlib
import math

import numpy as np

SAMPLE_RATE = 16_000
NOTCH_DEFAULTS = dict(n_bands=5, min_f=20, max_f=8000, min_bw=100, max_bw=1000,
                      min_coeff=10, max_coeff=100, min_g=0, max_g=0)
ALGORITHM_DEFAULTS = {
    'lnl': dict(NOTCH_DEFAULTS, n_f=5, min_bias=5, max_bias=20),
    'isd': dict(p=10, g_sd=2),
    'ssi': dict(NOTCH_DEFAULTS, snr_min=10, snr_max=40),
}
# Reference modes: 1, 2, 3 alone; 4 = 1+2+3 in series; 5 = 1+2; 6 = 1+3; 7 = 2+3;
# 8 = 1 and 2 in parallel (normalized sum).
MODES = {1: ('lnl',), 2: ('isd',), 3: ('ssi',), 4: ('lnl', 'isd', 'ssi'), 5: ('lnl', 'isd'),
         6: ('lnl', 'ssi'), 7: ('isd', 'ssi'), 8: ('parallel_lnl_isd',)}
FREQZ_POINTS = 512


def generator(master_seed, clip_id, mode):
    """Per-clip generator: SHA-256 of the seed, clip ID and mode, so draws do not
    depend on processing order."""
    if type(master_seed) is not int or master_seed < 0:
        raise ValueError('master_seed must be a non-negative integer')
    if not isinstance(clip_id, str) or not clip_id:
        raise ValueError('clip_id must be a nonempty string')
    digest = hashlib.sha256(f'rawboost:{master_seed}:{mode}:{clip_id}'.encode()).digest()
    return np.random.default_rng(int.from_bytes(digest[:16], 'big'))


def _uniform(rng, low, high):
    """Legacy ``np.random.uniform`` semantics: ``low + (high - low) * U`` also when
    ``high < low``, which the reference relies on (its gain bias makes min -5 dB and
    max -20 dB); ``Generator.uniform`` would refuse that range."""
    return low + (high - low) * rng.random()


def firwin_bandstop(numtaps, f1, f2, fs=SAMPLE_RATE):
    """Hamming-windowed sinc band-stop filter scaled to unit DC gain (scipy
    ``firwin(numtaps, [f1, f2], window='hamming', fs=fs)`` with ``pass_zero=True``)."""
    if type(numtaps) is not int or numtaps < 3 or numtaps % 2 == 0:
        raise ValueError('numtaps must be an odd integer >= 3 (a band-stop needs a centre tap)')
    nyquist = fs / 2
    if not 0 < f1 < f2 < nyquist:
        raise ValueError('cutoffs must satisfy 0 < f1 < f2 < fs/2')
    lo, hi = f1 / nyquist, f2 / nyquist
    m = np.arange(numtaps) - (numtaps - 1) / 2
    taps = lo * np.sinc(lo * m) + np.sinc(m) - hi * np.sinc(hi * m)
    taps *= np.hamming(numtaps)
    return taps / taps.sum()


def max_response(taps):
    """max |H(w)| over the 512 freqz frequencies k*pi/512, k = 0..511."""
    omega = np.arange(FREQZ_POINTS) * math.pi / FREQZ_POINTS
    phases = np.exp(-1j * np.outer(omega, np.arange(len(taps))))
    return float(np.abs(phases @ taps).max())


def notch_coefficients(rng, n_bands, min_f, max_f, min_bw, max_bw, min_coeff, max_coeff,
                       min_g, max_g, fs=SAMPLE_RATE):
    """Cascade of ``n_bands`` random notch filters normalized to a peak gain of G dB."""
    taps = np.ones(1)
    for _ in range(n_bands):
        fc = _uniform(rng, min_f, max_f)
        bw = _uniform(rng, min_bw, max_bw)
        count = int(_uniform(rng, min_coeff, max_coeff))
        if count % 2 == 0:
            count += 1
        f1, f2 = fc - bw / 2, fc + bw / 2
        f1 = max(f1, 1 / 1000)
        f2 = min(f2, fs / 2 - 1 / 1000)
        taps = np.convolve(firwin_bandstop(count, f1, f2, fs), taps)
    gain_db = _uniform(rng, min_g, max_g)
    return 10 ** (gain_db / 20) * taps / max_response(taps)


def filter_fir(x, taps):
    """Reference ``filterFIR``: zero-pad, causal FIR, then trim half the filter delay."""
    n = taps.shape[0] + 1
    padded = np.pad(x, (0, n))
    y = np.convolve(padded, taps)[:padded.shape[0]]
    return y[int(n / 2):int(y.shape[0] - n / 2)]


def _peak_normalize(x, always):
    peak = np.abs(x).max()
    if peak > 0 and (always or peak > 1):
        return x / peak
    return x


def linear_nonlinear_convolutive(x, rng, n_f, min_bias, max_bias, **notch):
    y = np.zeros_like(x)
    min_g, max_g = notch.pop('min_g'), notch.pop('max_g')
    for index in range(n_f):
        if index == 1:  # reference: the bias is applied once, from the second power on
            min_g, max_g = min_g - min_bias, max_g - max_bias
        taps = notch_coefficients(rng, min_g=min_g, max_g=max_g, **notch)
        y = y + filter_fir(np.power(x, index + 1), taps)
    y = y - y.mean()
    return _peak_normalize(y, False)


def impulsive_signal_dependent(x, rng, p, g_sd):
    beta = _uniform(rng, 0, p)
    count = int(x.shape[0] * (beta / 100))
    positions = rng.permutation(x.shape[0])[:count]
    factors = (2 * rng.random(count) - 1) * (2 * rng.random(count) - 1)
    y = x.copy()
    y[positions] = x[positions] + g_sd * x[positions] * factors
    return _peak_normalize(y, False)


def stationary_signal_independent(x, rng, snr_min, snr_max, **notch):
    noise = rng.normal(0, 1, x.shape[0])
    noise = _peak_normalize(filter_fir(noise, notch_coefficients(rng, **notch)), True)
    snr = _uniform(rng, snr_min, snr_max)
    signal_norm = np.linalg.norm(x)
    noise_norm = np.linalg.norm(noise)
    if noise_norm == 0:
        return x.copy()
    return x + noise / noise_norm * signal_norm / 10.0 ** (0.05 * snr)


_ALGORITHMS = {'lnl': linear_nonlinear_convolutive, 'isd': impulsive_signal_dependent,
               'ssi': stationary_signal_independent}
_INTEGER_KEYS = {'n_f': 1, 'n_bands': 1, 'min_coeff': 3, 'max_coeff': 3}


def _finite_number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _check_params(config):
    """List every out-of-range parameter (keys are checked by the caller)."""
    problems = []
    for name, values in config.items():
        typed = len(problems)
        for key, value in values.items():
            if key in _INTEGER_KEYS:
                if type(value) is not int or value < _INTEGER_KEYS[key]:
                    problems.append(f'{name}.{key} must be an integer >= {_INTEGER_KEYS[key]}')
            elif not _finite_number(value):
                problems.append(f'{name}.{key} must be a finite number')
        if len(problems) > typed:
            continue  # range checks need valid numbers for this algorithm
        if 'min_f' in values and not (0 < values['min_f'] <= values['max_f'] <= SAMPLE_RATE / 2):
            problems.append(f'{name}: need 0 < min_f <= max_f <= {SAMPLE_RATE // 2}')
        if 'min_bw' in values and not 0 < values['min_bw'] <= values['max_bw']:
            problems.append(f'{name}: need 0 < min_bw <= max_bw')
        if 'min_coeff' in values and values['min_coeff'] > values['max_coeff']:
            problems.append(f'{name}: need min_coeff <= max_coeff')
        if name == 'isd' and not (0 <= values['p'] <= 100 and values['g_sd'] >= 0):
            problems.append('isd: need 0 <= p <= 100 and g_sd >= 0')
        if name == 'ssi' and values['snr_min'] > values['snr_max']:
            problems.append('ssi: need snr_min <= snr_max')
    if problems:
        raise ValueError('; '.join(problems))


def rawboost(x, mode, master_seed, clip_id, params=None):
    """Apply RawBoost ``mode`` (1-8) to one waveform; returns ``(y, record)``.

    ``params`` overrides ``ALGORITHM_DEFAULTS`` per algorithm. The record lists the
    mode, seed, clip ID and parameters, so the draw can be rebuilt exactly.
    """
    if mode not in MODES or type(mode) is not int:
        raise ValueError(f'mode must be an integer in {sorted(MODES)}')
    x = np.asarray(x)
    if x.ndim != 1 or x.size == 0 or not np.issubdtype(x.dtype, np.floating) or not np.isfinite(x).all():
        raise ValueError('Expected a finite nonempty 1-D float waveform')
    if np.abs(x).max() > 1:
        # LnL raises samples to powers up to n_f; it assumes full scale is 1.
        raise ValueError('Expected samples in [-1, 1] (scale PCM16 by 1/32768 first)')
    config = {name: dict(values) for name, values in ALGORITHM_DEFAULTS.items()}
    for name, values in (params or {}).items():
        if name not in config or not isinstance(values, dict) or set(values) - set(config[name]):
            raise ValueError(f'Unknown RawBoost parameters for {name!r}')
        config[name].update(values)
    _check_params(config)
    rng = generator(master_seed, clip_id, mode)
    y = x.astype(np.float64)
    for step in MODES[mode]:
        if step == 'parallel_lnl_isd':
            y = _peak_normalize(linear_nonlinear_convolutive(y, rng, **dict(config['lnl']))
                                + impulsive_signal_dependent(y, rng, **config['isd']), False)
        else:
            y = _ALGORITHMS[step](y, rng, **dict(config[step]))
    if not np.isfinite(y).all():
        raise FloatingPointError('RawBoost produced non-finite samples')
    used = sorted({'lnl', 'isd'} if mode == 8 else set(MODES[mode]))
    record = {'algorithm': 'rawboost', 'mode': mode, 'master_seed': master_seed, 'clip_id': clip_id,
              'params': {name: config[name] for name in used}, 'sample_rate': SAMPLE_RATE,
              'numpy_version': np.__version__, 'bit_generator': type(rng.bit_generator).__name__,
              'implementation': 'numpy re-implementation of the reference firwin/freqz/lfilter; '
                                'float64 internally'}
    return y.astype(x.dtype), record
