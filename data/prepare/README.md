# Pilot audio preparation

CPU-only tooling for partial FR-D8/FR-D12/FR-D13 groundwork.
This makes **review candidates**, not an accepted training dataset. There is no
browser component; use unit tests rather than Playwright for this stage.

```text
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python data/prepare/prepare_audio.py --condition --manifest D:/AVA-Data/manifests/csalt-balanced-v2/balanced.json --source-root D:/AVA-Data/raw/csalt-urdu --output-root D:/AVA-Data/processed/csalt-balanced-new-run
```

The output must be a new directory outside the repository and disjoint from the
source directory. A repeated run needs a different output name; existing outputs
are never overwritten. The CLI returns nonzero for malformed input or quarantined
sources. An output with no candidates can still be a valid processing run; inspect
the counts and uncovered ranges rather than treating exit zero as acceptance.

## Input and provenance

Input JSON contains a nonempty `files` list, each with a relative POSIX `path`, a
safe `speaker_id` and the actual source `sha256`. Optional `revision` preserves
the dataset revision. Conditioned runs copy published source/version/recording,
language/class, licence, use scope, cloning status, split and listening provenance
to both source and clip rows. The original `clip_id` is retained as
`source_clip_id`. Other fields are ignored. Duplicate source paths,
invalid metadata and path traversal abort before output creation. Missing,
symlinked, corrupt, hash-mismatched or unsupported sources are quarantined in the
report while other sources proceed. Pilot limits: 10,000 entries and 10 MB per
source; manifest input is capped at 8 MB. Inputs must be regular files. Sources
must be 16 kHz mono 16-bit PCM WAV in legacy mode. Explicit `--condition` accepts
8–192 kHz mono/stereo PCM16 WAV and requires the pinned CPU audio dependencies.

The output contains `clips/` and `report.json`. Every clip records source path,
source hash, speaker ID, output hash and half-open sample offsets
`[start_sample, end_sample)`. With no `--condition`, samples are copied exactly.
Conditioned runs use the 16 kHz canonical sample grid for offsets; original WAV
hash, native rate/count, canonical PCM hash and transform versions bind the chain.
The report also records the exact input manifest hash. No denoising,
codec transform, split assignment or label-dependent treatment occurs. The same
algorithm can later process both classes, but full synthetic lineage is not yet
supported. Keep the original source manifest alongside the report.

## Conditioning order

`conditioning.py` contains pure, class-independent transformations: arithmetic
stereo downmix, soxr HQ resampling to 16 kHz (bypassed at 16 kHz), deterministic
PCM16 quantization, then per-candidate sample-peak normalization to −3 dBFS
**after** segmentation. No normalization changes the pause proposals. Silence
stays silent. Gain, pre/post metrics, resampling saturation and component versions
are recorded. Worst-channel native clipping is measured before downmixing and
retained as `native_possible_digital_clipping`. Native clipping and resampling
saturation flags cover the whole source and conservatively flag all its candidates;
they do not locate the affected samples within each candidate. Source clipping and
low-level flags survive gain adjustment. This is
sample-peak normalization, not LUFS normalization or noise removal; large gains
can amplify background noise and deserve targeted listening.

Install with `python -m pip install -r requirements-audio.txt` in your environment,
or use the uv command above. Dependency installation needs network access once;
the processing functions do not. NumPy is BSD licensed; Python-SoXR/libsoxr is
LGPL-2.1-or-later. Review distribution obligations when packaging the application.
API references: [Python-SoXR](https://python-soxr.readthedocs.io/en/latest/),
[NumPy](https://numpy.org/doc/stable/).

This pilot conversion does not perform the architecture's telephone 8 kHz
round-trip, codec augmentation or Silero VAD. Sharing these primitives with the
future runtime and proving full train/serve parity remain later work.

Publishing reserves the output directory atomically, then moves staged clips and
the report into it. Interrupted publication leaves an `INCOMPLETE` marker (or a
directory without a report); do not consume such output. It will not be overwritten
on rerun. Per-source segmentation outcomes distinguish all-quiet, too-short,
no-valid-pause-segments, partial coverage and full coverage.

## Boundaries and flags

Audio is examined in 20 ms windows. Internal candidate boundaries are centers of
quiet runs lasting at least 120 ms, with RMS below -40 dBFS. Source start/end are
also boundaries. A deterministic selection maximizes retained samples in
nonoverlapping 3–6 second intervals. Speech that has no usable pause can remain
entirely uncovered rather than being cut at an arbitrary time. All-quiet audio
produces no candidates. Every uncovered range remains traceable in the report.

Quiet-energy boundaries are **not word-boundary or speech detection**. Quiet
phonemes can trigger them; background noise can hide pauses. These conservative
heuristics need listening review and later comparison with VAD before scaling.

Flags request review; they do not prove defects or reject a candidate:

- `low_rms`: whole-clip RMS below -35 dBFS.
- `mostly_quiet`: more than half of windows below -40 dBFS.
- `possible_digital_clipping`: more than 0.1% of samples at normalized absolute
  amplitude >= 0.999. Earlier clipped-and-attenuated audio may not trigger it.

Conditioned candidates have `review_status: unreviewed`; the legacy exact-sample
mode retains `pending_listening`. Neither status is an ingestion gate. Established
CSALT publisher labels and approved noncommercial detection scope are retained;
new cuts have not thereby passed listening QA. Use sampled/targeted review under
FR-D13. Noise scoring, ASR, codec augmentation, speaker splits and end-to-end
train/serve parity remain subsequent work.

## Verification

```text
uv run --no-project --python 3.11 python -m unittest discover -s tests/unit -p test_audio_preparation.py -v
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python -m unittest discover -s tests/unit -p test_audio_conditioning.py -v
```

Tests use fictional generated PCM and temporary directories. No real audio or
filled source manifest belongs in Git. The portable project state records only
aggregate results; `.local/data-location.json` locates this machine's data.
