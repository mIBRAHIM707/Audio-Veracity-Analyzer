# Punjabi source preflight

Retrieved 4 October 2026. Bounded research handoff for todo7; no audio, Parquet
shards, transcripts or private row manifests downloaded. No GPU, participant
contact, recording, cloning, commits or pushes. Owned path: this handoff only.
Research performed by the Codex conditioning-review worker; no test-design model
provenance is claimed. Shared source register, state and todo remain coordinator-owned.
Observed checkout HEAD: `ad13029fa283183c23e84d736ac424b777e775c3`; existing
uncommitted work was preserved.

## Pinned source and integrity metadata

Official repository: `facebook/omnilingual-asr-corpus`. Observed main revision:
`8648ba8946377697b427ae952076e49fc0e5e44d`.
The [repository API](https://huggingface.co/api/datasets/facebook/omnilingual-asr-corpus)
reported this revision. The [pinned file-list API](https://huggingface.co/api/datasets/facebook/omnilingual-asr-corpus/tree/8648ba8946377697b427ae952076e49fc0e5e44d/data/pnb_Arab)
reported these four files; all paths are relative to `data/pnb_Arab/`:

| File | Bytes | Publisher LFS SHA-256 |
|---|---:|---|
| `dev-00000-of-00001.parquet` | 150,175,361 | `c23df7625a896dd4a789822f55861f6818926c5a1b85dd06f80b189806381c6e` |
| `test-00000-of-00001.parquet` | 181,442,179 | `a66ab5fdaebf9ad62e8690404bb24a55c0449f768eb7566346838244c9e3aa64` |
| `train-00000-of-00002.parquet` | 464,239,672 | `d8ad5551672394f4f9995ffa3f71f3065f611a121f44ca1170a2780fde4f812d` |
| `train-00001-of-00002.parquet` | 461,380,749 | `5c97043f8ff00e209273e6775690561d982937a68964db2b2a93fcc38ec2e922` |

Total compressed shard bytes: **1,257,237,961** (about 1.257 decimal GB).
These are published expected hashes, not locally verified payload hashes.
Future fetches should use `/resolve/<revision>/<path>`, require exact size and
SHA-256, and retain original shards immutably outside Git.

## Published schema and aggregate evidence

The [official viewer info endpoint](https://datasets-server.huggingface.co/info?dataset=facebook%2Fomnilingual-asr-corpus&config=pnb_Arab)
returned `partial=false` and response header `x-revision` matching the pin.
Its schema is nine strings (`language`, `speaker_id`, `prompt_id`, `prompt`,
`segment_id`, `raw_text`, `iso_639_3`, `glottocode`, `iso_15924`), `duration`
as float64, and `audio` as the Hugging Face Audio feature. The
[pinned source card](https://huggingface.co/datasets/facebook/omnilingual-asr-corpus/blob/8648ba8946377697b427ae952076e49fc0e5e44d/README.md)
describes FLAC audio. The physical Parquet audio struct, native sample format and
actual decoding remain unverified; no shard footer was fetched.

Official statistics endpoints also returned `partial=false` and matching
`x-revision` headers:

| Split / evidence | Rows | Distinct publisher speaker labels | Duration range, seconds | Approximate hours |
|---|---:|---:|---:|---:|
| [train](https://datasets-server.huggingface.co/statistics?dataset=facebook%2Fomnilingual-asr-corpus&config=pnb_Arab&split=train) | 420 | 6 | 48.29752–88.05008 | 7.295 |
| [dev](https://datasets-server.huggingface.co/statistics?dataset=facebook%2Fomnilingual-asr-corpus&config=pnb_Arab&split=dev) | 77 | 1 | 47.11329–65.24808 | 1.221 |
| [test](https://datasets-server.huggingface.co/statistics?dataset=facebook%2Fomnilingual-asr-corpus&config=pnb_Arab&split=test) | 84 | 1 | 51.40898–89.81479 | 1.484 |

Hours are calculated from reported row counts multiplied by rounded mean
durations, not from local decoding: approximately **10.0004 hours / 581 rows**.
The eight speaker labels are disjoint between these publisher splits; this is
metadata evidence, not biometric identity verification. No null speaker labels
were reported. All three splits report `west2386` for `glottocode` and `s01` for
every `segment_id`. Consequently, `segment_id` alone cannot identify recordings.
Use pinned shard path plus row index for immutable lineage, retain prompt and
speaker fields, and test any proposed composite recording key for uniqueness.
Namespace speaker grouping by source and language, not by split, so duplicated
identities cannot be hidden by a split prefix. Version alone must not create a new
identity when an existing source speaker appears in a later revision.

## Provenance, licence and remaining unknowns

The source card describes commissioned spontaneous recordings and transcriptions
for ASR/language identification, and declares **CC-BY-4.0**. Preserve attribution,
licence URL, source revision and transformation history. The card does not expose
individual consent records or an explicit cloning authorization.

The [publisher paper, sections 3.3.3 and 6](https://arxiv.org/html/2511.09690v1)
describes compensated native speakers recruited through local partners and a
collection target of ten speakers per language, with actual shortfalls. It
documents prompted natural speech, avoidance of personal information, and staged
quality review. These are corpus-level process statements, not proof of the
identity, consent scope or quality of each Punjabi row. Its language table calls
`pnb_Arab` Western Panjabi. Neither that label nor `west2386` establishes Majhi
coverage, regional balance, nationality, or the identity of speakers across
corpora. No separate dialect, region, consent or verified identity column appears
in the reported schema. Sampled native-speaker assessment remains useful for the
target variety; no such listening was performed here.

User authorization permits noncommercial public-dataset intake. This handoff does
not amend the register or resolve PRD Q1's separate derivative-synthesis question.
No public-source licence is treated as cloning consent. FR-D1/FR-E5 source
provenance can advance; FR-D8 speaker/lineage separation and FR-D10 dialect/consent
statistics still require explicit downstream evidence. No participant recording
is authorized by this work.

## Smallest sensible next pilot

1. For a decoder/importer smoke only, fetch the single 150.2 MB dev shard under
   the pinned revision, verify its full publisher hash, and extract at most five
   recordings deterministically. State clearly that this represents one publisher
   speaker and is not a balanced Punjabi corpus. Keep its publisher dev designation
   and exclude these exploratory clips from claims of untouched final evaluation.
2. For the useful multi-speaker pilot, start with one train shard (about 464 MB),
   inspect metadata locally, and select up to five source recordings per available
   publisher speaker with a deterministic seed and a 30-recording total cap. Do
   not promise all six train speakers from one shard: per-shard distribution is
   unknown. Fetch the second train shard only if needed for the declared coverage;
   the maximum two-shard transfer is 925,620,421 bytes. Preserve dev/test for later
   evaluation if choosing this route directly.
3. Preserve source FLAC bytes and parent shard hash/row lineage outside Git;
   record locally extracted-byte hashes separately from publisher shard hashes.
   Validate duration, channels, sample rate, decoding and signal quality on every
   selected source. Decode copies on CPU before the current PCM16 WAV preparation
   path; do not silently assume FLAC bit depth. Native recordings are roughly one
   minute long, so the existing 3–6 second preparation should retain uncovered
   ranges and source/speaker lineage. Keep the full-source transcript as source
   metadata; do not claim it is aligned to newly cut clips.
4. Retain publisher language/provenance; use sampled and targeted listening for
   dialect/quality assessment without an individual-file intake gate. Record
   unknown cloning consent and leave final AVA split assignment unresolved.

Range reads or streaming could transfer less data, but cannot verify a complete
publisher shard SHA-256 without reading the entire shard. Do not describe a
partial read's locally computed hash as publisher verification.

## Validation and limitations

Only official Meta/Hugging Face metadata and the publisher paper were inspected.
Viewer responses were checked against the repository revision via `x-revision`;
row counts and statistics are reported publisher-hosted evidence, not independently
recomputed. No audio quality, dialect or identity claim was experimentally tested.
No implementation or substantive test cases were authored in this handoff.
`uv run --no-project --python 3.11 python scripts/check_foundation.py` passed:
16 adapters, 29 mirrored skills / 214 files, and eight shared roles checked.
