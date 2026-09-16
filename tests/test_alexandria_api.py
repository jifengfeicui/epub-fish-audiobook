from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
