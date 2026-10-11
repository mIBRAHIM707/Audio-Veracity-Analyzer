# Offline listening review

This is an optional aid for sampled and targeted QA. Established CSALT originals
do not require individual listening approval before routine ingestion. An
unreviewed entry describes missing per-clip listening evidence, not rejection.

Generate a review page beside an existing preparation report:

```text
uv run --no-project --python 3.11 python data/review/build_review.py --report D:/AVA-Data/processed/csalt-pilot-v2/report.json
```

Open the resulting `review.html` in a browser on the same machine. No server,
internet connection or upload is needed. Keep the report, HTML and clips directory
together. The builder verifies candidate hashes before creating the page and
refuses to overwrite an existing page. It does not approve the data for training.
Pilot pages are limited to 200 candidates and 5,000 characters per listening note
so a complete export remains within the import size limit. Larger datasets need
separate review batches; this is not yet a multi-reviewer database.

## Review procedure

1. Enter a reviewer identifier, not participant identity.
2. Play the clip. Check intelligibility, actual language, background interference,
   competing speakers, audible distortion, and chopped words at both ends.
3. Choose **keep**, **reject**, or **needs recut**, and write a short reason when
   needed. Keep means this reviewer considers it usable for the current audio
   quality check; rights, split assignment and training acceptance are separate.
4. Export decisions before closing. They are not automatically saved to disk.
5. To resume, reopen the page and import the exported JSON. The complete review
   inventory and hashes must match this exact preparation report.

Start with flagged candidates, then review the rest of this small pilot. A signal
flag is a prompt to inspect, not an automatic rejection. All clips start
**unreviewed**, and the assistant has not performed listening QA.

The human export is a separate artifact; it does not mutate the preparation
report or source WAV files. Keep the export with the data outside Git. If browser
downloads go elsewhere, move it into approved local storage. The page remembers
edits only while open; export regularly. No localStorage or remote persistence.

The export documents a reviewer's entries, not proof of listening completion or
a tamper-proof certificate. Do not submit an empty/unreviewed export as completed
QA. If a clip changes after page generation, regenerate a new preparation run and
review page; the browser itself does not rehash audio on each playback.
