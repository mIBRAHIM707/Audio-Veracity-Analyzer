"""Claude-authored tests (19 November 2026, attempt 10) for PRD FR-U5 snippet playback:
services/retention/playback.py, the API route `GET /v1/alerts/{id}/snippet`
(services/api/app.py) and its command-line flags (services/api/__main__.py).

Authored by Claude (Claude Code), not by GPT-6 Astra. Groups:
- reader tests (always run, also on the Linux CI job): snippets written by the real
  `SnippetStore` with a deliberately insecure test sealer that still authenticates its
  associated data, read back as WAV; missing, expired (TTL boundary), future creation
  time, tampered audio/header, a file copied under another alert's name, malformed IDs;
- AES-256-GCM tests (need requirements-retention.txt): the production sealer end to
  end, and a wrong key;
- API and command-line tests (need requirements-api.txt): session, audit before audio,
  each refusal and its status, metrics, the concurrency cap, start-up refusals.
Synthetic tones only; no real audio. Playback evidence, not detector evidence.
"""
import contextlib
import hashlib
import io
import json
import os
import struct
import tempfile
import threading
import time
import unittest
import unittest.mock
import wave
from pathlib import Path

import numpy as np

from services.retention import snippets as snippets_module
from services.retention.playback import REASONS, SnippetReader, SnippetUnavailable, check_alert_id, to_wav
from services.retention.snippets import SnippetStore
from tests.unit import test_analysis_api as api_fixture

TestClient = api_fixture.TestClient
if TestClient is not None:
    from services.api import app as app_module
    from services.api.app import create_app, route_label
    from services.inference import analysis
    from tests.unit import test_local_auth as auth_fixture
API = unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False
T0 = 1_800_000_000.0
TTL = 7 * 86400.0


class TestSealer:
    """NOT SECURE. Binds the associated data like AEAD does (a SHA-256 tag over key,
    data and plaintext), so header/alert-hash changes fail as with AES-GCM; XOR only."""

    algorithm = 'TEST-XOR-NOT-SECURE'

    def __init__(self, key=b'k1'):
        self.key = key

    def _tag(self, plaintext, associated):
        return hashlib.sha256(self.key + bytes(associated) + bytes(plaintext)).digest()

    def seal(self, plaintext, associated):
        return self._tag(plaintext, associated) + bytes(b ^ 0x5A for b in plaintext)

    def open(self, sealed, associated):
        plaintext = bytes(b ^ 0x5A for b in sealed[32:])
        if sealed[:32] != self._tag(plaintext, associated):
            raise ValueError('snippet failed authentication')
        return plaintext


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


def tone(seconds=2.0, rate=8000):
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)


def parse_wav(data):
    with wave.open(io.BytesIO(data), 'rb') as wav:
        return wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), \
            np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2')


class Folder:
    def __init__(self, case):
        folder = tempfile.TemporaryDirectory()
        case.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / 'snippets'
        self.clock = Clock()

    def store(self, sealer=None, clock=None):
        return SnippetStore(self.root, sealer or TestSealer(), clock=clock or self.clock, _insecure_test_sealer=True)

    def reader(self, sealer=None, ttl_s=TTL, clock=None):
        return SnippetReader(self.root, sealer or TestSealer(), ttl_s=ttl_s, clock=clock or self.clock,
                             _insecure_test_sealer=True)

    def path(self, alert_id):
        return self.root / (hashlib.sha256(alert_id.encode('utf-8')).hexdigest() + '.snip')


# -- reader ---------------------------------------------------------------------------

