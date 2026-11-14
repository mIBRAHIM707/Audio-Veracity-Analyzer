# Phase 1 QA: requirement coverage and source/identity limitations

**Provenance.** Independent read-only ava-reviewer pass (Claude, Opus 5.5) on
9 November 2026 over commits a1569b4, 4521a4c, 5bc95f5 and 836aca8 on
`feat/4-daily-development`; consolidated by the coordinator, who fixed the
documentation inconsistencies it raised (see the end). Test totals are the
runner's recorded results.

**Verdict: conditional pass as infrastructure.** No data or product acceptance is
established by phase 1.

## Requirement coverage

Status: Passing (infra) = code and fixture tests behave as specified; Passing
(data) = a real read-only run on external pilot data matches the claim; Partial;
Blocked; Unmeasured.

| Req | Infrastructure evidence | Data / product evidence | Status |
|---|---|---|---|
| FR-D1 sources | Punjabi importer follow-ups (8 Claude-authored + 13 Astra tests; `tests/pnb` 36 OK) | 17 CSALT and 6 Meta speakers; Common Voice and other sources not imported | Partial |
| FR-D6 codec augmentation | none (phase 3) | none | Not started |
| FR-D8 splits | 16 Claude-authored cases + 20 baseline tests; validator checks crossings and content digest | Schema-2 proposal: 687 rows, 23 speakers in 22 groups, 520/75/92, zero crossings; no held-out generator, dialect or real-call partitions | Partial (proposal only) |
| FR-D11 permitted pools | `migrate_policy.py`, 8 tests | 352 genuine rows trainable, 68 publisher synthetic rows benchmark-only; no AVA-generated fakes exist, so the same-speaker rule is untested on real fakes; deviation from the PRD sentence awaits Tughral | Partial |
| FR-D12 one conditioning path | `verify_prepared_output.py`, 40 tests, byte-exact rebuild | Genuine and existing-synthetic data through one script, PASS on 450/50/187 clips; no codec chain, overlap report or leakage probes | Partial |
| FR-D13 quality checks | metrics and flags recomputed exactly | flags on all 687 clips; no noise score, language check or 5% listening sample | Partial; listening Unmeasured |
| FR-E3 data locality | repo-internal outputs refused; aggregate-only output | guard and foundation checks passed before every commit (daily run) | Passing (infra) |
| FR-E5 licence register | — | CSALT and Meta entries record the 5 Oct basis and `fr-d11-2026-10-05`; no datasheet yet | Partial |
| D11 shortcut risk | — | all 637 trainable rows are genuine, Urdu and Punjabi | Unmet until AVA fakes exist |
| A4 speaker-disjoint | cases C06–C08c, C12 | zero speaker, recording and hash crossings on real data; CSALT synthetic "same speaker" is only the publisher folder label | Passing (infra) |
| Exact-mode preparation | fixtures only | CSALT exact-mode pilot v2 not re-verifiable (input manifest absent) | Unmeasured on real data |

## Remaining source and identity limitations

1. Speaker IDs are namespaced per source; no cross-source identity check exists.
2. CSALT publishes 2 recordings byte-identically under 2 speaker folders each
   (4 original rows, all selected; 4 duplicated derived clip hashes). The proposer
   keeps the copies in one group, but at least one label is wrong: **do not use
   those recordings as cloning references** until identity is resolved
   (`data/collect/README.md`).
3. Punjabi dialect is unknown; `pnb_Arab`/west2386 does not establish Majhi; six
   publisher speakers from one train shard.
4. 17 Urdu and 6 Punjabi speakers versus the 60–80 speaker target. Meta splits
   4/1/1, so Punjabi dev and test each hold one speaker.
5. Corpus permission is not consent: migrated rows are cloneable while
   `consent_status: not_individually_verified`, resting on the owner decision and
   the institutional approval.
6. Approval reference pending (todo 8a); if its scope excludes public-corpus
   cloning, the `fr-d11-2026-10-05` copies must be superseded.
7. Genuine-only shortcut (D11) applies to all trainable speakers.
8. Benchmark-only speaker groups take hashed allocation slots (spec decision 9,
   open; not material for current data).
9. Exact-mode pilot verifiable only by fixtures.
10. The prepared-output verifier re-implements the same recipe with the same soxr
    version; `verify_pnb_pilot.py` checks the pre-migration policy only, so migrated
    Meta manifests have no import-level verifier.
11. Proposals do not warn about mixed policy versions.
12. Punjabi rows over ~104 s are excluded before ranking; may bias other shards.
13. No listening QA on any derived clip; only exact-hash duplicates are caught.

## Inconsistencies raised and resolution

Fixed the same day: FR-D6 mis-cited for shared conditioning (now FR-D12);
"Urdu-only" shortcut wording (now all trainable speakers); duplicate CSALT rows
not restricted for cloning (rule recorded); guard/foundation runs not recorded
(now in the daily run); pnb follow-up test count (8); stale "v2 rerun is 1.4"
notes; Meta register entry lacked the policy version and had a stale next action.

Left for a human: the PRD FR-D11 sentence that makes publisher synthetic clips
trainable (proposed note: publisher synthetic clips without known reference audio
are benchmark-only); whether 9c is satisfied by today's conditional-pass reviews;
older stale sections of `current-state.md` and `todo.md` (no-commit instruction,
"current step" notes, "v2" naming, 9b's Astra request); the frozen spec intent
saying "no commits".
