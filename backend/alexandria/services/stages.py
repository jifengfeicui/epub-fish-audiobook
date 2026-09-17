from __future__ import annotations

import copy
import hashlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable

from backend.alexandria.db.repository import Repository


LogCallback = Callable[[str], None]


class StageCancelled(RuntimeError):
    pass


def _slug(value: str) -> str:
    import re

    value = re.sub(r"[\\/:*?\"<>|]", "_", value).strip(" .")
    return value[:100] or "chapter"


class StageExecutor:
    """Runs existing Alexandria and Fish logic while SQLite owns persistent state."""

    def __init__(self, repository: Repository, root: Path):
        self.repo = repository
        self.root = root.resolve()

    def _run(self, command: list[str], log: LogCallback, should_stop: Callable[[], bool] = lambda: False) -> None:
        process = subprocess.Popen(
            command,
            cwd=self.root,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        lines: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            for line in process.stdout:
                lines.put(line)
            lines.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        output: list[str] = []
        while True:
            if should_stop():
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                reader.join(timeout=1)
                process.stdout.close()
                raise StageCancelled("Stage cancelled")
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                break
            line = line.strip()
            if line:
                output.append(line)
                log(line)
        code = process.wait()
        reader.join(timeout=1)
        process.stdout.close()
        if code:
            detail = next((line for line in reversed(output) if "Error" in line), output[-1] if output else "")
            raise RuntimeError(detail or f"Stage process failed with exit code {code}")

    def _config(self) -> dict[str, Any]:
        settings = self.repo.get_settings(reveal_secrets=True)
        return {
            "llm": {
                "base_url": settings.get("llm_base_url", "http://localhost:1234/v1"),
                "api_key": settings.get("llm_api_key", "local"),
                "model_name": settings.get("llm_model", "local-model"),
            },
            "generation": settings.get("generation", {}),
            "prompts": settings.get("prompts", {}),
        }

    @staticmethod
    def _file_hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _input_hash(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def generate_input_hash(self, chapter_id: int, project: dict[str, Any]) -> str:
        chapter = self.repo.get_chapter_record(chapter_id)
        app_config = self._config()
        project_settings = project["settings"]
        return self._input_hash({
            "source_hash": chapter.source_hash,
            "settings": {
                "first_person_speaker": project_settings.get("first_person_speaker"),
                "single_speaker": project_settings.get("single_speaker"),
                "speaker_name": project_settings.get("speaker_name"),
                "instruct": project_settings.get("instruct"),
            },
            "llm": app_config["llm"],
            "generation": app_config["generation"],
            "prompts": {
                "system_prompt": app_config["prompts"].get("system_prompt"),
                "user_prompt": app_config["prompts"].get("user_prompt"),
            },
            "generate_script": self._file_hash(self.root / "app" / "generate_script.py"),
            "script_validation": self._file_hash(self.root / "app" / "script_validation.py"),
            "default_prompts": self._file_hash(self.root / "default_prompts.txt"),
        })

    def review_input_hash(self, generated: dict[str, Any], project: dict[str, Any]) -> str:
        app_config = self._config()
        project_settings = project["settings"]
        return self._input_hash({
            "generated_hash": generated["content_hash"],
            "settings": {
                "first_person_speaker": project_settings.get("first_person_speaker"),
                "context_window": project_settings.get("context_window"),
            },
            "llm": app_config["llm"],
            "generation": app_config["generation"],
            "prompts": {
                "review_system_prompt": app_config["prompts"].get("review_system_prompt"),
                "review_user_prompt": app_config["prompts"].get("review_user_prompt"),
            },
            "review_script": self._file_hash(self.root / "app" / "review_script.py"),
            "script_validation": self._file_hash(self.root / "app" / "script_validation.py"),
            "default_prompts": self._file_hash(self.root / "review_prompts.txt"),
        })

    def generate(self, chapter_id: int, project: dict[str, Any], log: LogCallback, should_stop: Callable[[], bool] = lambda: False) -> dict[str, Any]:
        chapter = self.repo.get_chapter_record(chapter_id)
        project_dir = self.repo.storage_root / "projects" / project["id"]
        work_root = project_dir / "work"
        work_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"generate-{chapter.position:04d}-", dir=work_root) as temp_name:
            temp = Path(temp_name)
            source = temp / "source.txt"
            output = temp / "generated.json"
            config = temp / "config.json"
            source.write_text(chapter.source_text, encoding="utf-8")
            config.write_text(json.dumps(self._config(), ensure_ascii=False), encoding="utf-8")
            command = [
                sys.executable,
                "-u",
                str(self.root / "app" / "generate_script.py"),
                str(source),
                "--output",
                str(output),
                "--config",
                str(config),
            ]
            settings = project["settings"]
            if settings.get("single_speaker"):
                command += [
                    "--single-speaker",
                    "--speaker-name",
                    settings.get("speaker_name") or "NARRATOR",
                    "--instruct",
                    settings.get("instruct") or "Neutral narration.",
                ]
            elif settings.get("first_person_speaker"):
                command += ["--first-person-speaker", settings["first_person_speaker"]]
            else:
                command += ["--no-first-person-speaker"]
            self._run(command, log, should_stop)
            entries = json.loads(output.read_text(encoding="utf-8"))
        return self.repo.save_revision(chapter_id, "generated", entries, self.generate_input_hash(chapter_id, project))

    def review(self, chapter_id: int, project: dict[str, Any], generated: dict[str, Any], log: LogCallback, should_stop: Callable[[], bool] = lambda: False) -> dict[str, Any]:
        chapter = self.repo.get_chapter_record(chapter_id)
        project_dir = self.repo.storage_root / "projects" / project["id"]
        work_root = project_dir / "work"
        work_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"review-{chapter.position:04d}-", dir=work_root) as temp_name:
            temp = Path(temp_name)
            source = temp / "source.txt"
            input_path = temp / "generated.json"
            output = temp / "reviewed.json"
            config = temp / "config.json"
            source.write_text(chapter.source_text, encoding="utf-8")
            input_path.write_text(json.dumps(generated["entries"], ensure_ascii=False), encoding="utf-8")
            config.write_text(json.dumps(self._config(), ensure_ascii=False), encoding="utf-8")
            command = [
                sys.executable,
                "-u",
                str(self.root / "app" / "review_script.py"),
                "--script",
                str(input_path),
                "--output",
                str(output),
                "--source",
                str(source),
                "--config",
                str(config),
            ]
            settings = project["settings"]
            if settings.get("context_window"):
                command += ["--context-window", str(settings["context_window"])]
            if settings.get("first_person_speaker"):
                command += ["--first-person-speaker", settings["first_person_speaker"]]
            else:
                command += ["--no-first-person-speaker"]
            self._run(command, log, should_stop)
            entries = json.loads(output.read_text(encoding="utf-8"))
        return self.repo.save_revision(chapter_id, "reviewed", entries, self.review_input_hash(generated, project))

    def analyze_characters(self, project: dict[str, Any]) -> list[dict[str, Any]]:
        from openai import OpenAI

        characters = self.repo.refresh_characters(project["id"])
        pending = [row for row in characters if not row["user_edited"] and (not row["gender"] or not row["personality"])]
        if not pending:
            return characters
        samples = self.repo.character_samples(project["id"], [row["speaker"] for row in pending])
        settings = self.repo.get_settings(reveal_secrets=True)
        client = OpenAI(
            base_url=settings.get("llm_base_url", "http://localhost:1234/v1"),
            api_key=settings.get("llm_api_key", "local"),
            timeout=60,
        )
        payload = [{"speaker": row["speaker"], "lines": samples[row["speaker"]]} for row in pending]
        def metadata_item(speaker: str) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    "speaker": {"const": speaker},
                    "gender": {"type": "string", "minLength": 1},
                    "personality": {"type": "string", "minLength": 1},
                },
                "required": ["speaker", "gender", "personality"],
                "additionalProperties": False,
            }
        response = client.chat.completions.create(
            model=settings.get("llm_model", "local-model"),
            messages=[
                {"role": "system", "content": "Return only a JSON array. Infer audiobook character metadata from quoted lines. Use 未知 when gender cannot be inferred."},
                {"role": "user", "content": "For every character return speaker, gender, and one concise Chinese personality sentence. Keep speaker exact; do not omit or add characters.\n" + json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.2,
            max_tokens=4096,
            reasoning_effort="none",
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "character_metadata",
                    "strict": True,
                    "schema": {
                        "type": "array",
                        "prefixItems": [metadata_item(row["speaker"]) for row in pending],
                        "minItems": len(pending),
                        "maxItems": len(pending),
                    },
                },
            },
        )
        text = (response.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise ValueError("Character analysis did not return a JSON array")
        allowed = [row["speaker"] for row in pending]
        if any(not isinstance(row, dict) for row in parsed):
            raise ValueError("Character analysis contains a non-object entry")
        speakers = [row.get("speaker") for row in parsed]
        if len(speakers) != len(set(speakers)) or set(speakers) != set(allowed):
            raise ValueError("Character analysis must return every requested speaker exactly once")
        updates = []
        for row in parsed:
            gender = row.get("gender")
            personality = row.get("personality")
            if not isinstance(gender, str) or not gender.strip() or not isinstance(personality, str) or not personality.strip():
                raise ValueError(f"Character analysis is incomplete for {row.get('speaker', '')}")
            updates.append({"speaker": row["speaker"], "gender": gender, "personality": personality})
        return self.repo.update_characters(project["id"], updates, automated=True)

    def render(
        self,
        chapter_id: int,
        project: dict[str, Any],
        log: LogCallback,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> Path:
        from fish_adapter.cache import fingerprint_entry
        from fish_adapter.emotion_mapper import build_fish_text
        from fish_adapter.fish_adapter import DEFAULT_CONFIG, _write_audio_atomic
        from fish_adapter.renderer import FishRenderer
        from tools.render_book import merge_mp3

        chapter = self.repo.get_chapter_record(chapter_id)
        script = self.repo.get_script(chapter_id)
        if not script["entries"]:
            raise RuntimeError(f"Chapter {chapter.position} has no reviewed script")
        speakers = list(dict.fromkeys(entry["speaker"] for entry in script["entries"]))
        voices = self.repo.assign_voices(project["id"], speakers)
        settings = self.repo.get_settings(reveal_secrets=True)
        api_key = settings.get("fish_api_key", "")
        if not api_key:
            raise RuntimeError("Fish API key is not configured")
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["api"]["base_url"] = settings.get("fish_base_url", config["api"]["base_url"])
        config["api"]["model"] = settings.get("fish_model", config["api"]["model"])
        config["tts"].update(settings.get("fish_tts", {}))
        config["render"]["workers"] = settings.get("fish_workers", 5)
        audio_dir = self.repo.storage_root / "projects" / project["id"] / "audio" / f"{chapter.position:04d}"
        audio_dir.mkdir(parents=True, exist_ok=True)
        renderer = FishRenderer(config, api_key)
        aborted = threading.Event()

        def check_stop() -> None:
            if aborted.is_set() or should_stop():
                raise StageCancelled("Stage cancelled")

        def synthesize(entry: dict[str, Any]) -> tuple[int, Path, str, int]:
            check_stop()
            position = int(entry["position"])
            voice_id = voices[entry["speaker"]]["reference_id"]
            fingerprint = fingerprint_entry(entry, voice_id, config["api"]["model"], config["tts"])
            relative = f"projects/{project['id']}/audio/{chapter.position:04d}/{position:06d}.mp3"
            output = self.repo.storage_root / relative
            reusable = self.repo.reusable_audio_segment(chapter_id, position, fingerprint)
            if reusable:
                check_stop()
                log(f"第 {chapter.position} 章片段 {position} 使用缓存")
                return position, self.repo.storage_root / reusable, fingerprint, 0
            try:
                check_stop()
                result = renderer.synthesize(build_fish_text(entry["text"], entry.get("instruct", "")), voice_id)
                check_stop()
                _write_audio_atomic(output, result.audio)
                try:
                    check_stop()
                except StageCancelled:
                    output.unlink(missing_ok=True)
                    raise
                self.repo.save_audio_segment(
                    project_id=project["id"],
                    chapter_id=chapter_id,
                    revision_id=script["revision_id"],
                    position=position,
                    fingerprint=fingerprint,
                    path=relative,
                    status="done",
                    attempts=result.attempts,
                )
            except StageCancelled:
                raise
            except Exception as exc:
                check_stop()
                self.repo.save_audio_segment(
                    project_id=project["id"],
                    chapter_id=chapter_id,
                    revision_id=script["revision_id"],
                    position=position,
                    fingerprint=fingerprint,
                    path=relative,
                    status="failed",
                    attempts=1,
                    error=str(exc),
                )
                raise
            check_stop()
            log(f"第 {chapter.position} 章片段 {position} 已渲染")
            return position, output, fingerprint, result.attempts

        files: dict[int, Path] = {}
        workers = max(1, min(int(settings.get("fish_workers", 5)), 16))
        entries = iter(script["entries"])
        pool = ThreadPoolExecutor(max_workers=workers)
        pending = set()
        try:
            for _ in range(workers):
                check_stop()
                entry = next(entries, None)
                if entry is None:
                    break
                pending.add(pool.submit(synthesize, entry))

            while pending:
                check_stop()
                done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in done:
                    pending.remove(future)
                    position, path, _fingerprint, _attempts = future.result()
                    files[position] = path
                    check_stop()
                    entry = next(entries, None)
                    if entry is not None:
                        pending.add(pool.submit(synthesize, entry))
        except Exception:
            aborted.set()
            for future in pending:
                future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            pool.shutdown()

        check_stop()
        ordered_entries = sorted(script["entries"], key=lambda row: row["position"])
        chapter_output = self.repo.storage_root / "projects" / project["id"] / "output" / f"{chapter.position:04d}-{_slug(chapter.title)}.mp3"
        merge_mp3(
            [files[row["position"]] for row in ordered_entries],
            chapter_output,
            speakers=[row["speaker"] for row in ordered_entries],
        )
        self.repo.save_artifact(project["id"], chapter_id, "chapter_mp3", chapter_output)
        return chapter_output

    def merge_book(self, project: dict[str, Any], log: LogCallback) -> Path:
        from tools.render_book import concat_mp3

        chapters = self.repo.list_chapters(project["id"])
        artifacts = self.repo.list_artifacts(project["id"])
        by_chapter = {row["chapter_id"]: self.repo.storage_root / row["path"] for row in artifacts if row["kind"] == "chapter_mp3"}
        missing = [
            chapter["position"]
            for chapter in chapters
            if chapter["id"] not in by_chapter
            or not by_chapter[chapter["id"]].is_file()
            or not by_chapter[chapter["id"]].stat().st_size
        ]
        if missing:
            raise RuntimeError(f"Cannot merge book; missing chapter audio: {missing}")
        output_dir = self.repo.storage_root / "projects" / project["id"] / "output"
        output = output_dir / f"{_slug(project['title'])}.mp3"
        concat_mp3([by_chapter[chapter["id"]] for chapter in chapters], output)
        self.repo.save_artifact(project["id"], None, "book_mp3", output)
        log("整书 MP3 已合并")
        return output
