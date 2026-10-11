# Shared tooling regression cases

Designed and implemented by **gpt-6-astra** on 2026-10-02, based on working-tree scripts over commit `09541b596cb7dae3d85c61028276d59161552098`. This records actual model provenance for this session. It does not prove client model availability or agent discovery.

Scope: `scripts/sync_agents.py`, `sync_bmad.py`, `export_context.py`, `check_foundation.py`, and `repository_guard.py`. Executable cases live in [test_foundation_tools.py](../../tests/unit/test_foundation_tools.py). FR-E3/FR-E6 are the PRD data-locality and repository-guard requirements. All cases support **FR-W4** (portable tooling and automated checks); FT-04/FT-10 additionally support **FR-W2** (Astra selection), and FT-08/FT-09 support **FR-W1** (shared context). `TOOL-*` references below denote narrower tooling contracts from script docstrings and [shared workflow](../engineering/workflow.md); they are not additional product requirements. Passing these cases is partial evidence for those requirements, not native-client acceptance.

Shared preconditions: Python 3.11 and Git available; fresh temporary directory per test; fictional text only. Temporary repositories have independent indexes and no remote or commit requirement. Module roots are patched into those directories. Every temporary file and repository is cleaned by `TemporaryDirectory`; the working repository's index is never touched. Retain test command, exit status and failure details. No browser, paid API, native agent, network, real audio or personal data is involved.

| Case | Requirement / contract | Priority / layer | Preconditions and inputs | Expected outcome | Automation / evidence |
|---|---|---|---|---|---|
| FT-01 | FR-E3, FR-E6; TOOL-GUARD | P0 / unit | Forbidden audio/model/key extensions, environment secret names, participant manifests, mixed-case private/raw paths; safe names at 1,000,000 and 1,000,001 bytes | Block forbidden paths; permit `.env.example`; permit exact 1 MB and reject one byte above | `test_guard_forbidden_paths_and_exact_size_boundary`; subtest identifies failing path |
| FT-02 | FR-E6; TOOL-STAGED | P0 / Git integration | Temporary repo; stage 1,000,001-byte text, replace working copy with small text | Staged guard fails using index blob; working-tree guard passes | `test_staged_guard_reads_index_blob_not_smaller_working_copy`; real `git cat-file` evidence |
| FT-03 | FR-E6; TOOL-STAGED | P0 / Git integration | Stage small text then enlarge working copy; remove staged addition and delete file | Staged guard passes while working-tree guard fails; empty staged selection passes without missing-blob failure | `test_staged_guard_ignores_unstaged_large_edit_and_deleted_paths`; no commit history required |
| FT-04 | TOOL-ADAPTERS; Astra model boundary | P0 / filesystem integration | Fictional shared instructions and test-designer catalog with quoted description; generate twice and check | Byte-identical adapters; valid TOML; Codex selects `gpt-6-astra`; Claude inherits model and states it cannot run Astra; clean check | `test_agent_generation_is_idempotent_and_preserves_model_boundary` |
| FT-05 | TOOL-ADAPTERS; read-only drift check | P0 / filesystem integration | Missing adapters, then generated adapter deliberately altered | Check returns nonzero in both cases, identifies drift and neither creates nor rewrites adapters | `test_agent_check_reports_missing_and_edited_adapters_without_writing` |
| FT-06 | TOOL-BMAD; complete resources and manual stale review | P0 / filesystem integration | Installed manifest; skill and nested resource; cache and unrelated skill; generate twice; add stale destination; alter resource | Resources mirror identically; caches/unrelated skills excluded; stale file reported and preserved; altered file detected without repair in check mode | `test_bmad_mirror_copies_resources_ignores_cache_and_reports_stale` |
| FT-07 | TOOL-BMAD; missing installation | P0 / filesystem integration | No installed source/manifest | Fail without creating destination mirror | `test_bmad_missing_installation_does_not_create_mirror` |
| FT-08 | FR-E3; TOOL-EXPORT explicit selection | P0 / filesystem integration | Explicit listed docs/catalog role plus unrelated `docs/agents/private-notes.md`, private CSV and `.env` marker files | Export contains approved role and base revision; excludes every unapproved marker and filename | `test_context_exports_only_explicit_files_and_catalog_roles`; Git revision lookup stubbed, actual file selection/writing exercised |
| FT-09 | TOOL-EXPORT fail closed | P0 / filesystem integration | Remove required `AGENTS.md` from otherwise valid context | Clear missing-context failure; no export artifact created | `test_context_missing_required_file_fails_without_export` |
| FT-10 | TOOL-FOUNDATION; Astra model and honest validation scope | P0 / unit with fixture files | Complete minimal static foundation; valid adapter, then wrong model | Valid config passes with static-only disclaimer; wrong model fails with required model named | `test_foundation_rejects_wrong_astra_model_and_reports_static_scope`; subprocess drift checks stubbed, separately tested above |

## Boundaries and uncovered cases

These tests exercise meaningful failure paths without claiming full content privacy scanning. Filename/size guards do not detect secrets embedded in allowed text. Exported allowlisted documentation still requires review before sharing. Model fields and valid generated files do not prove clients discover roles or can access the requested model.

Future hardening cases include malicious catalog traversal/symlink targets, stale generated agent adapters after role removal, malformed catalogs, genuinely staged deletions/renames from committed history, subprocess failure reporting, concurrent generation/export, interrupted writes, and context output symlink handling. No current test claims those guarantees. Shared-tree scripts are not mutated by this suite; the coordinator owns fixes.

## Execution

```text
uv run --no-project --python 3.11 python -m unittest discover -s tests/unit -p test_foundation_tools.py -v
```

Initial execution on 2026-10-02 ran 10 tests: eight passed, two failed with three assertions. FT-01 exposed case-sensitive private-directory checks (`Private`, `RAW`); FT-08 exposed wildcard inclusion of unlisted role-folder Markdown. The coordinator fixed both scripts; the unchanged assertions then passed: **10 tests passed on Python 3.11**, exit 0.

The required integrated static check also passed:

```text
uv run --no-project --python 3.11 python scripts/check_foundation.py
```

Observed output: 16 agent adapters checked; 29 BMAD skills / 214 files checked; eight shared roles and canonical context present; exit 0. It explicitly states native-client discovery is a separate check. uv's no-project warning did not affect either result.
