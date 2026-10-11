# Claude Code continuation — 4 October 2026

This handoff supersedes historical pending-approval and source-download notes.
Tughral reports approval obtained. Record the reference/scope when relevant; do
not ask again whether approval exists. Individual recording/cloning consent and
the approved protocol still determine permitted participant activities. No outside
messages have been authorized. The approval packet is a draft, not automatically
the approved protocol.

## Working agreement

Read AGENTS.md, CLAUDE.md, docs/context/todo.md, current-state.md, the canonical
BMAD PRD/architecture and docs/engineering/workflow.md. Work in small steps;
after each QA pass update and display the todo list, then continue automatically.
Do not commit, push, create a PR, merge, reset or discard existing work. Preserve
the substantial dirty checkout, including user-authored planning changes.
The active checkout is feat/11-pilot-audio-preparation; the sibling
ava-pr-integration worktree is outdated. Do not switch to it.

This is a transfer of coordination to Claude, not authorization for competing
writers in this checkout. Codex agents and their private state do not transfer;
use the saved role contracts in docs/agents and adapters in .claude/agents.
BMAD is already installed. Astra authored existing test catalogs; Claude may run
them, but must not claim to be Astra. Use Playwright when browser work starts.

## Exact stopping point

Todo 1–6 completed: genuine intake/conditioning, synthetic benchmark, CPU model
smoke. Todo 7a completed; 7b importer QA just finished, real import not run.
Astra reports 13/13 tests passing, zero skips, and foundation checks passing.
Tests cover real tiny Parquet/FLAC, exact samples/hashes/lineage, partial rename
publication failure markers, archive mutation before final hash verification,
incomplete source rejection and publisher speaker-ID bounds. Ownership released.
Files: data/collect/meta_pnb_pilot.py, data/collect/PNB_README.md,
tests/pnb/test_meta_pnb_pilot.py, docs/qa/pnb-pilot-test-cases.md.
Independent review's four findings were addressed: publisher_split preservation,
incomplete source-root detection, real partial-publication tests, archive mutation
test. The latter source-root checks also modify data/prepare/prepare_audio.py.

Read spec-pnb-pilot-intake.md under _bmad-output/implementation-artifacts and
pnb-source-preflight.md beside this file. First train shard is already verified:

- External archive root: D:/AVA-Data/source-archives/meta-pnb
- Source manifest: source.json
- Archive: train-00000-of-00002.parquet, 464239672 bytes
- SHA256: d8ad5551672394f4f9995ffa3f71f3065f611a121f44ca1170a2780fde4f812d
- Meta revision: 8648ba8946377697b427ae952076e49fc0e5e44d
- 210 rows, all six train speaker labels. No second shard needed for this pilot.

Next, inspect actual output paths for prior attempts and run the documented
importer with two sources per speaker (12 total), preserving FLAC, native decoded
WAV, source transcripts and shard/row lineage. Proposed fresh destinations:
D:/AVA-Data/raw/meta-pnb-pilot-v1 and
D:/AVA-Data/manifests/meta-pnb-pilot-v1. Never overwrite existing output.
Verify originals and decoded samples independently, then mark 7b complete.
Use shared --condition preparation for 7c; verify output hashes, format, lineage,
scope, speaker coverage and uncovered ranges. No transcript-to-cut alignment,
Majhi dialect, cloning consent or final training eligibility may be inferred.

## Following bounded steps

8. Review docs/data-collection/approval-pack.md against the actual approved
protocol. Complete collection preparation, assign collection and coding work to
all four (Tughral, Ibrahim, Ilsa, Ayesha). Keep outstanding protocol details
explicit without reimposing the superseded blanket approval blocker.

9. Implement reproducible speaker/lineage-disjoint pilot split proposals with
QA. Preserve benchmark_only/training_eligible exclusions and original publisher
splits. A proposal must not silently become an accepted training/test set.
Cross-source identity and missing consent/reference mappings remain limitations.

## Commands and evidence

Use requirements-pnb.txt for tests/pnb; requirements-audio.txt for tests/unit.
Python 3.11 via uv. Existing CPU tests use requirements-model.txt and an explicit
--extra-index-url https://download.pytorch.org/whl/cpu (see model README).
Avoid repeating the expensive real model smoke absent a relevant model change.
Run relevant tests, scripts/check_foundation.py, scripts/repository_guard.py and
scripts/export_context.py when closing the handoff. Preserve failures honestly.

.local/data-location.json locates real external data and reports. Audio, weights,
real manifests and completed consent forms remain outside Git under D:/AVA-Data.
The portable allowlisted export is .local/context/ava-context.md; it is not a data
backup. This dirty work and local data will not appear in another clone via Git.
Use this same local checkout for this transfer, with Codex no longer writing.

Previous tooling caveat: PowerShell hung in this Codex session; cmd.exe with
login=false worked, but quoted inline Python commands failed. Prefer script files
and tool workdir rather than shell-quoted inline programs.
