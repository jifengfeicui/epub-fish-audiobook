from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from backend.alexandria.db.database import Database
from backend.alexandria.db.repository import Repository
from backend.alexandria.services.bilibili import BilibiliService


class BilibiliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = Database(self.root / "alexandria.db")
        self.database.create_schema()
        self.repo = Repository(self.database, self.root)
        self.project = self.repo.create_project(
            project_id="book",
            title="测试书",
            source_filename="book.txt",
            source_path="projects/book/source/book.txt",
            source_sha256="0" * 64,
            chapters=[
                {"index": 1, "title": "一", "href": "1", "text": "a"},
                {"index": 2, "title": "二", "href": "2", "text": "b"},
                {"index": 3, "title": "三", "href": "3", "text": "c"},
            ],
            render_start_mode="after_review_batch",
            release_batch_size=3,
        )
        self.service = BilibiliService(self.repo)

    def tearDown(self):
        self.database.close()
        self.temporary.cleanup()

    def artifact(self, chapter_index: int, content: bytes = b"mp3") -> dict:
        chapter = self.repo.list_chapters("book")[chapter_index]
        path = self.root / "projects" / "book" / "output" / f"{chapter['position']}.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return self.repo.save_artifact("book", chapter["id"], "chapter_mp3", path)

    def test_continuous_prefix_stops_at_first_missing_artifact(self):
        self.artifact(0)
        self.artifact(2)
        self.assertEqual([item["position"] for item in self.service._continuous_audio("book")], [1])

    def test_excluded_chapters_are_not_bilibili_parts(self):
        chapters = self.repo.list_chapters("book")
        self.repo.set_chapter_inclusion("book", [chapters[1]["id"], chapters[2]["id"]])
        self.artifact(1)
        self.artifact(2)
        continuous = self.service._continuous_audio("book")
        self.assertEqual([item["position"] for item in continuous], [2, 3])
        self.assertEqual([item["included_position"] for item in continuous], [1, 2])
        status = self.service.status("book", sync_remote=False)["chapters"]
        self.assertEqual([item["position"] for item in status], [1, 2])
        self.assertEqual([item["included_position"] for item in status], [1, 2])

    def test_prepare_uses_continuous_included_position_in_manifest_and_video_name(self):
        chapters = self.repo.list_chapters("book")
        self.repo.set_chapter_inclusion("book", [chapters[1]["id"], chapters[2]["id"]])
        self.artifact(1)
        self.artifact(2)

        def render(_image, _audio, output, _title, _ffmpeg, _log, _stop):
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"video")

        def download(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"image")
            return "https://image"

        with patch("backend.alexandria.services.bilibili.shutil.which", side_effect=lambda name: name), \
             patch.object(self.service, "_probe_audio", return_value=2.5), \
             patch.object(self.service, "_validate_video", return_value=2.5), \
             patch.object(self.service, "_render_video", side_effect=render), \
             patch.object(self.service, "_download_image", side_effect=download):
            self.service.prepare("book", lambda _message: None, lambda: False)

        records = self.service.load_manifest("book")["chapters"]
        self.assertEqual([item["included_position"] for item in records], [1, 2])
        self.assertEqual([Path(item["video"]).name for item in records], ["0001-二.mp4", "0002-三.mp4"])
        self.assertTrue(all("position" not in item for item in records))

    def test_old_manifest_gets_continuous_included_positions(self):
        self.service.save_manifest("book", {
            "status": "ready",
            "chapters": [{"chapter_id": 2, "position": 2}, {"chapter_id": 3, "position": 3}],
        })

        _manifest, records = self.service._prepared_records("book")

        self.assertEqual([item["included_position"] for item in records], [1, 2])

    def test_job_payload_is_persisted(self):
        job = self.repo.create_job("book", "bilibili", {"action": "prepare"})
        self.assertEqual(job["payload"], {"action": "prepare"})

    def test_changed_audio_is_reported_as_outdated(self):
        artifact = self.artifact(0, b"new")
        chapter = self.repo.list_chapters("book")[0]
        root = self.service._root("book")
        (root / "videos").mkdir(parents=True)
        (root / "videos" / "one.mp4").write_bytes(b"video")
        self.service.save_manifest("book", {
            "version": 1,
            "status": "ready",
            "chapters": [{
                "chapter_id": chapter["id"], "source_sha256": "old", "published_sha256": "old",
                "video": "videos/one.mp4", "image": "images/one.jpg", "audio_path": artifact["path"],
            }],
        })
        state = self.service.status("book", sync_remote=False)
        self.assertEqual(state["chapters"][0]["bilibili_state"], "outdated")

    def test_prepared_replacement_stays_outdated_until_published(self):
        artifact = self.artifact(0, b"new")
        chapter = self.repo.list_chapters("book")[0]
        root = self.service._root("book")
        (root / "videos").mkdir(parents=True)
        (root / "videos" / "one.mp4").write_bytes(b"video")
        self.service.save_manifest("book", {
            "version": 1,
            "status": "ready",
            "chapters": [{
                "chapter_id": chapter["id"], "source_sha256": artifact["sha256"], "published_sha256": "old",
                "video": "videos/one.mp4", "image": "images/one.jpg", "audio_path": artifact["path"],
            }],
        })

        state = self.service.status("book", sync_remote=False)

        self.assertEqual(state["chapters"][0]["bilibili_state"], "outdated")
        self.assertEqual(state["prepared_count"], 1)

    def test_media_path_cannot_escape_project_directory(self):
        chapter = self.repo.list_chapters("book")[0]
        self.service.save_manifest("book", {"chapters": [{"chapter_id": chapter["id"], "video": "../../outside.mp4"}]})
        with self.assertRaisesRegex(ValueError, "path_escape"):
            self.service.media_path("book", chapter["id"], "video")

    def test_published_project_blocks_reparse(self):
        source = self.root / "projects" / "book" / "source" / "book.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("第1章\n正文", encoding="utf-8")
        self.service.save_manifest("book", {"publication": {"bvid": "BV1234567890"}})
        with self.assertRaisesRegex(RuntimeError, "bilibili_publication_exists"):
            self.repo.reparse_project("book")

    def test_cookie_values_are_removed_from_logs(self):
        self.service.cookie_path.parent.mkdir(parents=True)
        self.service.cookie_path.write_text(json.dumps({
            "cookie_info": {"cookies": [{"name": "SESSDATA", "value": "secret-cookie"}]},
            "token_info": {"access_token": "secret-token"},
        }), encoding="utf-8")
        self.assertEqual(self.service._redact("secret-cookie secret-token"), "*** ***")

    def test_invalid_audio_stops_before_image_download(self):
        self.artifact(0)
        with patch("backend.alexandria.services.bilibili.shutil.which", side_effect=lambda name: name), \
             patch.object(self.service, "_probe_audio", side_effect=ValueError("bad audio")), \
             patch.object(self.service, "_download_image") as download:
            with self.assertRaisesRegex(ValueError, "音频预检失败"):
                self.service.prepare("book", lambda _message: None, lambda: False)
        download.assert_not_called()

    def test_changed_audio_reuses_existing_image(self):
        artifact = self.artifact(0, b"changed")
        chapter = self.repo.list_chapters("book")[0]
        root = self.service._root("book")
        image = root / "images" / f"{chapter['id']}.jpg"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"image")
        self.service.save_manifest("book", {"version": 1, "status": "ready", "form": {}, "chapters": [{
            "chapter_id": chapter["id"], "position": 1, "source_sha256": "old", "image": image.relative_to(root).as_posix(),
            "image_url": "https://picsum.test/old.jpg", "video": "videos/old.mp4", "audio_path": artifact["path"],
        }]})

        def render(_image, _audio, output, _title, _ffmpeg, _log, _stop):
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"video")

        with patch("backend.alexandria.services.bilibili.shutil.which", side_effect=lambda name: name), \
             patch.object(self.service, "_probe_audio", return_value=2.5), \
             patch.object(self.service, "_validate_video", return_value=2.5), \
             patch.object(self.service, "_render_video", side_effect=render) as render_mock, \
             patch.object(self.service, "_download_image") as download:
            self.service.prepare("book", lambda _message: None, lambda: False)
        render_mock.assert_called_once()
        download.assert_not_called()
        self.assertEqual(self.service.load_manifest("book")["chapters"][0]["image_url"], "https://picsum.test/old.jpg")

    def test_unchanged_audio_reuses_valid_video(self):
        artifact = self.artifact(0, b"same")
        chapter = self.repo.list_chapters("book")[0]
        root = self.service._root("book")
        image = root / "images" / f"{chapter['id']}.jpg"
        video = root / "videos" / "0001-一.mp4"
        image.parent.mkdir(parents=True, exist_ok=True)
        video.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"image")
        video.write_bytes(b"video")
        self.service.save_manifest("book", {"version": 1, "status": "ready", "form": {}, "chapters": [{
            "chapter_id": chapter["id"], "source_sha256": artifact["sha256"], "image_url": "https://picsum.test/image.jpg",
            "image": image.relative_to(root).as_posix(), "video": video.relative_to(root).as_posix(), "audio_path": artifact["path"],
        }]})

        with patch("backend.alexandria.services.bilibili.shutil.which", side_effect=lambda name: name), \
             patch.object(self.service, "_probe_audio", return_value=2.5), \
             patch.object(self.service, "_validate_video", return_value=2.5), \
             patch.object(self.service, "_render_video") as render, \
             patch.object(self.service, "_download_image") as download:
            self.service.prepare("book", lambda _message: None, lambda: False)
        render.assert_not_called()
        download.assert_not_called()

    @patch("backend.alexandria.services.bilibili.httpx.get")
    def test_studio_response_is_flattened_for_web_edit(self, get: Mock):
        self.service.cookie_path.parent.mkdir(parents=True)
        self.service.cookie_path.write_text(json.dumps({"token_info": {"access_token": "token"}}), encoding="utf-8")
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"code": 0, "data": {
            "archive": {"title": "稿件", "is_only_self": 1, "limited_free": 1, "aid": 42},
            "videos": [{"filename": "one", "cid": 1}],
        }}
        get.return_value = response
        studio = self.service._studio("BV1234567890")
        self.assertEqual(studio["title"], "稿件")
        self.assertEqual(studio["videos"][0]["filename"], "one")
        self.assertNotIn("archive", studio)
        self.assertNotIn("limited_free", studio)

    def test_remote_mismatch_does_not_replace_expected_filename(self):
        manifest = {
            "form": {"visibility": "only_self"},
            "publication": {"bvid": "BV1234567890", "title": "稿件"},
            "chapters": [{"published_sha256": "sha", "remote_filename": "expected"}],
        }
        with patch.object(self.service, "_studio", return_value={
            "title": "稿件", "is_only_self": 1, "videos": [{"filename": "unexpected", "cid": 2}],
        }):
            self.service._sync_remote(manifest)
        self.assertTrue(manifest["publication"]["remote_mismatch"])
        self.assertEqual(manifest["chapters"][0]["remote_filename"], "expected")

    def test_sync_remote_recovers_uploaded_part_from_remote_title(self):
        manifest = {
            "form": {"visibility": "only_self"},
            "publication": {"bvid": "BV1234567890", "title": "稿件"},
            "pending": {"action": "publish", "parts": 1},
            "chapters": [{
                "source_sha256": "sha",
                "video": "videos/0001-一.mp4",
            }],
        }
        with patch.object(self.service, "_studio", return_value={
            "title": "稿件", "is_only_self": 1,
            "videos": [{"title": "0001-一", "filename": "remote-name", "cid": 2}],
        }):
            self.service._sync_remote(manifest)
        record = manifest["chapters"][0]
        self.assertEqual(record["published_sha256"], "sha")
        self.assertEqual(record["remote_filename"], "remote-name")
        self.assertFalse(manifest["publication"]["remote_mismatch"])
        self.assertNotIn("pending", manifest)

    def test_publish_and_append_update_remote_mapping(self):
        records = [
            {"chapter_id": 1, "source_sha256": "one", "video": "videos/1.mp4", "image": "images/1.jpg"},
            {"chapter_id": 2, "source_sha256": "two", "video": "videos/2.mp4", "image": "images/2.jpg"},
        ]
        manifest = {"status": "ready", "chapters": records, "form": {}}
        remote_one = {"title": "title", "is_only_self": 1, "videos": [{"filename": "one", "cid": 1}]}
        with patch.object(self.service, "_cookie_data", return_value={}), \
             patch.object(self.service, "_prepared_records", return_value=(manifest, records)), \
             patch.object(self.service, "_validate_records"), \
             patch.object(self.service, "_ensure_biliup", return_value=Path("biliup.exe")), \
             patch.object(self.service, "_run_process", return_value="done BV1234567890 av42"), \
             patch.object(self.service, "_studio", return_value=remote_one), \
             patch.object(self.service, "_sync_remote"), \
             patch.object(self.service, "save_manifest"):
            self.service.publish("book", {"parts": 1, "source": "来源", "title": "title", "author": "", "publisher": "", "tid": 201, "tags": "tag", "desc": "desc", "visibility": "only_self", "line": "cnbldsa"}, lambda _message: None, lambda: False)
        self.assertEqual(records[0]["published_sha256"], "one")
        self.assertEqual(manifest["publication"]["bvid"], "BV1234567890")

        records[0]["source_sha256"] = "one-changed"
        remote_two = {"title": "title", "is_only_self": 1, "videos": [{"filename": "one", "cid": 1}, {"filename": "two", "cid": 2}]}
        with patch.object(self.service, "_prepared_records", return_value=(manifest, records)), \
             patch.object(self.service, "_validate_records"), \
             patch.object(self.service, "_ensure_biliup", return_value=Path("biliup.exe")), \
             patch.object(self.service, "_run_process", return_value="done"), \
             patch.object(self.service, "_studio", side_effect=[remote_one, remote_two]), \
             patch.object(self.service, "_sync_remote"), \
             patch.object(self.service, "save_manifest"):
            self.service.append("book", {"parts": 1, "line": "cnbldsa"}, lambda _message: None, lambda: False)
        self.assertEqual(records[1]["published_sha256"], "two")
        self.assertEqual(records[0]["published_sha256"], "one")

    def test_append_recovers_when_remote_edit_finished(self):
        records = [
            {"chapter_id": 1, "source_sha256": "one", "published_sha256": "one", "remote_filename": "one", "video": "videos/1.mp4"},
            {"chapter_id": 2, "source_sha256": "two", "video": "videos/2.mp4"},
        ]
        manifest = {
            "status": "ready", "chapters": records, "publication": {"bvid": "BV1234567890"},
            "pending": {"action": "append", "parts": 1, "published_before": 1},
        }
        remote = {"videos": [{"filename": "one", "cid": 1}, {"filename": "two", "cid": 2}]}
        with patch.object(self.service, "_prepared_records", return_value=(manifest, records)), \
             patch.object(self.service, "_validate_records"), \
             patch.object(self.service, "_studio", return_value=remote), \
             patch.object(self.service, "_run_process") as upload, \
             patch.object(self.service, "_sync_remote"), \
             patch.object(self.service, "save_manifest"):
            self.service.append("book", {"parts": 1, "line": "cnbldsa"}, lambda _message: None, lambda: False)
        upload.assert_not_called()
        self.assertNotIn("pending", manifest)
        self.assertEqual(records[1]["published_sha256"], "two")

    def test_replace_recovers_already_appended_part(self):
        records = [{"chapter_id": 1, "source_sha256": "new", "published_sha256": "old", "remote_filename": "old", "video": "videos/1.mp4", "image": "images/1.jpg"}]
        manifest = {"status": "ready", "chapters": records, "publication": {"bvid": "BV1234567890"}, "pending": {"action": "replace", "chapter_id": 1, "source_sha256": "new", "old_filename": "old"}}
        appended = {"filename": "new", "cid": 2}
        studio_extra = {"videos": [{"filename": "old", "cid": 1}, appended]}
        studio_done = {"videos": [appended]}
        with patch.object(self.service, "_prepared_records", return_value=(manifest, records)), \
             patch.object(self.service, "_validate_records"), \
             patch.object(self.service, "_studio", side_effect=[studio_extra, studio_done]), \
             patch.object(self.service, "_run_process") as upload, \
             patch.object(self.service, "_edit_studio") as edit, \
             patch.object(self.service, "_sync_remote"), \
             patch.object(self.service, "save_manifest"):
            self.service.replace("book", {"chapter_id": 1, "line": "cnbldsa"}, lambda _message: None, lambda: False)
        upload.assert_not_called()
        edit.assert_called_once()
        self.assertNotIn("pending", manifest)
        self.assertEqual(records[0]["remote_filename"], "new")

    def test_append_does_not_mark_ten_published_when_remote_added_only_one(self):
        records = [{"chapter_id": i, "source_sha256": str(i), "video": f"videos/{i}.mp4"} for i in range(11)]
        records[0].update(published_sha256="0", remote_filename="first")
        manifest = {"status": "ready", "chapters": records, "publication": {"bvid": "BV1234567890"}}
        initial = {"videos": [{"filename": "first"}]}
        partial = {"videos": [{"filename": "first"}, {"filename": "second"}]}
        with patch.object(self.service, "_prepared_records", return_value=(manifest, records)), \
             patch.object(self.service, "_validate_records"), \
             patch.object(self.service, "_ensure_biliup", return_value=Path("biliup.exe")), \
             patch.object(self.service, "_run_process", return_value="done"), \
             patch.object(self.service, "_studio", side_effect=[initial, partial]):
            with self.assertRaisesRegex(RuntimeError, "bilibili_append_remote_state_ambiguous"):
                self.service.append("book", {"parts": 10}, lambda _message: None, lambda: False)
        self.assertTrue(all(not record.get("published_sha256") for record in records[1:]))
        self.assertEqual(self.service.load_manifest("book")["pending"]["parts"], 10)

    def test_upload_progress_is_persisted_per_job_before_emitting_logs(self):
        job = self.repo.create_job("book", "bilibili", {"action": "append", "parts": 10})
        messages = []

        def append(_project, _payload, log, _stop):
            log("Checkpoint saved: 4 files uploaded")

        def log(message):
            self.assertEqual(self.repo.get_job(job["id"])["payload"]["uploaded_parts"], 4)
            messages.append(message)

        with patch.object(self.service, "append", side_effect=append):
            self.service.run(job, log, lambda: False)
        self.assertEqual(messages, ["Checkpoint saved: 4 files uploaded"])
        self.assertEqual(self.repo.get_job(job["id"])["payload"]["parts"], 10)
        self.repo.set_job_status(job["id"], "completed")
        new_job = self.repo.create_job("book", "bilibili", {"action": "append", "parts": 10})
        self.assertNotIn("uploaded_parts", new_job["payload"])

    @patch("backend.alexandria.services.bilibili.httpx.post")
    def test_qr_pending_success_and_expiry(self, post: Mock):
        start = Mock()
        start.raise_for_status.return_value = None
        start.json.return_value = {"code": 0, "data": {"auth_code": "auth", "url": "https://qr"}}
        pending = Mock()
        pending.raise_for_status.return_value = None
        pending.json.return_value = {"code": 86039}
        success = Mock()
        success.raise_for_status.return_value = None
        success.json.return_value = {"code": 0, "data": {"cookie_info": {"cookies": []}, "token_info": {"access_token": "token"}}}
        post.side_effect = [start, pending, success]
        login = self.service.start_login()
        self.assertEqual(post.call_args_list[0].kwargs["headers"]["Referer"], "https://www.bilibili.com/")
        self.assertEqual(self.service.poll_login(login["session_id"])["status"], "pending")
        self.assertEqual(post.call_args_list[1].kwargs["headers"]["User-Agent"].split("/", 1)[0], "Mozilla")
        with patch.object(self.service, "account_status", return_value={"logged_in": True, "name": "tester"}):
            self.assertEqual(self.service.poll_login(login["session_id"]), {"status": "success", "name": "tester"})
        stored = json.loads(self.service.cookie_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["platform"], "BiliTV")

    @patch("backend.alexandria.services.bilibili.httpx.post")
    def test_qr_login_412_has_retryable_error(self, post: Mock):
        blocked = Mock(status_code=412)
        post.return_value = blocked

        with self.assertRaisesRegex(RuntimeError, "bilibili_login_blocked"):
            self.service.start_login()

    @patch("backend.alexandria.services.bilibili.httpx.post", side_effect=httpx.ConnectError("offline"))
    def test_qr_login_network_error_is_wrapped(self, _post: Mock):
        with self.assertRaisesRegex(RuntimeError, "bilibili_login_start_failed"):
            self.service.start_login()

    @patch("openai.OpenAI")
    def test_extract_metadata_uses_book_source_and_returns_fields(self, openai_cls: Mock):
        response = Mock()
        response.choices = [Mock(message=Mock(content='{"author":"作者甲","publisher":"出版社乙","description":"一段简介"}'))]
        openai_cls.return_value.chat.completions.create.return_value = response

        metadata = self.service.extract_metadata("book")

        self.assertEqual(metadata, {"author": "作者甲", "publisher": "出版社乙", "description": "一段简介"})
        request = openai_cls.return_value.chat.completions.create.call_args.kwargs
        self.assertIn("一", request["messages"][1]["content"])
        self.assertIn("二", request["messages"][1]["content"])
        self.assertEqual(request["reasoning_effort"], "none")
        self.assertEqual(request["response_format"]["type"], "json_schema")

    @patch("openai.OpenAI")
    def test_extract_metadata_accepts_json_code_fence_and_empty_fields(self, openai_cls: Mock):
        response = Mock()
        response.choices = [Mock(message=Mock(content='```json\n{"author":"","publisher":"","description":"简介"}\n```'))]
        openai_cls.return_value.chat.completions.create.return_value = response

        self.assertEqual(self.service.extract_metadata("book"), {"author": "", "publisher": "", "description": "简介"})

    @patch("openai.OpenAI")
    def test_extract_metadata_wraps_llm_errors(self, openai_cls: Mock):
        openai_cls.return_value.chat.completions.create.side_effect = RuntimeError("provider failed")

        with self.assertRaisesRegex(RuntimeError, "bilibili_metadata_failed"):
            self.service.extract_metadata("book")


if __name__ == "__main__":
    unittest.main()
