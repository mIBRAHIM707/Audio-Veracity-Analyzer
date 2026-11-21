# Phase 3 QA: shared audio path (backlog 3.QA; FR-D6/D7/D12/D13, FR-I3/I4)

**Provenance.** Independent QA by ava-reviewer (Claude), 14 November 2026. Read-only
pass on `feat/4-daily-development` at d4d2b1c over `data/prepare/{prepare_audio,
conditioning,codecs,noise,augment}.py`, `services/inference/analysis.py` (file-mode
conditioning) and `tests/unit/test_{audio_conditioning,conditioning_parity,packet_loss,
noise_mixing,jitter_late_loss,g711_codec,roundtrip_jitter,augment_chain,
verify_prepared_output}.py`, plus `test_audio_preparation.py`/`test_analysis_service.py`
for malformed-input cases. The reviewer's shell was denied, so **no test was run here**;
pass results are the runner's recorded ones. Statements about CPython `wave` behaviour
are from reading, not execution.

**Verdict: conditional pass as infrastructure.** No blocking defect found. File-mode
parity is proven sample for sample, originals are never opened for writing, and all
derivations are seeded, versioned and rebuildable. Conditions: a recorded decision on
the architecture §2.2 deviations (M1), and the malformed-input and immutability test
gaps (L1, L4). These are fixture tests with generated PCM: **not** evidence of detector
accuracy, live/streaming parity, VAD behaviour or real-corpus robustness.

## Requirement coverage

| Req | Code | Tests | Status |
|---|---|---|---|
| A2 / task 1.9 one conditioning path | `conditioning.py:46-68` used by `prepare_audio.py:204`, `analysis.py:205`, `augment.py:139`, `codecs.py:293` | `test_conditioning_parity.py:106-132` | Covered for file mode at the canonical stage. Live/streaming side not built (3.1 remaining) |
| FR-D6 G.711, loss 0-5 %, jitter, chain per clip, copies not counted | `codecs.py`, `augment.py:115-147`, `:250-256` | g711 (audioop oracle, all 65 536 values), packet_loss, jitter_late_loss, roundtrip_jitter, concealment_limit, augment_chain | Partial: G.711 only. GSM, AMR-NB, Opus/G.729 not in scope (3.3 blocked) |
| FR-D7 noise 0-20 dB | `noise.py`, `augment.py:125-132` | noise_mixing, augment_chain (order, clipping refusal) | Partial: clip-level SNR; corpora, RIRs, speech-active SNR not in scope (3.4/3.2 blocked) |
| FR-D12 one script, parity | prepare (cut 3-6 s, peak normalize) then augment (chain) | parity, conditioning, augment_chain | Partial: two stages, no trim, no leakage probes; normalization differs from §2.2 (M1) |
| FR-D13 quality checks, lineage | `prepare_audio.py:83-101`, chain records, `--verify`, `scripts/verify_prepared_output.py` | verify_prepared_output (one tamper per check), augment verify tests | Partial: length, quiet fraction, RMS, clipping flags. Noise score, language and ASR checks not built |
| FR-I3 resample, VAD, partial tail | `analysis.py:103-113`, `:216-218` | parity (zero-padded tail), analysis_service | Partial: resample and partial tail only. VAD/no_speech not in scope (3.2 blocked) |
| FR-I4 4 s / 2 s windows | `analysis.py:31`, `:103-113` | parity (window coordinates rebuild every sample) | Covered for file mode; config rules are phase 4 |

## 1. Sample-level parity

**Proven (file mode).** `test_conditioning_parity.py:106-132` feeds one native WAV to
`process_manifest(condition=True)` and to `AnalysisService.analyze_wav`, rebuilds the
service's canonical int16 stream from the float32 windows the fake detector received
(exact `/32768` grid check, overlap agreement, zero-only padding), and asserts its
SHA-256 equals `canonical_pcm_sha256`, equal sample counts, and that every prepared
clip's bytes equal `peak_normalize(canonical[start:end])`. Ten formats: 8, 11.025,
16, 22.05, 44.1, 48 kHz, mono and stereo, fractional output lengths, near full scale.
This is real sample evidence, not metadata. The augment return step uses the same
`canonicalize(narrow, 8000, 1)` as the service would use for an 8 kHz file, and
`test_augment_chain.py:86-117` checks copies against an independent composition.

