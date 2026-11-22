"""Claude-authored tests (22 November 2026; not GPT-6 Astra): the FR-O5 snippet key job
`services/retention/keygen.py` used by the Compose and k3s stacks (backlog 11.1; PRD
FR-O5 "AES-256-GCM at rest").

Off (the default): nothing is done and nothing can fail (review M1).
Normal: a missing key is created once (32 bytes, owner-only on POSIX, no temporary file
left) and kept unchanged on every later run. Boundary/malformed: an existing key that
breaks a loader rule (size, symlink, folder, group-readable) is refused and left as it
is; relative paths, a missing folder and a folder inside the repository are refused.
Failure: a short random source and an unwritable folder are refused. Concurrency: many
jobs at once create exactly one key and every job reports the same bytes. Output never
holds the key or the path. Where the cryptography package is installed, the live
service's loader accepts the created file.
"""
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest import mock

from services.retention import keygen

POSIX = os.name == 'posix'


class EnsureKeyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ava-keygen-')
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.key = self.folder / 'snippet.key'

    def run_main(self, path, retention='on', **kwargs):
        out = io.StringIO()
        code = keygen.main(['--retention', retention, '--key-file', str(path)], stdout=out, **kwargs)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        return code, json.loads(lines[0]), out.getvalue()

    def assert_no_path(self, text, path):
        """Review L5: JSON escapes a Windows backslash, so check the escaped form too."""
        for form in (str(path), json.dumps(str(path))[1:-1]):
            self.assertNotIn(form, text)

    def test_off_does_nothing_even_with_a_missing_or_broken_key_volume(self):
        """Review M1: with retention off live never opens the key, so the job must not be
        able to stop it: no folder, a broken key, an unwritable folder all exit 0."""
        broken = self.folder / 'broken.key'
        broken.write_bytes(b'x')
        for path in (self.folder / 'missing' / 'snippet.key', broken, 'relative.key', self.key):
            with self.subTest(path=str(path)[-20:]):
                code, record, _ = self.run_main(path, retention='off')
                self.assertEqual((code, record), (0, {'event': 'retention_key', 'result': 'skipped'}))
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ['broken.key'], 'off creates nothing')
        self.assertEqual(broken.read_bytes(), b'x')
        out = io.StringIO()
        self.assertEqual(keygen.main(['--key-file', str(self.key)], stdout=out), 0, 'the default is off')
        self.assertFalse(self.key.exists())

    def test_a_missing_key_is_created_once_and_then_kept(self):
        code, record, text = self.run_main(self.key)
        self.assertEqual((code, record), (0, {'event': 'retention_key', 'result': 'created'}))
        data = self.key.read_bytes()
        self.assertEqual(len(data), keygen.KEY_BYTES)
        self.assertNotIn(data.hex(), text)
        self.assert_no_path(text, self.folder)
        if POSIX:
            self.assertEqual(stat.S_IMODE(self.key.stat().st_mode), 0o400)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ['snippet.key'], 'no temporary file left')
        for _ in range(2):
            code, record, _ = self.run_main(self.key)
            self.assertEqual((code, record['result']), (0, 'kept'))
            self.assertEqual(self.key.read_bytes(), data, 'an existing key is never replaced')

    def test_two_folders_get_different_keys(self):
        other = self.folder / 'other'
        other.mkdir()
        self.assertEqual(keygen.ensure_key(self.key), 'created')
        self.assertEqual(keygen.ensure_key(other / 'snippet.key'), 'created')
        self.assertNotEqual(self.key.read_bytes(), (other / 'snippet.key').read_bytes())

    def test_an_existing_key_that_breaks_a_rule_is_refused_and_left(self):
        cases = {'short': b'k' * 31, 'long': b'k' * 33, 'empty': b''}
        for name, data in cases.items():
            with self.subTest(name):
                path = self.folder / f'{name}.key'
                path.write_bytes(data)
                os.chmod(path, 0o600)
                code, record, text = self.run_main(path)
                self.assertEqual((code, record['event']), (2, 'refused'))
                self.assertIn('exactly 32 raw bytes', record['reason'])
                self.assertEqual(path.read_bytes(), data, 'a refused key is left for the operator')
                self.assert_no_path(text, self.folder)
        (self.folder / 'dir.key').mkdir()
        code, record, _ = self.run_main(self.folder / 'dir.key')
        self.assertEqual((code, record['reason']), (2, 'the existing key must be a regular file'))

    @unittest.skipUnless(POSIX and hasattr(os, 'mkfifo'), 'POSIX FIFO')
    def test_a_fifo_in_place_of_the_key_is_refused_without_hanging(self):
        """Review L3/R3: a file swapped after the name check. `lstat` is made to report a
        regular file, so only the non-blocking open plus the descriptor check can refuse
        the FIFO (without O_NONBLOCK the open would hang; without fstat it would read)."""
        os.mkfifo(self.key, 0o600)
        real_lstat = os.lstat
        regular = os.stat_result((stat.S_IFREG | 0o400,) + tuple(real_lstat(self.folder))[1:])

        def lstat(path, *args, **kwargs):
            return regular if Path(path) == self.key else real_lstat(path, *args, **kwargs)
        done = threading.Event()
        result = {}

        def run():
            with mock.patch('os.lstat', side_effect=lstat):
                result['out'] = self.run_main(self.key)
            done.set()
        threading.Thread(target=run, daemon=True).start()
        self.assertTrue(done.wait(10), 'the job hung on a FIFO')
        code, record, _ = result['out']
        self.assertEqual((code, record['reason']), (2, 'the existing key must be a regular file'))

    @unittest.skipUnless(POSIX, 'POSIX folder fsync')
    def test_a_folder_fsync_failure_after_the_link_still_reports_created(self):
        """Review L2: some mounts refuse a folder fsync; the key is linked by then."""
        real_fsync = os.fsync

        def fsync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError(22, 'invalid argument')
            return real_fsync(descriptor)
        with mock.patch('os.fsync', side_effect=fsync):
            self.assertEqual(keygen.ensure_key(self.key), 'created')
        self.assertEqual(len(self.key.read_bytes()), 32)

    def test_stale_temporary_files_of_a_killed_job_are_removed(self):
        """Review L7: only this job's own temp names, and only after the grace period."""
        stale, young = self.folder / '.snippet-key-abc123.tmp', self.folder / '.snippet-key-young1.tmp'
        foreign = self.folder / 'other-file.tmp'
        for path in (stale, young, foreign):
            path.write_bytes(b'k' * 32)
        old = stale.stat().st_mtime - 3600
        os.utime(stale, (old, old))
        os.utime(foreign, (old, old))
        self.assertEqual(keygen.ensure_key(self.key), 'created')
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()),
                         sorted([young.name, foreign.name, 'snippet.key']))

    @unittest.skipUnless(POSIX, 'POSIX permission bits')
    def test_a_group_readable_key_is_refused(self):
        self.key.write_bytes(b'k' * 32)
        os.chmod(self.key, 0o640)
        code, record, _ = self.run_main(self.key)
        self.assertEqual(code, 2)
        self.assertIn('not be readable by group or others', record['reason'])
        self.assertEqual(stat.S_IMODE(self.key.stat().st_mode), 0o640, 'not silently fixed')

    def test_a_symlinked_key_is_refused(self):
        real = self.folder / 'real.key'
        real.write_bytes(b'k' * 32)
        os.chmod(real, 0o600)
        try:
            os.symlink(real, self.key)
        except (OSError, NotImplementedError):
            self.skipTest('this account cannot create symlinks')
        code, record, _ = self.run_main(self.key)
        self.assertEqual(code, 2)
        self.assertIn('not a symlink', record['reason'])

    def test_bad_locations_are_refused_and_nothing_is_created(self):
        inside = keygen.REPO_ROOT / 'tests'
        cases = [('relative.key', 'must be absolute'),
                 (str(self.folder / 'no-such-folder' / 'snippet.key'), 'does not exist'),
                 (str(inside / 'keygen-never-created.key'), 'outside the repository')]
        for path, words in cases:
            with self.subTest(path=Path(path).name):
                code, record, text = self.run_main(path)
                self.assertEqual(code, 2)
                self.assertIn(words, record['reason'])
                self.assert_no_path(text, path)
        self.assertFalse((inside / 'keygen-never-created.key').exists())
        self.assertFalse((self.folder / 'no-such-folder').exists(), 'a missing volume is not created')
        afile = self.folder / 'afile'
        afile.write_bytes(b'')
        code, record, _ = self.run_main(afile / 'snippet.key')
        self.assertEqual(code, 2)

    def test_a_short_random_source_and_an_unwritable_folder_are_refused(self):
        code, record, _ = self.run_main(self.key, urandom=lambda n: b'x' * (n - 1))
        self.assertEqual((code, record['reason']), (2, 'the random source did not return 32 bytes'))
        self.assertFalse(self.key.exists())
        with mock.patch('tempfile.mkstemp', side_effect=PermissionError(13, 'denied')):
            code, record, _ = self.run_main(self.key)
        self.assertEqual(code, 2)
        self.assertIn('not writable: PermissionError', record['reason'])
        with mock.patch('os.link', side_effect=OSError(28, 'no space')):
            code, record, _ = self.run_main(self.key)
        self.assertEqual(code, 2)
        self.assertIn('could not be written: OSError', record['reason'])
        self.assertEqual(list(self.folder.iterdir()), [], 'no key and no temporary file after a failure')

    def test_concurrent_jobs_create_exactly_one_key(self):
        results, errors = [], []
        barrier = threading.Barrier(8)

        def job():
            try:
                barrier.wait(5)
                results.append(keygen.ensure_key(self.key))
            except Exception as error:  # reported below
                errors.append(error)
        threads = [threading.Thread(target=job) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), ['created'] + ['kept'] * 7)
        self.assertEqual(len(self.key.read_bytes()), 32)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ['snippet.key'])

    def test_a_race_lost_at_the_link_checks_the_winner(self):
        winner = b'w' * 32
        real_link = os.link

        def link(source, target):
            Path(target).write_bytes(winner)
            os.chmod(target, 0o600)
            return real_link(source, target)  # now raises FileExistsError
        with mock.patch('os.link', side_effect=link):
            self.assertEqual(keygen.ensure_key(self.key), 'kept')
        self.assertEqual(self.key.read_bytes(), winner)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ['snippet.key'])

    def test_the_live_loader_accepts_the_created_key(self):
        from services.retention.snippets import AesGcmSealer
        keygen.ensure_key(self.key)
        try:
            sealer = AesGcmSealer.from_key_file(self.key)
        except RuntimeError:
            self.skipTest('requirements-retention.txt not installed')
        sealed = sealer.seal(b'audio', b'aad')
        self.assertEqual(sealer.open(sealed, b'aad'), b'audio')


if __name__ == '__main__':
    unittest.main()
