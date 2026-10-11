---
title: 'Balanced CSALT genuine pilot and consolidated source inventory'
type: 'feature'
created: '2026-10-03'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
---

<frozen-after-approval reason="user approved todo items 1–3">

## Intent

Expand the genuine Urdu pilot across all available CSALT source speakers, retain
published labels, and consolidate local originals in a single manifest with an
explicit balanced working selection. Use automated structural/audio checks for
every original, sampled listening and targeted review rather than mandatory
individual listening. Preserve originals and prior downloads. Keep licences,
source hashes, release/speaker identities and unassigned splits traceable; no
new cloning permission or model acceptance is implied.

</frozen-after-approval>

## Implementation Notes

- Cohesive intake increment: standalone stdlib importer, fictional tests, QA
  policy and shared-state update. No unrelated application changes or irreversible
  actions; known prior dirty foundation/planning edits preserved.
- User authorizes accepting CSALT as an established source for this research
  ingestion. Record noncommercial detection scope under published CC BY-NC 4.0;
  do not silently extend to commercial deployment or voice cloning.
- Choose 20 originals per source speaker. Existing files are preferred with
  deterministic hash ordering. Preserve extra local files and explicitly exclude
  them from the balanced pilot. Record incomplete groups instead of inventing
  speaker coverage if integrity checks fail.
- Official pinned release catalog lists 3398 genuine WAVs across 17 speaker
  folders. Catalog is cached outside Git; no speaker-level records committed.
- Parent owns importer/docs; GPT-6 Astra owns test design/regressions; independent
  reviewer examines code. No browser behavior changed, so Playwright is not needed.
- Integrated Python QA: 74 tests discovered, 72 passed, two existing symlink tests
  skipped on Windows. All 13 new importer regressions passed without skips.
- First real run exposed nine valid 20 kHz mono PCM16 originals in Speaker_17.
  Corrected intake to preserve native 8–192 kHz mono/stereo PCM16, with a format
  processing flag. The raw inventory must not impose the later 16 kHz target.
  No resampling occurs here; the preparation CLI's stricter contract is unchanged.
- Final v2 run: 520 verified originals retained, balanced selection 340 = 20 per
  each of 17 source speakers, 45.502 minutes, 87.975 MB. 270 new downloads across
  two runs. All prior 250 hashes unchanged. Inventory and selection independently
  reconciled; structural intake validator passed all 520 rows. No split assigned.
- Final integrated QA: 76 tests discovered, 74 passed, two existing Windows
  symlink skips. All 15 CSALT tests pass. Native-format delta independently reviewed.

## Review Triage Log

- Medium, patched: direct writes could leave truncated final sources. Downloads
  now stage, flush, sync and rehash before an atomic no-overwrite hard link.
- Medium, patched: catalog hash was recomputed after the run. It now identifies
  exactly the bytes parsed for selection and verification, tested under mutation.
- Medium, patched: completeness only covered supplied speakers. The production
  CLI now checks the official 17-speaker inventory before creating files.
- Medium, patched: an incomplete balanced manifest could feed preparation. It is
  now withheld on any selected shortfall; diagnostic CSV/report remain available.
- All four fixes independently rechecked; no unresolved findings in that scope.