class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.folder = Folder(self)

    def test_a_stored_snippet_comes_back_as_the_same_samples_in_a_wav(self):
        pcm = tone(2.5)
        self.assertTrue(self.folder.store().put('a1', call_id='c1', direction='caller', pcm=pcm, sample_rate=8000))
        got = self.folder.reader().read('a1')
        channels, width, rate, samples = parse_wav(got['wav'])
        self.assertEqual((channels, width, rate), (1, 2, 8000))
        np.testing.assert_array_equal(samples, pcm)
        self.assertEqual((got['seconds'], got['sample_rate'], got['direction'], got['call_id'], got['created']),
                         (2.5, 8000, 'caller', 'c1', T0))
        self.assertEqual(got['wav'][:4] + got['wav'][8:12], b'RIFFWAVE')

    def test_an_urdu_alert_id_and_a_16_khz_snippet(self):
        pcm = tone(1.0, 16000)
        self.folder.store().put('انتباہ:۱', call_id='c2', direction='agent', pcm=pcm, sample_rate=16000)
        got = self.folder.reader().read('انتباہ:۱')
        self.assertEqual(parse_wav(got['wav'])[2], 16000)
        np.testing.assert_array_equal(parse_wav(got['wav'])[3], pcm)

    def test_no_snippet_is_missing(self):
        with self.assertRaises(SnippetUnavailable) as caught:
            self.folder.reader().read('never-stored')
        self.assertEqual(caught.exception.reason, 'missing')
        self.folder.root.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(SnippetUnavailable) as caught:
            self.folder.reader().read('never-stored')
        self.assertEqual(caught.exception.reason, 'missing')

    def test_the_ttl_boundary_matches_cleanup(self):
        store = self.folder.store()
        store.put('a1', call_id='c1', direction='caller', pcm=tone(1.0), sample_rate=8000)
        reader = self.folder.reader(ttl_s=3600.0)
        self.folder.clock.now = T0 + 3600.0 - 0.001
        self.assertEqual(reader.read('a1')['created'], T0)
        self.folder.clock.now = T0 + 3600.0
        with self.assertRaises(SnippetUnavailable) as caught:
            reader.read('a1')
        self.assertEqual(caught.exception.reason, 'expired')
        # cleanup with the same TTL deletes exactly what the reader calls expired
        self.assertEqual(store.cleanup(snippets_module.RetentionPolicy(True, ttl_s=3600.0)), 1)
        with self.assertRaises(SnippetUnavailable) as caught:
            reader.read('a1')
        self.assertEqual(caught.exception.reason, 'missing')

    def test_a_creation_time_in_the_future_is_unreadable(self):
        ahead = Clock(T0 + snippets_module.CLOCK_SKEW_S + 1)
        self.folder.store(clock=ahead).put('a1', call_id='c1', direction='caller', pcm=tone(0.5), sample_rate=8000)
        with self.assertRaises(SnippetUnavailable) as caught:
            self.folder.reader().read('a1')
        self.assertEqual(caught.exception.reason, 'unreadable')
        within = Clock(T0 + snippets_module.CLOCK_SKEW_S - 1)
        self.folder.store(clock=within).put('a2', call_id='c1', direction='caller', pcm=tone(0.5), sample_rate=8000)
        self.assertEqual(self.folder.reader().read('a2')['created'], within.now, 'small skew is tolerated')

    def assert_unreadable(self, alert_id, reader=None):
        with self.assertRaises(SnippetUnavailable) as caught:
            (reader or self.folder.reader()).read(alert_id)
        self.assertEqual(caught.exception.reason, 'unreadable')
        self.assertNotIn(str(self.folder.root), str(caught.exception))

    def test_changed_files_and_a_wrong_key_are_unreadable(self):
        store = self.folder.store()
        for alert_id in ('audio', 'header', 'moved-from', 'short'):
            store.put(alert_id, call_id='c1', direction='caller', pcm=tone(1.0), sample_rate=8000)
        audio = self.folder.path('audio')
        data = bytearray(audio.read_bytes())
        data[-5] ^= 0x01
        audio.write_bytes(bytes(data))
        self.assert_unreadable('audio')
        header = self.folder.path('header')
        data = header.read_bytes()
        header.write_bytes(data.replace(json.dumps(T0).encode(), json.dumps(T0 + 1).encode(), 1))
        self.assertNotEqual(header.read_bytes(), data, 'the header timestamp was changed')
        self.assert_unreadable('header')
        os.replace(self.folder.path('moved-from'), self.folder.path('moved-to'))
        self.assert_unreadable('moved-to')  # the alert-ID hash is authenticated
        short = self.folder.path('short')
        short.write_bytes(short.read_bytes()[:10])
        self.assert_unreadable('short')
        store.put('fine', call_id='c1', direction='caller', pcm=tone(1.0), sample_rate=8000)
        self.assert_unreadable('fine', self.folder.reader(sealer=TestSealer(b'other key')))
        self.assertEqual(self.folder.reader().read('fine')['seconds'], 1.0)

    def test_a_folder_in_place_of_the_snippet_is_never_served(self):
        self.folder.path('dir').mkdir(parents=True)
        with self.assertRaises(SnippetUnavailable) as caught:
            self.folder.reader().read('dir')
        self.assertIn(caught.exception.reason, ('unreadable', 'error'))  # IsADirectory vs PermissionError (Windows)

    def test_malformed_alert_ids_are_refused_before_any_file_access(self):
        reader = self.folder.reader()
        with unittest.mock.patch.object(snippets_module, 'open', create=True,
                                        side_effect=AssertionError('no file access')):
            for alert_id in ('', 'x' * 129, 'a\x00b', None, 7, b'a1'):
                with self.assertRaisesRegex(ValueError, 'alert ID'):
                    reader.read(alert_id)
        check_alert_id('x' * 128)

    def test_path_like_alert_ids_only_ever_name_a_hashed_file_in_the_folder(self):
        # The file name is the SHA-256 of the ID: no ID can reach a file outside the folder.
        outside = self.folder.root.parent / 'outside.snip'
        self.folder.store().put('real', call_id='c1', direction='caller', pcm=tone(0.5), sample_rate=8000)
        outside.write_bytes(self.folder.path('real').read_bytes())
        for alert_id in ('../outside', '..\\outside', '%2e%2e%2foutside', '/etc/passwd', 'C:\\x', '.', '..'):
            with self.assertRaises(SnippetUnavailable) as caught:
                self.folder.reader().read(alert_id)
            self.assertEqual(caught.exception.reason, 'missing', alert_id)

    def test_construction_rules(self):
        repo = Path(__file__).resolve().parents[2]
        with self.assertRaisesRegex(ValueError, 'outside the repository'):
            SnippetReader(repo / 'tmp-snippets', TestSealer(), clock=time.time, _insecure_test_sealer=True)
        with self.assertRaisesRegex(ValueError, 'AES-256-GCM'):
            SnippetReader(self.folder.root, TestSealer(), clock=time.time)
        for ttl in (0, -1, float('nan'), float('inf'), True, '7'):
            with self.assertRaisesRegex(ValueError, 'ttl_s'):
                self.folder.reader(ttl_s=ttl)
        with self.assertRaisesRegex(ValueError, 'reason'):
            SnippetUnavailable('x', reason='other')
        self.assertEqual(REASONS, ('missing', 'expired', 'unreadable', 'error'))

    def test_to_wav_header_fields(self):
        data = to_wav(np.array([0, 1, -1, 32767, -32768], dtype=np.int16), 8000)
        self.assertEqual(len(data), 44 + 10)
        self.assertEqual(struct.unpack('<I', data[24:28])[0], 8000)
        self.assertEqual(struct.unpack('<I', data[40:44])[0], 10)
        np.testing.assert_array_equal(parse_wav(data)[3], [0, 1, -1, 32767, -32768])


