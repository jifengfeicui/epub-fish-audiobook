from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from backend.alexandria.db.repository import Repository
from backend.alexandria.services.stages import StageCancelled


BILIUP_VERSION = "1.2.4"
BILIUP_URL = "https://github.com/biliup/biliup/releases/download/v1.2.4/biliupR-v1.2.4-x86_64-windows.zip"
BILIUP_SHA256 = "cb5af47aeaffd63719c94fa354a4d1404dd8437b6cc215513ec4e6054177c93e"
PICSUM_URL = "https://picsum.photos/1920/1080"
DEFAULT_TAGS = "有声书,读书,知识分享,AI配音"
TV_APP_KEY = "4409e2ce8ffd12b8"
TV_APP_SECRET = "59b43e04ad6965f34319062b478f83dd"
QR_CREATE_URL = "https://passport.bilibili.com/x/passport-tv-login/qrcode/auth_code"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-tv-login/qrcode/poll"
ACCOUNT_URL = "https://api.bilibili.com/x/web-interface/nav"
STUDIO_URL = "https://member.bilibili.com/x/client/archive/view"
EDIT_URL = "https://member.bilibili.com/x/vu/web/edit"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return value[:100] or "chapter"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _signed(values: dict[str, Any]) -> dict[str, str]:
    data = {key: str(value) for key, value in values.items()}
    raw = "&".join(f"{key}={data[key]}" for key in sorted(data))
    data["sign"] = hashlib.md5((raw + TV_APP_SECRET).encode()).hexdigest()
    return data


