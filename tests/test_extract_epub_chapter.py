import sys
import unittest
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from extract_epub_chapter import ChapterTextExtractor  # noqa: E402


class ChapterTextExtractorTests(unittest.TestCase):
    def test_extracts_blocks_and_skips_script_and_style(self):
        extractor = ChapterTextExtractor()
        extractor.feed(
            "<html><style>hidden</style><body><h1>标题</h1>"
            "<p>第一段 <b>加粗</b>。</p><script>ignored()</script><p>第二段。</p></body></html>"
        )

        self.assertEqual(extractor.get_text(), "标题\n\n第一段 加粗。\n\n第二段。")


if __name__ == "__main__":
    unittest.main()
