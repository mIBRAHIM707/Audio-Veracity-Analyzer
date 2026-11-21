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
Every entry point (preparation, augmentation, the analysis service and the prepared-
output verifier) accepts only one `fmt ` chunk before `data` with plain PCM format tag
1 (`wav_format.py`); WAVE_FORMAT_EXTENSIBLE, float and µ-law are refused on every
Python version.

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

## G.711 telephone codec (partial backlog 3.3)

`codecs.py` provides G.711 mu-law and A-law `encode`/`decode` (Sun reference
tables, checked exhaustively against CPython 3.11 `audioop`) and
`telephone_roundtrip(canonical, law)`: soxr HQ to 8 kHz, encode, decode, return
8 kHz PCM16 plus a lineage record (codec, rates, saturation count, encoded and
decoded hashes). Return to the model grid with `canonicalize(decoded, 8000, 1)`.
`drop_packets` (or `telephone_roundtrip(..., loss_rate=, seed=, concealment=)`)
applies seeded per-packet (20 ms) loss from 0 to 5 % with silence or
repeat-last-packet concealment and records seed, counts and a lost-index hash.
`derive_loss(master_seed, clip_id)` gives each clip its own plan (rate uniform
0-5 %, seed); `telephone_roundtrip(..., loss_plan=plan)` records the derivation
version, master seed and exact manifest clip ID so the chain can be rebuilt.
`drop_late_packets(encoded, law, jitter_ms, buffer_ms, seed)` models jitter as
late-packet loss: each packet gets a seeded exponential extra delay (mean
`jitter_ms`, at most 100 ms); a packet later than the fixed playout buffer
(`buffer_ms`, at most 400 ms) is concealed like a lost one. Packets are never
reordered. `derive_jitter(master_seed, clip_id)` gives each clip a plan (mean
delay uniform 0-20 ms behind a 60 ms buffer, so at most about 5 % late packets;
seed independent of `derive_loss`). `telephone_roundtrip(..., jitter_plan=plan)`
applies it together with any loss: lost and late packets form one mask that is
concealed once, so repeat concealment only repeats packets that arrived in time.
The record keeps `packet_loss` and `jitter` separately plus `packet_concealment`
(union count and fraction, overlap count, union index hash). The 5 % bounds are
expected rates per mechanism, so loss and late packets together can conceal more
than 5 % of a clip's packets; the PRD does not say whether FR-D6's 0-5 % bounds the
sum. Plans for different clip IDs or master seeds are refused.
Concealment modes: `silence`, `repeat` (current default) and `repeat_then_silence`,
the architecture §2.2 jitter-buffer rule ("repeat last 20 ms frame, then zero-fill
after 3 consecutive losses", read as three repeats, silence from the fourth lost
packet; a burst with nothing received before it is silence throughout). The switch is
a hard cut, not a fade, as in §2.2. Records carry `concealment` and `repeat_limit` (3,
or None for the other modes). Zero-fill is the G.711 silence code: 0 for mu-law, +8
for A-law, so a serving buffer that zero-fills decoded PCM differs by +8 on A-law.
Training chains meant to match serving need `concealment='repeat_then_silence'`; the
manifest wiring must choose it explicitly, and the serving jitter buffer should reuse
`REPEAT_LIMIT` and `_conceal` rather than copy them.
`augment.py` (below) wires it over prepared clips; GSM and AMR-NB are in
`ffmpeg_codecs.py` (next section) and are also selectable in `augment.py`.

## GSM 06.10 and AMR-NB through ffmpeg (partial backlog 3.3, 14 Nov 2026)

