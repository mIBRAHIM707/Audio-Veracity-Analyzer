# AVA current state

Updated 4 October 2026. This is shared, reviewable project memory, not a transcript.
The coordinator updates it after meaningful integration. Consult actual Git status
and latest test reports before relying on a historical result below.

Current user instruction: continue the todo list; **do not commit, push or open a
PR until Tughral supplies commit instructions**. Work remains in this checkout.
On 4 October he requested automatic progression in small steps: maintain and show
`docs/context/todo.md` after each completion, then continue without another prompt.
Earlier PR preparation created a separate `ava-pr-integration` sibling worktree
with local cherry-picks; it was not pushed and no PR was opened for this work.
That worktree does not contain subsequent conditioning work; do not treat it as
the current implementation or merge it automatically.

## Product and team

AVA is GIKI's four-person research prototype for passive CPU-only Urdu/Pakistani
Punjabi synthetic-speech detection. Tughral owns model/research infrastructure;
Ibrahim owns substantial backend, telephony and deployment work; Ilsa owns dataset
engineering; Ayesha owns data QA and dashboard code. These are working allocations,
not exclusive silos. Each person recruits/records their own participants after approval.

First-month recruitment proposal: 5–8 participants per person, 20–32 total. Protect
coding time for all four. The long-term PRD target remains 60–80 recorded speakers
and about 50,000 original clips, excluding codec copies.

## Canonical requirements and recent decisions

- Canonical docs: `_bmad-output/planning-artifacts/prd.md` and `architecture.md`.
  Root filenames are compatibility pointers, not competing sources of truth.
- Latest user-authored PR inspected: #26, “PRD and architecture v0.2 after title
  defence”, head `44bb37d024cf0e5ea9e7627cab74f2da138d7ecc`. It uses root document
  paths; this checkout moved them into BMAD output.
- Model: AntiDeepfake MMS-300M with SLS; XLS-R/SLS fallback. Mamba is superseded.
- Asterisk ARI passive tap is primary; SIPREC is stretch. AMR-NB is file/replay first.
- CPU-only inference. PRD permits separate training compute under its data rules,
  but current implementation work does not depend on GPU access.
- Tughral reported **approval obtained on 4 October**, superseding his 2 October
  pending status. Approval reference, scope and conditions are not yet recorded
  here. Follow the approved protocol and obtain individual consent for the relevant
  activity; institutional approval does not itself supply participant cloning consent.
- **5 October 2026, project-owner decision (Tughral):** public corpora whose
  licences permit derivative works may be used for voice cloning and training
  in this research under the university approval: CSALT Urdu (CC BY-NC 4.0) and
  Meta Omnilingual pnb_Arab (CC BY 4.0). Common Voice is excluded from cloning
  pending its no-reidentification terms. Participant cloning still needs the
  individual opt-in; no cloned voice is released. Recorded in PRD D11, FR-D11,
  Q1, R13, Appendix A and AGENTS.md. Existing manifests still carry the earlier
  conservative flags and are regenerated under todo 9e, not edited in place.
  Tughral also confirmed CTDISR-2025 imposes no obligation on this local
  project; the PRD already treats it as design motivation only.
- CSALT is accepted by the project owner as an established source for noncommercial
  detection research under its published licence. Retain published class/language
  labels with provenance. Automated checks apply to every file; sampled/targeted
  listening replaces any earlier per-file human approval gate for routine intake.
  Other unresolved public-source reviews and the same-speaker cloning-consent
  interaction remain open. This policy supersedes earlier blanket pending-use notes.
- Meta `pnb_Arab` source preflight is complete. One train shard was downloaded
  and hash-verified: 210 rows across all six published train speaker labels.
  The importer has 13 passing Astra tests, and on 4 October the real 12-source
  pilot was imported (todo 7b, see below). No listening or exact Majhi coverage
  established. Coordination transferred from Codex to Claude on 4 October; see
  `docs/context/handoffs/claude-resume.md` and `pnb-pilot-import.md`.
- User requests Astra for test-case design, Playwright for browser crawling/testing,
  BMAD, portable context, and simultaneous Claude Code/Codex development.

## Implemented locally

