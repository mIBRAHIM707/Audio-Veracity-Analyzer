# Shared Claude Code and Codex workflow

## Source of truth and safe parallelism

Use `AGENTS.md` for common behavior, `CLAUDE.md` as Claude's entrypoint, BMAD planning
documents for requirements/design, and `docs/context/current-state.md` for evidence.
Agent prompts live in `docs/agents/`; native wrappers are generated from those files.
One integration coordinator reconciles shared context and the PRD.

Two clients may read one checkout, but must not concurrently write it. Create one
branch/worktree per task from a common committed baseline. Merely using different
branches in the same directory does not isolate the working tree or Git index.

The current checkout contains uncommitted user planning changes and new tooling.
New worktrees do not inherit those files. First review and commit the intended
baseline on an integration branch; do not blindly stage the entire workspace.
No such baseline commit or worktree is created automatically by this setup.

Once the baseline is committed, example commands from the repository are:

```text
git worktree add ../ava-claude-data -b feat/11-data-intake HEAD
git worktree add ../ava-codex-qa -b test/3-foundation-qa HEAD
```

Launch Claude Code inside the first directory and Codex inside the second. Give
each an explicit issue, owned paths and acceptance criteria. Each worktree has
its own dependencies, reports, browser ports and model/temp output locations.
Use `git worktree list` to inspect ownership. Never delete another active worktree.

## Task lifecycle

1. Read context and relevant FR/ADR IDs. Select/create the issue before implementation.
2. Inspect status and choose one bounded change. The coordinator assigns owned paths.
3. Delegate independent research, Astra case design, implementation or review only
   where useful. No two writers own the same file. Keep shared PRD/state edits with
   the coordinator; workers return proposed changes in their handoff.
4. Write testable acceptance criteria. Astra designs substantive cases. Implement
   small changes and run the appropriate unit/integration/browser checks.
5. A different reviewer assesses the diff, test evidence and requirement coverage.
6. Write a per-task handoff. Commit only owned changes; integrate sequentially by
   PR/cherry-pick, resolve conflicts, and rerun tests on the combined result.
7. Update current state and relevant PRD/architecture passages in the same integrated
   change. Keep IDs stable and document changed assumptions or unmet gates.

Default handoff template: `docs/context/handoffs/TEMPLATE.md`. Include task, owner,
base commit, owned files, modifications, exact commands/results, unresolved defects,
model provenance, next action, and proposed shared-document changes.

## BMAD availability

BMAD 6.12.0 core and BMM are already installed. Preserve this version and existing
customizations. Claude discovers `.claude/skills`; Codex discovers `.agents/skills`.
`python scripts/sync_bmad.py` mirrors the complete installed `bmad-*` skill folders
including supporting resources, avoiding fragile Windows symlinks. This is a
project-managed mirror, not a claim the BMAD installer registered a second IDE.
`--check` detects divergence. Upgrade deliberately, then regenerate/check the mirror.

Use one installation source per client; installing an additional BMAD plugin may
create duplicate commands. After starting a fresh session, use the client's skill
picker or `/bmad-help` in Claude and `$bmad-help` in Codex if listed. Reading
`.agents/skills/bmad-help/SKILL.md` is the portable fallback when discovery differs.

BMAD supplies planning/development workflows; it is not a browser crawler. Playwright
supplies browser execution. GPT-6 Astra supplies test design. Their responsibilities
are intentionally separate.

## Cross-model handoff

Any model can read `AGENTS.md`, current state, the relevant canonical PRD/architecture
sections and a role file. Use `python scripts/export_context.py` to build a local
allowlisted handoff bundle without Git history, raw audio, personal manifests,
secrets, browser reports or machine-specific configuration. Review the bundle before
sharing. Future documentation changes can contain private information; allowlisting
is not a guarantee that the content is safe.

The export is not automatically uploaded and does not include the entire source tree.
Provide the needed source files separately after review when a task requires code.
No assistant can recover private conversation history from these files.

## Model and tool boundaries

The Codex `ava_test_designer` role pins `gpt-6-astra`. Other roles inherit the chosen
model. Claude's similarly named role prepares the handoff; Claude cannot select
an OpenAI model using its native `model` field. Run Astra in Codex and return the
case catalog to Claude for execution. If unavailable, report the limitation and
continue independent tasks; never falsify model provenance.

Do not run recursive autonomous loops or paid API requests merely because a role
file exists. Role descriptions authorize bounded work within the user task, not
unlimited execution, external messages, unattended deployment or publication.

## Reference documentation

- [Codex AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [Codex skills](https://learn.chatgpt.com/docs/build-skills)
- [Claude Code subagents](https://code.claude.com/docs/en/sub-agents)
- [BMAD installation](https://docs.bmad-method.org/start/install-bmad/)

Client commands and schemas were inspected on 2 October 2026. Installed versions
at setup: Codex CLI 0.155.0-alpha.16.3 and Claude Code 2.1.202. Check actual role
discovery after reload; these files do not alter personal model access or policies.