`ffmpeg_codecs.py` runs the FR-D6 GSM 06.10 (libgsm) and AMR-NB 4.75/7.4/12.2 kbps
(OpenCORE AMR-NB, RFC 4867 storage, DTX off) roundtrips through a local ffmpeg over
pipes: `Ffmpeg(path)` checks the version is 9.0.2, that both codec libraries are
present and (optionally) a SHA-256 pin, and records the executable hash in every
record. `telephone_codec_roundtrip(canonical, codec, ffmpeg, rate=16000)` mirrors
`codecs.telephone_roundtrip` (soxr HQ to 8 kHz, encode, decode, 8 kHz PCM16 plus
lineage: codec, mode bit rate, encoder/decoder, encoded and decoded hashes, frames).
Encoded streams are checked against known frame sizes (GSM 33 bytes; AMR 13/20/32
bytes with the mode bits in each frame header); decode refuses foreign or truncated
streams itself, because ffmpeg 9.0.2 decodes an AMR-WB header forced as AMR. Measured
and pinned by tests (14 Nov, every remainder 0-159): the AMR stream has
floor((samples + 50) / 160) + 1 frames (ffmpeg's 50-sample padding plus a flush frame),
and decoded AMR lags the input by exactly 40 samples (5 ms) in every mode, GSM by 0.
`roundtrip` drops that delay and cuts to the input length, so decoded sample i lines
up with input sample i (a test re-measures lag 0 on seeded noise); the record keeps
`delay_compensated_samples` and the exact ffmpeg arguments. `AVA_REQUIRE_FFMPEG=1`
turns the real-ffmpeg skip into a failure. AMR-NB frame loss (14 Nov):
`roundtrip(..., loss_plan=codecs.derive_loss(seed, clip_id))` drops 20 ms frames with
the G.711 Bernoulli mask rule and replaces each by an RFC 4867 NO_DATA frame, so the
OpenCORE decoder conceals natively and timing is kept; the record holds `frame_loss`
and `received_sha256`. GSM frame loss (`drop_gsm_frames`) uses the same plans with
repeat_then_silence substitution: architecture §2.2 parity (hard cut after 3 repeats),
not GSM 06.11 gradual muting. Measured 14 Nov in an 8-frame burst inside noise: the
first silence frame decodes at 15.6 % of the speech RMS (decoder state), later ones at
1.5 %. A GSM loss spec must name `"concealment": "repeat_then_silence"`; AMR loss specs
name none (decoder-native). Jitter (late frames, `jitter_plan` from `derive_jitter`,
same model as G.711) joins the loss mask and is concealed once; records carry
`frame_loss`, `frame_jitter`, `frame_concealment`, and the 16 kHz wrapper also exposes
them under G.711's `packet_loss`/`jitter`/`packet_concealment` names (AMR mask counts
include ffmpeg's padding and flush frame). Both selectable in `augment.py` specs. A
live AMR-NB decoder has no encoder padding (parity is 6.x).

Tool: Gyan.FFmpeg 9.0.2 full build, GPL-3.0 as built (libgsm: permissive TU Berlin licence; OpenCORE AMR: Apache-2.0),
installed per user with `winget install --id Gyan.FFmpeg --version 9.0.2 --exact`;
`ffmpeg.exe` SHA-256 589e50b766d251afdf181dd664d40bd94407e200b019989fd7468c7d118a28d0,
227,823,104 bytes. It is run as a separate program, not linked. Point `AVA_FFMPEG` (or
the `Ffmpeg(path)` argument) at it; the winget command alias is not visible to every
process. Tests: `tests/unit/test_ffmpeg_codecs.py` (19 Claude-authored; the 7 real-ffmpeg
cases skip when it is absent, as in CI). Fictional tones and seeded noise only:
plumbing, not quality.

### Opus VoIP path (backlog 3.4, PRD FR-D6; 15 Nov 2026)

`opus_8`, `opus_12`, `opus_16`: libopus (in the same ffmpeg build; BSD-3-Clause) at
8/12/16 kbps CBR, 20 ms frames, `-application voip`, Ogg Opus (RFC 7845) over the pipe
with `+bitexact` muxing (without it the Ogg serial number is random). The PRD allows
Opus or G.729; ffmpeg has a G.729 decoder but no encoder, so G.729 is not offered.
`roundtrip` appends one 20 ms frame of zeros, decodes at 48 kHz, removes
`OPUS_DELAY_48K` = 4 samples (measured 15 Nov on band-limited seeded noise at
8/12/16/64 kbps, two seeds, best alignment in a 0-12 search every time; re-measured by a
test), brings it to 8 kHz with the shared soxr HQ step and cuts to the input length.
The stream is checked strictly: page checksums (RFC 3533 CRC, known-answer tested),
one logical stream, BOS/EOS, OpusHead v1 mono with pre-skip 312 and 8 kHz input,
OpusTags, packet count `ceil((6n + 312) / 960)` (tested on every partial-frame length),
CBR packet size, one 20 ms mono frame per packet (code 0, or code 3 with one frame,
which libopus uses to pad CBR packets).

Loss and jitter use the same per-clip plans and mask rule as G.711 over audio packets
(the mask covers the pre-skip and padding packets, so a loss there counts in
`concealed_fraction` without changing the output, as for AMR's flush frame). A lost or
late packet becomes a TOC-only code-0 packet (same configuration, one zero-length
frame; TOC layout RFC 6716 §3.1); libopus treats it as lost and runs its own
packet-loss concealment (measured with ffmpeg 9.0.2, including a lost first packet);
ffmpeg's Ogg demuxer refuses truly empty packets. Pages are
rebuilt with the same granule positions and new checksums; records carry
`applied_via: opus_toc_only_packets`, `concealment: opus_decoder_native_plc`. Measured:
the 48 kHz decoder output before the first lost packet is bit-identical; after the
soxr step earlier 8 kHz samples can move by one rounding step (block FFT filtering).
Augmentation specs take `"codec": "opus_12"` (etc.) with `"concealment": null` and
`packet_loss`/`jitter` booleans, like AMR-NB. Tests: `tests/unit/test_ffmpeg_opus.py`
(Claude-authored, 15 Nov 2026; real-ffmpeg cases skip in CI). Not modelled: Opus
in-band FEC, DTX, VBR, bandwidth switching, and RTP packetization (6.x).
Units: Opus records add `delay_rate: 48000` and `decode_rate: 48000`, so their
`delay_compensated_samples` (4) and `decoded_samples_before_alignment` count 48 kHz
samples; GSM/AMR records keep their earlier keys unchanged (no `delay_rate`) and count
8 kHz samples. Because `Ffmpeg` checks every codec's encoder and decoder at start-up, an
ffmpeg build without libopus is now refused even for GSM/AMR-only runs (the pinned
Gyan build has it).

## Additive noise at SNR (partial backlog 3.4, PRD FR-D7)

`noise.py` `mix_at_snr(speech, noise, snr_db, seed)` adds same-rate mono PCM16
noise at 0 to 20 dB SNR from a seeded offset (wrapping when the noise is short),
rounds and clips to int16, and records the SNR basis, target and achieved SNR,
seed, offset, wrap/repeat count, speech/noise/output hashes, gain and clipped-sample
count. Callers must check clipping. `derive_noise(master_seed, clip_id)` gives
each clip its own plan (SNR uniform 0-20 dB, offset seed; independent of the
packet-loss tag) and `mix_planned(speech, noise, plan)` records the derivation,
master seed and clip ID and refuses altered plans. SNR is whole-clip mean square by
default. Speech-active SNR (15 Nov 2026): `speech_mask(pcm16k, vad)` uses the serving
windower's frame rule and threshold (512-sample float32 frames int16/32768, probability
>= 0.5, last part-frame zero-padded; checked frame for frame against `SpeechWindower`),
but on the clean parent clip with the VAD reset per clip, whereas serving runs on the
received stream with state carried across the call. It returns the mask and the
per-frame decisions. `mix_at_snr(..., active_mask=mask, mask_source=...)` takes the
speech power over the active samples only (at least 0.5 s, else `InsufficientSpeech`;
noise power still over the whole excerpt). This is mean-square power over Silero speech
frames, not ITU-T P.56 active speech level. A VAD exception or invalid probability is a
systemic `VadError`. Records without a mask are unchanged.
No noise corpus is included; `augment.py` applies it when a chain names a noise file.
Room impulse responses are `reverb.py`, described with the chain below.

## Recorded augmentation chain (backlog 3.3/3.4 wiring; FR-D6, FR-D7, FR-D13)

`augment.py` applies one declared chain to every clip of a published prepared root
(never modified) and writes **augmented copies** to a new root outside the
repository. Run it as a module (a script-directory `codecs` import would shadow the
standard library):

```text
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python -m data.prepare.augment --prepared-root <prepared root> --spec <chain.json> --output-root <new root> [--noise <noise.wav>] [--rir <rir.wav> ...]
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python -m data.prepare.augment --prepared-root <prepared root> --spec <chain.json> --verify <augmented root> [--noise <noise.wav>] [--rir <rir.wav> ...]
```

The specification has exactly `chain_version` (`ava-telephone-chain-v1`),
`master_seed`, `codec` (`g711_ulaw`/`g711_alaw`), `concealment` (no default; use
`repeat_then_silence` to match the serving buffer), `packet_loss` and `jitter`
(booleans; plans from `derive_loss`/`derive_jitter`) and `noise` (null, or
`{"sha256": <PCM SHA-256 of a 16 kHz mono PCM16 WAV>, "label", "source_id",
"licence_ref"}`). Order: noise at 16 kHz (`derive_noise`, before the channel), G.711
roundtrip with one lost-or-late mask, then `canonicalize` back to 16 kHz. No
normalization follows. The derivation clip ID is content-based,
`<source_sha256>:<start_sample>:<end_sample>`, so same-named files in different
corpora do not share draws. A clip whose noise mix would clip (error row keeps
`refusal: noise_clipping` and the drawn `snr_db`; the summary counts them), or that
is digital silence under noise, is listed in `errors` and others proceed (exit code
1). Refusals can skew the 0–20 dB spread toward high SNR on loud clips; check the
refusal counts per SNR band before training. Parent clips are peak-normalized to
−3 dBFS, so no headroom is added before mixing (open data decision). Each copy row carries the parent clip path and hash,
parent provenance (review/listening statuses only as `parent_review_status` and
`parent_listening_status`; the copy itself starts `review_status: unreviewed`; its
length is `chain.output_sample_count`, which can differ by one sample from the
parent range for odd-length parents), `augmented_copy: true`, `counts_toward_clip_total: false`, the
full per-step chain record and review metrics. The report binds the parent report
hash and the specification hash. `--verify` rebuilds the whole report read-only and
compares every field (labels, speaker IDs, provenance, flags, errors, summary), the
expected specification, the audio bytes at rebuilt paths and unlisted files; it
reuses the writer's build step, so it detects drift and tampering, not a defect
shared with the writer. Chains are not stacked on an augmented root.

GSM/AMR-NB chains (14 Nov 2026): `"codec"` may be `gsm`, `amr_nb_4.75`, `amr_nb_7.4` or
`amr_nb_12.2` with `"jitter": false` and `"concealment": null` (except GSM with loss,
below); `"packet_loss"` may be true
(per-clip `derive_loss` plan; AMR-NB: NO_DATA frames with decoder-native concealment;
GSM: architecture §2.2 repeat_then_silence frame substitution with a GSM-encoded
silence frame, spec `"concealment": "repeat_then_silence"`); pass the pinned ffmpeg with `--ffmpeg <path>` (or
`AVA_FFMPEG`) and, for manifest runs, `--ffmpeg-sha256` (see `ffmpeg_codecs.
PINNED_WINDOWS_SHA256`). The record carries the executable hash, so `--verify` needs
the same binary. A codec-tool failure aborts the run before anything is written.
`"jitter": true` is also accepted for GSM/AMR-NB (late frames joined with loss in one
concealed mask; GSM then names `repeat_then_silence`).

Codec mix (14 Nov 2026): instead of the four channel keys (`codec`, `concealment`,
`packet_loss`, `jitter`) a spec may give `"codec_mix"`: 2-8 distinct channels, each
those four keys plus an integer `"weight"` (1-1000), with `chain_version`,
`master_seed` and `noise` shared. Each copy takes one channel by a seeded draw over
(`ava-codec-mix-v1`, master seed, derivation clip ID), independent of the loss, jitter
and noise draws, with probability weight / total weight (`augment.derive_channel`). The
copy row records `chain.codec_choice` (draw, channel index, codec, weight, the chosen
single-channel spec hash), its file name ends in the chosen codec, and the summary adds
`codec_counts` and `channel_counts`. A mix that can choose GSM/AMR-NB/Opus needs `--ffmpeg`
for every run and verify, even if every clip drew G.711 (no output then depends on the
tool). A root still holds one copy per parent: several codecs for the same parent need
several roots (for example other seeds). Channels differing only in a setting that
cannot matter (G.711 concealment with no loss or jitter) are accepted and counted as
separate channels. Single-channel specs, records and reports are unchanged (pinned by
a test against the pre-mix code).
Example: `{"chain_version": "ava-telephone-chain-v1", "master_seed": 7, "noise": null,
"codec_mix": [{"codec": "g711_ulaw", "concealment": "repeat_then_silence",
"packet_loss": true, "jitter": true, "weight": 2}, {"codec": "amr_nb_7.4",
"concealment": null, "packet_loss": true, "jitter": false, "weight": 1}]}`.
Opus channels (`opus_8`/`opus_12`/`opus_16`, 15 Nov) work in single specs and mixes.
Speech-active SNR (15 Nov): the `noise` object may add `"snr_basis": "speech_active"`
and `"vad_sha256"` (the VAD model's SHA-256, so the spec hash names the VAD); writer,
verifier and the training window loader then need that VAD (`vad=`; CLI
`--vad-model <silero_vad.onnx>`: any ONNX file whose SHA-256 equals the spec's, normally
the pinned Silero 6.2.3; runtime from `requirements-vad.txt`). A clip with under 0.5 s
of detected speech becomes an error row tagged `refusal: insufficient_speech` and
counted in `summary.insufficient_speech_refusals` (check it per label: it can bias the
data); a VAD fault aborts the run before anything is written (verify reports it). Each
copy records `mask_source` (VAD hash, `mask_rule: ava-speech-mask-v1`, threshold, frame
size, minimum, runtime version, per-frame decisions). Rebuilding is runtime-sensitive:
another CPU or onnxruntime build can flip a frame near 0.5 and the verifier then reports
drift, so verify on the machine/runtime that wrote the root. The loader re-runs the VAD
on every clip of such a root (CPU time). Specs without these keys keep the whole-clip
SNR and their hashes. G.729 (no encoder in ffmpeg) remains open.

Room impulse responses (16 Nov 2026, PRD FR-D7): a single or mix spec may add
`"reverb": {"rirs": [{"sha256", "label", "source_id", "licence_ref"}, ...]}` (1-64
distinct 16 kHz mono PCM16 RIRs, at most 2 s each, by PCM SHA-256). Pass every RIR WAV
with `--rir <path>` (repeat; order free; no missing or extra file) to the writer and
`--verify`, and `rir_paths=` to the training window loader. Each copy picks one RIR by
a seeded draw over (`ava-rir-pick-v1`, master seed, derivation clip ID), independent of
the other draws, and the chain becomes room -> noise -> channel -> 16 kHz.
`reverb.apply_rir` takes the RIR's direct path as the first arrival (the first sample
within 20 dB of the peak, since an early reflection can be stronger than the direct
sound), drops the samples before it so the copy is not delayed, and flips a negative
direct path positive (`direct_sign` recorded); keeps the input length (the tail past
the end is cut); and scales the result to the input's whole-clip mean square (level
preserved). Every RIR is checked when loaded (not silent, at most 2 s, direct path no
later than 0.5 s); an unusable RIR stops the run before anything is written. The step
record holds the RIR hash/labels, pool index, direct-path and peak indices, sign,
direct-to-reverberant ratio (+/-2.5 ms), pre-direct and cut-tail energy fractions, gain
and hashes; the summary adds `rir_counts` and `reverb_clipping_refusals` (a copy that
would clip is an error row tagged `refusal: reverb_clipping` with the RIR hash).
FFT rounding can differ in the last bits between platforms or numpy builds, and the
records hold float fields, so, as with the VAD, verify on the machine and numpy that
wrote the root; this includes `models/ava/windows.load_windows`, which runs the verifier
before training. Which RIRs represent a handset or a speakerphone is the RIR entry's
`label`, chosen by a person from the corpus metadata (DRR is recorded, not used to
classify; an open PRD question). No RIR corpus is provisioned yet (see the 16 Nov daily
report). OpenSLR SLR28 (Apache-2.0 bundle, 16 kHz; simulated and real RIRs plus
point-source noises) is the intended source. Not yet checked: its real RIRs come from
RWCP, REVERB 2014 and AIR, whose own terms may differ from the bundle licence (record a
`licence_ref` per source database), and some may be multichannel or longer than 2 s,
which these rules refuse. Channel choice and trimming would be a separate, recorded
preparation step. That step should also review RIRs with a large `peak_index -
direct_index` gap or a low DRR: noise or sweep pre-echo within 20 dB of the peak would
be taken as the first arrival and delay the copy (review L8). Specs without the key keep their hashes and reports.