@unittest.skipIf(not HAVE_CRYPTO, 'requirements-retention.txt not installed')
class AesGcmPlaybackTests(unittest.TestCase):
    def test_the_production_sealer_round_trip_and_a_wrong_key(self):
        from services.retention.snippets import AesGcmSealer
        folder = Folder(self)
        key = os.urandom(32)
        store = SnippetStore(folder.root, AesGcmSealer(key), clock=folder.clock)
        pcm = tone(3.0)
        self.assertTrue(store.put('a1', call_id='c1', direction='caller', pcm=pcm, sample_rate=8000))
        self.assertNotIn(pcm.tobytes()[:64], folder.path('a1').read_bytes(), 'not in the clear on disk')
        got = SnippetReader(folder.root, AesGcmSealer(key), clock=folder.clock).read('a1')
        np.testing.assert_array_equal(parse_wav(got['wav'])[3], pcm)
        with self.assertRaises(SnippetUnavailable) as caught:
            SnippetReader(folder.root, AesGcmSealer(os.urandom(32)), clock=folder.clock).read('a1')
        self.assertEqual(caught.exception.reason, 'unreadable')


# -- API ------------------------------------------------------------------------------

class CountingReader:
    """Wraps a real reader; counts reads and can fail or block on demand."""

    def __init__(self, reader):
        self.reader, self.reads, self.fail, self.block = reader, [], None, None

    def read(self, alert_id):
        self.reads.append(alert_id)
        if self.block is not None:
            entered, release = self.block
            entered.set()
            release.wait(10)
        if self.fail is not None:
            raise self.fail
        return self.reader.read(alert_id)