**Only metadata or not at all.** (a) Gain: prepared clips are peak-normalized per clip,
augmented copies and the service apply none; the test records this divergence
(`:134-154`) rather than bounding it. The baseline's `F.layer_norm` over each window
(`models/smoke/antideepfake.py:80-86`) cancels gain, but no test pins that claim. (b)
Neither path follows §2.2's batch rule (any input to 8 kHz first) (M1). (c) Untested at
the WAV boundary: 192 kHz, 8 kHz stereo, very short inputs where soxr yields no samples.
(d) Live/streaming parity is not testable yet (no live audio conditioning in
`services/scorer`).

## 2. Malformed formats

Both paths use CPython 3.11 `wave`, so refusals match.

| Case | prepare (quarantine) | service (422 unless noted) | Tested |
|---|---|---|---|
| Not RIFF / garbage / empty bytes | refused | refused | yes, both (`test_audio_preparation.py:184-198`, `test_analysis_service.py:133-145`) |
| Truncated data (even and odd byte cuts, stereo) | refused | refused | yes, both |
| 8-bit, 24-bit, 3 channels, 7999 / 192001 / 6000 Hz | refused | refused | yes (parity `:156-172`, conditioning `:96-103`) |
| Float (tag 3) or other non-PCM tag | `wave.Error`, refused | refused | **no** (L1) |
| WAVE_FORMAT_EXTENSIBLE PCM16 | refused on 3.11; accepted on 3.12+ `wave` | same | **no**: version-dependent (L1) |
| Header-only WAV (0 data frames) | "Empty or truncated" | same | **no** (L1) |
| Declared data size larger than payload | refused (truncated) | **misrouted**: 202 job or 413 from the header before reading (L2) | no |
| Odd trailing byte / RIFF pad | silently ignored (whole frames only) | same | no; acceptable |
| NaN/inf | float WAVs cannot reach conditioning; `_encode` refuses non-finite | same | non-finite only at function level |
| Stereo | mean downmix, worst-channel clipping kept | same | yes |
| Pathological JSON (deep nesting) in manifest or prepared report | `RecursionError` escapes `main` as a traceback (L3) | n/a | no |

## 3. Immutable originals

By construction both tools open inputs only with `'rb'` (`prepare_audio.py:186`,
`augment.py:160`); all writes go to a fresh staging directory beside a new output root
that must not exist and must be disjoint from the source/prepared root and outside the
repository (`prepare_audio.py:41-45`, `augment.py:269-272`), then are published under an
`INCOMPLETE` marker. `verify_augmented` and `verify_prepared_output.py` only read.
Tested: byte equality of sources after runs, including failures and quarantines
(`test_audio_conditioning.py:177,268,311-312`, `test_audio_preparation.py:90,170`); full
tree hash of the prepared root after augmenting (`test_augment_chain.py:170-176`);
output equal to, inside or containing the source root refused with a sentinel intact
(`test_audio_preparation.py:239-249`); output inside prepared root and inside the repo
refused (`test_augment_chain.py:288-310`); symlinked source quarantined
(`test_audio_preparation.py:225-237`, **skipped** on hosts without symlink rights,
likely this Windows runner). Untested: mtime/permissions, a run against read-only
sources, roots unchanged after either `--verify`, a dangling-symlink output root
resolving into the source tree, and augment output placed inside the raw originals
tree, which augment cannot detect because the prepared report does not record the
source root (L4).

## 4. Determinism and lineage

