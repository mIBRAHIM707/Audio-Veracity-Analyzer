# AVA shared agent instructions

Read this file in both Codex and Claude Code. User instructions take precedence.

## Start here

1. Read `docs/context/current-state.md` for measured state and unresolved work.
2. Read relevant requirements in `_bmad-output/planning-artifacts/prd.md` and
   decisions in `_bmad-output/planning-artifacts/architecture.md`.
3. Read `docs/engineering/workflow.md` and your role in `docs/agents/`.
4. Check `git status --short --branch`; preserve pre-existing changes.
5. State the task, owned paths, tests, and completion evidence before editing.

The BMAD documents are canonical. Root `prd.md` and `architecture.md` are pointers,
not alternate requirements. Historical teacher notes and PR issue titles can be
stale. Distinguish implemented behavior from planned requirements.

## Product constraints

- CPU-only serving; no runtime downloads, telemetry, CDN assets or internet dependency.
- Passive, advisory detector; AVA failure must not stop telephone calls.
- Urdu/Pakistani Punjabi, Asterisk tap primary; SIPREC stretch; live AMR-NB stretch.
- Model direction: AntiDeepfake MMS-300M plus SLS; XLS-R plus SLS fallback.
- Shared conditioning across training and serving; speaker/lineage-disjoint splits.
- No real audio, private manifests, consent forms, credentials or trained weights in Git.
- Tughral reported university approval obtained on 4 October 2026, covering the
  team's recording and voice-cloning research; the reference number is still to
  be recorded in `docs/data-collection/approval-pack.md`. Participants still give
  their own signed choices, including a separate cloning opt-in.
- Project-owner decision, 5 October 2026 (PRD FR-D11, D11): public corpora whose
  licences permit derivative works may be used for training and for voice cloning
  in this research: CSALT Urdu (CC BY-NC 4.0, noncommercial only) and Meta
  Omnilingual pnb_Arab (CC BY 4.0, attribution). Common Voice is excluded from
  cloning until its no-reidentification terms are reviewed. No cloned voice,
  public or participant, is released (PRD D14). Tool terms still apply
  (ElevenLabs output may not be used for detectors).
- CTDISR-2025 binds telecom licensees and none takes part; it is design
  motivation, not an obligation on this project (PRD §6.4).

## Parallel work and ownership

The user authorizes bounded specialist delegation when it helps this task. Use
independent tasks with explicit owned paths; avoid idle or duplicative agents.
Use separate Git worktrees/branches for simultaneous Claude Code and Codex writers.
Never have both clients change the same checkout, index, PRD, or shared state file.
One coordinator integrates changes and reconciles shared context. Workers write
separate `docs/context/handoffs/<task>.md` files. Do not stash, reset, discard or
commit another worker's changes. Follow the workflow's clean-baseline requirement.

## Test design and execution

- Use **GPT-6 Astra (`gpt-6-astra`)** to author substantive test-case designs as
  requested by the user. Record actual model provenance; never relabel Claude output.
- Claude can implement/run cases and prepare an Astra handoff. If Astra is
  unavailable, report it; do not silently substitute a test designer.
- Use Playwright for browser crawling and browser tests. Use Python unit tests
  for data/scoring logic and dedicated tests for audio, model and telephony behavior.
- Fixture smoke tests are infrastructure evidence, not AVA product acceptance.
- Test normal, boundary, malformed, failure/recovery and relevant concurrency paths.
  Do not weaken assertions, hide failures with retries, or mock the claimed evidence.
- Independent QA reviews findings and evidence; passing tests never means bug-free.

## Construction practices

Use cohesive modules, explicit contracts, typed boundaries, dependency injection
where there are real replaceable collaborators, and pure domain functions with
I/O at edges. Prefer composition and small changes; avoid speculative frameworks.
See `docs/engineering/design-principles.md` for project-specific patterns and tests.
Validate inputs, bound resource use, make lifecycle cleanup explicit, and keep
train/serve parity testable. Do not add architecture only to name a pattern.

## Commands and context maintenance

Python target: 3.11; Node target: 22. See root README for install/test commands.
Run the relevant tests and `python scripts/check_foundation.py` before handoff.
Update `docs/context/current-state.md` with evidence after meaningful integrated
changes. Update PRD requirements when behavior/scope changes, preserving IDs and
the distinction between proposals and accepted changes. Update CLAUDE.md only
when entrypoints/commands/policy change. Never use the PRD as a chat transcript.

BMAD 6.12.0 is installed. Claude skills: `.claude/skills/bmad-*`; Codex mirror:
`.agents/skills/bmad-*`. After deliberate BMAD changes run
`python scripts/sync_bmad.py`, then its `--check`. Do not install a duplicate plugin.
Generated role adapters: `python scripts/sync_agents.py`; edit canonical role
files/catalog first, and run `--check` to detect drift.

Issue-linked branches and reviewable PRs remain the project workflow. Do not push
directly to main. No AI attribution trailers. Do not send messages to participants
or outside contacts without explicit user authorization.