- **Todo 8b complete: collection preparation written; 8a and 8c blocked on the
  approval document.** `docs/data-collection/collection-plan.md` lists the eight
  fields only the approval can supply (reference, body, protocol and form
  versions, activities, pilot size, storage and withdrawal terms, compensation,
  off-site processing, publication intent), the four-person allocation (all four
  code and collect; Ilsa and Ayesha lead outreach to women participants; Tughral
  and Ibrahim, who have Claude Max and Codex Pro, take integration and review and
  pair with the other two, whose coding tasks arrive as scoped issues with tests),
  recruitment and scheduling, the session procedure, the intake flow into the
  external intake CSV and validator, QA cadence and explicit limits.
  `session-script.md` is the Parts A/B/C template with placeholders for approved
  prompt text. `source_register.json` and `approval-pack.md` now say "approval
  reported 4 October, reference pending" instead of "pending approval"; the start
  gate remains unevidenced and recording has not started. GitHub: the repository
  has two collaborators (mIBRAHIM707 admin, tughral1 write); Ilsa and Ayesha still
  need access. PR #26 (PRD and architecture v0.2) is open awaiting review.
- **Todo 7d, verifier part, complete.** `scripts/verify_pnb_pilot.py` makes the
  7b verification reproducible from the repository: it takes the archive,
  manifest and raw roots plus the publisher archive hash on the command line,
  refuses under `python -O`, uses no bare asserts, re-derives selection and clip
  IDs without importing the importer, re-reads only the selected rows' audio,
  and reports aggregates only. With `--write-verification` it records its own
  SHA-256, dependency versions and per-file transcript hashes, and compares
  transcript hashes against a prior record when one exists. Read-only run on the
  real pilot: PASS, 12 rows, 6 speakers, 2 per speaker, 13.0646 minutes, all
  48 kHz mono, 15 aggregate checks. Tests: 15 Claude-authored fixture cases
  (tampered WAV, FLAC and transcript, flipped policy fields, INCOMPLETE markers,
  write-once, `-O` refusal, prior-hash match and mismatch); the whole `tests/pnb`
  folder (13 Astra plus 15 new) passes. The machine-local prototype in `.local/`
  is superseded but kept; its `verification.json` occupies the output name, so the
  live transcript-hash comparison needs a fresh record in a new manifest root.
- **Todo 7 reviewed: PASS.** An independent ava-reviewer pass on 7b found no
  blocking defects, confirmed lineage, external storage and policy fields by its
  own recomputation, and raised two medium findings about the verifier (assert
  stripping under `-O`, fixed; non-reproducible machine-local script, queued as
  todo 7d) plus low importer follow-ups (also 7d). A second independent pass on
  7c also returned PASS: all 187 clips recomputed from the FLAC originals with an
  independent re-implementation of the pause segmentation yielding identical
  ranges, every metric and flag recomputed, no transcript text or Arabic-script
  code point anywhere in the processed report. Its medium finding (the 7c
  verifier checked tiling and counted flags rather than recomputing them) is
  covered by the review for this run and folded into 7d. All ten 7c flags are
  `mostly_quiet`; one of two repeat unit-suite runs hit an intermittent Windows
  temporary-directory cleanup error after assertions passed.
- **Todo 7b complete: Punjabi pilot import.** `data/collect/meta_pnb_pilot.py`
  ran against the verified first Meta `pnb_Arab` train shard (464,239,672 bytes,
  publisher SHA-256 matched before and after extraction) and selected two
  recordings from each of the six publisher train speakers: 12 recordings,
  13.0646 minutes, all native 48 kHz mono PCM16 FLAC of roughly one minute each,
  preserved byte-exact beside decoded native-rate WAV copies and source-only
  transcript files. 13 Astra tests passed, foundation checks passed, and an
  independent verifier re-read the 12 embedded payloads straight from the shard,
  recomputed the deterministic selection and compared FLAC bytes, decoded samples,
  WAV and FLAC hashes, durations, transcripts, metadata, namespacing and every
  policy field: 13 aggregate checks passed. Every row carries `training_eligible:
  false`, `cloning_status: not_authorized`, `consent_status:
  not_individually_verified`, `dialect: unknown`, `split: unassigned` and the
  publisher `train` split. Whole-file screening raised only the expected format
  and duration flags. Six speakers from one shard is a pilot, not Punjabi
  coverage; dev, test and the second train shard were not fetched. Outputs and
  verification live outside Git via `.local/data-location.json`. Independent
  reviewer pass requested; see `docs/context/handoffs/pnb-pilot-import.md`.
