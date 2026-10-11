# AVA Claude Code entrypoint

@AGENTS.md

Both Claude Code and Codex use the shared instructions above. Begin by reading
`docs/context/current-state.md`, the relevant BMAD planning documents, and
`docs/engineering/workflow.md`. This repository implements an on-premises,
CPU-only, passive Urdu/Punjabi audio deepfake research prototype.

For the 4 October session transfer, read `docs/context/handoffs/claude-resume.md`.
Tughral now reports approval obtained; this supersedes historical pending-approval
notes. Record its scope and follow the approved protocol with individual consent.

Use `docs/context/todo.md` as the active checklist. After each small step passes
QA, mark it complete, show the updated checklist, and continue to the next available
step automatically. Preserve blockers and do independent work; do not infer approval.

For established CSALT data, retain published labels and run automated checks on
all files with sampled/targeted listening. Do not block routine intake on per-clip
human approval. See `data/collect/README.md` for scope and the balanced manifest.

## Native capabilities

- BMAD is installed in `_bmad/`; invoke the installed `/bmad-help` skill to navigate
  workflows. Do not reinstall or upgrade it without an intentional version change.
- Project agents are in `.claude/agents/`; their detailed contracts are shared in
  `docs/agents/`. Use only the agents needed for the bounded task.
- `ava-test-designer` in Claude is an Astra handoff role, not an OpenAI model
  running inside Claude. GPT-6 Astra test design runs in Codex; Claude can execute
  the resulting cases with the same Playwright/Python commands.
- Neither tool's private conversation history is the source of project truth.
  Shared state, decisions, tests, and per-task handoffs must live in the repository.

## Commands

```text
uv run --no-project --python 3.11 python scripts/check_foundation.py
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python -m unittest discover -s tests/unit -v
python scripts/sync_agents.py --check
python scripts/sync_bmad.py --check
```

Optional CPU model tests use `requirements-model.txt` and `tests/model/`; uv also
needs explicit `--extra-index-url https://download.pytorch.org/whl/cpu`. See
`models/smoke/README.md`. Keep this environment separate from lightweight data tests.

Use the installed Playwright commands in `package.json`; see `README.md` for setup.
Use `data/prepare/prepare_audio.py --condition` for explicit native PCM16 to 16 kHz
mono conversion and per-clip −3 dBFS peak normalization; see its README for the
external-output command. Originals stay immutable. The current user instruction
is to continue the todo list and leave changes uncommitted until further notice.
Keep this file short. Update it when commands or operating policy changes; write
progress/evidence to `docs/context/current-state.md`, and product changes to the
canonical PRD. Do not duplicate the whole PRD here.

## Workflow

Every change is tracked as a GitHub issue, so the history of what was built and fixed lives in issues and PRs.

1. **Issue first.** Every feature, task, and bug gets an issue before work starts. Use the templates in `.github/ISSUE_TEMPLATE/` and cite the PRD requirement or task ID. Put each issue in its phase milestone (`Phase 0: Setup` through `Phase 5: Final Evaluation and Report`) and give it a workstream label (`ws-a-data`, `ws-b-model`, `ws-c-telephony`, `ws-d-platform`).
2. **Branch** from `main` as `<type>/<issue>-<slug>`, e.g. `feat/14-vad-gate`. Types: `feat`, `fix`, `docs`, `test`, `chore`.
3. **Commit** with the same type prefix: `feat: add Silero VAD gate`.
4. **PR** into `main` using the template; the body says `Closes #<issue>`.
5. **Review** is optional by team convention, but the `main` ruleset requires one approval; request one for risky or cross-workstream changes.

No AI attribution anywhere. Never push directly to `main`. Never commit audio, datasets, model weights, `.env`, keys or consent material; `.gitignore` and `scripts/repository_guard.py` cover these.

`/ava-teach <concept>` explains any design decision from the canonical documents; the `ava-viva` agent builds panel-question flashcards. Put personal, machine-specific notes in `CLAUDE.local.md` (gitignored).
