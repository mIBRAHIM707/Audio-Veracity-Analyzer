# AVA: Acoustic Veracity Analyzer

Final-year project (GIKI FCSE, BS Software Engineering, Sep 2026 to May 2027). Team: Ayesha, Ibrahim, Ilsa, Tughral.

AVA is an on-premises, CPU-only, passive detector for AI-synthesized (deepfake) voice in live Urdu and Punjabi phone calls. It receives a forked copy of call audio over SIPREC, scores 4 s windows every 2 s, and raises advisory alerts on an analyst dashboard. It never sits in the call path.

## Source of truth

- [prd.md](prd.md): requirements (FR-*, §6 non-functional targets), phase plan (§8), risks R1..R8 (§9), open questions (§10).
- [architecture.md](architecture.md): components (§2), model (§4), deployment and `ava.yaml` config (§7), security controls (§8), repo layout (§9), ADR-1..ADR-13 (§10).

Read the relevant section before designing or changing anything. If code and docs disagree, flag it rather than silently picking one. Cite requirement and ADR IDs in PR descriptions.

## Hard constraints

These come from the scope and regulation (CTDISR-2025, PDPB 2023). Don't trade them away for convenience.

- **CPU-only inference.** ONNX Runtime, INT8. No GPU at runtime (training may use GPUs).
- **No network egress** from runtime services. No model downloads, telemetry, CDN fonts, or third-party analytics at runtime. Models load from a local volume and startup fails loudly if one is missing.
- **Telephony audio**: 8 kHz mono, G.711 / GSM 06.10 / AMR-NB, upsampled to 16 kHz for the backbone.
- **Train/serve parity**: one `ingest` conditioning module (soxr resampling, Silero VAD) is shared by training augmentation and the runtime.
- **Speaker-disjoint splits**: no speaker appears in more than one of train/dev/test. One generator is held out as the unseen-attack test partition.
- **Passive and advisory**: AVA never sends media or re-INVITEs to the PBX beyond SIPREC answers.
- **Data minimization**: audio is processed in memory and discarded. Snippet retention is opt-in, TTL-bounded, and encrypted with AES-256-GCM.
- **Python 3.11** for every backend service and training (ADR-13). Dashboard: React + TypeScript + Vite, served by the API container.

## Repository layout (planned, architecture.md §9)

```
data/        manifests + scripts only; audio lives on a data volume, never in git
model/       ava/ (backbone, duabimamba, pooling), baselines/, train/eval/export/quantize
services/    siprec_srs/, ingest/, inference/, scorer/, api/
dashboard/   React + Vite
lab/         asterisk/, sipp/
deploy/      docker/, compose.yaml, k8s/
tests/       unit, integration, e2e
docs/        deployment guide, compliance checklist, model card
```

Most of this doesn't exist yet. Create folders as the work needs them and keep to this layout.

## Commands

None yet. Add build, test, lint, and run commands here as soon as they exist (PRD Phase 0 tasks 0.1 and 0.2).

## Workflow

Every change is tracked as a GitHub issue, so the history of what was built and fixed lives in issues and PRs.

1. **Issue first.** Every feature, task, and bug gets an issue before work starts. Use the templates in `.github/ISSUE_TEMPLATE/` (`gh issue create --template bug.md` or `feature.md`) and cite the PRD requirement or task ID. If you notice an unrelated bug mid-task, open a separate issue for it rather than fixing it in the current PR. Put each issue in its phase milestone (`Phase 0: Setup` … `Phase 5: Final Evaluation and Report`) and give it a workstream label (`ws-a-data`, `ws-b-model`, `ws-c-telephony`, `ws-d-platform`). PRD §8 tasks are titled `[N.M] <task>`. Phases 0–1 already have issues, and each later phase gets its issues when it starts.
2. **Branch** from `main` as `<type>/<issue>-<slug>`, e.g. `feat/14-vad-gate`, `fix/21-amr-decode`. Types: `feat`, `fix`, `docs`, `test`, `chore`.
3. **Commit** with the same type prefix: `feat: add Silero VAD gate`.
4. **PR** into `main` using the template. The body must say `Closes #<issue>` so merging closes the issue. Keep it to one issue per PR where practical.
5. **Review** is optional. Merge once you're satisfied, and request a review for risky or cross-workstream changes.

No AI attribution anywhere: no `Co-Authored-By: Claude` trailers, "Generated with Claude Code" lines, or similar in commits, PRs, issues, comments, or files.

Never push directly to `main`. Never commit audio, datasets, model weights, `.env`, or keys; `.gitignore` covers these.

`/ava-teach <concept>` explains any design decision from the docs (`/ava-teach curriculum` lists them all). Put personal, machine-specific notes in `CLAUDE.local.md` (gitignored).
