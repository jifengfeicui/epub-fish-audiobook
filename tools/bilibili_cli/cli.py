from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
WORK_ROOT = ROOT / "work"
TOOLS_DIR = ROOT / "tools"
COOKIE_PATH = ROOT / "secrets" / "cookies.json"
PICSUM_URL = "https://picsum.photos/1920/1080"
BILIUP_VERSION = "1.2.4"
BILIUP_URL = "https://github.com/biliup/biliup/releases/download/v1.2.4/biliupR-v1.2.4-x86_64-windows.zip"
BILIUP_ZIP_SHA256 = "cb5af47aeaffd63719c94fa354a4d1404dd8437b6cc215513ec4e6054177c93e"
CHAPTER_RE = re.compile(r"^(\d+)-(.+)\.mp3$", re.IGNORECASE)
DEFAULT_TAGS = "有声书,读书,知识分享,AI配音"


if os.name == "nt":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return value[:100] or "audiobook"


def run_json(command: list[str]) -> dict[str, Any]:
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"命令失败: {' '.join(command[:3])}\n{detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"命令没有返回有效 JSON: {' '.join(command[:3])}") from exc


def probe(path: Path, ffprobe: str = "ffprobe") -> dict[str, Any]:
    return run_json([
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height",
        "-of",
        "json",
        str(path),
    ])


def probe_audio(path: Path, ffprobe: str = "ffprobe") -> float:
    data = probe(path, ffprobe)
    if not any(stream.get("codec_type") == "audio" for stream in data.get("streams", [])):
        raise ValueError(f"没有有效音轨: {path}")
    try:
        duration = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"无法读取音频时长: {path}") from exc
    if duration <= 0:
        raise ValueError(f"音频时长必须大于 0: {path}")
    return duration


def validate_video(path: Path, source_duration: float, ffprobe: str = "ffprobe") -> float:
    data = probe(path, ffprobe)
    streams = data.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if not video or video.get("codec_name") != "h264" or video.get("width") != 1920 or video.get("height") != 1080:
        raise ValueError(f"视频必须是 1920x1080 H.264: {path}")
    if not audio or audio.get("codec_name") != "aac":
        raise ValueError(f"视频音轨必须是 AAC: {path}")
    try:
        duration = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"无法读取视频时长: {path}") from exc
    if duration <= 0 or abs(duration - source_duration) > 1.0:
        raise ValueError(f"视频与源音频时长不一致: {path} ({duration:.3f}s / {source_duration:.3f}s)")
    return duration


def _standalone_book(output_dir: Path) -> dict[str, Any]:
    found: list[dict[str, Any]] = []
    positions: set[int] = set()
    for path in output_dir.iterdir():
        if not path.is_file():
            continue
        match = CHAPTER_RE.fullmatch(path.name)
        if not match:
            continue
        position = int(match.group(1))
        if position in positions:
            raise ValueError(f"重复的章节编号 {position}: {output_dir}")
        positions.add(position)
        found.append({"position": position, "title": match.group(2), "audio": path.resolve()})
    if not found:
        raise ValueError(f"未找到数字前缀章节 MP3: {output_dir}")
    found.sort(key=lambda item: item["position"])
    return {"title": output_dir.parent.name, "kind": "standalone", "chapters": found}


