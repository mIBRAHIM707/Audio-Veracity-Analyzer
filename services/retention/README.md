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

**Encryption (14 Nov 2026).** `AesGcmSealer(key)` is AES-256-GCM from the pinned
`cryptography==50.0.2` (`requirements-retention.txt`; Apache-2.0 OR BSD-3-Clause,
installed under the owner's 14 Nov install decision): a random 96-bit nonce per
snippet stored before the ciphertext and 16-byte tag. `AesGcmSealer.from_key_file(path)`
loads exactly 32 raw bytes from a regular file outside the repository. Only an exact
`AesGcmSealer` is accepted by the store (no subclasses, no declared imitation);
without the package the constructor raises, so retention cannot start.
`open_snippet(root, alert_id, sealer)` reads a snippet back: any change to audio,
metadata, header/TTL timestamp or nonce, a wrong key, or a file copied under another
alert's name fails authentication. Store-logic tests still use an explicitly insecure
XOR sealer through the private `_insecure_test_sealer` keyword.

Open: key provisioning, storage protection and rotation (an operator decision; a
rotated key makes older snippets unreadable, and unreadable snippets are only purged
by TTL or switch-off). On POSIX the key file must not be group/world readable; on
Windows file ACLs are not checked. File names are a plain SHA-256 of the alert ID, so
if alert IDs were guessable a directory listing would reveal which alerts have
snippets (today's IDs are random; a keyed name is the fix if that changes). The pins
in `requirements-retention.txt` are exact versions without hashes.
TTL cleanup treats a header whose creation time is not a finite number at most 300 s
in the future as unreadable and deletes it; it never raises.

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
uv run --no-project --python 3.11 --with-requirements requirements-api.txt --with-requirements requirements-retention.txt python -m unittest tests.unit.test_snippet_retention tests.unit.test_snippet_aes_gcm -v
```
