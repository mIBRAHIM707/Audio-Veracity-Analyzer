"""FR-O5 opt-in alert snippet retention (backlog 5.3 partial).

Claude-authored test design, 14 November 2026. Synthetic int16 ramps only, written
to temporary directories outside the repository. The sealer here is a deliberately
insecure test stand-in (XOR); production needs a real AES-256-GCM sealer, which is
not installed, and the store refuses to run without one unless told it is a test.
"""
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest

import numpy as np

from services.retention.snippets import (AES_GCM, MAGIC, AesGcmSealer, CallRetention, RetentionPolicy,
                                         RingBuffer, SnippetStore, associated_data)


def leftover_tmp_is_os_artifact(path, deleted_keys):
    """Windows can briefly show a file being deleted as '<NAME>.tmp' while a scanner
    holds it; accept exactly that, never any other temp file."""
    return path.name.upper() in {(key + '.snip.tmp').upper() for key in deleted_keys}


class XorSealer:
    algorithm = 'TEST-XOR-NOT-SECURE'

    def __init__(self, fail=False):
        self.fail, self.aad = fail, []

    def seal(self, plaintext, aad):
        if self.fail:
            raise RuntimeError('key unavailable')
        self.aad.append(aad)
        return bytes(b ^ 0x5A for b in plaintext)

    @staticmethod
    def open(sealed):
        return bytes(b ^ 0x5A for b in sealed)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def ramp(n, start=0):
    return (np.arange(start, start + n) % 30000).astype(np.int16)


class TempRoot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / 'snippets'
        self.clock = Clock()

    def store(self, sealer=None):
        return SnippetStore(self.root, sealer or XorSealer(), clock=self.clock, _insecure_test_sealer=True)

    def files(self):
        return sorted(p.name for p in self.root.iterdir()) if self.root.exists() else []


class RingBufferTests(unittest.TestCase):
    def test_keeps_the_last_seconds_oldest_first_across_wraps(self):
        ring = RingBuffer(8000, seconds=1.0)  # 8000 samples
        stream = []
        for size in (3000, 3000, 3000, 500, 7999):
            chunk = ramp(size, start=sum(len(c) for c in stream))
            stream.append(chunk)
            ring.append(chunk)
            expected = np.concatenate(stream)[-8000:]
            np.testing.assert_array_equal(ring.snapshot(), expected)
        ring.append(ramp(20000, start=5))  # larger than the ring
        np.testing.assert_array_equal(ring.snapshot(), ramp(20000, start=5)[-8000:])

    def test_snapshot_is_a_copy_and_input_is_checked(self):
        ring = RingBuffer(8000, seconds=0.5)
        ring.append(ramp(100))
        snap = ring.snapshot()
        snap[:] = 7
        np.testing.assert_array_equal(ring.snapshot(), ramp(100))
        for bad in (np.zeros(10, np.float32), np.zeros((2, 2), np.int16)):
            with self.assertRaises(ValueError):
                ring.append(bad)
        for rate in (7999, 8000.0, 96000):
            with self.assertRaises(ValueError):
                RingBuffer(rate)
        with self.assertRaises(ValueError):
            RingBuffer(8000, seconds=1e-6)  # would hold zero samples


