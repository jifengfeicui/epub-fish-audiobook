from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from io import BytesIO

try:
    from fastapi.testclient import TestClient
    from backend.alexandria.main import create_app
except ImportError:  # Allows the legacy test suite to run before backend dependencies are installed.
    TestClient = None
    create_app = None


@unittest.skipIf(TestClient is None, "backend requirements are not installed")
class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(Path(self.temp.name), start_scheduler=False))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def test_health_and_secret_masking(self):
        self.assertEqual(self.client.get("/api/v1/health").json(), {"status": "ok"})
        response = self.client.put("/api/v1/settings", json={"fish_api_key": "top-secret"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["fish_api_key"], "********")
        self.assertNotIn("top-secret", self.client.get("/api/v1/settings").text)

    def test_fish_workers_default_and_range(self):
        self.assertEqual(self.client.get("/api/v1/settings").json()["fish_workers"], 5)
        self.assertEqual(self.client.put("/api/v1/settings", json={"fish_workers": 16}).json()["fish_workers"], 16)
        self.assertEqual(self.client.put("/api/v1/settings", json={"fish_workers": 0}).status_code, 422)
        self.assertEqual(self.client.put("/api/v1/settings", json={"fish_workers": 17}).status_code, 422)

    def test_voice_pool_endpoint_and_enabled_voice_shape(self):
        voices = self.client.get("/api/v1/voices")
        self.assertEqual(voices.status_code, 200)
        self.assertIn("enabled", voices.json()[0])
        source = "第1章\n正文"
        created = self.client.post(
            "/api/v1/projects",
            files={"file": ("book.txt", BytesIO(source.encode("utf-8")), "text/plain")},
        )
        self.assertEqual(created.status_code, 201)
        project_id = created.json()["id"]
        pool = self.client.get(f"/api/v1/projects/{project_id}/voice-pool")
        self.assertEqual(pool.status_code, 200)
        self.assertEqual(pool.json(), {"excluded_voice_ids": [], "narrator_voice_profile_id": None})
        self.assertEqual(
            self.client.put(f"/api/v1/projects/{project_id}/voice-pool", json={"excluded_voice_ids": [], "narrator_voice_profile_id": None}).status_code,
            200,
        )

    def test_job_types_render_gate_and_reparse(self):
        source = "第1章歸零\n正文\n第2章親戚\n正文"
        created = self.client.post(
            "/api/v1/projects",
            files={"file": ("book.txt", BytesIO(source.encode("utf-8")), "text/plain")},
        )
        self.assertEqual(created.status_code, 201)
        project_id = created.json()["id"]
        self.assertEqual(len(self.client.get(f"/api/v1/projects/{project_id}/chapters").json()), 2)
        self.assertEqual(
            self.client.post(f"/api/v1/projects/{project_id}/jobs", json={"type": "render"}).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(f"/api/v1/projects/{project_id}/jobs", json={"type": "merge"}).status_code,
            409,
        )
        job = self.client.post(f"/api/v1/projects/{project_id}/jobs", json={"type": "preprocess"})
        self.assertEqual(job.status_code, 201)
        self.assertEqual(job.json()["type"], "preprocess")
        self.assertEqual(self.client.post(f"/api/v1/projects/{project_id}/reparse").status_code, 409)
        self.client.post(f"/api/v1/jobs/{job.json()['id']}/cancel")
        reparsed = self.client.post(f"/api/v1/projects/{project_id}/reparse")
        self.assertEqual(reparsed.status_code, 200)
        self.assertIsNone(reparsed.json()["latest_job"])


if __name__ == "__main__":
    unittest.main()
