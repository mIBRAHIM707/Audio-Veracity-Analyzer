"""Claude-authored tests (14 November 2026) for serving the built dashboard (backlog 2.3).

Authored by Claude (Claude Code), not by GPT-6 Astra. Architecture §2.6 / ADR-6: the
API serves the static build on the same origin. Checks the mount never shadows an
API route, refuses a directory without index.html, and does not serve outside its
directory. Needs requirements-api.txt; skipped when FastAPI is not installed.
"""
from pathlib import Path
import tempfile
import unittest

try:
    import httpx
    from fastapi.testclient import TestClient
    from services.api.app import create_app
except ImportError:  # pragma: no cover - lightweight environment
    TestClient = None

from services.inference import analysis


@unittest.skipIf(TestClient is None, "requirements-api.txt not installed")
class DashboardMountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ava-dashboard-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.dist = root / "dist"
        (self.dist / "assets").mkdir(parents=True)
        (self.dist / "index.html").write_text("<!doctype html><title>fixture page</title>", encoding="utf-8")
        (self.dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
        (root / "secret.txt").write_text("outside", encoding="utf-8")
        self.service = analysis.AnalysisService(lambda: (lambda samples: [0.0, 0.0]), model_id="fx-model")

    def client(self, **kwargs):
        client = TestClient(create_app(self.service, **kwargs))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def test_index_and_assets_served_on_the_api_origin(self):
        client = self.client(dashboard_dir=self.dist)
        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("fixture page", page.text)
        self.assertTrue(page.headers["content-type"].startswith("text/html"))
        self.assertEqual(client.get("/assets/app.js").text, "console.log(1)")
        self.assertEqual(client.get("/missing.js").status_code, 404)

    def test_api_routes_are_not_shadowed(self):
        client = self.client(dashboard_dir=self.dist)
        self.assertEqual(client.get("/healthz").json()["model_id"], "fx-model")
        self.assertEqual(client.get("/v1/jobs/unknown").status_code, 404)
        self.assertEqual(client.get("/v1/jobs/unknown").json(), {"detail": "Unknown job"})
        self.assertEqual(client.post("/v1/analyze", files={"file": ("x.wav", b"junk", "audio/wav")}).status_code, 422)
        self.assertTrue(client.get("/metrics").text.startswith("# HELP"))
        self.assertEqual(client.get("/v1/calls").status_code, 503)  # no hub: still the API, not a file

    def test_no_traversal_outside_the_build(self):
        client = self.client(dashboard_dir=self.dist)
        # Sent as raw ASGI paths (no client-side normalisation) straight to the app.
        for raw in ("/../secret.txt", "/assets/../../secret.txt", "/%2e%2e/secret.txt",
                    "/assets/..%2f..%2fsecret.txt"):
            response = client.get(httpx.URL(f"http://testserver{raw}").copy_with(raw_path=raw.encode()))
            self.assertEqual(response.status_code, 404, raw)
            self.assertNotIn("outside", response.text, raw)

    def test_non_get_and_unknown_api_paths_stay_errors(self):
        client = self.client(dashboard_dir=self.dist)
        self.assertNotEqual(client.get("/v1/analyze").status_code, 200)  # never the page
        self.assertNotIn("fixture page", client.get("/v1/analyze").text)
        self.assertEqual(client.post("/v1/jobs/x").status_code, 405)

    def test_directory_without_index_is_refused(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        for bad in (empty, Path(self.tmp.name) / "nope", self.dist / "index.html"):
            with self.assertRaises(ValueError):
                create_app(self.service, dashboard_dir=bad)

    def test_without_dashboard_root_is_not_served(self):
        client = self.client()
        self.assertEqual(client.get("/").status_code, 404)


if __name__ == "__main__":
    unittest.main()
