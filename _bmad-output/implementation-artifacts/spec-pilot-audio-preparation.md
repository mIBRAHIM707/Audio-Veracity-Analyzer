---
title: 'Prepare the downloaded multi-speaker audio pilot'
type: 'feature'
created: '2026-10-03'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
---

<frozen-after-approval reason="user authorized preparation and continuation">

## Intent

Prepare the 50 downloaded genuine clips from ten additional CSALT speakers into
separate 3–6 second candidate WAV clips with traceable source/speaker/sample offsets
and a review report. Preserve all originals. Use conservative energy-based pause
boundaries and retain uncovered intervals rather than forcing cuts through
continuous speech. Report low level, quiet content and possible digital clipping
as review flags, never listening acceptance. Run entirely on CPU and store real
audio/manifests outside Git. No split assignment, cloning, model fitting or remote
audio processing is authorized by this task.

</frozen-after-approval>

## Implementation Notes

- Existing dirty baseline was already explained to the user; their instruction
  to begin and continue authorizes this additive work, not discarding prior edits.
- Base HEAD: `09541b596cb7dae3d85c61028276d59161552098`.
- No unresolved user intent choices or irreversible operations. Small standalone
  stdlib CLI, fictional tests and documentation; no existing caller depends on it.
- Source input: external pilot-report-speakers02-11.json files list. Existing
  intake validator validates metadata only; do not change its contract.
- New owned paths: data/prepare/prepare_audio.py and README.md (coordinator);
  tests/unit/test_audio_preparation.py and docs/qa/audio-preparation-test-cases.md
  (actual GPT-6 Astra). Shared state updates owned by coordinator.
- Related work: issue #11; partial FR-D8, FR-D12, FR-D13 groundwork. Initial scope
  supports 16 kHz mono PCM16 sources only; unsupported formats are quarantined.
- Keep exact source samples in derivatives. Gain normalization, VAD/ASR, codec
  augmentation and train/serve parity are later stages, not claimed complete.
- Launcher succeeded using relative paths from unchanged repository cwd; no BMAD
  source patch or reinstall needed for the Windows quoting failure.
- Final verification: 52 Python tests discovered, 51 passed, 1 symlink test skipped
  because Windows denied link creation. Foundation and repository checks passed.
- Real v2 pilot: 66 candidates, 24 review flagged, 287.607 retained seconds and
  98.439 uncovered seconds. Exact sample accounting and immutable hashes verified.
- No browser review layer applies to this CLI. One context-free independent code
  reviewer ran; Astra separately designed and executed fictional regression tests.

## Review Triage Log

- Medium, patched: source size check before unbounded read allowed growing or
  nonregular input; regular-file checks and bounded reads now enforce the limit.
- Medium, patched: JSON loaded before bounds; manifest input now capped at 8 MB
  before parsing. Regression coverage exercises both input size limits.
- Medium, patched: check-then-rename could replace a concurrent empty directory on
  POSIX; atomic mkdir now reserves output. Publication writes an INCOMPLETE marker
  until clips and report have moved. Interrupted outputs are never reused.
- Medium, patched: zero-candidate sources lacked actionable reasons; explicit
  outcomes and summary counts identify short/quiet/no-boundary sources and coverage.
- Independent reviewer rechecked all four fixes with no unresolved findings.