- **Todo 7c complete: Punjabi pilot preparation.** The shared `--condition`
  path (`data/prepare/prepare_audio.py`) turned the 12 decoded WAVs into 187
  conditioned candidates of 3 to 6 s: 762.993 s retained, 20.884 s uncovered,
  10 review-flagged, 7 sources fully and 5 partially covered, zero errors, 66 s
  wall time. All 12 sources were resampled by soxr HQ from 48 kHz; outputs are
  16 kHz mono PCM16 at −3 dBFS sample peak. An independent verifier rebuilt every
  clip from the immutable WAV (mono mean, soxr HQ, quantization, slice, peak gain)
  and matched bytes and hashes, confirmed canonical PCM hashes, unique output
  hashes, identical lineage fields across pilot, source and clip rows, no
  transcript text on clip rows, gap-free retained-plus-uncovered accounting, and
  the summary totals. Clips per speaker range from 26 to 39, so equal source
  counts did not yield equal clip counts. Unit suite: 104 tests, 102 passed, two
  Windows symlink skips. These are review candidates with `review_status:
  unreviewed`, not an accepted dataset; no telephone-band round-trip, codec
  augmentation, VAD, listening QA or split assignment occurred.
- **Todo 6 complete: CPU baseline smoke.** The pinned official MMS-300M
  AntiDeepfake model scored 12 local clips (6 genuine, 3 VITS, 3 Tacotron),
  55.98275 seconds of audio, with four CPU threads. Final hardened v3 run:
  19.38719 seconds inference, RTF 0.3463; loading 9.7096 seconds and warmup
  2.6785 seconds separate; sampled process peak RSS 2.772 GB including loading.
  Strictly loaded 421 backbone tensors and both original trained head tensors;
  eight known pretraining-only tensors explicitly omitted. No random SLS head.
  All input hashes and finite scores verified; repeat logits matched v2 within
  1e-6. Python connection/DNS guard observed zero attempts (not an OS network
  trace). Twenty Astra model tests passed, no skips, and independent review closed.
  Fairseq/ONNX parity, language accuracy and telephone capacity remain unestablished.
  See `docs/context/handoffs/cpu-detector-smoke.md` and the separate optional model
  environment. v1 is incomplete; v2 predates snapshot hardening; use v3 evidence.
- **Todo item5 complete: separate existing-synthetic benchmark.** Imported68
  publisher-hash-verified originals (34 VITS-family,34 Tacotron;2per17folders per
  family),10.490minutes, from the same pinned CSALT revision. Prepared50clips,
  237.8265seconds:9VITS and41Tacotron,10signalflags,zeroquarantines.391.561seconds
  remain uncovered; no forced cuts. This generator imbalance after pause selection
  limits representative evaluation; the sample is only for CPU smoke tests.
  `benchmark_only=true`, `training_eligible=false` and unknown version/reference/
  target-identity fields survive both preparation modes. No cloning performed.
  Independent source/output reconstruction, hashes, peak/format and scope checks
  passed.104Python tests discovered,102passed,twoexistingWindowssymlinkskips;
  all12Astra benchmark cases passed. Two integration review findings fixed.
  External reports located by `.local/data-location.json`.
- **Todo item 4 complete: balanced-source conditioning pilot.** The 340 originals
  produced 450 derived 3–6 second candidates, 1,958.4425 seconds (32.641 minutes),
  zero quarantines, 154 signal-flagged candidates. All outputs are 16 kHz mono PCM16
  with −3 dBFS sample peaks. Nine native 20 kHz sources were resampled; 331 bypassed
  resampling. Originals, publisher metadata and inventory IDs remain traceable.
  All 520 original hashes were independently rechecked. Every output hash, format,
  gain-transformed sample slice, source ID and retained/uncovered range passed
  independent reconstruction checks. 771.6716 seconds remain uncovered; source
  outcomes: 224 full coverage, 55 partial, 59 no valid pause segments, two too short.
  All 17 speakers appear in outputs, with 10–40 candidates per speaker: equal
  source counts do not imply balanced derived clips. No final splits assigned.
  Final output is conditioned v2 via `.local/data-location.json`; v1 predates review
  fixes and is preserved separately. QA: 92 tests discovered, 90 passed, two Windows
  symlink skips. One prior full run had a Windows temporary-directory cleanup error
  after assertions; an unchanged rerun passed. All 16 Astra conditioning tests and
  independent fix review passed. See `docs/context/handoffs/balanced-audio-conditioning.md`.
- `data/prepare/conditioning.py` supplies class-independent soxr HQ conversion
  and per-clip peak normalization. Explicit `--condition` uses these in preparation;
  legacy exact-sample mode remains. Pinned CPU dependencies are installed through
  `requirements-audio.txt`; CI/test commands updated. Native clipping warnings
  survive stereo downmix, and pre-gain flags survive normalization. This is source
  preparation, not telephone-domain simulation, VAD or full train/serve parity.
