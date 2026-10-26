from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cli


class DiscoveryTests(unittest.TestCase):
    def test_standalone_sorts_numbered_chapters_and_excludes_book_mp3(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            output = Path(name) / "测试书" / "output"
            output.mkdir(parents=True)
            for filename in ("010-十.mp3", "002-二.mp3", "测试书.mp3", "note.txt"):
                (output / filename).write_bytes(b"x")
            book = cli.discover_book(output)
            self.assertEqual(book["title"], "测试书")
            self.assertEqual([item["position"] for item in book["chapters"]], [2, 10])
            self.assertEqual([item["title"] for item in book["chapters"]], ["二", "十"])

    def test_alexandria_uses_database_title_order_and_active_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            data = Path(name) / "data"
            project_id = "11111111-1111-1111-1111-111111111111"
            output = data / "projects" / project_id / "output"
            output.mkdir(parents=True)
            first = output / "0001-数据库标题一.mp3"
            second = output / "0002-数据库标题二.mp3"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            connection = sqlite3.connect(data / "alexandria.db")
            connection.executescript(
                """
                CREATE TABLE projects (id TEXT PRIMARY KEY, title TEXT NOT NULL);
                CREATE TABLE chapters (id INTEGER PRIMARY KEY, project_id TEXT, position INTEGER, title TEXT);
                CREATE TABLE artifacts (chapter_id INTEGER, kind TEXT, status TEXT, path TEXT);
                """
            )
            connection.execute("INSERT INTO projects VALUES (?, ?)", (project_id, "数据库书名"))
            connection.execute("INSERT INTO chapters VALUES (1, ?, 2, '第二章')", (project_id,))
            connection.execute("INSERT INTO chapters VALUES (2, ?, 1, '第一章')", (project_id,))
            connection.execute("INSERT INTO artifacts VALUES (1, 'chapter_mp3', 'active', ?)", (second.relative_to(data).as_posix(),))
            connection.execute("INSERT INTO artifacts VALUES (2, 'chapter_mp3', 'active', ?)", (first.relative_to(data).as_posix(),))
            connection.commit()
            connection.close()
            book = cli.discover_book(output)
            self.assertEqual(book["title"], "数据库书名")
            self.assertEqual(book["kind"], "alexandria")
            self.assertEqual([item["title"] for item in book["chapters"]], ["第一章", "第二章"])


class ValidationTests(unittest.TestCase):
    def test_invalid_audio_stops_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            audio = Path(name) / "001-坏音频.mp3"
            audio.write_bytes(b"bad")
            chapters = [{"position": 1, "title": "坏音频", "audio": audio}]
            with patch.object(cli, "probe_audio", side_effect=ValueError("没有有效音轨")):
                with self.assertRaisesRegex(ValueError, "音频预检失败"):
                    cli.preflight_audio(chapters)
            self.assertNotIn("duration", chapters[0])

    def test_manifest_round_trip_supports_resume(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "work" / "manifest.json"
            expected = {"version": 1, "status": "preparing", "chapters": [{"position": 2, "source_sha256": "abc"}]}
            cli.atomic_json(path, expected)
            self.assertEqual(cli.load_manifest(path), expected)
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_partial_upload_reuses_matching_checkpoint_entries(self) -> None:
        with tempfile.TemporaryDirectory() as name, patch.dict(os.environ, {"LOCALAPPDATA": name}):
            videos = [str(Path(name) / f"{number}.mp4") for number in range(3)]
            source = cli.checkpoint_path(videos)
            cli.atomic_json(source, {
                "videos": [{"filename": str(number)} for number in range(2)],
                "uploaded_files": videos[:2],
            })
            cli.seed_partial_checkpoint(videos, videos[:2])
            partial = cli.load_manifest(cli.checkpoint_path(videos[:2]))
            self.assertEqual(partial["uploaded_files"], videos[:2])
            self.assertEqual([item["filename"] for item in partial["videos"]], ["0", "1"])


if __name__ == "__main__":
    unittest.main()
