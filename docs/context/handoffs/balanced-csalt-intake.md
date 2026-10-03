# Balanced CSALT intake — 3 October 2026

Completed the user's approved todo items 1–3: source-aware QA policy, wider genuine
Urdu coverage and consolidated source inventory. Related issue #11; partial FR-D1,
FR-D8 and FR-D13. Existing user planning/foundation changes are preserved.

## Data results

Pinned source release: `eb8f16623108324867e4424a646937409c64b82b`. Official file
metadata enumerated 3398 genuine WAVs across 17 source speaker folders. The local
inventory now contains 520 originals; 340 form the balanced working selection
(20 per speaker). The other 180 are preserved extras from Speaker_01, explicitly
excluded by `selected_for_pilot=false`.

Downloaded 270 new originals across two runs. The balanced selection contains
45.502 minutes / 87.975 MB: 331 files at 16 kHz mono PCM16 and nine at 20 kHz mono
PCM16. The latter are preserved unchanged and flagged for later format conversion.
All 520 originals passed publisher SHA256/size and decoded-WAV checks. Independent
verification proved all 250 previously downloaded originals remained unchanged,
the CSV/JSON selection agrees, and each source speaker has exactly 20 selections.
The existing structural validator passed all 520 inventory rows.

Machine-local `.local/data-location.json` points to the final v2 `inventory.csv`,
`balanced.json`, `report.json` and source catalog. The first v1 diagnostic report
remains preserved; it correctly withheld the balanced manifest when nine valid
20 kHz originals were rejected by the initial overly strict intake format check.
Use v2. No source manifests or audio are stored in Git.

## QA policy and meaning

The project owner accepts CSALT for noncommercial detection research under its
published CC BY-NC 4.0 terms. Retain publisher-supplied language/class labels with
provenance. Every file receives automated checks; listening is sampled/targeted,
not an individual ingestion approval gate. The PRD and Claude context reflect
this. Signal/duration/format flags are not automatic rejection or failed listening.

`use_status=approved` means this declared noncommercial research scope. It is not
cloning consent or commercial permission. Splits remain unassigned. Human review
was reported for some unspecified clips but no individual review facts invented.

## Implementation and verification

Importer: `data/collect/csalt_pilot.py`; deterministic existing-first selection,
staged verified no-overwrite downloads, consolidated inventory and balanced output
only when required speaker counts pass. Production CLI checks all 17 speakers.
Initial catalog bytes bind the provenance hash. Corrupt existing files are never
replaced. Unexpected local files are reported rather than silently imported.

Actual GPT-6 Astra authored 15 importer tests; all passed. Full Python suite:
76 discovered, 74 passed and two existing symlink tests skipped on Windows.
Foundation/repository checks passed. An independent reviewer found four issues
(download publication, catalog hash binding, speaker completeness, partial output)
which were fixed and rechecked; native-format preservation was reviewed separately.
No browser changes or browser test run were needed for this intake increment.

## Next task

Implement and verify a separate conditioning stage that converts copies to
16 kHz mono, preserves source/derivative lineage and handles loudness consistently.
The existing preparation CLI still requires 16 kHz mono, so do not feed the mixed
native-format balanced manifest into it unchanged. Existing raw audio is immutable.
Then prepare this balanced pilot and proceed to the separate synthetic benchmark.