@API
class SnippetApiTests(unittest.TestCase):
    def make(self, with_snippets=True):
        self.folder = Folder(self)
        self.folder.store().put('a1', call_id='c1', direction='caller', pcm=tone(2.0), sample_rate=8000)
        self.auth, self.store, _, _ = auth_fixture.authenticator()
        self.reader = CountingReader(self.folder.reader())
        service = analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')
        app = create_app(service, auth=self.auth, snippets=self.reader if with_snippets else None)
        client = TestClient(app, base_url='https://testserver')
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def login(self, client):
        response = client.post('/v1/auth/login', json={'username': 'ana', 'password': auth_fixture.PASSWORD})
        self.assertEqual(response.status_code, 200)

    def snippet_audits(self):
        return [row for row in self.store.audits if row[1] == 'alerts.snippet']

    def test_route_labels(self):
        self.assertEqual(route_label('/v1/alerts/0b6a-uuid/snippet'), '/v1/alerts/:id/snippet')
        for path in ('/v1/alerts/a/b/snippet', '/v1/alerts//snippet', '/v1/alerts/snippet'):
            self.assertEqual(route_label(path), 'other', path)

    def test_playback_needs_a_session(self):
        client = self.make()
        self.assertEqual(client.get('/v1/alerts/a1/snippet').status_code, 401)
        self.assertEqual(self.reader.reads, [])

    def test_a_snippet_is_audited_then_sent_uncached(self):
        client = self.make()
        self.login(client)
        response = client.get('/v1/alerts/a1/snippet')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['content-type'], 'audio/wav')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
        self.assertEqual(response.headers['x-ava-snippet-seconds'], '2.000')
        np.testing.assert_array_equal(parse_wav(response.content)[3], tone(2.0))
        self.assertEqual(self.snippet_audits(), [('ana', 'alerts.snippet', 'ok',
                                                  {'alert_id': 'a1', 'seconds': 2.0, 'direction': 'caller'})])
        metrics = client.get('/metrics').text
        self.assertIn('ava_snippet_reads_total{outcome="ok"} 1\n', metrics)
        self.assertIn('ava_api_requests_total{route="/v1/alerts/:id/snippet",status="2xx"} 1\n', metrics)

    def test_each_refusal_has_its_status_and_no_audio_or_audit_row(self):
        client = self.make()
        self.login(client)
        cases = [('missing', 404, 'No snippet is kept for this alert'),
                 ('expired', 410, 'The snippet for this alert has expired'),
                 ('unreadable', 500, 'The stored snippet could not be authenticated or read'),
                 ('error', 503, 'The snippet store could not be read (error)')]
        for reason, status, detail in cases:
            self.reader.fail = SnippetUnavailable('x', reason=reason)
            response = client.get('/v1/alerts/a1/snippet')
            self.assertEqual((response.status_code, response.json()), (status, {'detail': detail}), reason)
        self.reader.fail = RuntimeError('unexpected')
        response = client.get('/v1/alerts/a1/snippet')
        self.assertEqual((response.status_code, response.json()['detail']),
                         (503, 'The snippet store could not be read (error)'))
        self.reader.fail = None
        self.assertEqual(client.get('/v1/alerts/' + 'x' * 129 + '/snippet').status_code, 422)
        self.assertEqual(self.snippet_audits(), [])
        metrics = client.get('/metrics').text
        for outcome, count in (('missing', 1), ('expired', 1), ('unreadable', 1), ('error', 2), ('invalid', 1),
                               ('ok', 0)):
            self.assertIn(f'ava_snippet_reads_total{{outcome="{outcome}"}} {count}\n', metrics)

    def test_a_real_missing_and_expired_snippet_through_the_route(self):
        client = self.make()
        self.login(client)
        self.assertEqual(client.get('/v1/alerts/other/snippet').status_code, 404)
        self.folder.clock.now = T0 + TTL
        self.assertEqual(client.get('/v1/alerts/a1/snippet').status_code, 410)

    def test_no_audio_is_sent_when_the_audit_row_cannot_be_written(self):
        client = self.make()
        self.login(client)
        self.store.audit_fail = 'unavailable'
        response = client.get('/v1/alerts/a1/snippet')
        self.assertEqual((response.status_code, response.json()['detail']),
                         (503, 'Playback not recorded in the audit log (unavailable)'))
        self.assertNotIn('audio', response.headers['content-type'])
        self.assertIn('ava_snippet_reads_total{outcome="unaudited"} 1\n', client.get('/metrics').text)

    def test_more_playbacks_than_the_cap_are_refused_busy(self):
        entered, release = threading.Event(), threading.Event()
        with unittest.mock.patch.object(app_module, 'MAX_SNIPPET_READS', 1):
            client = self.make()
        self.login(client)
        self.reader.block = (entered, release)
        first = []
        worker = threading.Thread(target=lambda: first.append(client.get('/v1/alerts/a1/snippet').status_code))
        worker.start()
        try:
            self.assertTrue(entered.wait(10))
            second = client.get('/v1/alerts/a1/snippet')
            self.assertEqual((second.status_code, second.json()['detail']),
                             (503, 'Too many snippet playbacks at once (busy); retry later'))
            self.assertEqual(client.get('/healthz').status_code, 200)
        finally:
            release.set()
            worker.join(10)
        self.reader.block = None
        self.assertEqual(first, [200])
        self.assertEqual(client.get('/v1/alerts/a1/snippet').status_code, 200, 'the slot is free again')
        self.assertIn('ava_snippet_reads_total{outcome="busy"} 1\n', client.get('/metrics').text)

    def test_a_playback_waiting_for_its_audit_row_still_holds_its_slot(self):
        # Review M1: decrypted audio waiting for an account slot counts against the cap.
        entered, release = threading.Event(), threading.Event()
        with unittest.mock.patch.object(app_module, 'MAX_SNIPPET_READS', 1):
            client = self.make()
        self.login(client)
        audit = self.store.audit

        def slow_audit(*args):
            if args[1] == 'alerts.snippet':
                entered.set()
                release.wait(10)
            return audit(*args)
        self.store.audit = slow_audit
        first = []
        worker = threading.Thread(target=lambda: first.append(client.get('/v1/alerts/a1/snippet').status_code))
        worker.start()
        try:
            self.assertTrue(entered.wait(10))
            second = client.get('/v1/alerts/a1/snippet')
            self.assertEqual(second.status_code, 503)
            self.assertIn('busy', second.json()['detail'])
            self.assertEqual(len(self.reader.reads), 1, 'the second request never decrypted anything')
        finally:
            release.set()
            worker.join(10)
        self.assertEqual(first, [200])
        self.assertEqual(len(self.snippet_audits()), 1)

    def test_without_a_reader_the_route_answers_503_and_a_reader_needs_accounts(self):
        client = self.make(with_snippets=False)
        self.login(client)
        response = client.get('/v1/alerts/a1/snippet')
        self.assertEqual((response.status_code, response.json()['detail']),
                         (503, 'Snippet playback is not configured on this server'))
        self.assertNotIn('ava_snippet_reads_total', client.get('/metrics').text)
        service = analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')
        with self.assertRaisesRegex(ValueError, 'local accounts'):
            create_app(service, snippets=self.reader)


