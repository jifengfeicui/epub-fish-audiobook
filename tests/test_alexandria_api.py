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
        invalid_range = self.client.post(
            f"/api/v1/projects/{project_id}/jobs",
            json={"type": "render", "payload": {"from_included_position": 0, "to_included_position": 1}},
        )
        self.assertEqual(invalid_range.status_code, 422)
        self.assertEqual(invalid_range.json()["detail"], "The selected chapter range is invalid")
        job = self.client.post(f"/api/v1/projects/{project_id}/jobs", json={"type": "preprocess"})
        self.assertEqual(job.status_code, 201)
        self.assertEqual(job.json()["type"], "preprocess")
        self.assertEqual(self.client.post(f"/api/v1/projects/{project_id}/reparse").status_code, 409)
        self.client.post(f"/api/v1/jobs/{job.json()['id']}/cancel")
        reparsed = self.client.post(f"/api/v1/projects/{project_id}/reparse")
        self.assertEqual(reparsed.status_code, 200)
        self.assertIsNone(reparsed.json()["latest_job"])

    def test_chapter_inclusion_endpoint(self):
        created = self.client.post(
            "/api/v1/projects",
            files={"file": ("book.txt", BytesIO("第1章\n正文\n第2章\n正文".encode("utf-8")), "text/plain")},
        )
        project_id = created.json()["id"]
        chapters = self.client.get(f"/api/v1/projects/{project_id}/chapters").json()
        self.assertTrue(all(chapter["included"] for chapter in chapters))
        updated = self.client.put(
            f"/api/v1/projects/{project_id}/chapters",
            json={"included_chapter_ids": [chapters[1]["id"]]},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual([row["position"] for row in updated.json() if row["included"]], [2])
        self.assertEqual([row["included_position"] for row in updated.json()], [None, 1])
        self.assertEqual(self.client.get(f"/api/v1/projects/{project_id}").json()["included_chapter_count"], 1)
        self.assertEqual(
            self.client.put(f"/api/v1/projects/{project_id}/chapters", json={"included_chapter_ids": []}).status_code,
            422,
        )

    def test_bilibili_account_and_job_payload(self):
        created = self.client.post(
            "/api/v1/projects",
            files={"file": ("book.txt", BytesIO("第1章\n正文".encode("utf-8")), "text/plain")},
        )
        project_id = created.json()["id"]
        self.assertEqual(self.client.get("/api/v1/bilibili/account").json(), {"logged_in": False, "name": None})
        job = self.client.post(f"/api/v1/projects/{project_id}/bilibili/jobs", json={"action": "prepare"})
        self.assertEqual(job.status_code, 201)
        self.assertEqual(job.json()["type"], "bilibili")
        self.assertEqual(job.json()["payload"], {"action": "prepare"})
        invalid = self.client.post(f"/api/v1/projects/{project_id}/bilibili/jobs", json={"action": "append", "parts": 0})
        self.assertEqual(invalid.status_code, 422)
        invalid_line = self.client.post(f"/api/v1/projects/{project_id}/bilibili/jobs", json={"action": "append", "parts": 1, "line": "unknown"})
        self.assertEqual(invalid_line.status_code, 422)

    def test_artifact_play_still_returns_audio(self):
        created = self.client.post(
            "/api/v1/projects",
            files={"file": ("book.txt", BytesIO("第1章\n正文".encode("utf-8")), "text/plain")},
        )
        project_id = created.json()["id"]
        chapter_id = self.client.get(f"/api/v1/projects/{project_id}/chapters").json()[0]["id"]
        output = Path(self.temp.name) / "projects" / project_id / "output" / "1.mp3"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"audio")
        artifact = self.client.app.state.repository.save_artifact(project_id, chapter_id, "chapter_mp3", output)
        response = self.client.get(f"/api/v1/artifacts/{artifact['id']}/play")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "audio/mpeg")
        self.assertEqual(response.content, b"audio")


if __name__ == "__main__":
    unittest.main()
