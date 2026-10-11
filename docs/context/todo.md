# AVA working checklist

Updated 4 October 2026. This is the active sequence; evidence lives in
`current-state.md` and `handoffs/`. Check marks mean the stated bounded task passed
its QA, not that the final dataset or product is accepted.

Work in small steps. After each completion, update this file, show the checklist
to Tughral and continue automatically to the next available step. Do not wait for
another “continue”. Leave commits, pushes and PR creation for his later instruction.
If a step requires unavailable approval/access, record the blocker and continue
independent preparation. Do not treat elapsed time as approval.

- [x] 1. Establish automated QA and sampled/targeted listening policy.
- [x] 2. Expand genuine Urdu coverage to 17 published speaker folders.
- [x] 3. Consolidate inventory: 520 originals, balanced selection of 340.
- [x] 4. Prepare genuine pilot: 450 normalized clips, original bytes unchanged;
  90 Python tests passed, two Windows symlink skips.
- [x] 5. Add a separate existing-synthetic benchmark.
  - [x] 5a. Verify source contents, licence and available provenance:
    3,396 published WAV entries, Tacotron/VITS families, CC BY-NC4.0;
    exact generator/consent/reference mapping remains unknown and explicit.
  - [x] 5b. Import a small pinned sample with immutable hashes and benchmark-only scope:
    68 originals, 10.490 minutes, all publisher hashes and native decoding verified;
    34 per generator family, two per published speaker folder; zero errors.
  - [x] 5c. Prepare with the shared conditioning path and complete QA:
    50 clips (9 VITS, 41 Tacotron), 3.964 minutes, zero quarantines;
    all hashes/lineage/benchmark exclusions verified. Full suite102 passed, two skips.
- [x] 6. Run a CPU pretrained-detector smoke test.
  - [x] 6a. Verify official checkpoint, classifier/class order, licence and CPU path:
    pinned NII revision, existing2-class classifier, fake0/real1, CC BY-NC-SA4.0;
    matching CPU dependencies installed. Full-file hash check accompanies download.
  - [x] 6b. Execute a small genuine/synthetic run; record scores, time and memory:
    all12 clips scored on CPU with the trained head,18.675 seconds inference for
    55.983 seconds of audio; loading/warmup separate. Integration QA follows.
  - [x] 6c. QA evidence and record what the smoke test cannot establish:
    20 model tests passed; independent review closed. Final hardened run:
    19.387 seconds inference, 2.772 GB sampled peak RSS; no accuracy acceptance.
- [ ] 7. Build a small Pakistani Punjabi `pnb_Arab` pilot with source/rights checks.
  - [x] 7a. Verify official source metadata: pinned revision, CC BY4.0, train
    contains six publisher speaker labels; exact dialect and cloning consent unknown.
  - [x] 7b. Import a small sample from a verified train shard; retain FLAC/shard lineage:
    12 recordings, all six publisher speakers x 2, 13.065 minutes, 48 kHz mono PCM16
    FLAC preserved with decoded WAV copies and source-only transcripts; 13 Astra tests
    passed; independent re-read of shard bytes, samples, hashes, selection and policy
    fields passed (13 checks). No dialect, consent, identity or eligibility inferred.
  - [x] 7c. Prepare copies and check hashes, speaker coverage and metadata:
    shared `--condition` run produced 187 clips (762.993 s) from the 12 sources,
    20.884 s uncovered, 10 review-flagged, 7 full and 5 partial coverage, zero errors;
    every clip independently reconstructed byte-exact (soxr HQ 48 to 16 kHz,
    PCM16, segment, -3 dBFS peak), lineage identical across pilot/source/clip rows,
    gap-free sample accounting, 26 to 39 clips per speaker; 104 unit tests, 102 passed,
    two Windows symlink skips (one intermittent cleanup error seen on a repeat run).
    All 10 flags are `mostly_quiet`. Independent review: PASS, full recomputation.
    Candidates only; no listening, dialect or eligibility claim.
  - [ ] 7d. Follow-ups from the 7b and 7c reviews.
    - [x] Parameterised import verifier `scripts/verify_pnb_pilot.py` with 15
      Claude-authored fixture tests (`tests/pnb/test_verify_pnb_pilot.py`): refuses
      under `-O`, no bare asserts, collects failures, records verifier hash, library
      versions and transcript hashes; real pilot read-only run PASS (12 rows, 6
      speakers, 13.0646 min). Prior-hash comparison stays inactive until a fresh
      record is written beside the prototype's; see `handoffs/pnb-verifier.md`.
    - [ ] Parameterised prepared-output verifier that recomputes segmentation, metrics
      and flags, scans values for transcript text, checks root contents, reads FLAC.
    - [ ] Importer tightening: vacuous publisher-hash branch and string `review_flags`,
      625 s versus 10 MB bound, CLI exit-code and 48 kHz/stereo fixtures, archive root
      outside the repo. Needs an Astra case update since the catalogue is Astra's.
    - [ ] `tests/unit/test_audio_preparation.py` fixture cleanup retry (Windows
      `WinError 145`); do not use `ignore_cleanup_errors`.
