# Punjabi pilot intake regression cases

Designed and implemented by **gpt-6-astra** on 2026-10-04 against working-tree changes based on commit `ad13029fa283183c23e84d736ac424b777e775c3`. Actual model provenance applies to this session. No commits or pushes are authorized.

Scope: [Punjabi importer](../../data/collect/meta_pnb_pilot.py), [fictional tests](../../tests/pnb/test_meta_pnb_pilot.py), [pilot spec](../../_bmad-output/implementation-artifacts/spec-pnb-pilot-intake.md), and the [source preflight evidence](../context/handoffs/pnb-source-preflight.md). FR-D1 supports source intake; FR-D8 supports speaker/row/hash lineage; FR-D13 supports decoding and quality screening; FR-E3/FR-E6 require local protected artifacts; FR-W2 requires honest layered QA. No test establishes Majhi coverage, demographic representation, cloning consent or final training eligibility.

Fixtures write actual small Parquet files containing four-second mathematical PCM signals encoded as FLAC. They use two fictional publisher speaker IDs, repeat `segment_id=s01` and reuse prompt IDs to expose incorrect grouping. All archives, originals, WAVs and transcripts live in temporary external directories and clean up after tests. No real 464 MB archive, corpus speech or network is used. Optional PyArrow/soundfile/NumPy/soxr dependencies are isolated from lightweight CI. Browser applicability is none.

All cases are P0 for this bounded importer. Selection uses complete speaker groups: two recordings per available speaker, capped at 30 total; if complete groups cannot fit or a speaker is insufficient, selection rejects instead of silently dropping speakers. Native PCM16 FLAC bytes remain immutable; decoded native-rate WAV is a separate derived file. Shared conditioning later creates a canonical 16 kHz grid and retains both original-audio and archive lineage.

| Case | Requirement / layer | Inputs and expected evidence | Executable test |
|---|---|---|---|
| PNB-01 | FR-D8 / Parquet integration | Two true publisher speakers with repeated prompt/segment labels yield two balanced speaker groups and four unique shard/row recording IDs; archive unchanged | `test_real_speaker_groups_not_repeated_segment_or_prompt_ids` |
| PNB-02 | FR-D1, FR-D8 / pure selection | Reversed metadata order selects identical identities; two per speaker; insufficient groups, invalid target/count and cap overflow rejected | `test_selection_is_order_independent_balanced_and_respects_total_cap` |
| PNB-03 | FR-D8, FR-D13 / decode integration | Extracted FLAC bytes exactly match embedded bytes; separate FLAC/WAV/archive hashes; WAV frames exactly match int16 decode at native 20 kHz; source speaker namespaced, publisher split train, dialect unknown, cloning unauthorized; transcript stored separately and marked source-only | `test_original_flac_and_decoded_pcm16_native_samples_are_exact` |
| PNB-04 | FR-D1, FR-D13 / metadata preflight | Wrong language, empty/null or overlong speaker ID, nonfinite/zero duration fail before either output root exists; namespace prefix remains within downstream speaker-ID limit | `test_invalid_row_language_missing_identity_and_nonfinite_duration_abort` |
| PNB-05 | FR-E3 / archive integrity | Publisher digest mismatch, wrong size and truncated archive reject before mutation | `test_publisher_archive_hash_size_and_truncation_fail_before_mutation` |
| PNB-06 | FR-E3 / path preflight | Relative escape/absolute local name, publisher dev path and traversal path reject; no output roots | `test_unsafe_manifest_paths_or_nontraining_partition_rejected` |
| PNB-07 | FR-D13 / decode integration | Valid PCM24 FLAC is unsupported: errors, diagnostic output and incomplete marker, no usable pilot; archive unchanged; no silent PCM16 conversion | `test_pcm24_flac_fails_without_silent_lossy_conversion` |
| PNB-08 | FR-D13 / mixed batch | One invalid FLAC with three valid siblings yields three verified and one error; diagnostics remain, pilot manifest withheld | `test_corrupt_embedded_audio_keeps_diagnostics_and_withholds_pilot` |
| PNB-09 | FR-E3 / output preflight | Existing raw/output roots and overlapping archive/raw/output roots reject without overwriting sentinel or creating new roots | `test_existing_or_overlapping_destinations_preserve_sentinels` |
| PNB-10 | FR-E3 / publication fault injection | Fail actual rename of staged pilot manifest after destination reservation; both roots retain `INCOMPLETE` and usable pilot manifest is absent | `test_publication_failure_cannot_leave_usable_complete_pilot` |
| PNB-11 | FR-D8, FR-D12 / preparation integration | Four verified 20 kHz sources produce four conditioned candidates with original FLAC/shard/row/language/split/prompt/segment lineage; raw bytes unchanged; source transcript text not copied as aligned candidate transcript | `test_conditioning_preserves_archive_audio_row_lineage_without_aligned_transcript` |
| PNB-12 | FR-E3 / concurrent-change fault injection | Mutate the archive after a decode; final integrity verification rejects publication; neither destination root created | `test_archive_modified_during_decode_refuses_publication` |
| PNB-13 | FR-E3 / preparation preflight | Otherwise valid pilot with `INCOMPLETE` in raw root | Reject conditioning before prepared output exists; marker preserved | `test_preparation_refuses_incomplete_raw_root_before_output` |

## Execution

```text
uv run --no-project --with-requirements requirements-pnb.txt --python 3.11 python -m unittest discover -s tests/pnb -v
```

Final targeted result on Windows/Python 3.11 with pinned requirements: **13 tests passed**, no skips, exit 0, in 2.002 seconds. Initial execution found missing `publisher_split` on individual output records: report-level split alone did not preserve the agreed preparation lineage field. The coordinator added that field and the unchanged lineage assertions passed. Expanded regressions exercised actual interrupted rename publication, archive mutation during decode, and the raw-root incomplete gate. No real archive was loaded and no commits or pushes were made.

Tests verify publisher metadata handling, not independent language or identity recognition. PCM16 exactness is tested for synthetic native mono audio; exhaustive codecs/subtypes, long real recordings, symlink/junction races, partial filesystem failures beyond injected rename, and memory use across production shards remain separate work. Only selected audio rows are intended to be decoded; these fixtures do not benchmark whole-shard memory consumption. Transcript text belongs to full source recordings and carries no timing alignment to cuts. No per-file human-approval gate is inferred from test passes.
