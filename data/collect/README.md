# Dataset intake pilot

Initial groundwork from PR #26, revision `44bb37d024cf0e5ea9e7627cab74f2da138d7ecc`:
FR-D1, FR-D8, FR-D13, FR-E1, FR-E2 and FR-E5. Related existing issues: #6 and #11.
Issue #11's old Common Voice pa-PK wording is superseded by this PRD.
The metadata validator and CSALT importer provide partial intake/audio QA; they
do not complete those requirements or establish model readiness.

## What comes first

1. Review each source's actual terms and record evidence in a private rights register.
   `source_register.json` records project decisions and remaining questions.
   CSALT noncommercial detection intake is approved by the project owner; this
   tool itself grants no rights. Separate detection use from cloning use.
2. Select controlled storage outside the repository for audio and real manifests.
   Keep names, contacts and consent forms separately from anonymous speaker IDs.
3. Import a small permitted genuine sample: aim initially for 50 Urdu and 50
   Pakistani Punjabi clips spanning several speakers, not thousands of files.
   If Punjabi access is unresolved, start Urdu tooling and report the gap.
4. Copy `intake_template.csv` into that external storage and populate one row per
   original clip. Use `unassigned` until speaker grouping and the split policy are
   reviewed. Do not assign splits independently per clip.
5. Run the validator and automated audio checks. For established public research
   corpora such as CSALT, retain published class/language labels and use a small
   listening sample plus targeted review of suspicious outputs. Do not require
   every original to receive individual human approval before routine ingestion.
6. Add automated decoding, clipping/silence/loudness checks, import adapters,
   synthetic lineage, and the full FR-D8 manifest as subsequent increments.

## Run

Python 3.11+ standard library only; no model, GPU, network or package installation.

```text
python data/collect/validate_intake.py PATH_TO_EXTERNAL_STORAGE/pilot.csv
python -m unittest discover -s tests/unit -p test_intake.py -v
```

The command emits aggregate counts and row-number errors; exit 0 passes structural
checks and exit 1 fails. Counts include structurally populated rows even when a
later check fails; they are not accepted-dataset totals. A header-only template
fails deliberately. `approved` records a human review; the tool cannot grant rights.

`speaker_id` is a stable pseudonymous source key, not a guessed identity.
`source_recording_id` groups cuts from the same original recording. `sha256` is
the actual original file hash, not a random ID. `licence_ref` points to the private
review evidence. Store real filled manifests outside Git even if IDs are pseudonymous.

The validator checks required metadata, duplicate IDs, finite positive durations,
declared usage approval, and conflicting speaker/recording/hash split assignments.
It supports genuine Urdu and Pakistani Punjabi intake only. It does not read audio,
verify declared metadata against files, identify near-duplicates, reconcile the
same speaker across different corpora, enforce source-specific clip caps, validate
cloning permission, or prove dataset fairness. Other evaluation dialects and
synthetic lineage belong in the later full manifest implementation.

## Participant collection while approval is pending

User confirmed on 2 October 2026 that recording and cloning approval is not yet in
place. Prepare the protocol and request the approval described in PRD FR-E1 before
recording participants. Consent must include FR-E2's separate cloning opt-in,
withdrawal process, storage/deletion policy and permitted sharing audiences.

Once approved, pilot the process with 2-4 adults before larger recruitment. Each
session follows FR-D4: 10-12 minutes of read Urdu/Shahmukhi Punjabi sentences,
natural conversation/code-switching and a fictional bank-call role-play. Never
ask for actual account details. Use a WAV-capable recorder or the validated PBX
recording path; WhatsApp voice notes are excluded by the PRD. Preserve originals,
listen immediately, then cut accepted material into 3-6 second clips in processing.

Tughral, Ibrahim, Ilsa and Ayesha each handle their own participant scheduling,
consent, recording and secure transfer. Do not generate clones until the relevant
speaker opt-in and generator-use review are complete.

## Balanced CSALT genuine intake

`csalt_pilot.py` consumes a pinned official file catalog and builds a balanced
selection while preserving every existing original. Use 20 clips per speaker for
the first balanced pilot. Extra local originals remain in the consolidated
inventory with `selected_for_pilot=false`; they are not silently included in the
working sample. Selection prefers existing files, then uses a stable hash order.

```text
uv run --no-project --python 3.11 python data/collect/csalt_pilot.py --catalog D:/AVA-Data/source-metadata/csalt-genuine-catalog.json --source-root D:/AVA-Data/raw/csalt-urdu --output-root D:/AVA-Data/manifests/csalt-balanced-new-run --per-speaker 20
```

