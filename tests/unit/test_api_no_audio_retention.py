"""Default no-raw-audio retention on the analysis API (backlog 5.3; PRD FR-O5 default
"no raw audio stored", §6.3 data minimization; architecture "Audio minimization").

Claude-authored test design, 12 November 2026. Generated PCM through FastAPI's
TestClient; needs requirements-api.txt and is skipped without FastAPI. Checks the
upload spool directory, the job store after completion/shutdown and response bodies.
Not covered: process memory (including a running job's local payload until its
detector call returns, and short-lived exception tracebacks), logs, OS swap, other
Starlette/FastAPI versions, and the spool check on platforms whose TemporaryFile is
unnamed (Linux): there the directory check is skipped rather than passing vacuously.
tempfile globals are patched for the whole process, so run these sequentially.
"""
import numbers
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

from services.inference import analysis
from tests.unit import test_analysis_api as api_fixture

TestClient = api_fixture.TestClient


def holds_audio(value, minimum=1024):
    """True if a bytes-like object, NumPy array or numeric list of at least `minimum`
    elements is reachable (raw or decoded samples)."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value) >= minimum
    if isinstance(value, np.ndarray):
        return value.size >= minimum
    if isinstance(value, dict):
        return any(holds_audio(v, minimum) for v in value.values())
    if isinstance(value, (list, tuple)):
        if len(value) >= minimum and all(isinstance(v, numbers.Number) for v in value[:minimum]):
            return True
        return any(holds_audio(v, minimum) for v in value)
    return False


@unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')
class NoAudioRetentionTests(unittest.TestCase):
    def setUp(self):
        spool = tempfile.TemporaryDirectory()
        self.addCleanup(spool.cleanup)
        self.spool = Path(spool.name)
        # Multipart parsing spools uploads over 1 MB via tempfile; point it at a private directory.
        patcher = mock.patch.object(tempfile, 'tempdir', str(self.spool))
        patcher.start()
        self.addCleanup(patcher.stop)
        # Count real rollovers so "no spool file left" is not vacuously true.
        self.created = []
        original = tempfile.TemporaryFile

        def tracking(*args, **kwargs):
            handle = original(*args, **kwargs)
            self.created.append(getattr(handle, 'name', None))
            return handle

        rollover = mock.patch.object(tempfile, 'TemporaryFile', tracking)
        rollover.start()
        self.addCleanup(rollover.stop)
        service = analysis.AnalysisService(api_fixture.Detector, model_id='fx-model')
        self.client = TestClient(api_fixture.create_app(service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.jobs = self.client.app.state.jobs

    def post(self, payload):
        return self.client.post('/v1/analyze', files={'file': ('caller.wav', payload, 'audio/wav')})

    def assert_spool_empty(self):
        named = [name for name in self.created if isinstance(name, str)]
        if not named:
            self.skipTest('TemporaryFile is unnamed on this platform; spool directory check not meaningful')
        self.assertTrue(all(Path(name).resolve().is_relative_to(self.spool.resolve()) for name in named))
        self.assertEqual([p for p in self.spool.rglob('*') if p.is_file()], [],
                         'upload spool files are deleted after the request')

    def test_large_upload_spool_is_removed_and_job_keeps_no_audio(self):
        payload = api_fixture.wav(70.0)  # about 1.1 MB at 8 kHz: spooled to disk, queued as a job
        self.assertGreater(len(payload), 1 << 20)
        response = self.post(payload)
        self.assertEqual(response.status_code, 202, response.text)
        record = api_fixture.AnalysisApiTests.wait_job(self, self.client, response.json()['job_id'])
        self.assertEqual(record['status'], 'done')
        self.assertGreaterEqual(len(self.created), 1, 'the upload really spooled to a temporary file')
        self.assert_spool_empty()
        with self.jobs._condition:
            self.assertEqual(self.jobs._pending, [])
            self.assertFalse(holds_audio(list(self.jobs._jobs.values())))
            self.assertLess(len(repr(list(self.jobs._jobs.values()))), 20_000)
        self.assertLess(len(self.client.get(f"/v1/jobs/{record['job_id']}").content), 20_000)
        self.assertNotIn('caller.wav', str(record))

    def test_sync_and_refused_uploads_leave_nothing_behind(self):
        ok = self.post(api_fixture.wav(8.0))
        self.assertEqual(ok.status_code, 200)
        self.assertLess(len(ok.content), 20_000)
        self.assertFalse(holds_audio(ok.json(), minimum=256))
        self.assertNotIn('caller.wav', ok.text)
        refused = self.post(b'RIFF' + b'\x00' * (2 << 20))  # undecodable 2 MB body, spooled then refused
        self.assertEqual(refused.status_code, 422)
        self.assertGreaterEqual(len(self.created), 1, 'the refused 2 MB body was spooled')
        with self.jobs._condition:
            self.assertEqual(self.jobs._jobs, {})
        self.assert_spool_empty()

    def test_shutdown_drops_queued_audio(self):
        release = threading.Event()
        self.addCleanup(release.set)
        gate_service = analysis.AnalysisService(lambda: api_fixture.Detector(gate=release), model_id='fx')
        client = TestClient(api_fixture.create_app(gate_service))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        jobs = client.app.state.jobs
        ids = [client.post('/v1/analyze', files={'file': ('a.wav', api_fixture.wav(70.0), 'audio/wav')}).json()['job_id']
               for _ in range(2)]
        deadline = time.monotonic() + 5
        while jobs.get(ids[0])['status'] != 'running' and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(jobs.get(ids[0])['status'], 'running', 'first job holds the detector')
        jobs.close(timeout=0.1)
        with jobs._condition:
            self.assertEqual(jobs._pending, [])
            self.assertFalse(holds_audio(list(jobs._jobs.values())))
        self.assertEqual(jobs.get(ids[1])['status'], 'failed')
        release.set()


if __name__ == '__main__':
    unittest.main()
