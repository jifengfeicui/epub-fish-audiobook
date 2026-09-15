"""Render an EPUB book to chapter MP3 files and one full-book MP3."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
ADAPTER = ROOT / "fish_adapter"


class _TextExtractor(HTMLParser):
    BLOCKS = frozenset({"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "br", "hr", "tr", "section", "article"})
    SKIP = frozenset({"script", "style", "svg"})

    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[str] = []
        self.current: list[str] = []
        self.skip_depth = 0

    def _finish(self) -> None:
        value = html.unescape("".join(self.current))
        value = re.sub(r"\s+", " ", value).strip()
        if value:
            self.blocks.append(value)
        self.current = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self.SKIP:
            self.skip_depth += 1
        elif tag in self.BLOCKS:
            self._finish()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif tag in self.BLOCKS:
            self._finish()

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.current.append(data)

    def text(self) -> str:
        self._finish()
        return "\n\n".join(self.blocks).strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _resolve_href(base: str, href: str) -> str:
    href = href.split("#", 1)[0]
    if not href:
        return base
    return (Path(base).parent / href).as_posix() if "/" in base else href


def _safe_title(title: str, fallback: str) -> str:
    title = re.sub(r"\s+", " ", html.unescape(title or "")).strip()
    title = re.sub(r"[\\/:*?\"<>|]", "_", title)
    return title[:100] or fallback


def _is_non_content(item_id: str, href: str, title: str, text: str) -> bool:
    key = f"{item_id} {href} {title}".lower()
    if re.search(r"cover|copyright|toc|contents|\bnav\b", key):
        return True
    if re.match(r"x_d\d+\b|x_head\b|x_jian\d*\b", item_id.lower()):
        return True
    compact_title = re.sub(r"\s+", "", title)
    if compact_title in {"目录", "版权", "版权页", "致谢", "鸣谢", "插图鸣谢", "参考文献"}:
        return True
    # Empty/image-only pages are artwork, but short section-title pages are audible content.
    return not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", text)


def inspect_epub(epub_path: Path) -> list[dict[str, Any]]:
    """Return readable spine chapters with stable source paths and titles."""
    with zipfile.ZipFile(epub_path, "r") as archive:
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfile = next((e for e in container.iter() if _local_name(e.tag) == "rootfile"), None)
        if rootfile is None or not rootfile.get("full-path"):
            raise ValueError("EPUB has no OPF rootfile")
        opf_path = rootfile.get("full-path")
        opf = ET.fromstring(archive.read(opf_path))
        manifest: dict[str, dict[str, str]] = {}
        for item in opf.iter():
            if _local_name(item.tag) != "item":
                continue
            media = item.get("media-type", "")
            if item.get("id") and item.get("href") and ("html" in media or media.endswith("xhtml+xml")):
                manifest[item.get("id")] = {
                    "href": _resolve_href(opf_path, item.get("href")),
                    "properties": item.get("properties", ""),
                }
        spine = [e.get("idref") for e in opf.iter() if _local_name(e.tag) == "itemref" and e.get("idref")]

        labels: dict[str, str] = {}
        for name in archive.namelist():
            if not name.lower().endswith((".ncx", ".xhtml", ".html")):
                continue
            if not (name.lower().endswith(".ncx") or "nav" in name.lower()):
                continue
            try:
                toc = ET.fromstring(archive.read(name))
            except (KeyError, ET.ParseError):
                continue
            for point in toc.iter():
                if _local_name(point.tag) != "navPoint":
                    continue
                src = next((e.get("src") for e in point.iter() if _local_name(e.tag) == "content"), None)
                label = next((e.text or "" for e in point.iter() if _local_name(e.tag) == "text" and (e.text or "").strip()), "")
                if src:
                    labels[_resolve_href(name, src)] = label.strip()
            for anchor in toc.iter():
                if _local_name(anchor.tag) != "a" or not anchor.get("href"):
                    continue
                label = "".join(anchor.itertext()).strip()
                if label:
                    labels[_resolve_href(name, anchor.get("href"))] = label

        chapters: list[dict[str, Any]] = []
        for idref in spine:
            item = manifest.get(idref)
            if not item:
                continue
            href = item["href"]
            try:
                parser = _TextExtractor()
                parser.feed(archive.read(href).decode("utf-8", errors="replace"))
                text = parser.text()
            except (KeyError, UnicodeError):
                continue
            title = labels.get(href, "")
            if not title:
                title = next((line for line in text.splitlines()[:4] if len(line.strip()) <= 100), Path(href).stem)
            if _is_non_content(idref, href, title, text):
                continue
            chapters.append({"index": len(chapters) + 1, "id": idref, "href": href, "title": _safe_title(title, f"第{len(chapters) + 1}章"), "text": text + "\n"})
    return chapters


def _atomic_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _digest(*values: Any) -> str:
    raw = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _run(command: list[str], label: str) -> None:
    print(f"\n[{label}] {' '.join(command)}")
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode:
        raise RuntimeError(f"{label} failed with exit code {result.returncode}")


def load_voice_pool(path: Path) -> tuple[dict[str, dict[str, str]], list[dict[str, str]]]:
    raw = _load_json(path, None)
    if not isinstance(raw, dict) or not isinstance(raw.get("bindings"), dict) or not isinstance(raw.get("pool"), list):
        raise ValueError("voice pool must contain object bindings and array pool")
    bindings: dict[str, dict[str, str]] = {}
    for speaker, voice in raw["bindings"].items():
        if not isinstance(speaker, str) or not isinstance(voice, dict) or not isinstance(voice.get("reference_id"), str) or not voice["reference_id"].strip():
            raise ValueError(f"invalid binding for speaker {speaker!r}")
        bindings[speaker] = {"reference_id": voice["reference_id"].strip(), "name": str(voice.get("name", speaker))}
    pool: list[dict[str, str]] = []
    for voice in raw["pool"]:
        if not isinstance(voice, dict) or not isinstance(voice.get("reference_id"), str) or not voice["reference_id"].strip():
            raise ValueError("every voice pool entry needs a reference_id")
        pool.append({"reference_id": voice["reference_id"].strip(), "name": str(voice.get("name", voice["reference_id"]))})
    if "NARRATOR" not in bindings:
        raise ValueError("voice pool must bind NARRATOR")
    return bindings, pool


def assign_voices(speakers: list[str], config_path: Path, assignment_path: Path) -> dict[str, dict[str, str]]:
    bindings, pool = load_voice_pool(config_path)
    previous = _load_json(assignment_path, {})
    if not isinstance(previous, dict):
        previous = {}
    reserved = {voice["reference_id"] for voice in bindings.values()}
    configured_pool = {voice["reference_id"] for voice in pool}
    assigned = {
        speaker: dict(value)
        for speaker, value in previous.items()
        if isinstance(value, dict)
        and value.get("reference_id")
        and (speaker in bindings or (value.get("reference_id") in configured_pool and value.get("reference_id") not in reserved))
    }
    for speaker, voice in bindings.items():
        assigned[speaker] = dict(voice)
    available = [voice for voice in pool if voice["reference_id"] not in reserved]
    pool_ids = {voice["reference_id"] for voice in available}
    pool_pos = sum(1 for speaker, voice in assigned.items() if speaker not in bindings and voice.get("reference_id") in pool_ids)
    for speaker in speakers:
        if speaker in assigned:
            continue
        if not available:
            raise ValueError(f"no voice available for speaker {speaker}")
        assigned[speaker] = dict(available[pool_pos % len(available)])
        pool_pos += 1
    _atomic_json(assigned, assignment_path)
    return {speaker: assigned[speaker] for speaker in speakers}


def _slug(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|]", "_", value).strip(" .")
    return value[:100] or "chapter"


def merge_mp3(
    files: list[Path],
    output: Path,
    speakers: list[str] | None = None,
    pause_same_ms: int = 250,
    pause_other_ms: int = 500,
) -> None:
    """Join Fish chunks once, inserting the configured speaker pauses."""
    if not files:
        raise ValueError("no audio files to merge")
    if speakers is not None and len(speakers) != len(files):
        raise ValueError("speaker count must match audio file count")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temp_name:
        temp_dir = Path(temp_name)
        silence_files: dict[int, Path] = {}
        for duration in {pause_same_ms, pause_other_ms}:
            if duration <= 0:
                continue
            silence = temp_dir / f"silence-{duration}.mp3"
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={duration / 1000}",
                "-b:a", "128k", "-ar", "44100", "-ac", "1", str(silence),
            ]
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(f"FFmpeg silence generation failed: {result.stderr.strip()}")
            silence_files[duration] = silence

        concat_list = temp_dir / "concat.txt"
        with concat_list.open("w", encoding="utf-8", newline="\n") as handle:
            previous_speaker: str | None = None
            for index, file in enumerate(files):
                speaker = speakers[index] if speakers is not None else file.stem
                if previous_speaker is not None:
                    duration = pause_same_ms if speaker == previous_speaker else pause_other_ms
                    if duration > 0:
                        handle.write(f"file '{str(silence_files[duration].resolve()).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n")
                handle.write(f"file '{str(file.resolve()).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n")
                previous_speaker = speaker
        temp_output = output.with_suffix(output.suffix + ".part.mp3")
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
            "-safe", "0", "-i", str(concat_list), "-b:a", "128k", "-ar", "44100",
            "-ac", "1", str(temp_output),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"FFmpeg chapter merge failed: {result.stderr.strip()}")
        os.replace(temp_output, output)


def merge_chunk_audio(chapter_dir: Path, reviewed: list[dict[str, str]], output: Path) -> None:
    audio_dir = chapter_dir / "audio"
    files: list[Path] = []
    for index, item in enumerate(reviewed, 1):
        path = audio_dir / f"{index:06d}.mp3"
        if not path.exists() or path.stat().st_size == 0:
            raise RuntimeError(f"missing audio chunk: {path}")
        files.append(path)
    merge_mp3(files, output, speakers=[item["speaker"] for item in reviewed])


def concat_mp3(files: list[Path], output: Path) -> None:
    """Concatenate compatible chapter MP3 files without re-encoding."""
    if not files:
        raise ValueError("no chapter MP3 files to concatenate")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False, dir=output.parent) as handle:
        list_path = Path(handle.name)
        for path in files:
            escaped = str(path.resolve()).replace("'", "'\\''")
            handle.write(f"file '{escaped}'\n")
    temp_output = output.with_suffix(output.suffix + ".part.mp3")
    try:
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(temp_output)]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"FFmpeg chapter concat failed: {result.stderr.strip()}")
        os.replace(temp_output, output)
    finally:
        list_path.unlink(missing_ok=True)
        temp_output.unlink(missing_ok=True)


def render_book(args: argparse.Namespace) -> int:
    epub = args.epub.resolve()
    if not epub.exists():
        raise ValueError(f"EPUB not found: {epub}")
    if args.workers is not None and args.workers < 1:
        raise ValueError("--workers must be at least 1")
    book_dir = (args.book_dir.resolve() if args.book_dir else ROOT / "books" / epub.stem).resolve()
    chapters_dir = book_dir / "chapters"
    output_dir = book_dir / "output"
    chapters = inspect_epub(epub)
    if args.list_chapters:
        for chapter in chapters:
            print(f"{chapter['index']:03d}: {chapter['title']} ({len(chapter['text'].rstrip())} chars)")
        return 0
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg was not found on PATH")
    start = max(1, args.from_chapter or 1)
    end = min(len(chapters), args.to_chapter or len(chapters))
    if start > end:
        raise ValueError(f"invalid chapter range {start}-{end}; book has {len(chapters)} chapters")
    selected = chapters[start - 1:end]
    voice_pool = args.voice_config.resolve()
    load_voice_pool(voice_pool)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from fish_adapter.fish_adapter import _resolve_api_key, load_config
    _resolve_api_key(load_config(ADAPTER / "config.json"))
    manifest_path = book_dir / "book_manifest.json"
    previous_manifest = _load_json(manifest_path, {})
    previous_status = {
        item.get("index"): item.get("status")
        for item in previous_manifest.get("chapters", [])
        if isinstance(item, dict)
    } if isinstance(previous_manifest, dict) else {}
    book_manifest = {
        "epub": str(epub),
        "epub_fingerprint": _file_digest(epub),
        "selected_range": {"from": start, "to": end},
        "full_merge_hash": previous_manifest.get("full_merge_hash") if isinstance(previous_manifest, dict) else None,
        "chapters": [
            {
                "index": chapter["index"],
                "title": chapter["title"],
                "href": chapter["href"],
                "selected": start <= chapter["index"] <= end,
                "status": "pending" if start <= chapter["index"] <= end else previous_status.get(chapter["index"], "not_selected"),
            }
            for chapter in chapters
        ],
    }
    if isinstance(previous_manifest, dict) and previous_manifest.get("full_output"):
        book_manifest["full_output"] = previous_manifest["full_output"]
    _atomic_json(book_manifest, manifest_path)
    assignment_path = book_dir / "voice_assignments.json"
    all_speakers: list[str] = []
    chapter_records: list[dict[str, Any]] = []

    for chapter in selected:
        chapter_dir = chapters_dir / f"{chapter['index']:03d}-{_slug(chapter['title'])}"
        chapter_dir.mkdir(parents=True, exist_ok=True)
        source_path = chapter_dir / "source.txt"
        source_hash = _digest(chapter["href"], chapter["title"], chapter["text"])
        if args.force in {"extract", "all"} or not source_path.exists() or _load_json(chapter_dir / "stage.json", {}).get("source_hash") != source_hash:
            source_path.write_text(chapter["text"], encoding="utf-8")
        stage = _load_json(chapter_dir / "stage.json", {})
        stage.update({"index": chapter["index"], "title": chapter["title"], "href": chapter["href"], "source_hash": source_hash})
        _atomic_json(stage, chapter_dir / "stage.json")
        generated = chapter_dir / "generated.json"
        reviewed = chapter_dir / "reviewed.json"
        generation_hash = _digest(source_hash, args.first_person_speaker, _file_digest(APP / "config.json"), _file_digest(APP / "generate_script.py"), _file_digest(ROOT / "default_prompts.txt"))
        if args.force in {"script", "all"} or not generated.exists() or stage.get("generation_hash") != generation_hash:
            generate_cmd = [sys.executable, str(APP / "generate_script.py"), str(source_path), "--output", str(generated)]
            generate_cmd += ["--first-person-speaker", args.first_person_speaker] if args.first_person_speaker else ["--no-first-person-speaker"]
            _run(generate_cmd, f"generate {chapter['index']:03d}")
            stage["generation_hash"] = generation_hash
            _atomic_json(stage, chapter_dir / "stage.json")
        review_hash = _digest(_file_digest(generated), args.first_person_speaker, _file_digest(APP / "config.json"), _file_digest(APP / "review_script.py"), _file_digest(ROOT / "review_prompts.txt"))
        if args.force in {"review", "all"} or not reviewed.exists() or stage.get("review_hash") != review_hash:
            review_cmd = [sys.executable, str(APP / "review_script.py"), "--script", str(generated), "--output", str(reviewed), "--source", str(source_path)]
            review_cmd += ["--first-person-speaker", args.first_person_speaker] if args.first_person_speaker else ["--no-first-person-speaker"]
            _run(review_cmd, f"review {chapter['index']:03d}")
            stage["review_hash"] = review_hash
            _atomic_json(stage, chapter_dir / "stage.json")
        items = _load_json(reviewed, None)
        if not isinstance(items, list) or not items:
            raise ValueError(f"invalid reviewed script for chapter {chapter['index']}")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("speaker"), str) or not isinstance(item.get("text"), str) or not item["text"].strip():
                raise ValueError(f"invalid reviewed entry in chapter {chapter['index']}")
            if item["speaker"] not in all_speakers:
                all_speakers.append(item["speaker"])
        chapter_records.append({"chapter": chapter, "dir": chapter_dir, "reviewed": reviewed, "stage": stage, "items": items})

    assignments = assign_voices(all_speakers, voice_pool, assignment_path)
    book_dir.mkdir(parents=True, exist_ok=True)
    for record in chapter_records:
        chapter = record["chapter"]
        chapter_dir = record["dir"]
        items = record["items"]
        chapter_voices = chapter_dir / "voices.json"
        _atomic_json({speaker: assignments[speaker] for speaker in sorted({item["speaker"] for item in items})}, chapter_voices)
        output = output_dir / f"{chapter['index']:03d}-{_slug(chapter['title'])}.mp3"
        fish_cmd = [sys.executable, str(ADAPTER / "fish_adapter.py"), "--script", str(record["reviewed"]), "--voices", str(chapter_voices), "--config", str(ADAPTER / "config.json"), "--output", str(chapter_dir / "audio")]
        if args.workers is not None:
            fish_cmd += ["--workers", str(args.workers)]
        if args.force in {"audio", "all"}:
            fish_cmd += ["--only"] + [str(i) for i in range(1, len(items) + 1)]
        _run(fish_cmd, f"fish {chapter['index']:03d}")
        audio_manifest = chapter_dir / "audio" / "manifest.json"
        chunk_files = [chapter_dir / "audio" / f"{index:06d}.mp3" for index in range(1, len(items) + 1)]
        merge_hash = _digest([_file_digest(path) for path in chunk_files], [item["speaker"] for item in items], 250, 500)
        if args.force in {"merge", "all"} or not output.exists() or record["stage"].get("merge_hash") != merge_hash:
            merge_chunk_audio(chapter_dir, items, output)
        record["stage"].update({"audio_manifest": str(audio_manifest.relative_to(book_dir)), "output": str(output.relative_to(book_dir)), "merge_hash": merge_hash})
        _atomic_json(record["stage"], chapter_dir / "stage.json")
        for item in book_manifest["chapters"]:
            if item["index"] == chapter["index"]:
                item["status"] = "done"
                item["output"] = str(output.relative_to(book_dir))
                break
        _atomic_json(book_manifest, manifest_path)

    if len(selected) == len(chapters):
        full_output = output_dir / f"{_slug(epub.stem)}.mp3"
        chapter_files = [output_dir / f"{chapter['index']:03d}-{_slug(chapter['title'])}.mp3" for chapter in chapters]
        if not all(path.exists() and path.stat().st_size > 0 for path in chapter_files):
            raise RuntimeError("cannot merge full book: not every chapter MP3 is complete")
        full_hash = _digest([_file_digest(path) for path in chapter_files])
        book_state = _load_json(manifest_path, {})
        if args.force in {"merge", "all"} or not full_output.exists() or book_state.get("full_merge_hash") != full_hash:
            concat_mp3(chapter_files, full_output)
        print(f"Full book MP3: {full_output}")
        book_manifest["full_merge_hash"] = full_hash
        book_manifest["full_output"] = str((output_dir / f"{_slug(epub.stem)}.mp3").relative_to(book_dir))
    _atomic_json(book_manifest, manifest_path)
    print(f"Rendered {len(selected)} chapter(s) to {output_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epub", type=Path, required=True)
    parser.add_argument("--book-dir", type=Path)
    parser.add_argument("--voice-config", type=Path, default=ADAPTER / "voice_pool.json")
    parser.add_argument("--from-chapter", type=int)
    parser.add_argument("--to-chapter", type=int)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--first-person-speaker")
    parser.add_argument("--list-chapters", action="store_true")
    parser.add_argument("--force", choices=("extract", "script", "review", "audio", "merge", "all"))
    return parser


if __name__ == "__main__":
    try:
        raise SystemExit(render_book(build_parser().parse_args()))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
