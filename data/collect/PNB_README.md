# Bounded Punjabi pilot

`meta_pnb_pilot.py` imports locally staged official Meta `pnb_Arab` train shards
on CPU. It performs no downloads. Source evidence and publisher archive hashes
are in `docs/context/handoffs/pnb-source-preflight.md`.

Install `requirements-pnb.txt`, then run with external paths:

```text
python data/collect/meta_pnb_pilot.py --source-manifest SOURCE.json --archive-root ARCHIVES --raw-root NEW_RAW --output-root NEW_REPORTS --per-speaker 2 --max-sources 30
```

The source JSON requires `repo=facebook/omnilingual-asr-corpus`,
`revision=8648ba8946377697b427ae952076e49fc0e5e44d`, `language=pnb_Arab`,
`licence=CC-BY-4.0`, and `files` containing one or both official train shard paths.
Each file has `path`, basename-only `local_name`, integer `size`, `sha256`, and
matching `publisher_lfs_sha256`. The caller obtains these expected values from
the official pinned file-list API; this importer verifies bytes against them.
It never substitutes a locally computed hash for a missing publisher hash.

Archives, raw outputs and reports must be disjoint; both output roots must be
new and outside the repository. Archive size/hash and all row metadata are
checked before creating outputs. Selection uses revision/shard/row hashes and
an equal allocation per observed publisher speaker. A cap that cannot fit the
requested equal allocation fails explicitly; it never silently drops speakers.
Metadata scans exclude audio. Audio iteration uses batches of eight, and only
selected rows are converted to Python values for extraction.

Original selected FLAC bytes live in `flac/`; native-rate/channel PCM16 WAV copies
live in `wav/`. Unsupported subtype/format, rates outside 8–192 kHz, more than
two channels, duration disagreement over 0.1 seconds, and WAVs over 10 MB fail.
The current shared preparation CLI can consume successful `pilot.json` with
`--source-root NEW_RAW --condition`. It does the explicit canonicalization;
this importer does not silently resample or normalize native audio.

Source transcripts/prompts live in separate `transcripts/` JSON files and apply
only to original recordings. They are not aligned transcripts for derived clips.
Rows retain separate archive, original FLAC and immediate WAV hashes. Shard path
plus row index identifies the recording: publisher `segment_id` alone is not
unique. Source speaker labels are preserved and AVA speaker IDs are namespaced
`meta_pnb_...`, independent of split and revision.

`inventory.json` and `report.json` describe extraction outcomes. `pilot.json` is
published only if every selected recording succeeds. A failed extraction leaves
diagnostic directories marked `INCOMPLETE`; publication errors also leave markers
on reserved roots. Consumers must reject either root while its marker exists.
No destination is overwritten. Inspect failures and choose fresh output roots
for another attempt; cleanup of prior evidence is never automatic.

Publisher language becomes AVA `pnb`, with original `pnb_Arab` retained. Labels are
publisher-derived genuine speech. Dialect and identity remain unverified. The
importer writes `cloning_status: not_authorized` and `training_eligible: false`
as conservative defaults; since the project-owner decision of 5 October 2026
(PRD FR-D11) these speakers may be cloned and trained on under the university
approval, and manifests are regenerated with the updated fields under todo 9e.
`use_status: approved` on each row refers to project authorization for
public-source use under the publisher's CC BY 4.0 terms; it is not participant
consent, and no cloned voice is released.

## Independent verification

`scripts/verify_pnb_pilot.py` re-checks a published pilot without importing this
module: archive size and hash against the publisher value you pass, metadata
rows, deterministic selection, embedded FLAC bytes against stored originals,
independent decoding against stored WAVs, transcripts, lineage and policy fields.

```text
python scripts/verify_pnb_pilot.py --source-manifest SOURCE.json --archive-root ARCHIVES --raw-root RAW --manifest-root REPORTS --expected-archive-sha256 PUBLISHER_SHA256 --expected-speakers 6 [--write-verification]
```

It prints aggregates only, exits 1 on any failed check, refuses to run under
`python -O`, and with `--write-verification` writes `verification.json` once
(recording its own hash, dependency versions and transcript hashes) and never
overwrites an existing record. Tests: `tests/pnb/test_verify_pnb_pilot.py`. Original
publisher split is train, final AVA split is unassigned, and `training_eligible`
remains false. No Majhi, acoustic-quality, final split or train/serve parity claim
follows from successful extraction. Run automated checks on every selected source,
then sampled and targeted listening for derived quality and dialect evidence.
