import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from backend.alexandria.db.models import AppSetting
from backend.alexandria.main import _defaults, create_app
from backend.alexandria.services.bilibili import BilibiliService
from backend.alexandria.services.stages import StageExecutor


def profiles():
    return [
        {"id": "a", "name": "本地", "base_url": "http://localhost:1234/v1", "api_key": "secret-a", "model": "model-a"},
        {"id": "b", "name": "远程", "base_url": "https://example.test/v1", "api_key": "secret-b", "model": "model-b"},
    ]


class LlmSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.temp.name), start_scheduler=False)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.repo = self.app.state.repository

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def put_profiles(self, items=None, active="a", **other):
        response = self.client.put("/api/v1/settings", json={
            "llm_profiles": profiles() if items is None else items,
            "active_llm_profile_id": active, **other,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def project(self):
        return self.repo.create_project(
            title="Book", source_filename="book.txt", source_path="source/book.txt", source_sha256="0" * 64,
            chapters=[{"index": 1, "title": "Chapter", "href": "1", "text": "Hello"}],
            render_start_mode="after_review_batch", release_batch_size=3,
        )

    def test_migrates_legacy_settings_once_and_survives_restart(self):
        with self.repo.db.session() as session, session.begin():
            session.execute(delete(AppSetting).where(AppSetting.key.in_(("llm_profiles", "active_llm_profile_id"))))
            for key, value in {"llm_base_url": "https://old.test/v1", "llm_api_key": "old-secret", "llm_model": "old-model"}.items():
                session.add(AppSetting(key=key, value_json=json.dumps(value), is_secret=key == "llm_api_key"))
        self.repo.seed_settings(_defaults())
        migrated = self.repo.get_settings(reveal_secrets=True)
        self.assertEqual(migrated["llm_profiles"], [{
            "id": "default", "name": "默认配置", "base_url": "https://old.test/v1",
            "api_key": "old-secret", "model": "old-model",
        }])
        self.assertEqual(migrated["active_llm_profile_id"], "default")
        self.put_profiles(active="b")
        self.repo.seed_settings(_defaults())
        self.assertEqual(self.repo.get_settings(reveal_secrets=True)["llm_profiles"], profiles())
        with self.repo.db.session() as session:
            self.assertEqual(session.scalars(select(AppSetting).where(AppSetting.key.in_(("llm_base_url", "llm_api_key", "llm_model")))).all(), [])

        self.client.__exit__(None, None, None)
        self.app = create_app(Path(self.temp.name), start_scheduler=False)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.repo = self.app.state.repository
        self.assertEqual(self.repo.get_settings(reveal_secrets=True)["llm_profiles"], profiles())
        self.assertEqual(self.client.get("/api/v1/settings").json()["llm_model"], "model-b")

    def test_switch_delete_and_legacy_writes(self):
        self.put_profiles(llm_model="ignored", llm_api_key="ignored")
        self.assertEqual(self.repo.get_settings(reveal_secrets=True)["llm_api_key"], "secret-a")
        result = self.client.put("/api/v1/settings", json={"active_llm_profile_id": "b", "llm_model": "ignored"})
        self.assertEqual(result.json()["llm_model"], "model-b")
        response = self.client.put("/api/v1/settings", json={"llm_model": "updated-b", "llm_api_key": "********"})
        self.assertEqual(response.status_code, 200)
        raw = self.repo.get_settings(reveal_secrets=True)
        self.assertEqual(raw["llm_profiles"][0], profiles()[0])
        self.assertEqual(raw["llm_model"], "updated-b")
        self.assertEqual(raw["llm_api_key"], "secret-b")
        remaining = self.put_profiles([raw["llm_profiles"][0]])
        self.assertEqual(remaining["active_llm_profile_id"], "a")
        self.assertEqual(len(remaining["llm_profiles"]), 1)
        remaining["llm_profiles"][0]["api_key"] = ""
        self.put_profiles(remaining["llm_profiles"])
        self.assertEqual(self.repo.get_settings(reveal_secrets=True)["llm_api_key"], "")
        self.assertEqual(self.client.get("/api/v1/settings").json()["llm_profiles"][0]["api_key"], "")

    def test_masks_preserve_keys_by_id_and_redacts_inactive_key(self):
        masked = self.put_profiles()
        self.assertEqual([item["api_key"] for item in masked["llm_profiles"]], ["********", "********"])
        self.assertNotIn("secret-a", json.dumps(masked))
        self.assertNotIn("secret-b", json.dumps(masked))
        self.put_profiles(list(reversed(masked["llm_profiles"])), active="b")
        raw = self.repo.get_settings(reveal_secrets=True)
        self.assertEqual({item["id"]: item["api_key"] for item in raw["llm_profiles"]}, {"a": "secret-a", "b": "secret-b"})
        project = self.project()
        job = self.repo.create_job(project["id"])
        event = self.repo.add_event(job["id"], project["id"], "log", {
            "profiles": raw["llm_profiles"], "message": "failed: secret-a and secret-b",
        })
        replay = self.repo.list_events(project["id"], after=event["event_id"] - 1)
        self.assertNotIn("secret-a", json.dumps(replay))
        self.assertNotIn("secret-b", json.dumps(replay))

    def test_invalid_updates_are_atomic_and_do_not_echo_secrets(self):
        self.put_profiles()
        original = self.repo.get_settings(reveal_secrets=True)
        invalid = [([], "a"), (profiles(), "missing"), ([profiles()[0], profiles()[0]], "a")]
        for field, value in (("id", ""), ("name", " "), ("model", ""), ("base_url", "file:///tmp"),
                             ("base_url", "http://"), ("base_url", "https://bad host/v1"), ("base_url", "https://bad.test:99999")):
            items = profiles()
            items[0][field] = value
            invalid.append((items, "a"))
        items = profiles()
        items.append({**items[0], "id": "new", "api_key": "********"})
        invalid.append((items, "a"))
        for items, active in invalid:
            with self.subTest(items=items, active=active):
                response = self.client.put("/api/v1/settings", json={
                    "llm_profiles": items, "active_llm_profile_id": active,
                    "fish_api_key": "must-not-save", "generation": {"chunk_size": 1},
                })
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn("secret-a", response.text)
                self.assertEqual(self.repo.get_settings(reveal_secrets=True), original)
        response = self.client.put("/api/v1/settings", json={"llm_base_url": "ftp://invalid"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.repo.get_settings(reveal_secrets=True), original)

    def test_active_jobs_block_all_updates_but_paused_jobs_allow_switch(self):
        self.put_profiles()
        project = self.project()
        job = self.repo.create_job(project["id"])
        original = self.repo.get_settings(reveal_secrets=True)
        for status in ("queued", "running", "pausing"):
            self.repo.set_job_status(job["id"], status)
            response = self.client.put("/api/v1/settings", json={"active_llm_profile_id": "b", "fish_api_key": "new"})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(self.repo.get_settings(reveal_secrets=True), original)
        self.repo.set_job_status(job["id"], "paused")
        response = self.client.put("/api/v1/settings", json={"active_llm_profile_id": "b"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["llm_model"], "model-b")

    def test_cache_hashes_only_follow_effective_connection(self):
        self.put_profiles()
        project = self.project()
        chapter = self.repo.list_chapters(project["id"])[0]
        executor = StageExecutor(self.repo, Path(__file__).parents[1])
        generated = {"content_hash": "unchanged"}

        def hashes():
            return executor.generate_input_hash(chapter["id"], project), executor.review_input_hash(generated, project)

        original = hashes()
        items = profiles()
        items[0]["name"] = "改名"
        items[1]["model"] = "inactive-change"
        self.put_profiles(items)
        self.assertEqual(hashes(), original)
        for field, value in (("base_url", "https://changed.test/v1"), ("model", "changed-model"), ("api_key", "changed-key")):
            changed = copy.deepcopy(items)
            changed[0][field] = value
            self.put_profiles(changed)
            self.assertTrue(all(a != b for a, b in zip(hashes(), original)))
        self.put_profiles(active="b")
        self.assertTrue(all(a != b for a, b in zip(hashes(), original)))

    def test_all_llm_consumers_use_selected_connection(self):
        self.put_profiles(active="b")
        project = self.project()
        chapter = self.repo.list_chapters(project["id"])[0]
        executor = StageExecutor(self.repo, Path(__file__).parents[1])
        entries = [{"speaker": "Alice", "text": "Hello", "instruct": ""}]
        configs = []

        def run(command, _log, _stop):
            configs.append(json.loads(Path(command[command.index("--config") + 1]).read_text(encoding="utf-8"))["llm"])
            Path(command[command.index("--output") + 1]).write_text(json.dumps(entries), encoding="utf-8")

        with patch.object(executor, "_run", side_effect=run):
            generated = executor.generate(chapter["id"], project, lambda _: None)
            executor.review(chapter["id"], project, generated, lambda _: None)
        self.assertEqual(configs, [{"base_url": "https://example.test/v1", "api_key": "secret-b", "model_name": "model-b"}] * 2)

        with patch("openai.OpenAI") as client:
            response = Mock(choices=[Mock(message=Mock(content='[{"speaker":"Alice","gender":"女","personality":"温柔"}]'))])
            client.return_value.chat.completions.create.return_value = response
            executor.analyze_characters(project)
            response.choices[0].message.content = '{"author":"","publisher":"","description":"简介"}'
            BilibiliService(self.repo).extract_metadata(project["id"])
            self.assertEqual(client.call_count, 2)
            for call in client.call_args_list:
                self.assertEqual(call.kwargs["base_url"], "https://example.test/v1")
                self.assertEqual(call.kwargs["api_key"], "secret-b")
            for call in client.return_value.chat.completions.create.call_args_list:
                self.assertEqual(call.kwargs["model"], "model-b")
