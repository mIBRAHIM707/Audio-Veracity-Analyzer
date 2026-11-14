"""Opt-in alert snippet retention (backlog 5.3 partial; PRD FR-O5, §6.3; architecture
§2.4 "if retention is on, persist the 30 s ring buffer ending at the alert as one
snippet per alert_id", "Audio minimization").

FR-O5 rules implemented: default off (no raw audio stored); when on, a 30 s ring
buffer per direction, one snippet per alert ID persisted at `alert.raised`, TTL
cleanup (default 7 days), toggling applies to calls started after the change,
switching off purges existing snippets within one cleanup run, and write failures
never block the alert (every store and feed error is contained and counted).

Encryption at rest (AES-256-GCM) is delegated to a sealer. Only an exact
`AesGcmSealer` instance is accepted: AES-256-GCM from the pinned `cryptography`
package (requirements-retention.txt; owner decision 14 Nov 2026 allows installing it),
a 32-byte key loaded from a key file outside the repository, and a random 96-bit nonce
per snippet. Key provisioning and rotation remain an operator decision. Store-logic
tests may pass `_insecure_test_sealer=True` with a deliberately insecure sealer.
`open_snippet` reads one snippet back and authenticates it.

Storage layout (choices made here): one file per alert under the retention root,
named by SHA-256 of the alert ID (no input in file names). The file is written under
a unique `snip-*.tmp` name, flushed and fsynced, then hard-linked to its final name,
which fails if the name exists, so concurrent writers can never produce two snippets
for one alert. A small plaintext header holds only the format version, creation time
and sealer algorithm, so TTL cleanup never needs the key; the header and magic are
authenticated as associated data together with the alert-ID hash, so a changed TTL
timestamp is detectable when opening. The root must be outside the repository.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import struct
import tempfile
import threading
import time

import numpy as np

AES_GCM = 'AES-256-GCM'
RING_SECONDS = 30.0
DEFAULT_TTL_S = 7 * 24 * 3600.0
TEMP_GRACE_S = 60.0  # a temp file younger than this may belong to a write in progress
CLOCK_SKEW_S = 300.0  # a creation time further in the future is treated as invalid
MAGIC = b'AVASNIP1'
SUFFIX = '.snip'
TEMP_PREFIX = 'snip-'
DIRECTIONS = ('caller', 'agent')
REPO_ROOT = Path(__file__).resolve().parents[2]


KEY_BYTES = 32   # AES-256
NONCE_BYTES = 12  # 96-bit random nonce per snippet, stored in front of the ciphertext
TAG_BYTES = 16


class AesGcmSealer:
    """The only sealer accepted in production: AES-256-GCM from the pinned
    `cryptography` package (requirements-retention.txt). Sealed form: 12-byte random
    nonce + ciphertext + 16-byte tag. The key is 32 bytes, held only in memory; see
    `from_key_file`. Without the package the constructor raises RuntimeError, so the
    store cannot be built and retention stays off."""

    algorithm = AES_GCM

    def __init__(self, key):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as error:
            raise RuntimeError('AES-256-GCM needs the pinned cryptography package '
                               '(requirements-retention.txt)') from error
        if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_BYTES:
            raise ValueError(f'the AES-256-GCM key must be exactly {KEY_BYTES} bytes')
        self._aead = AESGCM(bytes(key))

    @classmethod
    def from_key_file(cls, path):
        """Load a raw 32-byte key from a regular file outside the repository (key
        provisioning and rotation are an operator decision, not made here)."""
        path = Path(path).absolute()
        if path.is_symlink():  # checked before resolving, which would follow the link
            raise ValueError('the retention key must be a regular file, not a symlink')
        path = path.resolve()
        if path == REPO_ROOT or REPO_ROOT in path.parents:
            raise ValueError('the retention key file must be outside the repository')
        if not path.is_file():
            raise ValueError('the retention key must be a regular file')
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) \
            | getattr(os, 'O_NONBLOCK', 0)
        with os.fdopen(os.open(path, flags), 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):  # swapped after the check above
                raise ValueError('the retention key must be a regular file')
            if os.name == 'posix' and info.st_mode & 0o077:
                raise ValueError('the retention key file must not be readable by group or others')
            key = handle.read(KEY_BYTES + 1)
        if len(key) != KEY_BYTES:
            raise ValueError(f'the retention key file must hold exactly {KEY_BYTES} raw bytes')
        return cls(key)

    def seal(self, plaintext, associated):
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, bytes(plaintext), bytes(associated))

    def open(self, sealed, associated):
        """Plaintext, or ValueError if the data, header or alert binding was changed."""
        from cryptography.exceptions import InvalidTag
        if len(sealed) < NONCE_BYTES + TAG_BYTES:
            raise ValueError('sealed snippet is too short')
        try:
            return self._aead.decrypt(sealed[:NONCE_BYTES], sealed[NONCE_BYTES:], bytes(associated))
        except InvalidTag:
            raise ValueError('snippet failed authentication') from None


class RetentionPolicy:
    """Immutable switch read at call start (FR-O5: toggling applies to later calls)."""

    def __init__(self, enabled=False, *, ring_seconds=RING_SECONDS, ttl_s=DEFAULT_TTL_S):
        if not isinstance(enabled, bool):
            raise ValueError('enabled must be a boolean')
        for name, value in (('ring_seconds', ring_seconds), ('ttl_s', ttl_s)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                    or value <= 0:
                raise ValueError(f'{name} must be a positive finite number')
        self.enabled, self.ring_seconds, self.ttl_s = enabled, float(ring_seconds), float(ttl_s)


def _check_pcm(pcm):
    if not isinstance(pcm, np.ndarray) or pcm.dtype != np.int16 or pcm.ndim != 1:
        raise ValueError('pcm must be a 1-D int16 numpy array')
    return pcm


def _check_rate(sample_rate):
    if type(sample_rate) is not int or not 8000 <= sample_rate <= 48000:
        raise ValueError('sample_rate must be an integer from 8000 to 48000')
    return sample_rate


class RingBuffer:
    """The last `seconds` of PCM16 audio for one direction, preallocated; append and
    snapshot are serialized by a lock (media and alert threads may differ)."""

    def __init__(self, sample_rate, seconds=RING_SECONDS):
        self.sample_rate = _check_rate(sample_rate)
        size = int(round(seconds * sample_rate))
        if size < 1:
            raise ValueError('the ring must hold at least one sample')
        self._data = np.zeros(size, dtype=np.int16)
        self._end = 0      # next write position
        self._filled = 0
        self._lock = threading.Lock()

    def append(self, pcm):
        pcm = _check_pcm(pcm)
        size = len(self._data)
        with self._lock:
            if len(pcm) >= size:
                self._data[:] = pcm[len(pcm) - size:]
                self._end, self._filled = 0, size
                return
            first = min(len(pcm), size - self._end)
            self._data[self._end:self._end + first] = pcm[:first]
            self._data[:len(pcm) - first] = pcm[first:]
            self._end = (self._end + len(pcm)) % size
            self._filled = min(size, self._filled + len(pcm))

    def snapshot(self):
        """A copy of the buffered audio, oldest first."""
        with self._lock:
            if self._filled < len(self._data):
                return self._data[:self._filled].copy()
            return np.concatenate([self._data[self._end:], self._data[:self._end]])


def _alert_key(alert_id):
    return hashlib.sha256(alert_id.encode('utf-8')).hexdigest()


def associated_data(alert_key, header):
    """What the sealer authenticates: magic, the plaintext header and the alert hash."""
    return MAGIC + struct.pack('>I', len(header)) + header + alert_key.encode('ascii')


class SnippetStore:
    """Sealed snippet files with TTL and purge. Methods never raise for I/O or sealer
    faults; they return False/counts and record `failures`."""

    def __init__(self, root, sealer, *, clock, _insecure_test_sealer=False):
        root = Path(root).resolve()
        if root == REPO_ROOT or REPO_ROOT in root.parents:
            raise ValueError('the retention root must be outside the repository')
        if type(sealer) is not AesGcmSealer and not _insecure_test_sealer:  # no subclasses
            raise ValueError(f'snippets need an {AES_GCM} sealer (AesGcmSealer); none is installed (FR-O5)')
        self.root, self._sealer, self._clock = root, sealer, clock
        self.algorithm = str(getattr(sealer, 'algorithm', 'unknown'))
        self._lock = threading.Lock()
        self.failures = {}
        self.stored = 0

    def _fail(self, name):
        with self._lock:
            self.failures[name] = self.failures.get(name, 0) + 1

    def put(self, alert_id, *, call_id, direction, pcm, sample_rate):
        """Persist one snippet for `alert_id`; False if one already exists or on any
        failure (counted). Never raises."""
        tmp = None
        try:
            if not isinstance(alert_id, str) or not alert_id:
                raise ValueError('alert_id must be a nonempty string')
            if direction not in DIRECTIONS:
                raise ValueError(f'direction must be one of {DIRECTIONS}')
            _check_pcm(pcm)
            _check_rate(sample_rate)
            key = _alert_key(alert_id)
            path = self.root / (key + SUFFIX)
            if path.exists():
                self._fail('duplicate_alert')
                return False
            header = json.dumps({'v': 1, 'created': float(self._clock()), 'algorithm': self.algorithm}).encode()
            meta = json.dumps({'call_id': call_id, 'direction': direction, 'sample_rate': sample_rate,
                               'samples': int(len(pcm))}).encode('utf-8')
            plaintext = struct.pack('>I', len(meta)) + meta + pcm.astype('<i2').tobytes()
            sealed = self._sealer.seal(plaintext, associated_data(key, header))
            self.root.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.root, prefix=TEMP_PREFIX, suffix='.tmp')
            with os.fdopen(fd, 'wb') as handle:
                handle.write(MAGIC + struct.pack('>I', len(header)) + header + sealed)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp, path)  # exclusive: fails if another writer got there first
            except FileExistsError:
                self._fail('duplicate_alert')
                return False
            with self._lock:
                self.stored += 1
            return True
        except Exception as error:
            self._fail(type(error).__name__)
            return False
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    self._fail('temp_unlink')

    def _created(self, path, now):
        """The header's creation time; ValueError for anything that is not a finite
        time no later than now + CLOCK_SKEW_S (such files are treated as unreadable and
        deleted, so a NaN, infinite or future timestamp cannot outlive the TTL)."""
        with open(path, 'rb') as handle:
            if handle.read(len(MAGIC)) != MAGIC:
                raise ValueError('not a snippet file')
            (size,) = struct.unpack('>I', handle.read(4))
            if size > 4096:
                raise ValueError('header too large')
            header = json.loads(handle.read(size))
        created = header.get('created') if isinstance(header, dict) else None
        if isinstance(created, bool) or not isinstance(created, (int, float)) \
                or not math.isfinite(created) or created > now + CLOCK_SKEW_S:
            raise ValueError('snippet creation time is not a finite past time')
        return float(created)

    def cleanup(self, policy):
        """One cleanup run: with retention off, delete every snippet (FR-O5 purge);
        with it on, delete those older than the TTL. Returns the number of snippets
        deleted. Unreadable snippet files are deleted (fail closed). Temp files older
        than TEMP_GRACE_S (crash leftovers, or OS delete-pending copies) are removed;
        younger ones may belong to a write in progress and are left."""
        if not self.root.exists():
            return 0
        now = float(self._clock())
        deleted = 0
        for path in list(self.root.iterdir()):
            try:
                suffix = path.suffix.lower()
                if suffix == '.tmp':
                    if time.time() - path.stat().st_mtime >= TEMP_GRACE_S:
                        path.unlink()
                    continue
                if suffix != SUFFIX:
                    continue
                expired = not policy.enabled
                if not expired:
                    try:
                        expired = now - self._created(path, now) >= policy.ttl_s
                    except (ValueError, KeyError, TypeError, RecursionError, OSError, struct.error):
                        expired = True  # fail closed: unreadable snippets are deleted
                if expired:
                    path.unlink()
                    deleted += 1
            except OSError:
                self._fail('cleanup_oserror')
            except Exception as error:  # cleanup never raises; one bad entry does not stop the run
                self._fail(f'cleanup_{type(error).__name__}')
        return deleted


MAX_SNIPPET_BYTES = 8_000_000  # 30 s at 48 kHz is 2.9 MB of PCM; generous bound


def open_snippet(root, alert_id, sealer):
    """Read and authenticate the snippet for `alert_id` (for FR-U5 playback/export).
    Returns {'created', 'algorithm', 'call_id', 'direction', 'sample_rate', 'pcm'}.
    Raises FileNotFoundError when there is none and ValueError when the file is not a
    snippet, was changed (audio, metadata, header/TTL timestamp) or belongs to another
    alert ID (a renamed file fails because the alert hash is authenticated)."""
    if not isinstance(alert_id, str) or not alert_id:
        raise ValueError('alert_id must be a nonempty string')
    key = _alert_key(alert_id)
    path = Path(root).resolve() / (key + SUFFIX)
    with open(path, 'rb') as handle:
        data = handle.read(MAX_SNIPPET_BYTES + 1)
    if len(data) > MAX_SNIPPET_BYTES:
        raise ValueError('snippet file too large')
    if data[:len(MAGIC)] != MAGIC or len(data) < len(MAGIC) + 4:
        raise ValueError('not a snippet file')
    (size,) = struct.unpack('>I', data[len(MAGIC):len(MAGIC) + 4])
    start = len(MAGIC) + 4
    if size > 4096 or start + size > len(data):
        raise ValueError('bad snippet header')
    header = data[start:start + size]
    plaintext = sealer.open(data[start + size:], associated_data(key, header))
    try:  # authenticated, but parse defensively: every failure is a ValueError
        info = json.loads(header)
        if info['v'] != 1 or info['algorithm'] != getattr(sealer, 'algorithm', None):
            raise ValueError('unsupported snippet version or algorithm')
        if len(plaintext) < 4:
            raise ValueError('sealed payload too short')
        (meta_size,) = struct.unpack('>I', plaintext[:4])
        if 4 + meta_size > len(plaintext) or (len(plaintext) - 4 - meta_size) % 2:
            raise ValueError('sealed payload layout is invalid')
        meta = json.loads(plaintext[4:4 + meta_size])
        pcm = np.frombuffer(plaintext[4 + meta_size:], dtype='<i2').astype(np.int16)
        if type(meta['samples']) is not int or len(pcm) != meta['samples']:
            raise ValueError('snippet sample count does not match its metadata')
        return {'created': float(info['created']), 'algorithm': info['algorithm'],
                'call_id': meta['call_id'], 'direction': meta['direction'],
                'sample_rate': meta['sample_rate'], 'pcm': pcm}
    except (KeyError, TypeError, struct.error, RecursionError, UnicodeDecodeError) as error:
        raise ValueError(f'snippet contents are malformed: {type(error).__name__}') from None


class CallRetention:
    """Ring buffers for one call under the policy current at its start. With the
    policy off, audio is not buffered at all. No method raises into the call path."""

    def __init__(self, call_id, policy, store, *, sample_rate, directions=DIRECTIONS):
        self.call_id, self.enabled, self._store = call_id, policy.enabled, store
        self.sample_rate = _check_rate(sample_rate)
        if not set(directions) <= set(DIRECTIONS):
            raise ValueError(f'directions must come from {DIRECTIONS}')
        self._buffers = ({d: RingBuffer(sample_rate, policy.ring_seconds) for d in directions}
                         if policy.enabled else {})
        self.feed_errors = 0

    def feed(self, direction, pcm):
        """Buffer audio for a scored direction; bad input is counted, never raised."""
        if not self.enabled:
            return False
        try:
            self._buffers[direction].append(pcm)
            return True
        except Exception:
            self.feed_errors += 1
            return False

    def on_alert_raised(self, alert_id, direction):
        """Persist the ring ending now; True if stored. Never raises (the alert must
        not wait on or fail because of retention)."""
        if not self.enabled:
            return False
        try:
            pcm = self._buffers[direction].snapshot()
        except Exception:
            return False
        return self._store.put(alert_id, call_id=self.call_id, direction=direction, pcm=pcm,
                               sample_rate=self.sample_rate)

    def close(self):
        self._buffers.clear()  # nothing outlives the call except stored snippets
        self.enabled = False