- **Current CSALT working selection:** 340 genuine originals, 20 each from all 17
  published speaker folders, 45.502 minutes / 87.975 MB. Consolidated local
  inventory: 520 verified originals; 180 extra Speaker_01 originals are retained
  but excluded from this selection. Downloaded 270 additional files. All 250
  previous original hashes remain unchanged; inventory/selection independently
  checked and the structural validator passed all 520 rows. Splits unassigned.
  Final native formats: 331 selected files at 16 kHz mono and nine at 20 kHz mono,
  all PCM16. Conditioning now supports both formats; originals stay untouched. Final manifests
  are v2 and located by `.local/data-location.json`; v1 is a preserved diagnostic
  attempt with no balanced output. Importer `data/collect/csalt_pilot.py` and handoff
  `docs/context/handoffs/balanced-csalt-intake.md` document the workflow. Tests:
  76 discovered, 74 passed, two existing Windows symlink skips; all 15 new importer
  tests passed. These results supersede the earlier local-holdings totals below.
- Offline listening review is available via `data/review/build_review.py`. The
  generated page lives beside the external v2 report; its location is recorded in
  `.local/data-location.json`. It displays 66 local candidates, all unreviewed by
  default, with reviewer decisions and report-bound JSON export/import. No server
  or upload. QA: 61 Python tests discovered, 59 passed and two symlink skips;
  17 Playwright tests passed (seven for review UI). Actual page/one-WAV decoding
  smoke check passed without HTTP requests. Assistant listening remains unavailable.
- User reports some clips sound fine based on their native Urdu listening review.
  Exact filenames and raw/prepared status are pending clarification; do not mark
  the whole batch accepted. Local CPU ASR screening is a possible next aid, not
  yet installed or executed; transcription is not perceptual listening QA.
- Implemented `data/prepare/prepare_audio.py`: bounded, hash-verified immutable
  WAV preparation, 3–6 second energy-pause candidates, quality flags, source/speaker
  sample-offset lineage and uncovered ranges. Final pilot v2: 50 sources -> 66
  candidates (287.607 seconds), 24 flagged for review, 98.439 seconds uncovered;
  zero quarantined inputs. 34 sources fully covered, 7 partly covered, 7 with no
  valid pause segments, 2 too short. All 50 original hashes remained unchanged;
  every output was independently checked against the exact source sample slice,
  and retained+uncovered ranges account for every original sample. No listening
  acceptance, normalization, codec augmentation or split assignment is implied.
  `.local/data-location.json` points to the final v2 output and report; v1 is an
  earlier run preserved separately. See `data/prepare/README.md` for limitations.
  Integrated QA: 52 tests discovered, 51 passed, one source-symlink test skipped
  because Windows denied symlink creation. Four independent review findings were
  fixed and rechecked. See `docs/context/handoffs/pilot-audio-preparation.md`.
- Expanded CSALT intake on 3 October with five genuine clips from each of
  `Speaker_02` through `Speaker_11`: 50 files, 10 additional speakers, 12.36 MB,
  6.43 minutes. All matched publisher SHA-256 hashes and decoded as 16 kHz mono
  16-bit PCM. Signal screening flagged 32 outside 3–6 seconds, 11 below -35 dBFS
  whole-file RMS, and six with more than 50% quiet windows (20 ms RMS < -40 dBFS).
  No near-full-scale screening flags; no download errors. Flags may overlap and
  do not establish audible quality or mandate rejection. No originals modified.
  Combined local holdings: 250 genuine files from 11 source speaker IDs, heavily
  weighted toward Speaker_01; this is not a balanced or approved training set.
  On the current machine `.local/data-location.json` points to audio and reports.
  It is ignored and does not transfer through Git. Receiving agents should read
  `pilot-report-speakers02-11.json` there before repeating or extending the work.
- Downloaded the complete CSALT `Bonafide/Speaker_01` folder into external local
  data storage on 3 October: 200 WAV files, 53.42 MB, 27.82 minutes. All 200 matched
  publisher SHA-256 hashes and passed WAV frame-read checks. Source revision:
  `eb8f16623108324867e4424a646937409c64b82b`. Source README and a download report
  are stored beside the audio outside Git. This single-speaker starter has not
  undergone listening QA or been approved for training/cloning; it is not the
  proposed multi-speaker pilot or a participant corpus.