The catalog contains `revision` (full 40-character commit) and `files`, each with
the official relative `path`, `size` and `lfs.oid` SHA256. Use official Hugging Face
file metadata for that revision. Never replace publisher hashes with locally
invented values. A new output directory is required. The importer downloads only
missing selected WAVs, verifies publisher hash/size and PCM format, and records
signal flags. A corrupt existing original is reported and never overwritten.
Native PCM16 originals at 8–192 kHz, mono or stereo, are preserved unchanged.
Sources outside the downstream 16 kHz mono format carry
`requires_format_processing`; they are valid intake, not broken recordings.
Use the preparation CLI's explicit `--condition` option for a mixed-format manifest;
it converts copies to 16 kHz mono and normalizes derived clips after segmentation.
The default exact-sample mode still requires 16 kHz mono input.
The production CLI requires the verified 17-speaker inventory. Downloaded files
are staged before atomic no-overwrite publication (requires local hard-link
support, available on this machine's NTFS data drive).

Outputs: `inventory.csv` (all local catalog originals), `balanced.json` (only
verified selected originals, usable by the preparation CLI), and `report.json`
(coverage, errors and local files outside the catalog). Ignore an output directory
while its `INCOMPLETE` marker exists. Failed rows are retained with their reason;
exit zero requires the requested balance and no file failures. Flags for quiet,
clipping or duration do not constitute individual listening rejection.
If selected files fail, only the diagnostic inventory/report are published;
`balanced.json` is withheld until every required speaker reaches the target.

CSALT use is approved by the project owner for noncommercial detection research
under its published CC BY-NC 4.0 terms. `approved` in this inventory refers to that
scope, not a new institutional approval, commercial licence, or cloning consent.
Class/language provenance is publisher-supplied. Individual listening status stays
explicitly unreviewed where no clip-specific review record exists. Preserve the
original published partitions and source speaker IDs; all AVA splits remain
`unassigned`. This genuine-only pilot is not yet a final training dataset.

## Separate existing-synthetic benchmark

`csalt_synthetic.py` imports a small deterministic selection from both published
synthetic partitions. The default is two originals per generator-family/speaker
folder:68 files across17 folders and two families. Full pinned revision and
publisher LFS SHA256/size are mandatory; native decoding and signal checks reuse
the genuine importer's functions. Existing files are verified and never replaced.

```text
uv run --no-project --python 3.11 python data/collect/csalt_synthetic.py --catalog D:/AVA-Data/source-metadata/csalt-synthetic-catalog.json --source-root D:/AVA-Data/raw/csalt-synthetic-benchmark --output-root D:/AVA-Data/manifests/csalt-synthetic-new-run --per-group 2
uv run --no-project --python 3.11 --with-requirements requirements-audio.txt python data/prepare/prepare_audio.py --condition --manifest D:/AVA-Data/manifests/csalt-synthetic-new-run/benchmark.json --source-root D:/AVA-Data/raw/csalt-synthetic-benchmark --output-root D:/AVA-Data/processed/csalt-synthetic-new-run
```

Both output roots must be new, external directories. `inventory.csv` and
`report.json` include failed entries; `benchmark.json` is published only for a
complete selection. Never consume an `INCOMPLETE` output. JSON booleans
`benchmark_only: true`, `training_eligible: false` survive preparation alongside
generator family, original partition and explicit unknown version/reference.

This permits a noncommercial detector smoke test. It does not satisfy final AVA
training requirements, establish consent, verify target identity, or generate any
new voice. Publisher speaker folders remain grouping keys. The genuine-only
`validate_intake.py` deliberately rejects synthetic rows; do not change labels to
make this benchmark pass that validator. See the Astra benchmark cases and source
verification handoff for the separate checks.

## Before generating synthetic data

PRD FR-D11 names two permitted cloning pools: participants who ticked the
cloning box, and, by project-owner decision of 5 October 2026 under the
university approval, CSALT Urdu (CC BY-NC 4.0) and Meta Omnilingual pnb_Arab
(CC BY 4.0) speakers. Common Voice is not in the cloning pool until its
no-reidentification terms are reviewed. Keep each speaker and every derivative
(reference audio, cuts, codec copies, clones, conversions) within one split, and
record the basis in the licence register. Manifests written before the decision
still say `cloning_status: not_authorized` and `training_eligible: false` for
these sources; regenerate them (todo 9e) rather than editing rows in place.

The source register links current official pages. Common Voice's page includes
no-reidentification/no-resharing conditions alongside its CC0 label; CSALT states
CC BY-NC 4.0. Meta's general card lists CC BY 4.0. On 2 October 2026 the official
README and file-list API confirmed pnb_Arab train/dev/test files; actual audio,
speaker counts and Majhi coverage still require inspection. Habib and UrduSpeech rights remain
pending. No participant has been contacted by these tools.