# -- command line ---------------------------------------------------------------------

@API
class SnippetCliTests(unittest.TestCase):
    def setUp(self):
        from tests.unit import test_api_cli as cli_fixture
        self.cli_fixture = cli_fixture
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.model = cli_fixture.model_dir(self.root)
        self.key = self.root / 'key.bin'
        self.key.write_bytes(os.urandom(32))
        if os.name == 'posix':
            self.key.chmod(0o600)
        self.snips = self.root / 'snips'

    def run_main(self, extra, made=None):
        from services.api.__main__ import main
        stderr = io.StringIO()
        auth, _, _, _ = auth_fixture.authenticator()
        auth.close = lambda: None
        parts = {'service': self.cli_fixture.fake_service, 'bind': self.cli_fixture.RecordingBind(),
                 'serve': lambda app, sock: None, 'history': lambda dsn: (None, True),
                 'auth': lambda dsn: (auth, True), **(made or {})}
        code = main(['--model-root', str(self.model), '--port', '0', *extra],
                    environ={'AVA_DB': 'postgresql://u:hunter2@secret-host/db'}, stderr=stderr, made=parts)
        return code, [json.loads(line) for line in stderr.getvalue().splitlines()], stderr.getvalue()

    def test_start_up_refusals_never_print_a_path_or_key(self):
        from services.api.__main__ import EXIT_REFUSED
        auth = ['--database-url-env', 'AVA_DB', '--auth', 'on']
        cases = [([*auth, '--retention-dir', str(self.snips)], 'needs both'),
                 ([*auth, '--retention-key-file', str(self.key)], 'needs both'),
                 (['--retention-dir', str(self.snips), '--retention-key-file', str(self.key)], 'needs --auth on'),
                 ([*auth, '--retention-ttl-days', '3'], 'only applies with --retention-dir'),
                 ([*auth, '--retention-ttl-days', '7'], 'only applies with --retention-dir'),
                 ([*auth, '--retention-dir', str(self.snips), '--retention-key-file', str(self.root / 'none')],
                  'snippet playback: the retention key must be a regular file')]
        if HAVE_CRYPTO:
            short = self.root / 'short.bin'
            short.write_bytes(b'x' * 31)
            cases.append(([*auth, '--retention-dir', str(self.snips), '--retention-key-file', str(short)],
                          'exactly 32 raw bytes'))
        for extra, expected in cases:
            code, records, text = self.run_main(extra)
            self.assertEqual((code, len(records), records[0]['event']), (EXIT_REFUSED, 1, 'refused'), extra)
            self.assertIn(expected, records[0]['reason'])
            self.assertNotIn(str(self.root), text)
            self.assertNotIn('hunter2', text)
        code, records, _ = self.run_main([*auth, '--retention-dir', str(self.snips), '--retention-key-file',
                                          str(self.key)], made={'snippets': self._raise(RuntimeError('no pkg'))})
        self.assertEqual(records[0]['reason'],
                         'snippet playback: missing dependency cryptography (requirements-retention.txt)')
        for days in ('0', '-1', 'nan', 'inf', '366', 'week'):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as usage:
                self.run_main([*auth, '--retention-ttl-days', days])
            self.assertIn('--retention-ttl-days', usage.getvalue())

    def test_a_folder_that_does_not_exist_yet_is_reported_not_refused(self):
        from services.api.__main__ import EXIT_OK
        code, records, text = self.run_main(['--database-url-env', 'AVA_DB', '--auth', 'on', '--retention-dir',
                                             str(self.snips), '--retention-key-file', str(self.key)],
                                            made={'snippets': lambda *args: object()})
        self.assertEqual(code, EXIT_OK, text)
        self.assertEqual(records[0]['snippet_playback'], {'ttl_days': 7.0, 'folder_exists': False})

    @staticmethod
    def _raise(error):
        def opener(*args):
            raise error
        return opener

    @unittest.skipIf(not HAVE_CRYPTO, 'requirements-retention.txt not installed')
    def test_the_real_opener_serves_a_snippet_stored_with_the_same_key(self):
        from services.api.__main__ import EXIT_OK
        from services.retention.snippets import AesGcmSealer
        store = SnippetStore(self.snips, AesGcmSealer(self.key.read_bytes()), clock=time.time)
        self.assertTrue(store.put('a1', call_id='c1', direction='caller', pcm=tone(1.0), sample_rate=8000))
        seen = {}

        def serve(app, sock):
            with TestClient(app, base_url='https://testserver') as client:
                client.post('/v1/auth/login', json={'username': 'ana', 'password': auth_fixture.PASSWORD})
                response = client.get('/v1/alerts/a1/snippet')
                seen['status'], seen['samples'] = response.status_code, parse_wav(response.content)[3]
        code, records, text = self.run_main(['--database-url-env', 'AVA_DB', '--auth', 'on', '--retention-dir',
                                             str(self.snips), '--retention-key-file', str(self.key),
                                             '--retention-ttl-days', '2'], made={'serve': serve})
        self.assertEqual(code, EXIT_OK, text)
        self.assertEqual(records[0]['snippet_playback'], {'ttl_days': 2.0, 'folder_exists': True})
        self.assertNotIn(str(self.root), text)
        self.assertEqual(seen['status'], 200)
        np.testing.assert_array_equal(seen['samples'], tone(1.0))


if __name__ == '__main__':
    unittest.main()
