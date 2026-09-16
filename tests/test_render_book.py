import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from render_book import (  # noqa: E402
    assign_voices,
    build_parser,
    concat_mp3,
    inspect_epub,
    merge_mp3,
    release_batches,
)


def make_epub(path: Path) -> None:
    container = """<?xml version='1.0'?><container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'><rootfiles><rootfile full-path='OPS/book.opf'/></rootfiles></container>"""
    opf = """<?xml version='1.0'?><package xmlns='http://www.idpf.org/2007/opf'><manifest>
      <item id='cover' href='cover.xhtml' media-type='application/xhtml+xml'/>
      <item id='toc' href='toc.xhtml' media-type='application/xhtml+xml'/>
      <item id='one' href='one.xhtml' media-type='application/xhtml+xml'/>
      <item id='two' href='two.xhtml' media-type='application/xhtml+xml'/>
      <item id='ncx' href='toc.ncx' media-type='application/x-dtbncx+xml'/>
    </manifest><spine><itemref idref='cover'/><itemref idref='toc'/><itemref idref='one'/><itemref idref='two'/></spine></package>"""
    ncx = """<?xml version='1.0' encoding='utf-8'?><ncx xmlns='http://www.daisy.org/z3986/2005/ncx'><navMap>
      <navPoint><navLabel><text>第一章</text></navLabel><content src='one.xhtml'/></navPoint>
      <navPoint><navLabel><text>第一章</text></navLabel><content src='two.xhtml'/></navPoint>
    </navMap></ncx>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OPS/book.opf", opf)
        archive.writestr("OPS/toc.ncx", ncx)
        archive.writestr("OPS/cover.xhtml", "<h1>封面</h1>")
        archive.writestr("OPS/toc.xhtml", "<h1>目录</h1><p>第一章</p>")
        archive.writestr("OPS/one.xhtml", "<h1>第一章</h1><p>第一章正文足够长，用于测试章节提取和顺序。</p>")
        archive.writestr("OPS/two.xhtml", "<h1>第一章</h1><p>重复标题也必须保留为独立的第二个章节。</p>")


class EpubTests(unittest.TestCase):
    def test_spine_order_filtering_unicode_and_duplicate_titles(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "book.epub"
            make_epub(path)
            chapters = inspect_epub(path)
        self.assertEqual([item["index"] for item in chapters], [1, 2])
        self.assertEqual([item["title"] for item in chapters], ["第一章", "第一章"])
        self.assertIn("第一章正文", chapters[0]["text"])
        self.assertIn("第二个章节", chapters[1]["text"])


class VoiceAssignmentTests(unittest.TestCase):
    def _config(self, root: Path, narrator="narrator") -> Path:
        path = root / "pool.json"
        path.write_text(json.dumps({
            "bindings": {
                "NARRATOR": {"reference_id": narrator, "name": "旁白"},
                "主角": {"reference_id": "lead", "name": "主角音色"},
            },
            "pool": [
                {"reference_id": "lead", "name": "已占用"},
                {"reference_id": "extra-a", "name": "甲"},
                {"reference_id": "extra-b", "name": "乙"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        return path

    def test_bindings_win_pool_rotates_and_assignments_persist(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            saved = root / "assignments.json"
            first = assign_voices(["NARRATOR", "主角", "路人甲", "路人乙", "路人丙"], config, saved)
            second = assign_voices(["NARRATOR", "主角", "路人甲", "路人乙", "路人丙"], config, saved)
        self.assertEqual(first, second)
        self.assertEqual(first["主角"]["reference_id"], "lead")
        self.assertEqual(first["路人甲"]["reference_id"], "extra-a")
        self.assertEqual(first["路人乙"]["reference_id"], "extra-b")
        self.assertEqual(first["路人丙"]["reference_id"], "extra-a")

    def test_changed_binding_replaces_only_bound_speaker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            saved = root / "assignments.json"
            original = assign_voices(["NARRATOR", "路人"], config, saved)
            changed = self._config(root, narrator="new-narrator")
            updated = assign_voices(["NARRATOR", "路人"], changed, saved)
        self.assertEqual(updated["NARRATOR"]["reference_id"], "new-narrator")
        self.assertEqual(updated["路人"], original["路人"])

    def test_pool_with_only_bound_voices_is_rejected_for_minor_character(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "pool.json"
            config.write_text(json.dumps({
                "bindings": {"NARRATOR": {"reference_id": "only"}},
                "pool": [{"reference_id": "only"}],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "no voice available"):
                assign_voices(["NARRATOR", "路人"], config, root / "assignments.json")


class ReleaseBatchTests(unittest.TestCase):
    def test_releases_full_and_short_batches(self):
        self.assertEqual(release_batches([1, 2, 3, 4, 5], "after_review_batch", 3), [[1, 2, 3], [4, 5]])

    def test_waits_for_all_reviews(self):
        self.assertEqual(release_batches([1, 2, 3, 4], "after_all_reviews", 3), [[1, 2, 3, 4]])

    def test_validates_release_size(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 20"):
            release_batches([1], "after_review_batch", 21)

    def test_render_start_cli_accepts_documented_hyphenated_values(self):
        args = build_parser().parse_args([
            "--epub", "book.epub", "--render-start", "after-all-reviews",
        ])
        self.assertEqual(args.render_start, "after_all_reviews")

    def test_render_start_cli_keeps_underscore_compatibility(self):
        args = build_parser().parse_args([
            "--epub", "book.epub", "--render-start", "after_review_batch",
        ])
        self.assertEqual(args.render_start, "after_review_batch")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class MergeTests(unittest.TestCase):
    def _tone(self, path: Path, duration: float) -> None:
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={duration}", "-ar", "44100", "-ac", "1", "-b:a", "128k", str(path),
        ], check=True)

    def _duration(self, path: Path) -> float:
        value = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path),
        ], text=True)
        return float(value.strip())

    def test_merge_uses_same_and_different_speaker_pauses(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = [root / f"{number}.mp3" for number in range(3)]
            for path in files:
                self._tone(path, 0.4)
            output = root / "chapter.mp3"
            merge_mp3(files, output, speakers=["甲", "甲", "乙"])
            self.assertAlmostEqual(self._duration(output), 1.95, delta=0.15)

    def test_full_book_concat_uses_stream_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = [root / f"chapter-{number}.mp3" for number in range(2)]
            for path in files:
                self._tone(path, 0.4)
            output = root / "book.mp3"
            concat_mp3(files, output)
            self.assertTrue(output.exists())
            self.assertAlmostEqual(self._duration(output), 0.8, delta=0.15)


if __name__ == "__main__":
    unittest.main()
