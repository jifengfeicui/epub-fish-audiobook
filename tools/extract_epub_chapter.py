"""Extract one XHTML chapter from an EPUB into paragraph-preserving UTF-8 text."""

import argparse
import html
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path


class ChapterTextExtractor(HTMLParser):
    BLOCK_TAGS = frozenset({
        "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "blockquote", "br", "hr", "tr", "section", "article",
    })
    SKIP_TAGS = frozenset({"style", "script"})

    def __init__(self):
        super().__init__()
        self.blocks = []
        self.current = []
        self.skip_depth = 0

    def _finish_block(self):
        text = html.unescape("".join(self.current))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            self.blocks.append(text)
        self.current = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
        elif tag in self.BLOCK_TAGS:
            self._finish_block()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif tag in self.BLOCK_TAGS:
            self._finish_block()

    def handle_data(self, data):
        if not self.skip_depth:
            self.current.append(data)

    def get_text(self):
        self._finish_block()
        return "\n\n".join(self.blocks)


def extract_chapter(epub_path, entry_name):
    with zipfile.ZipFile(epub_path, "r") as archive:
        try:
            content = archive.read(entry_name)
        except KeyError as exc:
            raise ValueError(f"EPUB entry not found: {entry_name}") from exc
    parser = ChapterTextExtractor()
    parser.feed(content.decode("utf-8", errors="replace"))
    text = parser.get_text().strip()
    if not text:
        raise ValueError(f"EPUB entry contains no readable text: {entry_name}")
    return text + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epub", required=True, type=Path)
    parser.add_argument("--entry", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    text = extract_chapter(args.epub, args.entry)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    print(f"Extracted {len(text.rstrip())} characters to {args.output.resolve()}")


if __name__ == "__main__":
    main()
