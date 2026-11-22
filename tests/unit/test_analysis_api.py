"""Claude-authored tests (9 November 2026) for services/api/app.py, backlog 2.2.

Authored by Claude (Claude Code), not by GPT-6 Astra. Uses FastAPI's in-process
TestClient with an injected fake detector; fictional synthetic PCM only. Needs
requirements-api.txt; skipped when FastAPI is not installed (lightweight CI).
"""
import io
import threading
import time
import unittest
import wave

import numpy as np

try:
    from fastapi.testclient import TestClient
    from services.api.app import create_app
except ImportError:  # pragma: no cover - lightweight environment
    TestClient = None

from services.inference import analysis


def wav(seconds, rate=8000):
    t = np.arange(round(seconds * rate)) / rate
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(np.rint(3000 * np.sin(2 * np.pi * 300 * t)).astype("<i2").tobytes())
    return stream.getvalue()


class Detector:
    def __init__(self, gate=None):
        self.gate = gate

    def __call__(self, samples):
        if self.gate is not None:
            self.gate.wait(5)
        return [2.0, 0.0]


@unittest.skipIf(TestClient is None, "requirements-api.txt not installed")
class AnalysisApiTests(unittest.TestCase):
    def make(self, detector=None, factory=None):
        service = analysis.AnalysisService(factory or (lambda: detector or Detector()), model_id="fx-model")
        client = TestClient(create_app(service))
        client.__enter__()  # runs the lifespan, so shutdown stops the job worker
        self.addCleanup(client.__exit__, None, None, None)
        return client, service

    def post(self, client, payload, name="fx.wav"):
        return client.post("/v1/analyze", files={"file": (name, payload, "audio/wav")})

    def wait_job(self, client, job_id, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = client.get(f"/v1/jobs/{job_id}").json()
            if record["status"] in ("done", "failed"):
                return record
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_health_then_sync_analysis(self):
        client, _ = self.make()
        self.assertEqual(client.get("/healthz").json(),
                         {"status": "ok", "detector_loaded": False, "model_id": "fx-model"})
        response = self.post(client, wav(8.0))
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["verdict"], body["calibration"], body["evidence_windows"]),
                         ("synthetic", "uncalibrated_baseline", 3))
        self.assertNotIn("fx.wav", response.text, "upload names are not echoed")
        self.assertTrue(client.get("/healthz").json()["detector_loaded"])

    def test_error_mapping(self):
        client, _ = self.make()
        self.assertEqual(self.post(client, b"not a wav").status_code, 422)
        self.assertEqual(self.post(client, wav(601.0)).status_code, 413)
        self.assertEqual(client.post("/v1/analyze").status_code, 422, "missing file field")
        self.assertEqual(client.get("/v1/jobs/unknown").status_code, 404)
        declared = client.post("/v1/analyze", content=b"x", headers={
            "content-length": str(analysis.MAX_BYTES * 2), "content-type": "multipart/form-data; boundary=fx"})
        self.assertEqual(declared.status_code, 413, "declared oversize refused before parsing")
        bogus = client.post("/v1/analyze", content=b"x", headers={
            "content-length": "abc", "content-type": "multipart/form-data; boundary=fx"})
        self.assertEqual(bogus.status_code, 413)

        def chunks():
            yield b"--fx\r\n"
        chunked = client.post("/v1/analyze", content=chunks(),
                              headers={"content-type": "multipart/form-data; boundary=fx"})
        self.assertEqual(chunked.status_code, 411, "chunked uploads cannot bypass the size bound")
        self.assertEqual(self.post(client, b"").status_code, 422, "empty upload")
        self.assertEqual(client.get("/openapi.json").status_code, 200)
        self.assertEqual(client.get("/docs").status_code, 404, "CDN-backed docs stay off")
        original = analysis.MAX_BYTES
        try:
            import services.api.app as app_module
            app_module.MAX_BYTES = 1000
            self.assertEqual(self.post(client, wav(4.0)).status_code, 413)
        finally:
            app_module.MAX_BYTES = original

    def test_detector_failure_is_500_and_service_recovers(self):
        calls = []

        def factory():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("fictional missing checkpoint")
            return Detector()
        client, _ = self.make(factory=factory)
        failed = self.post(client, wav(4.0))
        self.assertEqual(failed.status_code, 500)
        self.assertNotIn("fictional", failed.text, "internal messages are not leaked")
        self.assertEqual(self.post(client, wav(4.0)).status_code, 200)

    def test_long_file_returns_job_that_completes(self):
        client, _ = self.make()
        response = self.post(client, wav(61.0))
        self.assertEqual(response.status_code, 202, response.text)
        job_id = response.json()["job_id"]
        record = self.wait_job(client, job_id)
        self.assertEqual(record["status"], "done", record)
        self.assertEqual(record["result"]["evidence_windows"], 29)

    def test_job_failure_is_reported_not_raised(self):
        class Broken:
            def __call__(self, samples):
                raise RuntimeError("fictional fault")
        client, _ = self.make(Broken())
        job_id = self.post(client, wav(61.0)).json()["job_id"]
        record = self.wait_job(client, job_id)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"]["status"], 500)

    def test_busy_sync_request_gets_503_and_queue_is_bounded(self):
        gate = threading.Event()
        client, _ = self.make(Detector(gate))
        try:
            first = self.post(client, wav(61.0))
            self.assertEqual(first.status_code, 202)
            deadline = time.monotonic() + 5
            while client.get(f"/v1/jobs/{first.json()['job_id']}").json()["status"] != "running":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            self.assertEqual(self.post(client, wav(4.0)).status_code, 503)
            statuses = [self.post(client, wav(61.0)).status_code for _ in range(5)]
            self.assertEqual(statuses, [202, 202, 202, 202, 503])
        finally:
            gate.set()


    def test_shutdown_fails_queued_jobs_and_stops_worker(self):
        gate = threading.Event()
        client, _ = self.make(Detector(gate))
        running = self.post(client, wav(61.0)).json()["job_id"]
        deadline = time.monotonic() + 5
        while client.get(f"/v1/jobs/{running}").json()["status"] != "running":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        queued = self.post(client, wav(61.0)).json()["job_id"]
        jobs = client.app.state.jobs
        closer = threading.Thread(target=jobs.close)
        closer.start()
        gate.set()
        closer.join(10)
        self.assertFalse(jobs._worker.is_alive())
        self.assertEqual(jobs.get(queued)["status"], "failed")
        self.assertEqual(jobs.get(running)["status"], "done")
        self.assertEqual(self.post(client, wav(61.0)).status_code, 503)


if __name__ == "__main__":
    unittest.main()