- [ ] 8. Prepare original data collection. Tughral reported approval obtained on
  4 October; the approved protocol and individual consent govern every activity.
  - [ ] 8a. Record the approval reference, body, protocol/form versions, scope
    (recording / cloning / listening study), pilot size, conditions and validity in
    `docs/data-collection/approval-pack.md`; reconcile the draft with the approved
    materials. **Blocked on Tughral supplying the approval document.**
  - [x] 8b. Collection kit and allocation: `docs/data-collection/collection-plan.md`
    (open fields, four-person allocation with women-led outreach to women participants,
    tooling pairing, recruitment, intake flow, QA cadence, limits) and
    `session-script.md` (Parts A/B/C template with approved-text placeholders);
    `source_register.json` and the approval pack updated to "approval reported,
    reference pending"; no participant contact, prompt text or gate inferred.
  - [ ] 8c. Start gate evidenced per `approval-pack.md` (forms without placeholders,
    storage and withdrawal rehearsal with fictional material, licence check,
    Ilsa's and Ayesha's GitHub access). Then the 2 to 4 adult pilot.
- [ ] 9. Create reproducible speaker- and lineage-disjoint pilot splits; separately
  document missing identity/lineage that prevents final training-set assignment.
  Spec: `_bmad-output/implementation-artifacts/spec-split-proposals.md` (six gap
  decisions recorded 4 Oct).
  - [ ] 9a. Implementation and Claude-authored baseline tests (`data/manifest/`,
    `tests/unit/test_split_proposals.py`), real proposal run on the three local
    reports, validator pass. First run (v1) passed all checks but exposed that the
    CSALT synthetic benchmark shares all 17 speakers with CSALT genuine, so a
    `benchmark_only` partition swallowed every Urdu genuine clip. Decision 7 in the
    spec: benchmark rows follow their speaker and stay ineligible by flag. v2 run in
    progress, data-engineer role.
  - [ ] 9d. Intake follow-up from 9a: two CSALT genuine source hashes appear under
    two different publisher speaker folders (four duplicated clip hashes). The
    proposer merges them; intake should annotate or quarantine the originals and
    the datasheet must mention it.
  - [ ] 9e. Apply the 5 Oct owner decision (PRD FR-D11) to data: regenerate the
    CSALT genuine, CSALT synthetic and Meta pnb manifests into new versions with
    `cloning_status: permitted_public_corpus`, `training_eligible: true` and the
    approval/licence basis recorded; rerun the split proposal on them; keep the
    pre-decision manifests as evidence. Common Voice stays excluded from cloning.
- [ ] 10. Common Voice Urdu genuine intake (PRD FR-D1), genuine-only speakers.
  - [ ] 10a. A team member with a Mozilla account reviews the download terms
    (no reidentification, no rehosting or resharing) and records them with the date
    in the licence register; `source_register.json` status moves from
    `usage_review_pending`. Cloning stays excluded for this source.
  - [ ] 10b. Download cv-corpus-27.0 Urdu into the external data root with hashes.
  - [ ] 10c. Importer for the TSV plus MP3 layout: pseudonymous client ID as speaker
    key, validated set only, 30-clip cap per speaker, native decode checks; Astra
    handoff for cases; shared `--condition` preparation; add to the split proposal
    as genuine-only speakers.
  - [x] 9b. Astra test-design handoff written: `docs/qa/split-proposal-astra-handoff.md`
    (Claude-authored request; no Astra cases yet). **Tughral or Ibrahim: launch the
    Codex `ava_test_designer` role on it once 9a lands**, then run the returned cases here.
  - [ ] 9c. Independent review of the implementation and proposal evidence.

Current step: **9 — reproducible split proposals** (8a and 8c wait on the approval
document and start-gate evidence; 7d follow-ups are queued behind 9).

Handoff: todo 7 is complete as a pilot with an independent review verdict of PASS
(see `handoffs/pnb-pilot-import.md`). Step 8's preparation is written; the approval
reference and scope from Tughral unblock 8a and then 8c.
