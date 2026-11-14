# Alert snippet retention (backlog 5.3 partial; PRD FR-O5)

`snippets.py` implements opt-in retention of the audio around an alert. Default is
off: no raw audio is buffered or stored.

- `RetentionPolicy(enabled, ring_seconds=30, ttl_s=7 days)`: read when a call starts,
  so a toggle applies to calls started afterwards (FR-O5).
- `CallRetention(call_id, policy, store, sample_rate=...)`: with the policy on, a
  preallocated 30 s int16 `RingBuffer` per direction; `on_alert_raised(alert_id,
  direction)` persists the ring ending at the alert, never raises (write failures
  must not block the alert). With the policy off nothing is buffered.
- `SnippetStore(root, sealer, clock=...)`: one file per alert ID, named by SHA-256 of
  the ID, written to a unique temp file, fsynced and hard-linked to its final name
  (exclusive, so concurrent writers store exactly one snippet); a plaintext header
  holds only the format version, creation time and sealer algorithm; call ID,
  direction, sample rate and audio are sealed, with magic + header + alert-ID hash as
  associated data (a changed TTL timestamp fails authentication). `cleanup(policy)`
  deletes snippets older than the TTL, or every snippet when retention is off (purge
  within one run), deletes unreadable snippet files (fail closed) and temp files
  older than 60 s. The root must be outside the repository. Feed and store errors are
  counted, never raised into the call path.

**Blocker:** AES-256-GCM at rest needs a crypto library (`cryptography` is not in the
offline cache and adding it is a dependency decision). Only an `AesGcmSealer` instance
is accepted and that class cannot be constructed until then, so retention cannot be
enabled in production; tests use an explicitly insecure XOR sealer through the
private `_insecure_test_sealer` keyword. Key management is also undecided.

Known limits: the root needs a file system with hard links (NTFS/ext4; on FAT or
some network mounts every write fails and is counted); TTL cleanup reads the
unauthenticated header, so a tampered timestamp is only detected when a snippet is
opened; if a temp file cannot be unlinked (a scanner holding it) a sealed copy can
outlive a purge until the next cleanup run (60 s grace); feeding an unscored
direction is counted as a feed error.

Spec questions: architecture §2.7 mentions a "30 s WAV" while this writes a sealed
container (FR-U5 playback would decrypt to WAV); whether a call started before a
switch-off may still write after the purge until its end; whether the snippet should
end at the alert window rather than the latest audio fed; a disk quota.

Not done: wiring to the live tap audio and to `alert.raised` events, the
retention setting in configuration, scheduled cleanup, and secure deletion (deleted
files are unlinked, not overwritten; on Windows a file being scanned can briefly
appear as `<NAME>.tmp` during deletion, removed by the next cleanup).

```text
uv run --no-project --python 3.11 --with-requirements requirements-api.txt python -m unittest tests.unit.test_snippet_retention -v
```