def _alexandria_book(output_dir: Path) -> dict[str, Any] | None:
    project_dir = output_dir.parent
    if output_dir.name.lower() != "output" or project_dir.parent.name.lower() != "projects":
        return None
    data_root = project_dir.parent.parent
    database = data_root / "alexandria.db"
    if not database.is_file():
        return None
    uri = f"file:{database.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        project = connection.execute("SELECT title FROM projects WHERE id = ?", (project_dir.name,)).fetchone()
        if not project:
            raise ValueError(f"Alexandria 数据库中不存在项目: {project_dir.name}")
        rows = connection.execute(
            """
            SELECT c.position, c.title, a.path
            FROM chapters c
            LEFT JOIN artifacts a
              ON a.chapter_id = c.id AND a.kind = 'chapter_mp3' AND a.status = 'active'
            WHERE c.project_id = ?
            ORDER BY c.position
            """,
            (project_dir.name,),
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        raise ValueError(f"Alexandria 项目没有章节: {project_dir.name}")
    chapters: list[dict[str, Any]] = []
    for position, title, relative_path in rows:
        if not relative_path:
            raise ValueError(f"Alexandria 第 {position} 章缺少有效 chapter_mp3 artifact")
        audio = (data_root / relative_path).resolve()
        try:
            audio.relative_to(data_root.resolve())
        except ValueError as exc:
            raise ValueError(f"artifact 路径越过数据目录: {relative_path}") from exc
        if audio.parent != output_dir:
            raise ValueError(f"artifact 不属于指定 output 目录: {audio}")
        chapters.append({"position": int(position), "title": str(title), "audio": audio})
    return {"title": str(project[0]), "kind": "alexandria", "project_id": project_dir.name, "chapters": chapters}


def discover_book(output_dir: Path) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    if not output_dir.is_dir():
        raise ValueError(f"output 目录不存在: {output_dir}")
    return _alexandria_book(output_dir) or _standalone_book(output_dir)


def preflight_audio(chapters: list[dict[str, Any]], ffprobe: str = "ffprobe") -> None:
    errors: list[str] = []
    for chapter in chapters:
        path = chapter["audio"]
        if not path.is_file() or not path.stat().st_size:
            errors.append(f"文件不存在或为空: {path}")
            continue
        try:
            chapter["duration"] = probe_audio(path, ffprobe)
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError("音频预检失败，尚未生成任何视频:\n- " + "\n- ".join(errors))


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def download_image(destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    url = f"{PICSUM_URL}?random={uuid.uuid4().hex}"
    for attempt in range(1, 4):
        temporary = destination.with_suffix(".part.jpg")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Alexandria-Bilibili-CLI/1.0"})
            with urllib.request.urlopen(request, timeout=45) as response, temporary.open("wb") as output:
                content_type = response.headers.get_content_type()
                if not content_type.startswith("image/"):
                    raise ValueError(f"Picsum 返回了非图片内容: {content_type}")
                final_url = response.geturl()
                shutil.copyfileobj(response, output)
            if temporary.stat().st_size == 0:
                raise ValueError("Picsum 返回了空图片")
            os.replace(temporary, destination)
            return final_url
        except Exception as exc:
            last_error = exc
            temporary.unlink(missing_ok=True)
            if attempt < 3:
                time.sleep(attempt)
    raise RuntimeError(f"图片下载失败（已重试 3 次）: {last_error}")


def render_video(image: Path, audio: Path, output: Path, title: str, ffmpeg: str = "ffmpeg") -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".part.mp4")
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-loop",
        "1",
        "-framerate",
        "1",
        "-i",
        str(image),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1,fps=25",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "stillimage",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        "-movflags",
        "+faststart",
        "-metadata",
        f"title={title}",
        str(temporary),
    ]
    result = subprocess.run(command)
    if result.returncode:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg 转换失败: {audio}")
    os.replace(temporary, output)


def _chapter_record(previous: dict[str, Any], position: int) -> dict[str, Any]:
    for item in previous.get("chapters", []):
        if isinstance(item, dict) and item.get("position") == position:
            return item
    return {}


