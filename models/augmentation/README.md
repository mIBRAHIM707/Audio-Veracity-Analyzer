# Training-time waveform augmentation

`rawboost.py` (backlog 9.2; PRD FR-M4 XLS-R + AASIST + RawBoost baseline; architecture
§4.2 AVA training augmentation) re-implements RawBoost (Tak et al., ICASSP 2022) in
NumPy: (1) linear and non-linear convolutive noise, (2) impulsive signal-dependent
noise, (3) stationary signal-independent coloured noise at 10-40 dB SNR, and the
reference's combined modes 4-8. Defaults are the reference's published parameters.

Differences from the reference code, all deliberate:

- scipy is not a dependency, so `firwin` (Hamming band-stop, unit DC gain), `freqz`
  (512 points on [0, pi)) and `lfilter` (FIR) are NumPy re-implementations. Tests check
  their defining properties and an independent FFT; bit-parity with scipy is not
  established.
- Randomness comes from a per-clip `numpy.random.Generator` keyed by
  `(master_seed, mode, clip_id)` instead of the global RNG, so each draw is
  reproducible regardless of processing order and is recorded with its parameters.
- Ranges use legacy `uniform` semantics (`low + (high - low) * U`), which the
  reference relies on for its −5 to −20 dB gain bias on higher powers.
- The reference `filterFIR` trim is kept, including its one-sample advance.

RawBoost is a training-time transform on float waveforms; it is not part of the
recorded telephone/noise copy chain in `data/prepare/augment.py` and its outputs are
not stored as dataset copies.

```text
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python -m unittest tests.unit.test_rawboost -v
```
