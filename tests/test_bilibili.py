from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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
        self.assertEqual([item["position"] for item in self.service._continuous_audio("book")], [1, 2])
        self.assertEqual([item["position"] for item in self.service.status("book", sync_remote=False)["chapters"]], [2, 3])

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
        self.assertEqual(self.service.poll_login(login["session_id"])["status"], "pending")
        with patch.object(self.service, "account_status", return_value={"logged_in": True, "name": "tester"}):
            self.assertEqual(self.service.poll_login(login["session_id"]), {"status": "success", "name": "tester"})
        stored = json.loads(self.service.cookie_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["platform"], "BiliTV")


if __name__ == "__main__":
    unittest.main()