def prepare(args: argparse.Namespace) -> int:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("PATH 中必须同时存在 ffmpeg 和 ffprobe")
    book = discover_book(args.output_dir)
    if args.title:
        book["title"] = args.title
    book["author"] = args.author or ""
    book["publisher"] = args.publisher or ""
    preflight_audio(book["chapters"], ffprobe)

    prepared_dir = (args.prepared_dir or WORK_ROOT / safe_name(book["title"])).resolve()
    images_dir = prepared_dir / "images"
    videos_dir = prepared_dir / "videos"
    manifest_path = prepared_dir / "manifest.json"
    previous = load_manifest(manifest_path)
    manifest: dict[str, Any] = {
        "version": 1,
        "status": "preparing",
        "book": {
            "title": book["title"],
            "author": book["author"],
            "publisher": book["publisher"],
            "input_kind": book["kind"],
            "input_dir": str(args.output_dir.resolve()),
            "chapter_count": len(book["chapters"]),
        },
        "video": {"width": 1920, "height": 1080, "video_codec": "h264", "audio_codec": "aac", "fps": 25},
        "chapters": [item for item in previous.get("chapters", []) if isinstance(item, dict)],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if book.get("project_id"):
        manifest["book"]["project_id"] = book["project_id"]

    total = len(book["chapters"])
    current_positions = {chapter["position"] for chapter in book["chapters"]}
    records = {
        item["position"]: item
        for item in manifest["chapters"]
        if isinstance(item.get("position"), int) and item["position"] in current_positions
    }
    for index, chapter in enumerate(book["chapters"], 1):
        position = chapter["position"]
        audio: Path = chapter["audio"]
        stem = safe_name(f"{position:04d}-{chapter['title']}")
        image = images_dir / f"{stem}.jpg"
        video = videos_dir / f"{stem}.mp4"
        old = _chapter_record(previous, position)
        digest = sha256(audio)
        image_url = old.get("image_url", "") if old.get("source_sha256") == digest else ""
        reusable = old.get("source_sha256") == digest and image.is_file() and video.is_file()
        if reusable:
            try:
                video_duration = validate_video(video, chapter["duration"], ffprobe)
            except (OSError, ValueError, RuntimeError):
                reusable = False
        if reusable:
            print(f"[{index}/{total}] 复用 {video.name}")
        else:
            if not image.is_file() or old.get("source_sha256") != digest:
                print(f"[{index}/{total}] 下载图片 {image.name}")
                image_url = download_image(image)
            print(f"[{index}/{total}] 生成视频 {video.name}")
            render_video(image, audio, video, stem, ffmpeg)
            video_duration = validate_video(video, chapter["duration"], ffprobe)
        record = {
            "position": position,
            "title": chapter["title"],
            "audio": str(audio),
            "source_sha256": digest,
            "source_duration": round(chapter["duration"], 6),
            "image": str(image),
            "image_url": image_url,
            "video": str(video),
            "video_duration": round(video_duration, 6),
        }
        records[position] = record
        manifest["chapters"] = [records[key] for key in sorted(records)]
        atomic_json(manifest_path, manifest)

    manifest["status"] = "ready"
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    manifest["chapters"] = [records[chapter["position"]] for chapter in book["chapters"]]
    atomic_json(manifest_path, manifest)
    print(f"准备完成: {prepared_dir}")
    print(f"章节视频: {total}")
    return 0


def ensure_biliup() -> Path:
    executable = TOOLS_DIR / "biliup.exe"
    if executable.is_file():
        return executable
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"下载 biliup v{BILIUP_VERSION}...")
    with tempfile.TemporaryDirectory(prefix="biliup-") as temp_name:
        archive = Path(temp_name) / "biliup.zip"
        request = urllib.request.Request(BILIUP_URL, headers={"User-Agent": "Alexandria-Bilibili-CLI/1.0"})
        with urllib.request.urlopen(request, timeout=120) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        actual = sha256(archive)
        if actual.lower() != BILIUP_ZIP_SHA256:
            raise RuntimeError(f"biliup SHA-256 不匹配: {actual}")
        with zipfile.ZipFile(archive) as package:
            candidates = [name for name in package.namelist() if Path(name).name.lower() in {"biliup.exe", "biliupr.exe"}]
            if not candidates:
                candidates = [name for name in package.namelist() if name.lower().endswith(".exe") and "biliup" in name.lower()]
            if len(candidates) != 1:
                raise RuntimeError(f"无法从 biliup 压缩包中唯一定位程序: {candidates}")
            temporary = executable.with_suffix(".part.exe")
            with package.open(candidates[0]) as source, temporary.open("wb") as output:
                shutil.copyfileobj(source, output)
            os.replace(temporary, executable)
    return executable


def login(_args: argparse.Namespace) -> int:
    biliup = ensure_biliup()
    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run([str(biliup), "-u", str(COOKIE_PATH), "login"]).returncode


def default_description(book: dict[str, Any]) -> str:
    lines = [f"《{book['title']}》AI有声书。"]
    if book.get("author"):
        lines.append(f"作者：{book['author']}")
    if book.get("publisher"):
        lines.append(f"出版社：{book['publisher']}")
    lines.append("按章节分P，音频由TTS生成。")
    return "\n".join(lines)


