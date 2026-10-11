---
title: 'Separate existing-synthetic benchmark intake'
type: 'feature'
created: '2026-10-04'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent">

## Intent

Continue todo item5 in small QA-checked steps: obtain a small existing-synthetic
CSALT benchmark for later CPU smoke testing, retain publisher labels and licence,
source revision/hash and honest generator/identity provenance, and prepare it with
the same audio functions as the genuine pilot. Keep it separate from final AVA
training eligibility. Do not generate voices, contact participants, commit or push.

</frozen-after-approval>

## Implementation Notes

- No intent gaps or irreversible actions. Scope is one small benchmark intake;
  preserve dirty workspace under user's continuing authorization. Reuse existing
  csalt_pilot bounded reads, publisher checks, safe paths and native PCM inspection.
- New importer `data/collect/csalt_synthetic.py`, new Astra tests/catalog,
  provenance extension to preparation, source notes and current-state/checklist.
  No generic dataset framework or changes to the genuine importer contract.
- Source pinned to eb8f16623108324867e4424a646937409c64b82b. Metadata API enumerated
  3396 WAVs: 1698 each in Spoofed_TTS and Spoofed_Tacotron across17 folders each.
  Select2 per publisher-speaker/family =68 originals deterministically by hash.
- Label synthetic and language Urdu come from publisher. Generator families are
  VITS/Tacotron per publisher description; exact versions/checkpoints, prompts,
  reference audio and consent mapping are unknown. Folder speaker identity is a
  publisher grouping key, not independent proof of matching target identity.
- Explicit benchmark_only scope, training_eligible false, split unassigned,
  cloning_status not_authorized. No final AVA same-speaker acceptance implied.
- Given malformedcatalog, abort beforewrite; given corruptedexisting/download,
  recordfailurewithoutoverwriting, processvalidsiblings; publish usablemanifest
  onlywhen allrequestedgroups complete. Preserve INCOMPLETE on publishfailure.
- QA covers deterministicgroupselection, hashes/decode, failures, unsafepaths,
  originalimmutability, provenanceandtrainingexclusion aftersharedconditioning.
- User requested automatic checklist progression. No intermediate approval or
  commit step; explicit user authorization supersedes those skill defaults.
- Actual run68originals->50clips (9VITS/41Tacotron),237.8265s; zeroquarantines,
  10signalflags,391.561suncovered. Independent reconstruction/hash/scope QA passed.
  Full104testsdiscovered/102passed/2existingWindowssymlinkskips;12Astra casespassed.
  No commits or pushes. Completed todo5 and automatically proceeding to6.

## Review Triage Log

- Medium/fixed: preparation ignored upstream INCOMPLETE marker. Reject before
  output; Astra checks both conditioning modes. Reviewer verified fix.
- Medium/fixed: legacy preparation dropped benchmark exclusion and provenance.
  Preserve metadata independently of audio mode; Astra verified exact unchanged
  PCM plus metadata in both sources and clips. Reviewer verified fix.
- During test integration, importlib loading failed for the new importer;
  implementer fixed import fallback without weakening the tests.
