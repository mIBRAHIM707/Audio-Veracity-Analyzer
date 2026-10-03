# Pilot audio preparation

CPU-only standard-library tooling for partial FR-D8/FR-D12/FR-D13 groundwork.
This makes **review candidates**, not an accepted training dataset. There is no
browser component; use unit tests rather than Playwright for this stage.

```text
uv run --no-project --python 3.11 python data/prepare/prepare_audio.py --manifest D:/AVA-Data/raw/csalt-urdu/pilot-report-speakers02-11.json --source-root D:/AVA-Data/raw/csalt-urdu --output-root D:/AVA-Data/processed/csalt-pilot-new-run
```

The output must be a new directory outside the repository and disjoint from the
source directory. A repeated run needs a different output name; existing outputs
are never overwritten. The CLI returns nonzero for malformed input or quarantined
sources. An output with no candidates can still be a valid processing run; inspect
the counts and uncovered ranges rather than treating exit zero as acceptance.

## Input and provenance

Input JSON contains a nonempty `files` list, each with a relative POSIX `path`, a
safe `speaker_id` and the actual source `sha256`. Optional `revision` preserves
the dataset revision. Extra input fields are ignored. Duplicate source paths,
invalid metadata and path traversal abort before output creation. Missing,
symlinked, corrupt, hash-mismatched or unsupported sources are quarantined in the
report while other sources proceed. Pilot limits: 10,000 entries and 10 MB per
source; manifest input is capped at 8 MB. Inputs must be regular files. Sources
must be 16 kHz mono 16-bit PCM WAV; no implicit resampling occurs.

The output contains `clips/` and `report.json`. Every clip records source path,
source hash, speaker ID, output hash and half-open sample offsets
`[start_sample, end_sample)`. WAV samples are copied exactly; no gain, denoising,
codec transform, split assignment or label-dependent treatment occurs. The same
algorithm can later process both classes, but full synthetic lineage is not yet
supported. Keep the original source manifest alongside the report.

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

Every candidate has `review_status: pending_listening`. Intelligibility, language,
background noise, transcript accuracy and rights approval remain unassessed.
Normalization, source-wide quality acceptance, codec augmentation, speaker splits
and end-to-end train/serve parity remain subsequent work.

## Verification

```text
uv run --no-project --python 3.11 python -m unittest discover -s tests/unit -p test_audio_preparation.py -v
```

Tests use fictional generated PCM and temporary directories. No real audio or
filled source manifest belongs in Git. The portable project state records only
aggregate results; `.local/data-location.json` locates this machine's data.
