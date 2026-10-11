---
title: 'Condition and prepare the balanced genuine pilot'
type: 'feature'
created: '2026-10-03'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent">

## Intent

Prepare the 340 selected CSALT genuine originals consistently on CPU while
preserving their bytes, publisher metadata and source lineage. Convert supported
PCM16 mono/stereo sources to 16 kHz mono using soxr HQ, select existing 3–6 second
pause-bounded candidates, and peak-normalize each candidate to −3 dBFS.
Automated checks apply to all files; individual listening is not an ingestion gate.
Keep outputs outside Git and do not commit, push or open a PR until instructed.

</frozen-after-approval>

## Implementation Notes

- No intent gaps or irreversible actions. Small extension of existing preparation;
  existing exact-sample default remains supported. Reuse hash/path/bounds checks,
  segmentation and staged publication from `data/prepare/prepare_audio.py`.
- Own `data/prepare/conditioning.py`, preparation integration, dependency pins,
  CI install step, documentation/state and external batch artifacts. Astra owns
  fictional test cases and new test module. Independent reviewer owns no edits.
- Class-independent pure transformations use NumPy and soxr HQ; rate bypass for
  16 kHz. Stereo mean before resampling. Quantize deterministically to PCM16.
  Segment before gain so gain cannot alter current pause proposals. Silence stays
  silent; record gain and pre/post signal metrics, no LUFS claim.
- Coordinate offsets refer to canonical 16 kHz mono samples. Original WAV hash,
  native sample count/rate and canonical PCM hash preserve the transformation chain.
- This is source preparation, not telephony 8 kHz simulation, Silero VAD, codecs,
  train/serve parity acceptance, listening validation or split assignment.
- Validate fictional boundary/signal/failure cases, preserve legacy tests, run
  real 340-source batch and independently check original hashes, output formats,
  peak target, metadata and retained/uncovered sample accounting. Browser behavior
  unchanged; no additional Playwright cases needed for this CLI-only change.
- First external run completed 340 sources -> 450 candidates without quarantines.
  Independent review found native clipping could disappear in downmix; added
  worst-channel native clipping metadata and conservative source-level flags.
  Also retained inventory clip ID as source_clip_id and added conditioned mixed
  valid/malformed-batch recovery coverage. Re-running into v2 preserves v1.
- Final v2 independently verified: 340 sources, 450 clips, 1,958.4425 seconds,
  zero quarantines, 154 flags. All 520 original hashes remain unchanged. Final
  suite92 discovered/90pass/2Windows symlink skips;16 Astra casespass. One prior
  Windows temp cleanup failure documented in handoff; unchanged rerun passed.
  All review fixes independently verified. No new browser behavior. No commit
  performed because the user's explicit instruction overrides workflow commit step.

## Review Triage Log

- Medium / fixed: native clipped stereo plus silent channel lost warning after
  averaging. Measure worst-channel near-full-scale fraction before downmix and
  retain warning on source and all its candidates; targeted regression added.
- Low / fixed: source path/hash permitted recovery but omitted direct inventory
  join ID. Preserve original clip_id as source_clip_id on sources and candidates.
- Medium verification gap / fixed: conditioned mixed-batch quarantine had no
  integration evidence. Astra added valid-native plus hash/truncation/unsupported
  siblings and checked continued publication and immutable sources.
