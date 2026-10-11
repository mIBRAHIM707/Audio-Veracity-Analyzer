# Balanced pilot conditioning handoff

Completed 3 October 2026. Todo item 4; existing issue #11 context.
Base/current HEAD: `ad13029fa283183c23e84d736ac424b777e775c3`.
No commits, pushes or PRs created for this change; user explicitly deferred Git actions.

## Result

340 immutable genuine originals -> 450 derived 3–6 second candidates, 32.641 minutes.
All 17 source speaker folders remain represented, 10–40 outputs each; do not call
the derived inventory speaker-balanced. Nine originals needed 20→16 kHz conversion.
The other 331 were already 16 kHz mono. Every output is mono PCM16 at 16 kHz and
approximately −3 dBFS sample peak (integer amplitude 23198). Zero source quarantines;
154 candidates carry heuristic signal flags, not automatic rejection decisions.
All 520 stored original hashes remain unchanged.

Retained: 1,958.4425 s; uncovered: 771.6715625 s. Source coverage: 224 full, 55 partial,
59 without valid pause segments, two too short. No forced mid-speech cuts added.
Publisher labels/use scope are retained, but new cuts have not passed listening QA.

## Implementation and ownership

- Coordinator: `data/prepare/conditioning.py`, `prepare_audio.py`, README, pinned
  `requirements-audio.txt`, CI audio dependencies, CLAUDE/PRD/state/spec updates.
- Actual GPT-6 Astra agent: `tests/unit/test_audio_conditioning.py` and
  `docs/qa/audio-conditioning-test-cases.md`, 16 substantive fictional cases.
- Independent reviewer: numeric clipping probe, three findings, fix verification;
  no implementation edits.

Pure transforms downmix before soxr HQ resampling and deterministic PCM16
quantization. Segment selection uses canonical pre-gain audio; per-clip gain follows.
The legacy default still copies exact source samples. Original WAV hash, source
inventory ID, native rate/count, canonical PCM hash, 16 kHz sample offsets, component
versions, per-clip gain and final WAV hash form the auditable chain. No labels affect
transforms. Whole-source native clipping and resampling-saturation flags deliberately
apply to all its candidates; they are conservative rather than localized annotations.

## QA and review

```text
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python -m unittest discover -s tests/unit -q
uv run --no-project --python 3.11 python scripts/check_foundation.py
uv run --no-project --python 3.11 python scripts/repository_guard.py
```

Final full suite: 92 discovered, 90 passed, two existing Windows symlink skips.
One preceding run failed during `TemporaryDirectory` cleanup with WinError145
after the directory-source test assertions passed; subsequent inspection found
the remaining directory empty. No assertion was weakened or cleanup ignored.
An unchanged full rerun passed in17.515s. Root cause of the transient OS cleanup
failure is unconfirmed; preserve this evidence if it recurs.

All16 conditioning cases passed independently for Astra and reviewer. They cover
multi-rate tone fidelity, >40dB out-of-band attenuation, stereo cancellation,
resampling saturation, native clipping, silence/nonfinite safety, peak normalization,
immutable provenance, mixed malformed-batch quarantine and exact legacy behavior.
Review findings all fixed: native clipping evidence lost during downmix; missing
inventory join ID; missing conditioned mixed-batch failure/recovery evidence.

Foundation: eight shared roles, 16 adapters, 29 BMAD skills checked. Repository
guard passed. Browser code unchanged; this CLI work did not require new Playwright
cases. The previous 17 browser tests remain historical evidence, not a new run.
Hosted CI was not run.

## Local evidence and resume

`.local/data-location.json` points to conditioned balanced v2 `report.json` and
`verification.json`. v1 is preserved from before review fixes; use v2. External
verification reconstructed each normalized slice independently using NumPy/soxr,
checked every output hash/format/peak, source IDs, native/canonical lengths, and
gap-free nonoverlapping retained+uncovered coverage. The machine-local verifier
is `.local/verify_conditioned_batch.py`; original inventory recheck is
`.local/verify_balanced_csalt.py`. No recordings/manifests were added to Git.

Next is todo item5: separate existing-synthetic benchmark intake with documented
generator/licence/source lineage, then item6 CPU detector smoke test. Do not equate
this benchmark with same-consented-speaker AVA training eligibility. Telephone
8 kHz domain conversion/codecs, Silero VAD, noise/ASR checks, final splits, sampled
listening and full train/serve parity remain outstanding. No participant recording
or cloning approval was obtained. User's no-commit instruction persists.
