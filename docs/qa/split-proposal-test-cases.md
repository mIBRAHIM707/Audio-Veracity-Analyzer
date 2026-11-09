# Split proposals: test-case catalogue

**Provenance.** Claude-authored (Claude Code, Opus 5.5), 9 November 2026, branch
`feat/4-daily-development`, backlog 1.3. Not a GPT-6 Astra catalogue: Astra access
ended on 7 November and Claude now designs test cases. The earlier request in
`split-proposal-astra-handoff.md` is kept as the risk list these cases follow.

**Scope.** `data/manifest/propose_splits.py` and `validate_splits.py` at schema
version 2, which implements spec decision 7 (benchmark rows follow their speaker;
`benchmark_only` is a scope, not a partition). Requirements: FR-D8 (partial,
pilot proposals only), A4, D11/ADR-14 placement half, FR-E3. Fictional rows only,
stdlib only, temporary directories. Passing cases are infrastructure evidence for
placement rules, not dataset acceptance, consent or training eligibility.

| ID | Requirement / layer | Inputs and expected evidence | Test |
|---|---|---|---|
| C01 | A4, FR-D8 / unit | 9+5 speakers in two sources, 6 row orders; every speaker one partition, identical output | `test_c01_speaker_atomic_across_orders_and_interleaved_sources` |
| C02 | Decision 3 / CLI | Same input under `PYTHONHASHSEED` 0 and 31337 in separate processes; byte-identical `proposal.json` | `test_c02_byte_identical_proposal_under_two_interpreter_hash_seeds` |
| C03 | Decision 1, 3 / unit | Seed change moves hashed speakers, publisher-held-out speakers unchanged | `test_c03_seed_change_moves_hashed_speakers_only` |
| C04 | Decisions 2, 8 / unit | 4 and 5 speakers resolve remainder ties to 2/1/1 and 3/1/1 | `test_c04_allocation_four_and_five_speakers_resolve_ties_train_first` |
| C05 | Decision 2 / unit | 6 speakers: ascending speaker-hash order fills train×4, dev, test | `test_c05_allocation_follows_ascending_speaker_hash_order` |
| C06 | Decision 7, D11 / unit | Benchmark twin for every genuine speaker; validator ok; 16 trainable train rows remain; 12 benchmark rows counted | `test_c06_benchmark_twin_of_every_genuine_speaker_never_empties_training` |
| C07 | Decisions 1, 7 / unit | Benchmark row in a publisher-dev group stays `held_out_publisher_dev` with `benchmark_follows_speaker` | `test_c07_benchmark_row_in_publisher_held_out_group_stays_held_out` |
| C08 | Decision 7 / validator | Benchmark row with `training_eligible` true, `hashed_speaker` rule, or old `benchmark_only` partition is rejected | `test_c08_validator_rejects_benchmark_flag_or_rule_tampering` |
| C08b | Decision 7 / validator | Benchmark row moved alone shows one speaker crossing; `benchmark_only` false or dropped is rejected | `test_c08b_benchmark_row_moved_alone_or_flag_dropped_is_rejected` |
| C08c | Decision 9 / validator | Coordinated relabel (flag dropped, trainable, hashed rule) passes row checks but fails the `proposal_id` content digest; schema 1 rejected | `test_c08c_coordinated_relabel_of_benchmark_row_breaks_content_digest` |
| C09 | Decision 7 / validator | Rows lacking `training_eligible` are not trainable; only explicit true counts | `test_c09_trainable_counts_need_explicit_true` |
| C10 | Risk 8 / unit | Malformed lineage matrix (None, empty, wrong type, bad hex, non-finite or bool duration, string booleans) fails before output | `test_c10_malformed_lineage_matrix_fails_before_output` |
| C11 | Risk 7 / unit | Uppercase copy of a source hash still links two speakers | `test_c11_uppercase_hash_links_with_lowercase_twin` |
| C12 | Risk 7, todo 9d / unit | Shared `original_audio_sha256` across sources links the speakers; zero crossing | `test_c12_cross_source_duplicate_original_links_speakers_across_sources` |
| C13 | Risk 11, 12 / CLI | Validator exit 0 clean; 1 for accepted status, mismatched id, unknown split, mixed seed, unreadable JSON; 2 for usage; no speaker IDs in findings | `test_c13_exit_matrix_and_no_speaker_identities_in_findings` |
| C14 | Risk 13 / publication | Report schema 2, benchmark and trainable tables, limitation text, no `benchmark_only` partition | `test_c14_report_states_scope_and_schema_version_two` |

Baseline coverage (Claude-authored, `test_split_proposals.py`, updated to decision 7
the same day): hash vector, allocation table, order/seed determinism, 17/6/3/1/2
speakers, source-namespaced labels, hash-linked groups, benchmark-follows-speaker,
both-flags rule, publisher rules and conflicts, recording with two speakers, seed and
row bounds, validator rejections, within-partition duplicates, cross-partition hash
overlap, publication, no-overwrite and fault injection, input bounds, CLI exit codes.

**Execution (9 November 2026, after independent review fixes).**
`tests.unit.test_split_proposals_cases`: 16 passed; `tests.unit.test_split_proposals`:
20 passed. Full-suite figures are in the daily run report.

**Open question (spec decision 9).** Speaker groups with only benchmark rows still
take hashed allocation slots; not material for current CSALT data.

**Limitations.** Python 3.12 cross-version stability not run (repository pins
3.11). Concurrent `mkdir` races are not tested. Real proposal rerun under schema 2
is backlog 1.4. Nothing here establishes identity, consent, FR-D11 compliance or
eligibility.
