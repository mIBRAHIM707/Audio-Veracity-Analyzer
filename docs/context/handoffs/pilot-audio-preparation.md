# Pilot audio preparation — 3 October 2026

User authorized the next preparation stage for the 50-clip multi-speaker CSALT
pilot. Related issue: #11. Requirements: partial FR-D8/FR-D12/FR-D13, FR-E3.
Base HEAD: `09541b596cb7dae3d85c61028276d59161552098`.

Implemented a CPU stdlib CLI in `data/prepare/prepare_audio.py`, with usage and
limitations in its README. Originals remain immutable; outputs are separate.
Hash-verified 16 kHz mono PCM16 sources become 3–6 second candidate segments only
where energy-pause boundaries allow. Reports retain speaker/source/hash/sample
lineage, quality flags and all uncovered ranges. No normalization or resampling.

Actual GPT-6 Astra authored `tests/unit/test_audio_preparation.py` and
`docs/qa/audio-preparation-test-cases.md`. A separate context-free reviewer found
four issues: unbounded/nonregular source reads, unbounded JSON input, output
publication race, and unclear zero-candidate reasons. All were fixed and rechecked.

## Verified results

- `uv run --no-project --python 3.11 python -m unittest discover -s tests/unit`:
  52 discovered, 51 passed, one skipped because Windows denied symlink creation.
- `scripts/check_foundation.py` and `scripts/repository_guard.py`: passed.
- Real pilot run v2: 50 inputs, zero quarantined sources, 66 candidate clips,
  24 with review flags; 287.607 seconds retained, 98.439 seconds uncovered.
- Source outcomes: 34 fully covered, seven partly covered, seven without usable
  pause segments, two too short. All 50 original hashes remained unchanged.
- A separate local verifier checked every output hash and exact source sample
  slice and proved retained plus uncovered ranges cover every input sample.

Machine-local `.local/data-location.json` identifies output and report paths.
The previous v1 run is preserved; v2 is the final reviewed run. Real audio,
selection records, manifests and reports remain outside Git. No upload occurred.

## Remaining work

Listening QA has not occurred: no available audio-listening tool in this session.
Energy pauses do not guarantee word boundaries; flags do not prove noise or
intelligibility. Candidate review status is always pending listening. Source use
review, synthetic lineage, normalization, VAD evaluation, codec processing,
speaker-disjoint splits and training acceptance remain unimplemented.

Next: review candidates and uncovered audio before expanding ingestion. Use the
same eventual conditioning for both classes. No Playwright run is required for
this CLI-only change. Existing uncommitted planning/foundation work is preserved.
