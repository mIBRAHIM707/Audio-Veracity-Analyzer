# Task handoff: FR-D11 policy migration and split rerun (backlog 1.4, todo 9e)

- Author: Claude (Claude Code, Opus 5.5), scheduled session 9 November 2026.
  Tests are Claude-authored. Issue #11; PRD D11, FR-D11, D14; spec decisions 7 and 9.
- Owned files: `data/manifest/migrate_policy.py`, `tests/unit/test_migrate_policy.py`,
  `policy_version` added to preparation provenance (`data/prepare/prepare_audio.py`)
  and to the prepared-output verifier, `data/manifest/README.md` migration section.

## What changed

`migrate_policy.py` copies a complete published intake manifest into a new external
directory and applies the 5 October 2026 owner decision: CSALT Urdu genuine and Meta
pnb rows get `training_eligible: true` and `cloning_status: permitted_public_corpus`;
CSALT existing-synthetic benchmark rows keep `benchmark_only: true`,
`training_eligible: false` and their cloning status, with a separate basis text
saying why. Every row gains `policy_version: fr-d11-2026-10-05`, `policy_basis`,
`policy_licence` and `previous_policy`. `consent_status` is never upgraded: a
missing value becomes explicit `not_individually_verified`. Common Voice and any
unknown source are refused, as are unverified rows, mismatched labels or scope
(including a benchmark `use_scope` on genuine rows), already-migrated rows,
incomplete manifests, existing outputs, outputs inside the repository or input
directory, and `INCOMPLETE` inputs. The CSALT licence-register entry now records
the 5 October basis and the policy version.

**Deviation from todo 9e wording, for Tughral to confirm.** Todo 9e and FR-D11's
sentence "public-corpus genuine and synthetic clips are training-eligible" would
make the CSALT publisher synthetic clips trainable. They stay benchmark-only here:
their generator version, prompt and reference audio are unknown (FR-D2), and the
licence register already excluded them from AVA training. Consequence: until AVA
generates its own fakes, the 17 CSALT Urdu speakers appear in training as genuine
only, which is the genuine-only shortcut D11 warns about.

An independent review (conditional pass, no blockers) led to these changes; the
first real run with the pre-review code wrote `-frd11` directories, which are
superseded but kept. The figures below are from the `-frd11-r2` rerun.

## Real run (aggregates only; locations in `.local/data-location.json`)

| Step | Result |
|---|---|
| Migrate CSALT genuine (balanced v2) | 340 rows, 340 trainable |
| Migrate CSALT synthetic benchmark v1 | 68 rows, 0 trainable, 68 benchmark |
| Migrate Meta pnb pilot v1 | 12 rows, 12 trainable |
| Re-prepare (`--condition`) | 450 / 50 / 187 clips, 0 errors; totals identical to the pre-migration outputs |
| `verify_prepared_output.py` on all three | PASS, 18/18/19 aggregate checks |
| `propose_splits.py` seed `ava-split-2026-11-09` | 687 rows, 23 speakers in 22 groups, train 520 / dev 75 / test 92 |
| `validate_splits.py` | ok; zero crossings for speaker, recording and all four hash fields |
| Benchmark rows by partition | train 39, dev 6, test 5 |
| Trainable rows by partition | train 481, dev 69, test 87 |
| Within-partition duplicate warnings | 2 source hashes, 4 clip hashes (the two CSALT cross-speaker duplicates, merged into one group) |

CSALT allocation: 16 groups (17 speakers, two hash-linked) as 11/2/3; Meta: 6 as 4/1/1.
The pre-decision manifests, outputs and v1 proposals are unchanged evidence.

## Limits

- Not changed after review: symlinked manifests are followed like preparation does;
  proposals do not yet warn when inputs mix policy versions.

- A proposal, not an accepted split; no listening QA, dialect or identity claim.
- Six Punjabi speakers and seventeen Urdu speakers are a pilot, not coverage.
- Benchmark rows share partitions with genuine rows of the same speaker; trainers
  must filter by `training_eligible`/`benchmark_only`.
- Approval reference is still pending (todo 8a); the basis text says so.