class StoreTests(TempRoot):
    def test_refuses_without_the_aes_gcm_sealer_class_or_inside_the_repository(self):
        with self.assertRaisesRegex(ValueError, AES_GCM):
            SnippetStore(self.root, XorSealer(), clock=self.clock)

        class DeclaredAes(XorSealer):
            algorithm = AES_GCM
        with self.assertRaisesRegex(ValueError, AES_GCM):
            SnippetStore(self.root, DeclaredAes(), clock=self.clock)  # a declaration is not enough
        # 14 Nov: AesGcmSealer is now real (test_snippet_aes_gcm.py); a key is required.
        try:
            import cryptography  # noqa: F401
            expected = ValueError  # wrong key size
        except ImportError:
            expected = RuntimeError  # package missing: retention cannot start
        with self.assertRaises(expected):
            AesGcmSealer(b'short')

        class Imposter(AesGcmSealer):
            def __init__(self):
                pass

            def seal(self, plaintext, aad):
                return plaintext
        with self.assertRaisesRegex(ValueError, AES_GCM):
            SnippetStore(self.root, Imposter(), clock=self.clock)  # subclasses are not trusted
        repo = Path(__file__).resolve().parents[2]
        with self.assertRaisesRegex(ValueError, 'outside the repository'):
            SnippetStore(repo / 'tmp-snippets', XorSealer(), clock=self.clock, _insecure_test_sealer=True)

    def test_concurrent_writers_of_one_alert_store_exactly_one_snippet(self):
        store = self.store()
        results, go = [], threading.Event()

        def write(value):
            go.wait()
            results.append(store.put('same', call_id='c', direction='caller', pcm=ramp(1000, value),
                                     sample_rate=8000))

        threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        go.set()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [False] * 7 + [True])
        self.assertEqual((store.stored, store.failures), (1, {'duplicate_alert': 7}))
        self.assertEqual(self.files(), [hashlib.sha256(b'same').hexdigest() + '.snip'])

    def test_one_sealed_file_per_alert_named_by_hash_with_a_minimal_plaintext_header(self):
        sealer = XorSealer()
        store = self.store(sealer)
        pcm = ramp(240)
        self.assertTrue(store.put('alert-1', call_id='call-secret', direction='caller', pcm=pcm, sample_rate=8000))
        self.assertFalse(store.put('alert-1', call_id='call-secret', direction='caller', pcm=pcm, sample_rate=8000))
        [path] = list(self.root.iterdir())
        key = hashlib.sha256(b'alert-1').hexdigest()
        self.assertEqual(path.name, key + '.snip')
        raw = path.read_bytes()
        self.assertNotIn(b'call-secret', raw)
        self.assertNotIn(b'alert-1', raw)
        self.assertEqual(raw[:8], MAGIC)
        (size,) = struct.unpack('>I', raw[8:12])
        header = raw[12:12 + size]
        self.assertEqual(json.loads(header), {'v': 1, 'created': 1000.0, 'algorithm': sealer.algorithm})
        plaintext = XorSealer.open(raw[12 + size:])
        (meta_size,) = struct.unpack('>I', plaintext[:4])
        meta = json.loads(plaintext[4:4 + meta_size])
        self.assertEqual(meta, {'call_id': 'call-secret', 'direction': 'caller', 'sample_rate': 8000, 'samples': 240})
        np.testing.assert_array_equal(np.frombuffer(plaintext[4 + meta_size:], '<i2'), pcm)
        # The TTL header is authenticated: it is part of the associated data.
        self.assertEqual(sealer.aad, [associated_data(key, header)])
        self.assertIn(header, sealer.aad[0])
        self.assertEqual((store.stored, store.failures), (1, {'duplicate_alert': 1}))

    def test_failures_are_contained_and_leave_no_partial_files(self):
        store = self.store(XorSealer(fail=True))
        self.assertFalse(store.put('a', call_id='c', direction='caller', pcm=ramp(10), sample_rate=8000))
        self.assertEqual(store.failures, {'RuntimeError': 1})
        self.assertEqual(self.files(), [])
        good = self.store()
        for kwargs in (dict(direction='nobody', pcm=ramp(10), sample_rate=8000),
                       dict(direction='caller', pcm=np.zeros(10, np.float32), sample_rate=8000),
                       dict(direction='caller', pcm=ramp(10), sample_rate=44100.0)):
            self.assertFalse(good.put('v', call_id='c', **kwargs))
        self.assertEqual((good.failures, self.files()), ({'ValueError': 3}, []))
        self.root.parent.mkdir(parents=True, exist_ok=True)
        self.root.write_bytes(b'not a directory')  # the root is unusable
        broken = self.store()
        self.assertFalse(broken.put('b', call_id='c', direction='caller', pcm=ramp(10), sample_rate=8000))
        self.assertEqual(sum(broken.failures.values()), 1)
        self.assertFalse(self.store().put('', call_id='c', direction='caller', pcm=ramp(10), sample_rate=8000))

    def test_cleanup_applies_the_ttl_purges_when_off_and_fails_closed(self):
        store = self.store()
        on = RetentionPolicy(True, ttl_s=100.0)
        store.put('old', call_id='c', direction='caller', pcm=ramp(10), sample_rate=8000)
        self.clock.t = 1050.0
        store.put('new', call_id='c', direction='caller', pcm=ramp(10), sample_rate=8000)
        (self.root / 'corrupt.snip').write_bytes(b'garbage')
        (self.root / 'UPPER.SNIP').write_bytes(b'garbage too')
        crash = self.root / 'snip-crash.tmp'
        crash.write_bytes(b'partial')
        old = time.time() - 3600
        os.utime(crash, (old, old))
        (self.root / 'snip-writing.tmp').write_bytes(b'in progress')  # young: a live write
        (self.root / 'notes.txt').write_text('keep')
        self.clock.t = 1100.0
        self.assertEqual(store.cleanup(on), 3)  # 'old' (100 s) and both unreadable snippets
        old_key, new_key = (hashlib.sha256(name).hexdigest() for name in (b'old', b'new'))
        remaining = [p for p in self.root.iterdir()
                     if not leftover_tmp_is_os_artifact(p, [old_key, 'corrupt', 'UPPER'])]
        self.assertEqual(sorted(p.name for p in remaining),
                         sorted([new_key + '.snip', 'notes.txt', 'snip-writing.tmp']))
        self.assertEqual(store.cleanup(RetentionPolicy(False)), 1, 'switching off purges in one run')
        remaining = [p.name for p in self.root.iterdir()
                     if not leftover_tmp_is_os_artifact(p, [old_key, new_key, 'corrupt', 'UPPER'])]
        self.assertEqual(sorted(remaining), ['notes.txt', 'snip-writing.tmp'])