class BilibiliService:
    def __init__(self, repository: Repository):
        self.repo = repository
        self.cookie_path = repository.storage_root / "secrets" / "bilibili-cookies.json"
        self.tools_dir = repository.storage_root / "tools" / "biliup" / BILIUP_VERSION
        self.login_sessions: dict[str, dict[str, Any]] = {}

    def _root(self, project_id: str) -> Path:
        return self.repo.storage_root / "projects" / project_id / "bilibili"

    def _manifest_path(self, project_id: str) -> Path:
        return self._root(project_id) / "manifest.json"

    def load_manifest(self, project_id: str) -> dict[str, Any]:
        return _load_json(self._manifest_path(project_id))

    def save_manifest(self, project_id: str, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = _now()
        _atomic_json(self._manifest_path(project_id), manifest)

    def _default_form(self, project: dict[str, Any]) -> dict[str, Any]:
        return {
            "author": "",
            "publisher": "",
            "source": "",
            "title": f"《{project['title']}》AI有声书",
            "tid": 201,
            "tags": DEFAULT_TAGS,
            "desc": f"《{project['title']}》AI有声书。\n按章节分P，音频由TTS生成。",
            "visibility": "only_self",
            "line": "cnbldsa",
        }

    def _continuous_audio(self, project_id: str) -> list[dict[str, Any]]:
        chapters = self.repo.list_included_chapters(project_id)
        artifacts: dict[int, dict[str, Any]] = {}
        for artifact in self.repo.list_artifacts(project_id):
            if artifact["kind"] == "chapter_mp3" and artifact["chapter_id"] is not None:
                artifacts.setdefault(artifact["chapter_id"], artifact)
        result: list[dict[str, Any]] = []
        root = self.repo.storage_root.resolve()
        for chapter in chapters:
            artifact = artifacts.get(chapter["id"])
            if artifact is None:
                break
            path = (root / artifact["path"]).resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"artifact_path_escape:{artifact['id']}") from exc
            result.append({**chapter, "position": chapter["included_position"], "artifact": artifact, "audio": path})
        return result

    @staticmethod
    def _probe(path: Path, ffprobe: str) -> dict[str, Any]:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode:
            raise ValueError(f"媒体探测失败: {path}: {result.stderr.strip()}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(f"媒体探测结果无效: {path}") from exc

    @classmethod
    def _probe_audio(cls, path: Path, ffprobe: str) -> float:
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f"音频不存在或为空: {path}")
        data = cls._probe(path, ffprobe)
        if not any(stream.get("codec_type") == "audio" for stream in data.get("streams", [])):
            raise ValueError(f"没有有效音轨: {path}")
        try:
            duration = float(data["format"]["duration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"无法读取音频时长: {path}") from exc
        if duration <= 0:
            raise ValueError(f"音频时长必须大于0: {path}")
        return duration

    @classmethod
    def _validate_video(cls, path: Path, source_duration: float, ffprobe: str) -> float:
        data = cls._probe(path, ffprobe)
        streams = data.get("streams", [])
        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        if not video or video.get("codec_name") != "h264" or video.get("width") != 1920 or video.get("height") != 1080:
            raise ValueError(f"视频必须是1920x1080 H.264: {path}")
        if not audio or audio.get("codec_name") != "aac":
            raise ValueError(f"视频音轨必须是AAC: {path}")
        try:
            duration = float(data["format"]["duration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"无法读取视频时长: {path}") from exc
        if duration <= 0 or abs(duration - source_duration) > 1:
            raise ValueError(f"视频与音频时长不一致: {path}")
        return duration

    def _download_image(self, destination: Path) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)
        url = f"{PICSUM_URL}?random={uuid.uuid4().hex}"
        last_error: Exception | None = None
        for attempt in range(1, 4):
            temporary = destination.with_suffix(".part.jpg")
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "Alexandria/1.0"})
                with urllib.request.urlopen(request, timeout=45) as response, temporary.open("wb") as output:
                    if not response.headers.get_content_type().startswith("image/"):
                        raise ValueError("Picsum返回了非图片内容")
                    shutil.copyfileobj(response, output)
                    final_url = response.geturl()
                if not temporary.stat().st_size:
                    raise ValueError("Picsum返回了空图片")
                os.replace(temporary, destination)
                return final_url
            except Exception as exc:
                last_error = exc
                temporary.unlink(missing_ok=True)
                if attempt < 3:
                    time.sleep(attempt)
        raise RuntimeError(f"图片下载失败（已重试3次）: {last_error}")

    def _secret_values(self) -> list[str]:
        value = _load_json(self.cookie_path)
        secrets = [str(item.get("value", "")) for item in value.get("cookie_info", {}).get("cookies", [])]
        secrets.extend(str(value.get("token_info", {}).get(key, "")) for key in ("access_token", "refresh_token"))
        return [item for item in secrets if item]

    def _redact(self, text: str) -> str:
        for secret in self._secret_values():
            text = text.replace(secret, "***")
        return text

    def _run_process(
        self,
        command: list[str],
        log_path: Path,
        log: Callable[[str], None],
        should_stop: Callable[[], bool],
    ) -> str:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        output_queue: queue.Queue[str | None] = queue.Queue()
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                output_queue.put(line)
            output_queue.put(None)

        threading.Thread(target=read_output, daemon=True).start()
        with log_path.open("a", encoding="utf-8") as output:
            finished = False
            while not finished:
                if should_stop():
                    process.terminate()
                    try:
                        process.wait(5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise StageCancelled("B站任务已暂停")
                try:
                    line = output_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    finished = True
                    continue
                safe = self._redact(line.rstrip())
                output.write(safe + "\n")
                output.flush()
                lines.append(safe)
                log(safe)
        return_code = process.wait()
        if return_code:
            raise RuntimeError(f"biliup操作失败，退出码{return_code}；可重试，日志保存在{log_path}")
        return "\n".join(lines)

    def _render_video(
        self,
        image: Path,
        audio: Path,
        output: Path,
        title: str,
        ffmpeg: str,
        log: Callable[[str], None],
        should_stop: Callable[[], bool],
    ) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".part.mp4")
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-framerate", "1", "-i", str(image),
            "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-vf",
            "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1,fps=25",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", "-metadata", f"title={title}", str(temporary),
        ]
        try:
            project_id = self._project_id_from_path(output)
            self._run_process(command, self._root(project_id) / "last-prepare.log", log, should_stop)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        os.replace(temporary, output)

    def _project_id_from_path(self, path: Path) -> str:
        relative = path.resolve().relative_to((self.repo.storage_root / "projects").resolve())
        return relative.parts[0]

    def prepare(self, project_id: str, log: Callable[[str], None], should_stop: Callable[[], bool]) -> None:
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            raise RuntimeError("PATH中必须同时存在ffmpeg和ffprobe")
        project = self.repo.get_project(project_id)
        chapters = self._continuous_audio(project_id)
        if not chapters:
            raise RuntimeError("bilibili_no_continuous_audio")

        errors: list[str] = []
        for chapter in chapters:
            try:
                chapter["duration"] = self._probe_audio(chapter["audio"], ffprobe)
                chapter["source_sha256"] = _sha256(chapter["audio"])
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
        if errors:
            raise ValueError("音频预检失败，尚未生成任何视频:\n- " + "\n- ".join(errors))

        root = self._root(project_id)
        previous = self.load_manifest(project_id)
        form = {**self._default_form(project), **previous.get("form", {})}
        manifest = {
            "version": 1,
            "status": "preparing",
            "book": {"project_id": project_id, "title": project["title"]},
            "video": {"width": 1920, "height": 1080, "video_codec": "h264", "audio_codec": "aac", "fps": 25},
            "form": form,
            "chapters": [item for item in previous.get("chapters", []) if isinstance(item, dict)],
        }
        if previous.get("publication"):
            manifest["publication"] = previous["publication"]
        if previous.get("pending"):
            manifest["pending"] = previous["pending"]
        old_by_id = {item.get("chapter_id"): item for item in manifest["chapters"]}
        records: dict[int, dict[str, Any]] = {}
        for index, chapter in enumerate(chapters, 1):
            if should_stop():
                raise StageCancelled("B站任务已暂停")
            old = old_by_id.get(chapter["id"], {})
            stem = _safe_name(f"{chapter['position']:04d}-{chapter['title']}")
            image = root / "images" / f"{chapter['id']}.jpg"
            video = root / "videos" / f"{stem}.mp4"
            image_url = old.get("image_url", "")
            if not image.is_file() or not image.stat().st_size:
                log(f"[{index}/{len(chapters)}] 下载章节图片: {chapter['title']}")
                image_url = self._download_image(image)
            reusable = old.get("source_sha256") == chapter["source_sha256"] and video.is_file()
            if reusable:
                try:
                    video_duration = self._validate_video(video, chapter["duration"], ffprobe)
                except (OSError, ValueError):
                    reusable = False
            if reusable:
                log(f"[{index}/{len(chapters)}] 复用章节视频: {chapter['title']}")
            else:
                log(f"[{index}/{len(chapters)}] 生成章节视频: {chapter['title']}")
                self._render_video(image, chapter["audio"], video, stem, ffmpeg, log, should_stop)
                video_duration = self._validate_video(video, chapter["duration"], ffprobe)
            record = {
                "chapter_id": chapter["id"], "position": chapter["position"], "title": chapter["title"],
                "artifact_id": chapter["artifact"]["id"], "audio_path": chapter["artifact"]["path"],
                "source_sha256": chapter["source_sha256"], "source_duration": round(chapter["duration"], 6),
                "image": image.relative_to(root).as_posix(), "image_url": image_url,
                "video": video.relative_to(root).as_posix(), "video_duration": round(video_duration, 6),
            }
            for key in ("published_sha256", "remote_filename", "remote_cid"):
                if old.get(key) is not None:
                    record[key] = old[key]
            records[chapter["id"]] = record
            manifest["chapters"] = [records.get(item["id"], old_by_id.get(item["id"], {})) for item in chapters[:index]]
            self.save_manifest(project_id, manifest)
        manifest["status"] = "ready"
        manifest["chapters"] = [records[item["id"]] for item in chapters]
        self.save_manifest(project_id, manifest)

    def _cookie_data(self) -> dict[str, Any]:
        data = _load_json(self.cookie_path)
        if not data:
            raise RuntimeError("bilibili_not_logged_in")
        return data

    @staticmethod
    def _cookies(data: dict[str, Any]) -> dict[str, str]:
        return {str(item["name"]): str(item["value"]) for item in data.get("cookie_info", {}).get("cookies", []) if item.get("name") and item.get("value")}

    def account_status(self) -> dict[str, Any]:
        if not self.cookie_path.is_file():
            return {"logged_in": False, "name": None}
        try:
            response = httpx.get(ACCOUNT_URL, cookies=self._cookies(self._cookie_data()), timeout=15)
            response.raise_for_status()
            data = response.json()
            account = data.get("data") or {}
            if data.get("code") == 0 and account.get("isLogin"):
                return {"logged_in": True, "name": str(account.get("uname") or "B站用户")}
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        return {"logged_in": False, "name": None}

    def start_login(self) -> dict[str, Any]:
        timestamp = int(time.time())
        response = httpx.post(QR_CREATE_URL, data=_signed({"appkey": TV_APP_KEY, "local_id": 0, "ts": timestamp}), timeout=20)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"bilibili_login_start_failed:{payload.get('message') or payload.get('code')}")
        data = payload.get("data") or {}
        if not data.get("auth_code") or not data.get("url"):
            raise RuntimeError("bilibili_login_start_failed:invalid_response")
        session_id = uuid.uuid4().hex
        expires_at = time.time() + 180
        self.login_sessions[session_id] = {"auth_code": data["auth_code"], "url": data["url"], "expires_at": expires_at}
        return {"session_id": session_id, "url": data["url"], "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).isoformat()}

    def poll_login(self, session_id: str) -> dict[str, Any]:
        session = self.login_sessions.get(session_id)
        if not session or time.time() >= session["expires_at"]:
            self.login_sessions.pop(session_id, None)
            return {"status": "expired", "name": None}
        values = _signed({"appkey": TV_APP_KEY, "auth_code": session["auth_code"], "local_id": 0, "ts": int(time.time())})
        response = httpx.post(QR_POLL_URL, data=values, timeout=20)
        response.raise_for_status()
        payload = response.json()
        code = payload.get("code")
        if code == 86039:
            return {"status": "pending", "name": None}
        if code in (86038, -3):
            self.login_sessions.pop(session_id, None)
            return {"status": "expired", "name": None}
        if code != 0:
            raise RuntimeError(f"bilibili_login_poll_failed:{payload.get('message') or code}")
        login = payload.get("data") or {}
        if not login.get("cookie_info") or not login.get("token_info"):
            return {"status": "pending", "name": None}
        login["platform"] = "BiliTV"
        _atomic_json(self.cookie_path, login)
        self.login_sessions.pop(session_id, None)
        account = self.account_status()
        return {"status": "success", "name": account.get("name")}

    def _ensure_biliup(self) -> Path:
        executable = self.tools_dir / "biliup.exe"
        if executable.is_file():
            return executable
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="alexandria-biliup-") as temporary_dir:
            archive = Path(temporary_dir) / "biliup.zip"
            request = urllib.request.Request(BILIUP_URL, headers={"User-Agent": "Alexandria/1.0"})
            with urllib.request.urlopen(request, timeout=120) as response, archive.open("wb") as output:
                shutil.copyfileobj(response, output)
            actual = _sha256(archive)
            if actual.lower() != BILIUP_SHA256:
                raise RuntimeError(f"biliup SHA-256不匹配: {actual}")
            with zipfile.ZipFile(archive) as package:
                candidates = [name for name in package.namelist() if Path(name).name.lower() in {"biliup.exe", "biliupr.exe"}]
                if len(candidates) != 1:
                    raise RuntimeError("无法从biliup压缩包中唯一定位程序")
                part = executable.with_suffix(".part.exe")
                with package.open(candidates[0]) as source, part.open("wb") as output:
                    shutil.copyfileobj(source, output)
                os.replace(part, executable)
        return executable

    def _prepared_records(self, project_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        manifest = self.load_manifest(project_id)
        if manifest.get("status") != "ready":
            raise RuntimeError("bilibili_not_prepared")
        return manifest, manifest.get("chapters") or []

    def _validate_records(self, project_id: str, records: list[dict[str, Any]]) -> None:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise RuntimeError("PATH中不存在ffprobe")
        root = self._root(project_id).resolve()
        for record in records:
            audio = (self.repo.storage_root / record["audio_path"]).resolve()
            video = (root / record["video"]).resolve()
            for path, allowed in ((audio, self.repo.storage_root.resolve()), (video, root)):
                try:
                    path.relative_to(allowed)
                except ValueError as exc:
                    raise ValueError("bilibili_media_path_escape") from exc
            if not audio.is_file() or _sha256(audio) != record["source_sha256"]:
                raise RuntimeError(f"bilibili_source_changed:{record['chapter_id']}")
            self._validate_video(video, self._probe_audio(audio, ffprobe), ffprobe)

    def _studio(self, bvid: str) -> dict[str, Any]:
        login = self._cookie_data()
        access_token = login.get("token_info", {}).get("access_token")
        if not access_token:
            raise RuntimeError("bilibili_access_token_missing")
        response = httpx.get(STUDIO_URL, params={"access_key": access_token, "bvid": bvid}, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            raise RuntimeError(f"bilibili_studio_failed:{payload.get('message') or payload.get('code')}")
        data = payload["data"]
        archive = data.get("archive")
        if not isinstance(archive, dict):
            return data
        studio = {**archive, "videos": data.get("videos") if isinstance(data.get("videos"), list) else []}
        studio.pop("limited_free", None)
        return studio

    @staticmethod
    def _remote_videos(studio: dict[str, Any]) -> list[dict[str, Any]]:
        videos = studio.get("videos")
        return videos if isinstance(videos, list) else []

    def _sync_remote(self, manifest: dict[str, Any]) -> None:
        publication = manifest.get("publication") or {}
        bvid = publication.get("bvid")
        if not bvid:
            return
        studio = self._studio(bvid)
        remote = self._remote_videos(studio)
        publication["last_sync_at"] = _now()
        publication["remote_title"] = studio.get("title")
        publication["remote_visibility"] = "only_self" if studio.get("is_only_self") in (1, True) else "public"
        publication["remote_count"] = len(remote)
        publication.pop("sync_error", None)
        chapters = manifest.get("chapters") or []
        published_count = 0
        for item in chapters:
            if not item.get("published_sha256"):
                break
            published_count += 1
        expected_visibility = manifest.get("form", {}).get("visibility", publication.get("visibility"))
        mismatch = (
            len(remote) != published_count
            or bool(publication.get("title") and studio.get("title") != publication.get("title"))
            or bool(expected_visibility and publication["remote_visibility"] != expected_visibility)
        )
        for index, record in enumerate(chapters):
            if not record.get("published_sha256"):
                record.pop("remote_mismatch", None)
                continue
            current = remote[index] if index < len(remote) else {}
            expected = record.get("remote_filename")
            record["remote_mismatch"] = not current or bool(expected and current.get("filename") != expected)
            mismatch = mismatch or record["remote_mismatch"]
            if current and not expected:
                record["remote_filename"] = current.get("filename")
                record["remote_cid"] = current.get("cid")
            elif current and not record["remote_mismatch"]:
                record["remote_cid"] = current.get("cid")
        publication["remote_mismatch"] = mismatch
        manifest["publication"] = publication

    def status(self, project_id: str, *, sync_remote: bool = True) -> dict[str, Any]:
        project = self.repo.get_project(project_id)
        continuous = self._continuous_audio(project_id)
        audio_by_id = {item["id"]: item for item in continuous}
        manifest = self.load_manifest(project_id)
        if not manifest:
            manifest = {"version": 1, "book": {"project_id": project_id, "title": project["title"]}, "form": self._default_form(project), "chapters": []}
        if sync_remote and manifest.get("publication", {}).get("bvid"):
            try:
                self._sync_remote(manifest)
            except Exception as exc:
                publication = manifest.setdefault("publication", {})
                publication["sync_error"] = str(exc)
            self.save_manifest(project_id, manifest)
        records = {item.get("chapter_id"): item for item in manifest.get("chapters", [])}
        chapter_states = []
        prepared_count = published_count = 0
        root = self._root(project_id)
        for chapter in self.repo.list_included_chapters(project_id):
            audio = audio_by_id.get(chapter["id"])
            record = records.get(chapter["id"])
            if audio is None:
                state = "missing"
            elif not record or not (root / str(record.get("video", ""))).is_file():
                state = "missing"
            elif record.get("source_sha256") != audio["artifact"]["sha256"]:
                state = "outdated" if record.get("published_sha256") else "missing"
            elif record.get("remote_mismatch"):
                state = "remote_mismatch"
            elif record.get("published_sha256") == record.get("source_sha256"):
                state = "published"
            else:
                state = "ready"
            chapter_states.append({**chapter, "bilibili_state": state, "image_url": f"/api/v1/projects/{project_id}/bilibili/media/{chapter['id']}/image" if record else None, "video_url": f"/api/v1/projects/{project_id}/bilibili/media/{chapter['id']}/video" if record else None})
        for item in chapter_states:
            if item["bilibili_state"] not in ("ready", "published", "remote_mismatch"):
                break
            prepared_count += 1
        for record in manifest.get("chapters", []):
            if not record.get("published_sha256"):
                break
            published_count += 1
        appendable = 0
        for item in chapter_states[published_count:]:
            if item["bilibili_state"] != "ready":
                break
            appendable += 1
        return {
            "continuous_audio_count": len(continuous), "prepared_count": prepared_count, "published_count": published_count,
            "appendable_count": appendable, "form": {**self._default_form(project), **manifest.get("form", {})},
            "publication": manifest.get("publication"), "chapters": chapter_states,
        }

    def media_path(self, project_id: str, chapter_id: int, kind: str) -> Path:
        if kind not in ("image", "video"):
            raise KeyError(kind)
        manifest = self.load_manifest(project_id)
        record = next((item for item in manifest.get("chapters", []) if item.get("chapter_id") == chapter_id), None)
        if not record:
            raise KeyError(chapter_id)
        root = self._root(project_id).resolve()
        path = (root / record[kind]).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("bilibili_media_path_escape") from exc
        if not path.is_file():
            raise KeyError(path)
        return path

    def _apply_remote_mapping(self, manifest: dict[str, Any], studio: dict[str, Any], count: int) -> None:
        remote = self._remote_videos(studio)
        if len(remote) < count:
            raise RuntimeError(f"bilibili_remote_count_mismatch:{len(remote)}/{count}")
        for record, item in zip(manifest["chapters"][:count], remote[:count]):
            record["published_sha256"] = record["source_sha256"]
            record["remote_filename"] = item.get("filename")
            record["remote_cid"] = item.get("cid")
            record.pop("remote_mismatch", None)

    @staticmethod
    def _remote_prefix_matches(records: list[dict[str, Any]], remote: list[dict[str, Any]], count: int) -> bool:
        if len(remote) < count:
            return False
        return all(
            not record.get("remote_filename") or record["remote_filename"] == remote[index].get("filename")
            for index, record in enumerate(records[:count])
        )

    def _publication_id(self, output: str) -> tuple[str, str | None]:
        bvids = re.findall(r"BV[0-9A-Za-z]{10}", output)
        aids = re.findall(r"\bav(\d+)\b", output, re.IGNORECASE) + re.findall(r'"aid"\s*:\s*(?:Number\()?([0-9]+)', output)
        if not bvids:
            raise RuntimeError("bilibili_upload_succeeded_without_bvid")
        return bvids[0], aids[0] if aids else None

    def publish(self, project_id: str, payload: dict[str, Any], log: Callable[[str], None], should_stop: Callable[[], bool]) -> None:
        self._cookie_data()
        manifest, records = self._prepared_records(project_id)
        parts = int(payload["parts"])
        if parts < 1 or parts > len(records):
            raise ValueError("bilibili_invalid_parts")
        pending = manifest.get("pending")
        publication = manifest.get("publication") or {}
        if publication.get("bvid"):
            if not pending or pending.get("action") != "publish" or pending.get("parts") != parts:
                raise RuntimeError("bilibili_already_published")
            studio = self._studio(publication["bvid"])
            if len(self._remote_videos(studio)) != parts:
                raise RuntimeError("bilibili_publish_remote_state_ambiguous")
            self._apply_remote_mapping(manifest, studio, parts)
            manifest.pop("pending", None)
            self._sync_remote(manifest)
            self.save_manifest(project_id, manifest)
            return
        if pending and (pending.get("action") != "publish" or pending.get("parts") != parts):
            raise RuntimeError("bilibili_pending_operation_ambiguous")
        self._validate_records(project_id, records[:parts])
        if payload.get("visibility") == "public" and not payload.get("confirm_public"):
            raise ValueError("bilibili_public_confirmation_required")
        if not str(payload.get("source", "")).strip():
            raise ValueError("bilibili_source_required")
        project = self.repo.get_project(project_id)
        defaults = self._default_form(project)
        form = {**defaults, **payload}
        default_desc = defaults["desc"]
        if not str(form.get("desc", "")).strip() or form["desc"] == default_desc:
            lines = [f"《{project['title']}》AI有声书。"]
            if form.get("author"):
                lines.append(f"作者：{form['author']}")
            if form.get("publisher"):
                lines.append(f"出版社：{form['publisher']}")
            lines.append("按章节分P，音频由TTS生成。")
            form["desc"] = "\n".join(lines)
        manifest["form"] = {key: form[key] for key in ("author", "publisher", "source", "title", "tid", "tags", "desc", "visibility", "line")}
        if not pending:
            manifest["pending"] = {"action": "publish", "parts": parts, "started_at": _now()}
            self.save_manifest(project_id, manifest)
        root = self._root(project_id)
        videos = [str((root / record["video"]).resolve()) for record in records[:parts]]
        command = [str(self._ensure_biliup()), "-u", str(self.cookie_path), "upload", *videos, "--title", form["title"], "--tid", str(form["tid"]), "--tag", form["tags"], "--desc", form["desc"], "--cover", str((root / records[0]["image"]).resolve()), "--copyright", "2", "--source", form["source"], "--line", form.get("line") or "cnbldsa"]
        if form["visibility"] != "public":
            command += ["--is-only-self", "1"]
        output = self._run_process(command, root / "last-upload.log", log, should_stop)
        bvid, aid = self._publication_id(output)
        publication = {"bvid": bvid, "aid": aid, "published_at": _now(), "visibility": form["visibility"], "title": form["title"]}
        manifest["publication"] = publication
        self.save_manifest(project_id, manifest)
        studio = self._studio(bvid)
        if len(self._remote_videos(studio)) != parts:
            raise RuntimeError("bilibili_publish_remote_state_ambiguous")
        self._apply_remote_mapping(manifest, studio, parts)
        manifest.pop("pending", None)
        self._sync_remote(manifest)
        self.save_manifest(project_id, manifest)

    def append(self, project_id: str, payload: dict[str, Any], log: Callable[[str], None], should_stop: Callable[[], bool]) -> None:
        manifest, records = self._prepared_records(project_id)
        publication = manifest.get("publication") or {}
        bvid = publication.get("bvid")
        if not bvid:
            raise RuntimeError("bilibili_not_published")
        published = next((index for index, item in enumerate(records) if not item.get("published_sha256")), len(records))
        if any(item.get("published_sha256") and item.get("published_sha256") != item.get("source_sha256") for item in records[:published]):
            raise RuntimeError("bilibili_replace_outdated_first")
        parts = int(payload["parts"])
        remaining = records[published:]
        if parts < 1 or parts > len(remaining):
            raise ValueError("bilibili_invalid_parts")
        pending = manifest.get("pending")
        if pending and (
            pending.get("action") != "append"
            or pending.get("parts") != parts
            or pending.get("published_before") != published
        ):
            raise RuntimeError("bilibili_pending_operation_ambiguous")
        self._validate_records(project_id, remaining[:parts])
        studio = self._studio(bvid)
        remote = self._remote_videos(studio)
        if not self._remote_prefix_matches(records, remote, published):
            raise RuntimeError("bilibili_append_remote_state_ambiguous")
        if pending and len(remote) == published + parts:
            self._apply_remote_mapping(manifest, studio, published + parts)
            manifest.pop("pending", None)
            self._sync_remote(manifest)
            self.save_manifest(project_id, manifest)
            return
        if len(remote) != published:
            raise RuntimeError("bilibili_append_remote_state_ambiguous")
        if not pending:
            manifest["pending"] = {"action": "append", "parts": parts, "published_before": published, "started_at": _now()}
            self.save_manifest(project_id, manifest)
        root = self._root(project_id)
        videos = [str((root / item["video"]).resolve()) for item in remaining[:parts]]
        command = [str(self._ensure_biliup()), "-u", str(self.cookie_path), "append", "--vid", bvid, *videos, "--line", payload.get("line") or "cnbldsa"]
        self._run_process(command, root / "last-append.log", log, should_stop)
        studio = self._studio(bvid)
        if len(self._remote_videos(studio)) != published + parts:
            raise RuntimeError("bilibili_append_remote_state_ambiguous")
        self._apply_remote_mapping(manifest, studio, published + parts)
        manifest.pop("pending", None)
        self._sync_remote(manifest)
        self.save_manifest(project_id, manifest)

    def _edit_studio(self, studio: dict[str, Any]) -> None:
        login = self._cookie_data()
        cookies = self._cookies(login)
        csrf = cookies.get("bili_jct")
        if not csrf:
            raise RuntimeError("bilibili_csrf_missing")
        response = httpx.post(EDIT_URL, params={"t": int(time.time() * 1000), "csrf": csrf}, cookies=cookies, json=studio, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"bilibili_edit_failed:{payload.get('message') or payload.get('code')}")

    def replace(self, project_id: str, payload: dict[str, Any], log: Callable[[str], None], should_stop: Callable[[], bool]) -> None:
        manifest, records = self._prepared_records(project_id)
        publication = manifest.get("publication") or {}
        bvid = publication.get("bvid")
        if not bvid:
            raise RuntimeError("bilibili_not_published")
        chapter_id = int(payload["chapter_id"])
        index = next((index for index, item in enumerate(records) if item["chapter_id"] == chapter_id), -1)
        if index < 0 or not records[index].get("published_sha256"):
            raise ValueError("bilibili_chapter_not_published")
        target = records[index]
        self._validate_records(project_id, [target])
        pending = manifest.get("pending")
        if pending and (pending.get("action") != "replace" or pending.get("chapter_id") != chapter_id):
            raise RuntimeError("bilibili_pending_operation_ambiguous")
        if pending and pending.get("source_sha256") != target["source_sha256"]:
            raise RuntimeError("bilibili_pending_operation_ambiguous")

        published = next((index for index, item in enumerate(records) if not item.get("published_sha256")), len(records))
        studio = self._studio(bvid)
        remote = self._remote_videos(studio)
        appended: dict[str, Any] | None = None
        if not self._remote_prefix_matches(records, remote, published):
            raise RuntimeError("bilibili_replace_remote_state_ambiguous")
        if pending and len(remote) == published + 1:
            candidate = remote[-1]
            known = {item.get("filename") for item in remote[:published]}
            if candidate.get("filename") not in known:
                appended = candidate
        elif len(remote) != published:
            raise RuntimeError("bilibili_replace_remote_state_ambiguous")
        if not pending:
            pending = {"action": "replace", "chapter_id": chapter_id, "source_sha256": target["source_sha256"], "old_filename": target.get("remote_filename"), "started_at": _now()}
            manifest["pending"] = pending
            self.save_manifest(project_id, manifest)
        if appended is None:
            root = self._root(project_id)
            video = str((root / target["video"]).resolve())
            command = [str(self._ensure_biliup()), "-u", str(self.cookie_path), "append", "--vid", bvid, video, "--line", payload.get("line") or "cnbldsa"]
            self._run_process(command, root / "last-replace.log", log, should_stop)
            studio = self._studio(bvid)
            remote = self._remote_videos(studio)
            if len(remote) != published + 1 or not self._remote_prefix_matches(records, remote, published):
                raise RuntimeError("bilibili_replace_append_not_found")
            appended = remote[-1]
        desired = remote[:published]
        desired[index] = appended
        studio["videos"] = desired
        self._edit_studio(studio)
        verified = self._studio(bvid)
        current = self._remote_videos(verified)
        expected_filenames = [item.get("filename") for item in desired]
        if len(current) != published or [item.get("filename") for item in current] != expected_filenames:
            raise RuntimeError("bilibili_replace_verification_failed")
        target["published_sha256"] = target["source_sha256"]
        target["remote_filename"] = current[index].get("filename")
        target["remote_cid"] = current[index].get("cid")
        manifest.pop("pending", None)
        self._sync_remote(manifest)
        self.save_manifest(project_id, manifest)

    def run(self, job: dict[str, Any], log: Callable[[str], None], should_stop: Callable[[], bool]) -> None:
        action = job.get("payload", {}).get("action")
        handler = {"prepare": self.prepare, "publish": self.publish, "append": self.append, "replace": self.replace}.get(action)
        if handler is None:
            raise ValueError("Invalid B站 job action")
        if action == "prepare":
            handler(job["project_id"], log, should_stop)
        else:
            handler(job["project_id"], job["payload"], log, should_stop)