def validated_prepared(prepared_dir: Path) -> tuple[dict[str, Any], list[str]]:
    manifest_path = prepared_dir / "manifest.json"
    manifest = load_manifest(manifest_path)
    if manifest.get("version") != 1 or manifest.get("status") != "ready" or not manifest.get("chapters"):
        raise ValueError(f"无效或未完成的 manifest: {manifest_path}")
    if len(manifest["chapters"]) != manifest.get("book", {}).get("chapter_count"):
        raise ValueError(f"manifest 章节数量不完整: {manifest_path}")
    if not COOKIE_PATH.is_file():
        raise RuntimeError(f"尚未登录，请先运行 python cli.py login（凭据应生成于 {COOKIE_PATH}）")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("PATH 中不存在 ffprobe")
    videos: list[str] = []
    for chapter in manifest["chapters"]:
        audio = Path(chapter["audio"])
        if not audio.is_file() or sha256(audio) != chapter["source_sha256"]:
            raise ValueError(f"源音频已丢失或发生变化，请重新 prepare: {audio}")
        source_duration = probe_audio(audio, ffprobe)
        video = Path(chapter["video"])
        validate_video(video, source_duration, ffprobe)
        videos.append(str(video))
    return manifest, videos


def checkpoint_path(videos: list[str]) -> Path:
    value = 0
    for character in "_".join(videos):
        value = (value * 31 + ord(character)) & ((1 << 64) - 1)
    data_dir = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
    return data_dir / f"biliup_checkpoint_{value}.json"


def seed_partial_checkpoint(all_videos: list[str], selected_videos: list[str]) -> None:
    if len(selected_videos) == len(all_videos):
        return
    source = checkpoint_path(all_videos)
    target = checkpoint_path(selected_videos)
    if target.exists() or not source.exists():
        return
    checkpoint = load_manifest(source)
    selected = set(selected_videos)
    pairs = [
        (path, video)
        for path, video in zip(checkpoint.get("uploaded_files", []), checkpoint.get("videos", []))
        if path in selected
    ]
    if not pairs:
        return
    atomic_json(target, {
        "videos": [video for _, video in pairs],
        "uploaded_files": [path for path, _ in pairs],
    })
    print(f"已复用 {len(pairs)} 个已上传分P的 checkpoint")


def run_biliup(command: list[str], log_path: Path) -> str:
    output_lines: list[str] = []
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="")
            log.write(line)
            output_lines.append(line)
        return_code = process.wait()
    if return_code:
        raise RuntimeError(f"biliup 操作失败，退出码 {return_code}；日志: {log_path}")
    return "".join(output_lines)


def upload(args: argparse.Namespace) -> int:
    prepared_dir = args.prepared_dir.resolve()
    manifest_path = prepared_dir / "manifest.json"
    manifest, all_videos = validated_prepared(prepared_dir)
    if manifest.get("upload"):
        raise RuntimeError("该 manifest 已记录投稿，请使用 append 追加分P")
    if args.parts is not None and not 1 <= args.parts <= len(all_videos):
        raise ValueError(f"--parts 必须在 1 到 {len(all_videos)} 之间")
    videos = all_videos[:args.parts]
    seed_partial_checkpoint(all_videos, videos)
    cover = Path(manifest["chapters"][0]["image"])
    if not cover.is_file():
        raise ValueError(f"封面图片不存在: {cover}")
    book = manifest["book"]
    title = args.title or f"《{book['title']}》AI有声书"
    description = args.desc or default_description(book)
    biliup = ensure_biliup()
    command = [
        str(biliup),
        "-u",
        str(COOKIE_PATH),
        "upload",
        *videos,
        "--title",
        title,
        "--tid",
        str(args.tid),
        "--tag",
        args.tag,
        "--desc",
        description,
        "--cover",
        str(cover),
        "--copyright",
        "2",
        "--source",
        args.source,
    ]
    if args.line:
        command += ["--line", args.line]
    if not args.public:
        command += ["--is-only-self", "1"]
    combined = run_biliup(command, prepared_dir / "last-upload.log")
    manifest["upload"] = {
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "title": title,
        "tid": args.tid,
        "tags": args.tag,
        "copyright": 2,
        "source": args.source,
        "visibility": "public" if args.public else "only_self",
        "chapter_count": len(videos),
        "complete": len(videos) == len(all_videos),
        "bvids": sorted(set(re.findall(r"BV[0-9A-Za-z]{10}", combined))),
        "aids": sorted(set(
            re.findall(r"\bav\d+\b", combined, re.IGNORECASE)
            + [f"av{aid}" for aid in re.findall(r'"aid": Number\((\d+)\)', combined)]
        )),
    }
    atomic_json(manifest_path, manifest)
    print(f"投稿完成，记录已写入 {manifest_path}")
    return 0