class CallRetentionTests(TempRoot):
    def test_default_off_buffers_nothing_and_stores_nothing(self):
        call = CallRetention('c', RetentionPolicy(), self.store(), sample_rate=8000)
        call.feed('caller', ramp(8000))
        self.assertFalse(call.on_alert_raised('a1', 'caller'))
        self.assertFalse(self.root.exists())

    def test_on_alert_stores_the_last_30_seconds_ending_at_the_alert(self):
        store = self.store()
        call = CallRetention('c', RetentionPolicy(True), store, sample_rate=8000)
        fed = ramp(8000 * 45)
        for chunk in np.array_split(fed, 45):
            call.feed('caller', chunk)
        call.feed('agent', ramp(80))
        self.assertTrue(call.on_alert_raised('a1', 'caller'))
        self.assertTrue(call.on_alert_raised('a2', 'agent'))
        raw = (self.root / (hashlib.sha256(b'a1').hexdigest() + '.snip')).read_bytes()
        (size,) = struct.unpack('>I', raw[8:12])
        plaintext = XorSealer.open(raw[12 + size:])
        (meta_size,) = struct.unpack('>I', plaintext[:4])
        np.testing.assert_array_equal(np.frombuffer(plaintext[4 + meta_size:], '<i2'), fed[-8000 * 30:])
        call.close()
        self.assertFalse(call.on_alert_raised('a3', 'caller'))

    def test_policy_is_fixed_at_call_start(self):
        store = self.store()
        before = CallRetention('c1', RetentionPolicy(False), store, sample_rate=8000)
        after = CallRetention('c2', RetentionPolicy(True), store, sample_rate=8000)  # toggled on later
        for call in (before, after):
            call.feed('caller', ramp(800))
        self.assertEqual((before.on_alert_raised('x', 'caller'), after.on_alert_raised('y', 'caller')), (False, True))

    def test_a_store_or_feed_failure_never_raises_into_the_call_path(self):
        call = CallRetention('c', RetentionPolicy(True), self.store(XorSealer(fail=True)), sample_rate=8000,
                             directions=('caller',))
        self.assertTrue(call.feed('caller', ramp(10)))
        self.assertFalse(call.feed('agent', ramp(10)), 'only scored directions are buffered')
        self.assertFalse(call.feed('caller', np.zeros(10, np.float32)))
        self.assertEqual(call.feed_errors, 2)
        self.assertFalse(call.on_alert_raised('a', 'caller'))
        self.assertFalse(call.on_alert_raised('b', 'nobody'))
        for bad in (dict(enabled=1), dict(ttl_s=0), dict(ring_seconds=float('inf'))):
            with self.assertRaises(ValueError):
                RetentionPolicy(**bad)


if __name__ == '__main__':
    unittest.main()
