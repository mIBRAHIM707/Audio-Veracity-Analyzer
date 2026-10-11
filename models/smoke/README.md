# Local CPU detector smoke test

This adapter executes the original AntiDeepfake MMS-300M binary classifier on a
small, explicitly selected audio manifest. It does not implement AVA's SLS head,
establish numerical parity with fairseq, or measure accepted detection accuracy.

The [official model card](https://huggingface.co/nii-yamagishilab/mms-300m-anti-deepfake/blob/7928e119e09f616b2ce698892e00ca22dbaa40bf/README.md)
defines the waveform normalization, temporal pooling, original trained classifier,
and fake/real class order. Its pinned
[configuration](https://huggingface.co/nii-yamagishilab/mms-300m-anti-deepfake/blob/7928e119e09f616b2ce698892e00ca22dbaa40bf/config.json)
defines the backbone dimensions. NII/Yamagishi Lab publishes the weights under
CC-BY-NC-SA-4.0; retain the upstream attribution and licence with local artifacts.

The implementation uses the
[torchaudio fairseq conversion helper](https://docs.pytorch.org/audio/stable/_modules/torchaudio/models/wav2vec2/utils/import_fairseq.html)
from the pinned torchaudio version. It converts the `m_ssl.model.*` backbone keys
and loads `proj_fc.*` into the original 1024-to-2 linear layer. Both loads are
strict. Only `mask_emb`, `quantizer.*`, `project_q.*`, and `final_proj.*` under the
backbone prefix are omitted as pretraining-only tensors; every omission appears
in the report. No newly initialized classifier or missing-weight fallback is
allowed. Meta initialization and assigned checkpoint tensors avoid allocating a
second full random backbone. The positional convolution's legacy weight-norm
names are adapted only when required by the destination state keys.

Immediately before deserialization, the loader reads a bounded immutable byte
snapshot and verifies the pinned 1,269,622,944-byte size and SHA-256 on those same
bytes. Safetensors deserializes that snapshot without reopening the pathname.
This adds temporary checkpoint-sized memory during loading; the sampled RSS
report includes that phase. The byte snapshot is released after deserialization.

## Run

Provision the optional environment separately from inference. The first command
may download packages; the second uses that provisioned environment offline:

```text
uv run --no-project --python 3.11 --extra-index-url https://download.pytorch.org/whl/cpu --with-requirements requirements-model.txt python models/smoke/antideepfake.py --help
uv run --offline --no-project --python 3.11 --extra-index-url https://download.pytorch.org/whl/cpu --with-requirements requirements-model.txt python models/smoke/antideepfake.py --model-root D:/AVA-Data/models/antideepfake-mms-300m --manifest D:/AVA-Data/manifests/cpu-smoke-v1.json --source-root D:/AVA-Data/processed --output-root D:/AVA-Data/reports/cpu-smoke-v1 --threads 4
```

The adapter makes no network requests and accepts only local safetensors. It
does not use pickle, Hugging Face loading APIs, or runtime asset downloads.

The model directory must contain `model-source.json` with the exact official
`repo`, pinned `revision`, and a `files` list of `{path, sha256, size}` entries,
including `model.safetensors`. The runner verifies every listed file before
constructing the detector and additionally requires checkpoint SHA-256
`9bd5d9785bf72f91dfca8508651354946c4b307921cb358505e1b94035102224`, pinned from
the publisher download. Required `class_order` must be `['fake', 'real']`, and
required `input_rate` must be 16000. Model weights and private manifests stay
outside Git. Incomplete model/manifest bundles and incomplete source directories
are rejected before inference.
JSON NaN and Infinity constants are rejected in model metadata and the audio
manifest during preflight, including constants in otherwise unused fields.

The input JSON has a `files` list with 1–100 entries, each containing `path`,
`sha256`, and publisher `label` (`genuine` or `synthetic`). Other row metadata,
including source IDs and benchmark eligibility, is retained in each trial.
Paths are relative to `--source-root`. Inputs must already be 16 kHz mono PCM16
WAVs, 3–6 seconds long. The runner checks every hash and decodes every file before
inference; it does not resample or silently drop failed inputs. It preserves the
manifest order, so the caller owns deterministic balanced sample selection.
Manifest files are bounded to 2 MB and individual audio files to 10 MB.

## Evidence and limits

The model uses evaluation and inference modes on CPU. Whole-waveform layer
normalization follows the official card, then temporal mean pooling feeds the
original classifier. Report arrays retain class 0=fake/synthetic and class
1=real/genuine. Softmax values are not calibrated confidence estimates. An argmax
label is reported without claiming a validated operating threshold.

`report.json` records input provenance, logits, probabilities, per-file latency,
real-time factor, loading coverage, package versions, CPU thread count, platform,
and model/manifest hashes. Loading and one warm-up call are separate from measured
trials. Trial timing includes normalization, model execution, and score conversion;
it excludes preflight reads/hashing and model loading. These are sequential,
single-process measurements, not concurrent telephony capacity evidence.

Process RSS is sampled every 50 ms during model loading, warm-up and trials, with
an additional sample at each end. `sampled_peak_rss_bytes` is the maximum observed
sample, not an exact allocation peak; it includes other memory in the process.
The sampler stops on failure. A new external output directory is required, and
an `INCOMPLETE` marker remains if execution or report publication fails. Consume
the report only after that marker has disappeared. Existing reports and audio
are never intentionally overwritten.

Tiny test models exercise contracts and failure paths. Actual checkpoint loading
and CPU execution must be recorded separately; neither establishes fairseq
numerical parity, telephone robustness, calibration, or AVA product acceptance.