def append(args: argparse.Namespace) -> int:
    prepared_dir = args.prepared_dir.resolve()
    manifest_path = prepared_dir / "manifest.json"
    manifest, all_videos = validated_prepared(prepared_dir)
    upload_state = manifest.get("upload") or {}
    bvids = upload_state.get("bvids") or []
    vid = args.vid or (bvids[0] if bvids else None)
    if not vid:
        raise ValueError("缺少稿件 BV/AV 号，请使用 --vid 指定")
    completed = int(upload_state.get("chapter_count", 0))
    remaining = all_videos[completed:]
    if not remaining:
        print("所有章节均已发布，无需追加")
        return 0
    if args.parts is not None and not 1 <= args.parts <= len(remaining):
        raise ValueError(f"--parts 必须在 1 到 {len(remaining)} 之间")
    videos = remaining[:args.parts]
    biliup = ensure_biliup()
    command = [str(biliup), "-u", str(COOKIE_PATH), "append", "--vid", vid, *videos]
    if args.line:
        command += ["--line", args.line]
    combined = run_biliup(command, prepared_dir / "last-append.log")
    upload_state["chapter_count"] = completed + len(videos)
    upload_state["complete"] = upload_state["chapter_count"] == len(all_videos)
    upload_state["updated_at"] = datetime.now(timezone.utc).isoformat()
    upload_state["bvids"] = sorted(set(bvids + re.findall(r"BV[0-9A-Za-z]{10}", combined)))
    manifest["upload"] = upload_state
    atomic_json(manifest_path, manifest)
    print(f"追加完成，已发布 {upload_state['chapter_count']}/{len(all_videos)}P")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="将 Alexandria/章节 MP3 产物转换并投稿为 B 站多P有声书")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare", help="生成并验证章节视频")
    prepare_parser.add_argument("output_dir", type=Path)
    prepare_parser.add_argument("--prepared-dir", type=Path)
    prepare_parser.add_argument("--title")
    prepare_parser.add_argument("--author")
    prepare_parser.add_argument("--publisher")
    prepare_parser.set_defaults(handler=prepare)

    login_parser = subparsers.add_parser("login", help="通过 biliup 扫码登录")
    login_parser.set_defaults(handler=login)

    upload_parser = subparsers.add_parser("upload", help="将准备好的视频投稿为一个多P稿件")
    upload_parser.add_argument("prepared_dir", type=Path)
    upload_parser.add_argument("--source", required=True, help="转载来源说明")
    upload_parser.add_argument("--title")
    upload_parser.add_argument("--tid", type=int, default=201)
    upload_parser.add_argument("--tag", default=DEFAULT_TAGS)
    upload_parser.add_argument("--desc")
    upload_parser.add_argument("--line", help="指定 biliup 上传线路，例如 cnbldsa")
    upload_parser.add_argument("--parts", type=int, help="只提交前 N 个分P，之后使用 append 追加")
    upload_parser.add_argument("--public", action="store_true", help="公开投稿；默认仅自己可见")
    upload_parser.set_defaults(handler=upload)

    append_parser = subparsers.add_parser("append", help="向已投稿稿件追加后续分P")
    append_parser.add_argument("prepared_dir", type=Path)
    append_parser.add_argument("--vid", help="稿件 BV/AV 号；默认读取 manifest")
    append_parser.add_argument("--parts", type=int, help="本次最多追加 N 个分P")
    append_parser.add_argument("--line", help="指定 biliup 上传线路，例如 cnbldsa")
    append_parser.set_defaults(handler=append)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, sqlite3.Error, zipfile.BadZipFile) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