Loss, jitter and noise plans are SHA-256 of a versioned tag, master seed and exact clip
ID, with independent tags; consumers refuse plans not equal to a fresh derivation
(`codecs.py:280-291`, `noise.py:41-47`). Patterns and digests are pinned in tests; reruns
are byte-identical (`test_augment_chain.py:170-178`); records carry numpy/soxr/libsoxr
versions, mask index hashes and input/output hashes, and `--verify` rebuilds every copy.
The derivation ID is content-based (`augment.py:63-71`). Gaps: NumPy does not promise
`Generator` stream stability across versions, so an upgrade breaks `--verify` (detected,
but reported only as "does not rebuild"); odd-length parents (possible for a final
segment, `prepare_audio.py:120`) return from 8 kHz one sample longer or shorter while
the copy keeps the parent's `start_sample`/`end_sample` (L5); copies inherit the
parent's `review_status` and `listening_status` (L6).

## Findings

| ID | Sev | Location | Finding and fix |
|---|---|---|---|
| M1 | Medium | `analysis.py:8-12`, `:205`; `prepare_audio.py:229`; `augment.py:14`; `architecture.md:116,121` | **Trigger:** training a model on prepared clips or copies and serving it with `analysis.py`. **Consequence:** §2.2 says batch input goes to 8 kHz first and every window is peak-normalized to −3 dBFS "in training" too. Code: wideband files go straight to 16 kHz (flagged only), prepare normalizes per 3-6 s clip, copies and service not at all. Parity holds only for gain-invariant models; the planned SLS head may not be. **Fix:** record the decision (PRD/ADR) for both points. Then add a parity test that applies the chosen rule on both sides, and a model test that baseline scores are invariant to gain within a stated tolerance. |
| L1 | Low | `prepare_audio.py:193-202`; `analysis.py:85-99` | Untested malformed WAVs: float/µ-law format tags, WAVE_FORMAT_EXTENSIBLE PCM16 (refused on 3.11, accepted on 3.12+, so a Python upgrade changes intake silently), header-only zero-frame WAV, 0 channels. **Fix:** add these fixtures to both the prepare and service refusal tests. Pin the extensible behaviour explicitly, either refuse on every version or accept PCM16 extensible. |
| L2 | Low | `analysis.py:91-95` | Duration routing trusts the header's `nframes` before reading. A short file whose data-chunk size is inflated gets 202 (job later fails 422) or 413. **Fix:** after `readframes`, route on `len(raw) // (channels*2)`; or refuse `nframes*channels*2 > len(payload)` as 422 before routing. Test both. |
| L3 | Low | `prepare_audio.py:57`, `:295`; `augment.py:197`, `:316`, `:379` | Deeply nested JSON raises `RecursionError` (and an unexpected soxr/runtime fault inside a source aborts the whole batch); CLIs catch only OSError/ValueError and print a traceback. Nothing is published, so it is safe, but noisy. **Fix:** catch `RecursionError` at load and re-raise ValueError, as `verify_prepared_output.py` does. Add a nesting test. |
| L4 | Low | tests; `augment.py:269-272` | Immutability is proven by bytes only. **Fix:** add tests that run prepare and augment on read-only sources/parents (os.chmod; on Windows, read-only attribute), compare `stat` (size, mtime) before and after, run both verifiers and check tree hashes are unchanged, and check a dangling output symlink into the source root is refused. Optionally record the source root's identity in the prepared report so augment can refuse outputs inside it. |
| L5 | Low | `augment.py:139-147`, `:254-255` | Odd-length parent clip: the 16→8→16 kHz return changes length by one sample, while the copy keeps the parent's sample range; untested (fixture clips are even). **Fix:** test an odd-length parent. Either trim/pad to the parent length (recorded) or record `output_sample_count` against the range and document it. |
| L6 | Low | `augment.py:50-51` | `PARENT_FIELDS` copies `review_status` (and `listening_status`) into derived copies. FR-D13: processed outputs do not inherit listening QA. Harmless today (only `unreviewed`/`pending_listening` exist) but wrong once statuses are promoted. **Fix:** set copies to `review_status='unreviewed'` and keep the parent's as `parent_review_status`. Test it. |

