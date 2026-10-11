# Shared development foundation — 2 October 2026

## Scope and ownership

Implemented the user's BMAD, Astra test-design, Playwright, portable context and
concurrent Claude Code/Codex foundation request. Work was coordinated in the
existing `develop` working tree, with bounded file ownership for research,
GPT-6 Astra test design and Playwright implementation agents. Existing user
changes were preserved. No commit, push, merge or worktree was created.

Construction requirements are FR-W1–FR-W4 in the canonical BMAD PRD; architecture
decision ADR-15 records shared context and layered verification.

## Delivered

- Preserved existing BMAD 6.12.0 installation; mirrored 29 skills and their
  supporting resources into Codex's `.agents/skills` discovery location.
- Added eight comprehensive shared role contracts and 16 generated native
  adapters. Only the Codex test designer pins `gpt-6-astra`; the Claude adapter
  prepares a handoff to it. Other roles inherit their parent model.
- Added shared entrypoints, requirements pointers, current state, engineering
  principles, worktree workflow and an allowlisted local context exporter.
- Added pinned Playwright/Chromium browser tooling and a bounded, explicit-origin
  crawler with local reports, screenshots and traces.
- Added Python 3.11 tooling checks, agent/BMAD drift checks, staged-artifact guard,
  local pre-commit hook and GitHub Actions workflow.
- Actual GPT-6 Astra authored intake, browser and foundation test catalogs.
  Intake regressions exposed malformed quoted CSV acceptance, now fixed with
  strict CSV parsing. Tooling regressions exposed unintended context export and
  mixed-case private-directory bypasses; both were corrected.

## Verification

Python 3.11.15, Node 22, Playwright 1.63.0 and Chromium were provisioned locally.
The following checks passed:

```text
uv run --no-project --python 3.11 python scripts/check_foundation.py
uv run --no-project --python 3.11 python scripts/repository_guard.py
uv run --no-project --python 3.11 python -m unittest discover -s tests/unit -v
git diff --check
```

The Python suite passed 36 tests: 26 intake cases and 10 foundation-tooling
regressions. The tooling tests use temporary repositories to verify staged blob
behavior without altering the main index. The local pre-commit hook was installed
through `scripts/install_hooks.py`.

`npm run test:browser` passed 10 controlled-fixture tests in 9.9 seconds.
Independent static review identified false-success handling of failed same-origin
resources. The crawler now fails on same-origin HTTP errors, request failures and
trace-write failures; four regression cases cover those failures and a healthy
resource control. Expected origin/method blocks remain non-failing. Astra's browser
QA catalog records the exact mapping and remaining design-only cases.

## Boundaries and next action

These are metadata and development-infrastructure checks. No AVA product UI,
trained model, inference benchmark, collected participant corpus or deployment
acceptance is established. Recording/cloning approval remains pending.

Native agent files were statically validated; launching each role through each
installed client's discovery mechanism was not tested. Hosted CI has not run.
Removing a catalog role currently requires reviewing/removing its old generated
adapters manually; drift checks validate active roles. The repository guard is
a filename/size safeguard, not a complete secret or participant-data scanner.

Review and commit a coherent baseline before creating separate Claude and Codex
writer worktrees. New worktrees do not inherit this uncommitted setup. Then use
`docs/engineering/workflow.md` for task ownership, QA and integration. Refresh
the portable bundle with `scripts/export_context.py` after context changes;
the export remains local and should be reviewed before sharing.
