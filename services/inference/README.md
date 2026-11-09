# Inference: bounded file analysis (backlog 2.1)

`analysis.py` analyses one PCM16 WAV with an injected detector. It conditions
audio through the shared `data/prepare/conditioning.py` path (mono mean, soxr HQ
to 16 kHz), cuts 4 s windows every 2 s, zero-pads one partial tail window when at
least 1.5 s remains uncovered, scores each window and averages `p_synthetic` over
full windows (`aggregation: mean_of_full_windows`; proposed file-mode rule, the
streaming EMA/hysteresis of FR-I5 is backlog 4.2). Fewer than two full windows
gives `verdict: undetermined`, never genuine (FR-I5); a lone partial window never
decides. `margin` is the distance of the uncalibrated mean from 0.5, not a
confidence. Results always have the same keys, report `model_load_seconds`
separately from `processing_seconds`, and carry `calibration:
uncalibrated_baseline`, the model ID, a scope statement and flags; sources above
8 kHz are flagged `out_of_domain:wideband_source` because the telephone-band path
is not built yet. Peak normalization is not applied: the baseline layer-normalizes
each window, which cancels gain; an AVA SLS model must revisit this for parity.

Bounds and errors (each carries an HTTP status hint for the API layer in 2.2):
100 MB and 10 minutes (`InputTooLarge`, 413); undecodable or unsupported WAV
(`UnsupportedAudio`, 422); over 60 s in synchronous mode (`NeedsAsyncJob`, 202,
not a failure; a job worker calls `analyze_wav(payload, max_seconds=600)`);
concurrent request while one runs (`AnalyzerBusy`, 503, refused rather than
queued); detector load or scoring fault (`DetectorFailure`, 500). The detector
loads lazily once.

`baseline.py` builds the detector from the pinned AntiDeepfake MMS-300M checkpoint
with the strict hash-verified loader in `models/smoke/antideepfake.py`. No
downloads; CPU only. `cli.py` analyses one local file and prints the result
without its path (exit 2 and a hint for files over 60 s; `--long` allows up to
10 minutes):

```text
uv run --no-project --python 3.11 --extra-index-url https://download.pytorch.org/whl/cpu --with-requirements requirements-model.txt python -m services.inference.cli --model-root <local model dir> --wav <file>
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python -m unittest tests.unit.test_analysis_service -v
```

Real smoke, 9 November 2026, local checkpoint, 4 threads: a 4.9 s CSALT genuine
clip gave one window and `undetermined`; an 8.4 s 20 kHz CSALT synthetic benchmark
clip gave three windows, `synthetic`, mean 0.97; load 24.8 s, scoring 6.5 s. Two
clips are infrastructure evidence, not accuracy.

Not yet: VAD and speech-only windows, 8 kHz telephone path, codecs and MP3/AMR
decode (backlog 3.x), streaming EMA/hysteresis (4.x), API and jobs (2.2, 5.x),
calibration and any accuracy claim (9.x). The baseline was not trained or
validated on AVA Urdu/Punjabi data.
