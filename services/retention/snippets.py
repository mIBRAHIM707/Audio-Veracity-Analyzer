"""Opt-in alert snippet retention (backlog 5.3 partial; PRD FR-O5, §6.3; architecture
§2.4 "if retention is on, persist the 30 s ring buffer ending at the alert as one
snippet per alert_id", "Audio minimization").

FR-O5 rules implemented: default off (no raw audio stored); when on, a 30 s ring
buffer per direction, one snippet per alert ID persisted at `alert.raised`, TTL
cleanup (default 7 days), toggling applies to calls started after the change,
switching off purges existing snippets within one cleanup run, and write failures
never block the alert (every store and feed error is contained and counted).

Encryption at rest (AES-256-GCM) is delegated to a sealer. Only an `AesGcmSealer`
instance is accepted, and that class cannot be constructed yet: no AES-GCM library is
installed (`cryptography` is not in the offline cache), so retention cannot be turned
on in production until that dependency decision is made (blocker in the daily
report). Tests pass `_insecure_test_sealer=True` with a deliberately insecure sealer.

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
import struct
import tempfile
import threading
import time

import numpy as np

AES_GCM = 'AES-256-GCM'
RING_SECONDS = 30.0
DEFAULT_TTL_S = 7 * 24 * 3600.0
TEMP_GRACE_S = 60.0  # a temp file younger than this may belong to a write in progress
MAGIC = b'AVASNIP1'
SUFFIX = '.snip'
TEMP_PREFIX = 'snip-'
DIRECTIONS = ('caller', 'agent')
REPO_ROOT = Path(__file__).resolve().parents[2]


class AesGcmSealer:
    """The only sealer accepted in production. Not available: no AES-GCM library is
    installed; this class exists so the store's check names a concrete type."""

    algorithm = AES_GCM

    def __init__(self, *args, **kwargs):
        raise RuntimeError('AES-256-GCM is not available: a crypto dependency must be provisioned (FR-O5)')


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

    def _created(self, path):
        with open(path, 'rb') as handle:
            if handle.read(len(MAGIC)) != MAGIC:
                raise ValueError('not a snippet file')
            (size,) = struct.unpack('>I', handle.read(4))
            if size > 4096:
                raise ValueError('header too large')
            return float(json.loads(handle.read(size))['created'])

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
                        expired = now - self._created(path) >= policy.ttl_s
                    except (ValueError, KeyError, OSError, struct.error):
                        expired = True
                if expired:
                    path.unlink()
                    deleted += 1
            except OSError:
                self._fail('cleanup_oserror')
        return deleted


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