No high-severity findings. No egress, privacy or call-path problem in scope: all
modules are local CPU code and the fixtures are generated PCM.

## Interpretation choices needing PRD decisions

1. §2.2 batch rule (8 kHz bottleneck for all files) versus direct 16 kHz with an
   `out_of_domain:wideband_source` flag (M1).
2. Normalization unit: per window (§2.2), per clip (prepare) or none (service, copies).
3. Concealment: hard cut after 3 repeats; A-law silence decodes to +8 rather than 0.
4. Jitter: uniform mean delay (most clips well under 5 % late); realized loss∪late may
   exceed 5 % per clip.
5. SNR over the whole clip until VAD exists; clipping mixes refused, not rescaled.
6. Content-based derivation ID: byte-identical sources in two corpora get identical
   draws.
7. Malformed-header routing (L2) and WAVE_FORMAT_EXTENSIBLE acceptance (L1).

## Not in scope / not assessed

Not in scope (blocked): VAD and speech-only windows (3.2), GSM/AMR-NB/Opus/G.729 (3.3),
noise corpora, RIRs and speech-active SNR (3.4), speaker-turn boundary contract and
live-side parity (3.1). Not assessed: running any test, mutation testing, real-corpus
audio, `scripts/verify_prepared_output.py` internals beyond its tests, performance and
memory at the 10 MB/100 MB bounds, and TOCTOU races between preflight and publish.

## Resolution (coordinator, 14 November 2026)

Tests: `tests/unit/test_phase3_malformed_and_originals.py`, 14 Claude-authored tests
(14 November 2026). Unit suite with API dependencies 621 OK (2 skips) after the fixes.

- New defect found by the tests: an empty source file was quarantined with an empty
  error string (messageless EOFError). Preparation and the augmentation reader now
  name the exception; a fmt chunk cut after its tag still reaches that path and is
  tested.
- L1: `data/prepare/wav_format.py` (standard library only) refuses anything but exactly
  one `fmt ` chunk before `data` with tag 1, used by preparation, the augmentation
  reader and `decode_wav`. `scripts/verify_prepared_output.py` has its own mirror
  (`format_tags`) so it agrees with preparation on Python 3.12+. Float, mu-law,
  unknown, extensible, duplicate-fmt, header-only, zero-channel/rate/bit cases are
  refused on all three entry points and assert the rule's own message (3.11 `wave`
  alone refuses most of them, so only the message distinguishes the rule there).
- L2: `decode_wav` refuses a header whose frames cannot fit in the payload as
  truncated before duration routing; a real 70 s file still needs a job.
- L3: nested JSON in the manifest, prepared report, augmented report and specification
  is a ValueError; the augment CLI exits 1 without a traceback.
- L4: read-only originals (Windows read-only attribute on files) prepare and augment;
  sources, manifest, prepared and augmented trees keep content, size and mtime through
  both verifiers; refused outputs leave no staging directory. Still untested: a
  dangling output symlink (needs symlink rights) and read-only directories.
- L5: documented, not changed: a copy's `start_sample`/`end_sample` name the parent's
  range; its own length is `chain.output_sample_count`; odd-length parents tested.
- L6: copies start `review_status: unreviewed` and keep `parent_review_status` /
  `parent_listening_status`; tested. Existing augmented reports from before this change
  would no longer verify (their rows inherited the statuses); re-run `augment.py`.
- M1: not changed; sent to Tughral as a PRD/architecture decision with interpretation
  choices 1-7 above.
- Re-review (ava-reviewer, same day): pass; its lows (verifier mirror, tag-specific
  assertions, unexercised empty-error path, service matrix accepting 413, duplicate
  fmt chunks, misleading message for a far fmt chunk) were fixed as listed above.