- Five reproducibly sampled CSALT files received automated signal inspection
  (seed 20261003): all were readable 16 kHz mono 16-bit PCM and matched stored
  hashes; none had samples near digital full scale. Four exceeded the final
  6-second clip limit; two had substantially lower RMS levels than the others.
  Report `signal-inspection-five.json` is alongside the external download.
  No audio-listening tool was available: intelligibility, actual language,
  background noise and audible distortion remain unassessed. Screening thresholds
  are heuristics, not validated acceptance criteria. Originals were unchanged.
- Genuine-audio intake source register, CSV template and stdlib validator in
  `data/collect/`. This is metadata groundwork, not full audio QA or a dataset.
- Astra expanded regression cases and fixed malformed quoted CSV acceptance.
- Shared `AGENTS.md`, Claude entrypoint, role contracts, native agent adapters,
  BMAD Codex skill mirror and drift checks.
- Playwright CLI/crawler and controlled browser fixture tests. No AVA UI exists.
- Context export, repository artifact guard, pre-commit installer and CI workflow.
- Root PRD/architecture pointers preserve a single canonical location.

## Evidence and limits

Integrated QA: 36 Python tests passed (26 intake and 10 foundation tooling).
The final browser implementation agent run passed 10 Playwright fixture tests on
Chromium, including failed-resource and trace-write regressions. These results
cover infrastructure and metadata only. Foundation checks validated eight roles,
16 adapters and the 29-skill BMAD mirror; the repository artifact guard passed.
See `docs/context/handoffs/foundation-setup.md` for commands and limitations.

Tooling provisioned: Python 3.11.15, Node 22, Playwright 1.63.0/Chromium. Existing
BMAD version is 6.12.0; it was not upgraded. Native agent files require client
discovery/reload; static file validation is not proof of a launched native role.
An actual gpt-6-astra agent authored the test catalogs during this session.

## Not implemented or not established

No AVA API, dashboard, live tap, shared audio-conditioning runtime, trained AVA SLS
model, ONNX benchmark, collected participant corpus or final ML metrics exist.
No test pass establishes model quality, eight-call capacity, deployment readiness,
compliance certification, or freedom from future bugs. The final test set is not built.
The CI workflow is authored locally; a hosted CI run requires pushing the changes.
These setup results do not imply hosted CI or remote publication. See the current
user instruction and local-worktree note at the top before any Git actions.

## Existing workspace state to preserve

At setup start, develop was ahead 2/behind 2 of cached origin/develop. BMAD PRD and
architecture had user modifications; docs and ava-viva were untracked. Intake work
was also uncommitted. Do not discard these or create worktrees assuming they are in
HEAD. Review/stage a coherent baseline before starting concurrent writer worktrees.

GitHub read access worked once the old sandbox network restriction was removed;
the earlier auth failure report was not reliable evidence of invalid credentials.
Do not log credential values or put machine-specific paths into shared instructions.

## Next bounded work

Current step: todo7, a small `pnb_Arab` train-shard pilot. Metadata preflight is
in `docs/context/handoffs/pnb-source-preflight.md`: pinned official source,
CC BY4.0, six train speaker labels, long source recordings and FLAC/shard lineage
requirements. Use one verified train shard first; preserve publisher dev/test.
No cloning consent or exact Majhi coverage inferred. No final split assignment.
Commits, pushes and PR creation still await user instruction.

Current todo sequence: items 1–3 (QA policy, speaker coverage, consolidated
inventory), item4 (consistent preparation) and item5 (separate synthetic benchmark)
and item6 (CPU smoke) complete. Items7–9 remain: Punjabi pilot, approved
original collection, and reproducible speaker/lineage-disjoint splits.

1. Review/integrate this foundation on an issue-linked branch (existing #3/#4 cover
   repository/developer setup; source intake relates to #6/#11).
2. Confirm recording ethics body/protocol and separate cloning consent; prepare
   before collecting. No outside messages have been sent.
3. Verify permitted source access, especially pnb_Arab availability, and implement
   a small source importer plus decoded-audio QA with fictional tests.
4. Run a CPU pretrained checkpoint smoke test, preserving its classifier and
   recording class order, timing and memory; do not claim random SLS heads detect fakes.
5. Implement a first real batch API/UI vertical slice before product Playwright cases.

Some existing issue titles still use v0.1 numbering or Common Voice pa-PK. Use the
latest PRD and reconcile issue scope explicitly. The team plan Page is supplementary;
this repository's requirements and verified state are the cross-model starting point.
