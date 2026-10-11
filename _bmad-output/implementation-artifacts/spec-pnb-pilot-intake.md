---
title: 'Small Punjabi public-source intake and preparation'
type: 'feature'
created: '2026-10-04'
status: 'in-progress'
route: 'oneshot'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent">

## Intent

Import a small multi-speaker Punjabi pilot from the official Meta pnb_Arab
training partition, retaining immutable archive and original audio bytes and
honest publisher labels. Decode copies for shared CPU preparation, preserve
speaker/shard/row/recording lineage, and report actual coverage and QA. Do not
claim Majhi coverage, cloning consent or final training-set eligibility. Leave
publisher dev/test untouched. No commits, pushes or external audio upload.

</frozen-after-approval>

## Implementation Notes

- One verified first train shard initially:464,239,672bytes,
  SHA d8ad5551672394f4f9995ffa3f71f3065f611a121f44ca1170a2780fde4f812d,
  revision8648ba8946377697b427ae952076e49fc0e5e44d. Inspect metadata before claiming
  coverage. Select two recordings per present publisher speaker, capped30total;
  second train shard only if needed for a useful multi-speaker pilot.
- New `data/collect/meta_pnb_pilot.py`, optional `requirements-pnb.txt`, Astra
  fictional Parquet/FLAC tests in `tests/pnb/`, provenance extension and docs.
- Verify full publisher archive SHA/size before decoding. Read metadata without
  audio to select deterministically by revision/shard/row hash; iterate bounded
  audio batches to extract only selected rows. No unbounded whole-audio to_pylist.
- Preserve selected FLAC bytes exactly in external raw storage; decode PCM16 WAV
  copies with soundfile, preserving native rate/channels. Reject unsupported
  subtype/format rather than silently lossy converting. Current preparation will
  perform explicit canonicalization. Native WAV10MBlimit remains.
- Language pnb from publisher pnb_Arab; label genuine. Keep original split train,
  dialect/consent unknown, CC BY4.0 attribution and transcript at source level.
  Prompt_id/segment_id not globallyunique: source_recording_id uses pinned source
  shard path and row; speaker grouping is source-scoped and never split-scoped.
- Hash archive, extracted FLAC and decoded WAV separately. Output manifest uses
  decoded WAV path/hash as immediate preparation source and carries original
  audio+archive lineage; generated cuts have no aligned transcript claim.
- Reject invalid metadata/paths/hashes before mutation; never overwrite external
  originals/output, fail closed with INCOMPLETE on publication failure. Pure
  selection plus I/O edges; reuse current signal inspection and prepare pipeline.
- QA fictional selection/malformed/corruption/native-decode/lineage/publication
  tests, independent real hashes/decode and sample accounting, reviewer pass.
