"""FR-O5 AES-256-GCM encryption at rest for alert snippets (backlog 5.3).

Claude-authored test design, 14 November 2026. Synthetic int16 ramps, temporary
directories outside the repository, random test keys generated per run. Needs the
pinned `cryptography` (requirements-retention.txt); the module is skipped, with a
reason, when it is not installed.
"""
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

import numpy as np

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

from services.retention import snippets
from services.retention.snippets import (AES_GCM, MAGIC, AesGcmSealer, CallRetention, RetentionPolicy,
                                         SnippetStore, open_snippet)

REPO = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def ramp(n, start=0):
    return (np.arange(start, start + n) % 30000).astype(np.int16)


@unittest.skipUnless(HAVE_CRYPTO, 'cryptography not installed: use requirements-retention.txt')
class AesGcmSealerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'snippets'
        self.key = os.urandom(32)
        self.sealer = AesGcmSealer(self.key)
        self.clock = Clock()

    def test_known_answer_nist_gcm_test_cases_13_and_14(self):
        # McGrew/Viega GCM spec test cases 13/14: zero 256-bit key, zero 96-bit IV.
        sealer = AesGcmSealer(bytes(32))
        with mock.patch.object(snippets.os, 'urandom', return_value=bytes(12)):
            empty = sealer.seal(b'', b'')
            block = sealer.seal(bytes(16), b'')
        self.assertEqual(empty, bytes(12) + bytes.fromhex('530f8afbc74536b9a963b4f1c4cb738b'))
        self.assertEqual(block, bytes(12) + bytes.fromhex('cea7403d4d606b6e074ec5d3baf39d18'
                                                          'd0d1c8a799996bf0265b98b5d48ab919'))
        self.assertEqual(sealer.open(block, b''), bytes(16))

    def test_fresh_nonce_per_seal_and_sizes(self):
        one, two = self.sealer.seal(b'same plaintext', b'ad'), self.sealer.seal(b'same plaintext', b'ad')
        self.assertNotEqual(one[:12], two[:12])
        self.assertNotEqual(one, two)
        self.assertEqual(len(one), 12 + len(b'same plaintext') + 16)
        self.assertEqual(self.sealer.open(one, b'ad'), b'same plaintext')
        for changed in (b'AD', b''):
            with self.assertRaisesRegex(ValueError, 'authentication'):
                self.sealer.open(one, changed)
        with self.assertRaisesRegex(ValueError, 'too short'):
            self.sealer.open(one[:27], b'ad')

    def test_key_validation_and_key_files(self):
        for bad in (b'', bytes(16), bytes(31), bytes(33), 'k' * 32, None):
            with self.subTest(key=bad if bad is None else len(bad)):
                with self.assertRaises(ValueError):
                    AesGcmSealer(bad)
        good = self.base / 'retention.key'
        good.write_bytes(self.key)
        loaded = AesGcmSealer.from_key_file(good)
        self.assertEqual(loaded.open(self.sealer.seal(b'x', b'a'), b'a'), b'x')
        for name, size in (('short.key', 31), ('long.key', 33), ('empty.key', 0)):
            (self.base / name).write_bytes(bytes(size))
            with self.subTest(file=name):
                with self.assertRaisesRegex(ValueError, 'exactly 32'):
                    AesGcmSealer.from_key_file(self.base / name)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            AesGcmSealer.from_key_file(self.base)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            AesGcmSealer.from_key_file(self.base / 'missing.key')
        with self.assertRaisesRegex(ValueError, 'outside the repository'):
            AesGcmSealer.from_key_file(REPO / 'retention.key')  # refused before any read

    def test_production_store_accepts_only_the_real_sealer(self):
        store = SnippetStore(self.root, self.sealer, clock=self.clock)  # no test flag
        self.assertEqual(store.algorithm, AES_GCM)

        class Subclass(AesGcmSealer):
            pass
        with self.assertRaisesRegex(ValueError, AES_GCM):
            SnippetStore(self.root, Subclass(self.key), clock=self.clock)

    def test_alert_snippet_round_trip_is_encrypted_and_authenticated(self):
        store = SnippetStore(self.root, self.sealer, clock=self.clock)
        call = CallRetention('call-7', RetentionPolicy(True, ring_seconds=1.0), store, sample_rate=8000)
        call.feed('caller', ramp(12000))
        call.feed('agent', ramp(500, start=7))
        self.assertTrue(call.on_alert_raised('alert-1', 'caller'))
        snippet = open_snippet(self.root, 'alert-1', self.sealer)
        np.testing.assert_array_equal(snippet['pcm'], ramp(12000)[-8000:])
        self.assertEqual({k: snippet[k] for k in ('call_id', 'direction', 'sample_rate', 'algorithm', 'created')},
                         {'call_id': 'call-7', 'direction': 'caller', 'sample_rate': 8000,
                          'algorithm': AES_GCM, 'created': 1000.0})
        raw = next(self.root.glob('*.snip')).read_bytes()
        self.assertNotIn(ramp(12000)[-8000:].tobytes()[:64], raw)  # audio is not stored in clear
        self.assertNotIn(b'call-7', raw)
        self.assertNotIn(b'alert-1', raw)
        with self.assertRaises(FileNotFoundError):
            open_snippet(self.root, 'alert-unknown', self.sealer)

    def test_any_change_wrong_key_or_moved_file_fails_closed(self):
        store = SnippetStore(self.root, self.sealer, clock=self.clock)
        for alert in ('alert-a', 'alert-b'):
            self.assertTrue(store.put(alert, call_id='c', direction='agent', pcm=ramp(4000), sample_rate=16000))
        path_a = self.root / (snippets._alert_key('alert-a') + '.snip')
        original = path_a.read_bytes()
        header_end = len(MAGIC) + 4 + struct.unpack('>I', original[len(MAGIC):len(MAGIC) + 4])[0]
        cases = {
            'ciphertext_bit': original[:-40] + bytes([original[-40] ^ 1]) + original[-39:],
            'tag_bit': original[:-1] + bytes([original[-1] ^ 1]),
            'nonce_bit': original[:header_end] + bytes([original[header_end] ^ 1]) + original[header_end + 1:],
            'created_timestamp': original.replace(b'1000.0', b'9999.0'),
            'truncated': original[:-100],
            'bad_magic': b'X' + original[1:],
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                self.assertNotEqual(data, original)
                path_a.write_bytes(data)
                with self.assertRaises(ValueError):
                    open_snippet(self.root, 'alert-a', self.sealer)
        path_a.write_bytes(original)
        with self.assertRaisesRegex(ValueError, 'authentication'):
            open_snippet(self.root, 'alert-a', AesGcmSealer(os.urandom(32)))
        # A snippet copied under another alert's name is bound to its own alert hash.
        path_b = self.root / (snippets._alert_key('alert-b') + '.snip')
        path_b.write_bytes(original)
        with self.assertRaisesRegex(ValueError, 'authentication'):
            open_snippet(self.root, 'alert-b', self.sealer)
        self.assertEqual(open_snippet(self.root, 'alert-a', self.sealer)['direction'], 'agent')

    def test_ttl_cleanup_and_purge_work_without_the_key(self):
        store = SnippetStore(self.root, self.sealer, clock=self.clock)
        store.put('old', call_id='c', direction='caller', pcm=ramp(100), sample_rate=8000)
        self.clock.t = 1000.0 + 3600
        store.put('new', call_id='c', direction='caller', pcm=ramp(100), sample_rate=8000)
        keyless = SnippetStore(self.root, AesGcmSealer(os.urandom(32)), clock=self.clock)
        self.assertEqual(keyless.cleanup(RetentionPolicy(True, ttl_s=1800)), 1)
        self.assertEqual(open_snippet(self.root, 'new', self.sealer)['pcm'].tolist(), ramp(100).tolist())
        self.assertEqual(keyless.cleanup(RetentionPolicy(False)), 1)
        self.assertEqual(list(self.root.glob('*.snip')), [])


    def write_raw(self, alert_id, header, sealed):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / (snippets._alert_key(alert_id) + '.snip')
        path.write_bytes(MAGIC + struct.pack('>I', len(header)) + header + sealed)
        return path

    def test_cleanup_deletes_snippets_with_invalid_creation_times_and_never_raises(self):
        # Review findings 1-2: a null/list/NaN/infinite/future timestamp used to raise
        # TypeError out of cleanup or keep a snippet past its TTL.
        store = SnippetStore(self.root, self.sealer, clock=self.clock)
        self.assertTrue(store.put('valid', call_id='c', direction='caller', pcm=ramp(10), sample_rate=8000))
        bad = {'null': b'{"created": null}', 'list': b'[1, 2]', 'nested': b'{"created": {"t": 1}}',
               'nan': b'{"created": NaN}', 'inf': b'{"created": Infinity}', 'bool': b'{"created": true}',
               'future': b'{"created": 1000000.0}', 'not_json': b'\xff\xfe'}
        for name, header in bad.items():
            self.write_raw(f'bad-{name}', header, b'sealed')
        deleted = store.cleanup(RetentionPolicy(True, ttl_s=3600))
        self.assertEqual(deleted, len(bad))
        self.assertEqual([p.name for p in self.root.glob('*.snip')],
                         [snippets._alert_key('valid') + '.snip'])
        # Within the allowed clock skew a slightly future time is kept until its TTL.
        self.write_raw('skewed', b'{"v": 1, "created": 1100.0, "algorithm": "AES-256-GCM"}', b'sealed')
        self.assertEqual(store.cleanup(RetentionPolicy(True, ttl_s=3600)), 0)

    def test_authenticated_but_malformed_contents_are_value_errors(self):
        # Review finding 4: only someone with the key can make these.
        def header(**changes):
            return json.dumps({'v': 1, 'created': 1000.0, 'algorithm': AES_GCM, **changes}).encode()

        def meta(samples=2, **extra):
            body = json.dumps({'call_id': 'c', 'direction': 'caller', 'sample_rate': 8000,
                               'samples': samples, **extra}).encode()
            return struct.pack('>I', len(body)) + body

        cases = {
            'empty_plaintext': (header(), b''),
            'meta_size_beyond_payload': (header(), struct.pack('>I', 999)),
            'meta_not_object': (header(), struct.pack('>I', 2) + b'[]' + bytes(4)),
            'missing_samples': (header(), struct.pack('>I', 2) + b'{}'),
            'sample_count_mismatch': (header(), meta(samples=3) + bytes(4)),
            'odd_pcm_bytes': (header(), meta(samples=2) + bytes(5)),
            'other_algorithm': (header(algorithm='TEST-XOR'), meta() + bytes(4)),
            'other_version': (header(v=2), meta() + bytes(4)),
            'header_not_object': (b'[]', meta() + bytes(4)),
        }
        for name, (head, plaintext) in cases.items():
            with self.subTest(case=name):
                alert = f'crafted-{name}'
                key = snippets._alert_key(alert)
                self.write_raw(alert, head, self.sealer.seal(plaintext, snippets.associated_data(key, head)))
                with self.assertRaises(ValueError):
                    open_snippet(self.root, alert, self.sealer)
        good = header()
        key = snippets._alert_key('crafted-good')
        self.write_raw('crafted-good', good, self.sealer.seal(meta() + bytes(4), snippets.associated_data(key, good)))
        self.assertEqual(open_snippet(self.root, 'crafted-good', self.sealer)['pcm'].tolist(), [0, 0])

    def test_symlinked_key_file_is_refused(self):
        target = self.base / 'real.key'
        target.write_bytes(self.key)
        link = self.base / 'link.key'
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f'cannot create symlinks on this host: {error}')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            AesGcmSealer.from_key_file(link)


class AesGcmUnavailableTests(unittest.TestCase):
    def test_missing_package_is_a_runtime_error_so_retention_cannot_start(self):
        with mock.patch.dict('sys.modules', {'cryptography.hazmat.primitives.ciphers.aead': None}):
            with self.assertRaisesRegex(RuntimeError, 'requirements-retention.txt'):
                AesGcmSealer(bytes(32))


if __name__ == '__main__':
    unittest.main()