`tests/unit/test_conditioning_parity.py` checks that this preparation path and the
analysis service (`services/inference/`) condition the same WAV to identical
16 kHz samples and coordinates; the service applies no peak normalization.

## Verification

```text
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python -m unittest tests.unit.test_g711_codec tests.unit.test_packet_loss tests.unit.test_noise_mixing tests.unit.test_conditioning_parity tests.unit.test_augment_chain tests.unit.test_augment_codec_mix -v
uv run --no-project --python 3.11 python -m unittest discover -s tests/unit -p test_audio_preparation.py -v
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python -m unittest discover -s tests/unit -p test_audio_conditioning.py -v
```

Tests use fictional generated PCM and temporary directories. No real audio or
filled source manifest belongs in Git.

To verify a published output root independently, read-only, run
`scripts/verify_prepared_output.py` with the same input manifest and source root
and the mode the run used (`condition` or `exact`). It does not import this
package: it re-derives conditioning, metrics, flags, pause boundaries and the
retained-sample optimum, rebuilds every clip byte-for-byte, hashes original
audio (e.g. FLAC) when rows name it, scans `report.json` for transcript text and
Arabic-script characters and checks root contents. It prints one aggregate
`PASS`/`FAIL` line. Earlier evidence files in the root need `--allow-extra NAME`.

```text
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python scripts/verify_prepared_output.py --manifest <input.json> --source-root <raw root> --output-root <prepared root> --mode condition
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python -m unittest tests.unit.test_verify_prepared_output -v
``` The portable project state records only
aggregate results; `.local/data-location.json` locates this machine's data.
