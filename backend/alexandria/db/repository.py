from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import joinedload, selectinload

from .database import Database
from .models import (
    AppSetting,
    Artifact,
    AudioSegment,
    Chapter,
    Job,
    JobEvent,
    Project,
    ProjectSettings,
    ProjectVoiceExclusion,
    ScriptEntry,
    ScriptRevision,
    SpeakerAssignment,
    StageRun,
    VoiceProfile,
    utcnow,
)


ACTIVE_JOB_STATES = ("queued", "running", "pausing")
EDITABLE_JOB_STATES = ("paused", "completed")
SECRET_KEYS = {"llm_api_key", "fish_api_key"}


def _hash_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


class Repository:
    def __init__(self, database: Database, storage_root: Path):
        self.db = database
        self.storage_root = storage_root.resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.event_publisher: Callable[[str, dict[str, Any]], None] | None = None
        self._job_lock = threading.Lock()
        self._event_lock = threading.Lock()

    def create_project(
        self,
        *,
        project_id: str | None = None,
        title: str,
        source_filename: str,
        source_path: str,
        source_sha256: str,
        chapters: list[dict[str, Any]],
        render_start_mode: str,
        release_batch_size: int,
        from_chapter: int | None = None,
        to_chapter: int | None = None,
    ) -> dict[str, Any]:
        project_id = project_id or str(uuid.uuid4())
        with self.db.session() as session, session.begin():
            project = Project(
                id=project_id,
                title=title,
                source_filename=source_filename,
                source_path=source_path,
                source_sha256=source_sha256,
                settings=ProjectSettings(
                    render_start_mode=render_start_mode,
                    release_batch_size=release_batch_size,
                    from_chapter=from_chapter,
                    to_chapter=to_chapter,
                ),
            )
            for chapter in chapters:
                project.chapters.append(
                    Chapter(
                        position=int(chapter["index"]),
                        title=str(chapter["title"]),
                        href=str(chapter["href"]),
                        source_text=str(chapter["text"]),
                        source_hash=_hash_json([chapter["href"], chapter["title"], chapter["text"]]),
                    )
                )
            session.add(project)
        return self.get_project(project_id)

    def list_projects(self, archived: bool = False) -> list[dict[str, Any]]:
        with self.db.session() as session:
            projects = session.scalars(
                select(Project)
                .where(Project.archived_at.is_not(None) if archived else Project.archived_at.is_(None))
                .options(joinedload(Project.settings), selectinload(Project.chapters))
                .order_by(Project.updated_at.desc())
            ).unique().all()
            return [self._project_dict(project, self._latest_job(session, project.id)) for project in projects]

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self.db.session() as session:
            project = session.scalar(
                select(Project)
                .where(Project.id == project_id)
                .options(joinedload(Project.settings), selectinload(Project.chapters))
            )
            if project is None:
                raise KeyError(project_id)
            return self._project_dict(project, self._latest_job(session, project_id))

    def _latest_job(self, session, project_id: str) -> Job | None:
        return session.scalar(select(Job).where(Job.project_id == project_id).order_by(Job.created_at.desc()).limit(1))

    def _project_dict(self, project: Project, job: Job | None) -> dict[str, Any]:
        settings = project.settings
        counts: dict[str, int] = {}
        for chapter in project.chapters:
            if not chapter.included:
                continue
            counts[chapter.status] = counts.get(chapter.status, 0) + 1
        return {
            "id": project.id,
            "title": project.title,
            "source_filename": project.source_filename,
            "status": project.status,
            "chapter_count": len(project.chapters),
            "included_chapter_count": sum(chapter.included for chapter in project.chapters),
            "chapter_status_counts": counts,
            "created_at": _iso(project.created_at),
            "updated_at": _iso(project.updated_at),
            "archived_at": _iso(project.archived_at),
            "settings": {
                "render_start_mode": settings.render_start_mode,
                "release_batch_size": settings.release_batch_size,
                "from_chapter": settings.from_chapter,
                "to_chapter": settings.to_chapter,
                "first_person_speaker": settings.first_person_speaker,
                "context_window": settings.context_window,
                "single_speaker": settings.single_speaker,
                "speaker_name": settings.speaker_name,
                "instruct": settings.instruct,
            },
            "latest_job": self.job_dict(job) if job else None,
        }

    def update_project(self, project_id: str, values: dict[str, Any]) -> dict[str, Any]:
        with self.db.session() as session, session.begin():
            project = session.get(Project, project_id)
            if project is None:
                raise KeyError(project_id)
            active = session.scalar(
                select(Job.id).where(Job.project_id == project_id, Job.status.in_(ACTIVE_JOB_STATES)).limit(1)
            )
            if active and any(key != "title" for key in values):
                raise RuntimeError("project_running")
            archived = values.pop("archived", None)
            if archived is not None:
                project.archived_at = utcnow() if archived else None
            if values.get("title") is not None:
                project.title = values.pop("title")
            for key, value in values.items():
                if value is None and key not in ("first_person_speaker", "from_chapter", "to_chapter"):
                    continue
                setattr(project.settings, key, value)
            start = project.settings.from_chapter or 1
            end = project.settings.to_chapter or len(project.chapters)
            if start > end or end > len(project.chapters):
                raise RuntimeError("invalid_chapter_range")
            project.updated_at = utcnow()
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> None:
        with self.db.session() as session, session.begin():
            project = session.get(Project, project_id)
            if project is None:
                raise KeyError(project_id)
            active = session.scalar(
                select(Job.id).where(Job.project_id == project_id, Job.status.in_(ACTIVE_JOB_STATES)).limit(1)
            )
            if active:
                raise RuntimeError("project_running")
            session.delete(project)
        shutil.rmtree(self.storage_root / "projects" / project_id, ignore_errors=True)

    def reparse_project(self, project_id: str) -> dict[str, Any]:
        from tools.render_book import inspect_book

        bilibili_dir = self.storage_root / "projects" / project_id / "bilibili"
        if self._has_bilibili_publication(project_id):
            raise RuntimeError("bilibili_publication_exists")

        with self.db.session() as session:
            record = session.get(Project, project_id)
            if record is None:
                raise KeyError(project_id)
            source_path = record.source_path
        source = (self.storage_root / source_path).resolve()
        if self.storage_root not in source.parents or not source.is_file():
            raise FileNotFoundError(source)
        chapters = inspect_book(source)
        if not chapters:
            raise ValueError("No readable chapters found")
        with self.db.session() as session, session.begin():
            record = session.get(Project, project_id)
            active = session.scalar(select(Job.id).where(Job.project_id == project_id, Job.status.in_(ACTIVE_JOB_STATES)).limit(1))
            if active:
                raise RuntimeError("project_running")
            session.execute(delete(Job).where(Job.project_id == project_id))
            session.execute(delete(SpeakerAssignment).where(SpeakerAssignment.project_id == project_id))
            session.execute(delete(Artifact).where(Artifact.project_id == project_id))
            session.execute(delete(AudioSegment).where(AudioSegment.project_id == project_id))
            session.execute(delete(Chapter).where(Chapter.project_id == project_id))
            for chapter in chapters:
                session.add(Chapter(
                    project_id=project_id,
                    position=int(chapter["index"]),
                    title=str(chapter["title"]),
                    href=str(chapter["href"]),
                    source_text=str(chapter["text"]),
                    source_hash=_hash_json([chapter["href"], chapter["title"], chapter["text"]]),
                ))
            record.status = "ready"
            record.settings.from_chapter = None
            record.settings.to_chapter = None
            record.updated_at = utcnow()
        project_dir = self.storage_root / "projects" / project_id
        for name in ("audio", "work", "output"):
            shutil.rmtree(project_dir / name, ignore_errors=True)
        shutil.rmtree(bilibili_dir, ignore_errors=True)
        return self.get_project(project_id)

    def list_chapters(self, project_id: str) -> list[dict[str, Any]]:
        with self.db.session() as session:
            rows = session.scalars(select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.position)).all()
            included_position = 0
            result = []
            for row in rows:
                if row.included:
                    included_position += 1
                result.append(self.chapter_dict(row, included_position if row.included else None))
            return result

    def list_included_chapters(self, project_id: str) -> list[dict[str, Any]]:
        return [row for row in self.list_chapters(project_id) if row["included"]]

    def list_selected_chapters(self, project_id: str, payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        chapters = self.list_included_chapters(project_id)
        payload = payload or {}
        try:
            start_value = payload.get("from_included_position")
            end_value = payload.get("to_included_position")
            start = 1 if start_value is None else int(start_value)
            end = len(chapters) if end_value is None else int(end_value)
        except (TypeError, ValueError):
            raise RuntimeError("invalid_chapter_range") from None
        if start < 1 or end < start or end > len(chapters):
            raise RuntimeError("invalid_chapter_range")
        return [row for row in chapters if start <= row["included_position"] <= end]

    def _has_bilibili_publication(self, project_id: str) -> bool:
        try:
            manifest = json.loads((self.storage_root / "projects" / project_id / "bilibili" / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return bool(manifest.get("publication", {}).get("bvid"))

    def set_chapter_inclusion(self, project_id: str, included_chapter_ids: list[int]) -> list[dict[str, Any]]:
        included = set(included_chapter_ids)
        if not included:
            raise RuntimeError("no_included_chapters")
        if self._has_bilibili_publication(project_id):
            raise RuntimeError("bilibili_publication_exists")
        with self.db.session() as session, session.begin():
            project = session.get(Project, project_id)
            if project is None:
                raise KeyError(project_id)
            active = session.scalar(
                select(Job.id).where(Job.project_id == project_id, Job.status.in_(ACTIVE_JOB_STATES)).limit(1)
            )
            if active:
                raise RuntimeError("project_running")
            chapters = session.scalars(
                select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.position)
            ).all()
            if not included.issubset({chapter.id for chapter in chapters}):
                raise RuntimeError("invalid_chapter_ids")
            for chapter in chapters:
                chapter.included = chapter.id in included
            session.execute(update(Artifact).where(
                Artifact.project_id == project_id,
                Artifact.chapter_id.is_(None),
                Artifact.status == "active",
            ).values(status="stale"))
            project.updated_at = utcnow()
        self.refresh_characters(project_id)
        return self.list_chapters(project_id)

    @staticmethod
    def chapter_dict(chapter: Chapter, included_position: int | None = None) -> dict[str, Any]:
        return {
            "id": chapter.id,
            "project_id": chapter.project_id,
            "position": chapter.position,
            "included_position": included_position,
            "title": chapter.title,
            "included": chapter.included,
            "status": chapter.status,
            "active_revision_id": chapter.active_revision_id,
        }

    def get_chapter_record(self, chapter_id: int) -> Chapter:
        with self.db.session() as session:
            chapter = session.get(Chapter, chapter_id)
            if chapter is None:
                raise KeyError(chapter_id)
            session.expunge(chapter)
            return chapter

    def get_script(self, chapter_id: int) -> dict[str, Any]:
        with self.db.session() as session:
            chapter = session.get(Chapter, chapter_id)
            if chapter is None:
                raise KeyError(chapter_id)
            if not chapter.active_revision_id:
                return {"chapter_id": chapter_id, "revision": 0, "kind": None, "entries": []}
            revision = session.scalar(
                select(ScriptRevision)
                .where(ScriptRevision.id == chapter.active_revision_id)
                .options(selectinload(ScriptRevision.entries))
            )
            return self._revision_dict(revision)

    @staticmethod
    def _revision_dict(revision: ScriptRevision) -> dict[str, Any]:
        return {
            "chapter_id": revision.chapter_id,
            "revision_id": revision.id,
            "revision": revision.revision,
            "kind": revision.kind,
            "content_hash": revision.content_hash,
            "input_hash": revision.input_hash,
            "entries": [
                {"id": row.id, "position": row.position, "speaker": row.speaker, "text": row.text, "instruct": row.instruct}
                for row in revision.entries
            ],
        }

    def save_revision(self, chapter_id: int, kind: str, entries: list[dict[str, str]], input_hash: str | None = None) -> dict[str, Any]:
        normalized = [
            {"speaker": row["speaker"].strip(), "text": row["text"], "instruct": row.get("instruct", "").strip()}
            for row in entries
        ]
        content_hash = _hash_json(normalized)
        revision_id = str(uuid.uuid4())
        with self.db.session() as session, session.begin():
            chapter = session.get(Chapter, chapter_id)
            if chapter is None:
                raise KeyError(chapter_id)
            number = session.scalar(select(func.max(ScriptRevision.revision)).where(ScriptRevision.chapter_id == chapter_id)) or 0
            revision = ScriptRevision(id=revision_id, chapter_id=chapter_id, kind=kind, revision=number + 1, content_hash=content_hash, input_hash=input_hash)
            revision.entries = [ScriptEntry(position=index, **row) for index, row in enumerate(normalized, 1)]
            session.add(revision)
            chapter.active_revision_id = revision_id
            chapter.status = "reviewed" if kind in ("reviewed", "manual") else "generated"
        return self.get_script(chapter_id)

    def latest_revision(self, chapter_id: int, kind: str) -> dict[str, Any] | None:
        with self.db.session() as session:
            revision = session.scalar(
                select(ScriptRevision)
                .where(ScriptRevision.chapter_id == chapter_id, ScriptRevision.kind == kind)
                .options(selectinload(ScriptRevision.entries))
                .order_by(ScriptRevision.revision.desc())
                .limit(1)
            )
            return self._revision_dict(revision) if revision else None

    def edit_script(self, chapter_id: int, expected_revision: int, entries: list[dict[str, str]]) -> dict[str, Any]:
        with self.db.session() as session:
            chapter = session.get(Chapter, chapter_id)
            if chapter is None:
                raise KeyError(chapter_id)
            active = session.get(ScriptRevision, chapter.active_revision_id) if chapter.active_revision_id else None
            if active is None or active.revision != expected_revision:
                raise RuntimeError("revision_conflict")
            latest_job = self._latest_job(session, chapter.project_id)
            if latest_job is None or latest_job.status not in EDITABLE_JOB_STATES:
                raise RuntimeError("script_not_editable")
        project_id = chapter.project_id
        result = self.save_revision(chapter_id, "manual", entries)
        with self.db.session() as session, session.begin():
            session.execute(update(Artifact).where(Artifact.chapter_id == chapter_id, Artifact.status == "active").values(status="stale"))
            session.execute(update(Artifact).where(Artifact.project_id == project_id, Artifact.chapter_id.is_(None), Artifact.status == "active").values(status="stale"))
            session.execute(update(AudioSegment).where(AudioSegment.chapter_id == chapter_id).values(status="stale"))
            chapter = session.get(Chapter, chapter_id)
            chapter.status = "reviewed"
            project = session.get(Project, project_id)
            project.status = "ready_for_tts"
            project.updated_at = utcnow()
        return result

    def create_job(self, project_id: str, job_type: str = "preprocess", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._job_lock:
            return self._create_job(project_id, job_type, payload or {})

    def _create_job(self, project_id: str, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if job_type not in ("preprocess", "render", "merge", "bilibili", "character_analysis"):
            raise ValueError("Invalid job type")
        with self.db.session() as session, session.begin():
            project = session.get(Project, project_id)
            if project is None:
                raise KeyError(project_id)
            active = session.scalar(select(Job).where(Job.status.in_(ACTIVE_JOB_STATES)).limit(1))
            if active:
                raise RuntimeError("active_job")
            resumable = session.scalar(
                select(Job).where(Job.project_id == project_id, Job.status.in_(("paused", "interrupted"))).limit(1)
            )
            if resumable:
                raise RuntimeError("resumable_job")
            if job_type == "render":
                chapters = self.list_selected_chapters(project_id, payload)
                ready = 0
                for chapter in chapters:
                    record = session.get(Chapter, chapter["id"])
                    revision = session.get(ScriptRevision, record.active_revision_id) if record.active_revision_id else None
                    ready += int(revision is not None and revision.kind in ("reviewed", "manual"))
                if not chapters or ready != len(chapters):
                    raise RuntimeError("render_not_ready")
            elif job_type == "merge":
                chapter_ids = set(session.scalars(
                    select(Chapter.id).where(Chapter.project_id == project_id, Chapter.included.is_(True))
                ).all())
                artifacts = session.scalars(select(Artifact).where(
                    Artifact.project_id == project_id,
                    Artifact.kind == "chapter_mp3",
                    Artifact.status == "active",
                )).all()
                valid_ids = {
                    row.chapter_id
                    for row in artifacts
                    if (self.storage_root / row.path).is_file() and (self.storage_root / row.path).stat().st_size
                }
                if not chapter_ids or not chapter_ids.issubset(valid_ids):
                    raise RuntimeError("merge_not_ready")
            job = Job(
                id=str(uuid.uuid4()),
                project_id=project_id,
                job_type=job_type,
                payload_json=json.dumps(payload, ensure_ascii=False),
                status="queued",
                render_start_mode=project.settings.render_start_mode,
                release_batch_size=project.settings.release_batch_size,
            )
            project.status = "queued"
            session.add(job)
        self.add_event(job.id, project_id, "job.queued", {"status": "queued"})
        return self.get_job(job.id)

    def set_project_status(self, project_id: str, status: str) -> None:
        with self.db.session() as session, session.begin():
            project = session.get(Project, project_id)
            if project:
                project.status = status
                project.updated_at = utcnow()

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            return self.job_dict(job)

    @staticmethod
    def job_dict(job: Job) -> dict[str, Any]:
        return {
            "id": job.id,
            "project_id": job.project_id,
            "type": job.job_type,
            "payload": json.loads(job.payload_json or "{}"),
            "status": job.status,
            "render_start_mode": job.render_start_mode,
            "release_batch_size": job.release_batch_size,
            "current_chapter": job.current_chapter,
            "cancel_requested": job.cancel_requested,
            "error": job.error,
            "created_at": _iso(job.created_at),
            "started_at": _iso(job.started_at),
            "finished_at": _iso(job.finished_at),
        }

    def next_queued_job(self) -> str | None:
        with self.db.session() as session:
            return session.scalar(select(Job.id).where(Job.status == "queued").order_by(Job.created_at).limit(1))

    def set_job_status(self, job_id: str, status: str, error: str | None = None) -> dict[str, Any]:
        now = utcnow()
        with self.db.session() as session, session.begin():
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status == "cancelled" and status != "cancelled":
                return self.job_dict(job)
            job.status = status
            job.error = error
            if status == "running" and job.started_at is None:
                job.started_at = now
            if status in ("completed", "cancelled", "failed"):
                job.finished_at = now
            project = session.get(Project, job.project_id)
            project.status = status
        result = self.get_job(job_id)
        self.add_event(job_id, result["project_id"], f"job.{status}", {"status": status, "error": error})
        return result

    def request_pause(self, job_id: str) -> dict[str, Any]:
        with self.db.session() as session, session.begin():
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status not in ("running", "queued"):
                raise RuntimeError("not_running")
            job.cancel_requested = True
            job.status = "paused" if job.status == "queued" else "pausing"
            project = session.get(Project, job.project_id)
            project.status = job.status
        result = self.get_job(job_id)
        self.add_event(job_id, result["project_id"], f"job.{result['status']}", {"status": result["status"]})
        return result

    def resume_job(self, job_id: str) -> dict[str, Any]:
        with self._job_lock:
            return self._resume_job(job_id)

    def _resume_job(self, job_id: str) -> dict[str, Any]:
        with self.db.session() as session, session.begin():
            active = session.scalar(select(Job).where(Job.status.in_(ACTIVE_JOB_STATES), Job.id != job_id).limit(1))
            if active:
                raise RuntimeError("active_job")
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status not in ("paused", "interrupted"):
                raise RuntimeError("not_resumable")
            job.status = "queued"
            job.cancel_requested = False
            job.error = None
            job.finished_at = None
            project = session.get(Project, job.project_id)
            project.status = "queued"
            project_id = job.project_id
        self.add_event(job_id, project_id, "job.queued", {"status": "queued", "resumed": True})
        return self.get_job(job_id)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        with self.db.session() as session, session.begin():
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            job.cancel_requested = True
            job.status = "cancelled"
            job.finished_at = utcnow()
            project = session.get(Project, job.project_id)
            project.status = "cancelled"
        result = self.get_job(job_id)
        self.add_event(job_id, result["project_id"], "job.cancelled", {"status": "cancelled"})
        return result

    def is_stop_requested(self, job_id: str) -> bool:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            return job is None or job.cancel_requested

    def add_event(self, job_id: str, project_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        safe_payload = self._redact(payload)
        with self._event_lock:
            with self.db.session() as session, session.begin():
                event = JobEvent(job_id=job_id, project_id=project_id, event_type=event_type, payload_json=json.dumps(safe_payload, ensure_ascii=False))
                session.add(event)
                session.flush()
                result = {
                    "event_id": event.id,
                    "job_id": job_id,
                    "type": event_type,
                    "timestamp": _iso(event.created_at),
                    "payload": safe_payload,
                }
            if self.event_publisher:
                self.event_publisher(project_id, result)
        return result

    def list_events(self, project_id: str, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self.db.session() as session:
            events = session.scalars(
                select(JobEvent)
                .where(JobEvent.project_id == project_id, JobEvent.id > after)
                .order_by(JobEvent.id)
                .limit(limit)
            ).all()
            return [
                {
                    "event_id": row.id,
                    "job_id": row.job_id,
                    "type": row.event_type,
                    "timestamp": _iso(row.created_at),
                    "payload": json.loads(row.payload_json),
                }
                for row in events
            ]

    def _redact(self, value: Any, secrets: tuple[str, ...] | None = None) -> Any:
        if secrets is None:
            settings = self.get_settings(reveal_secrets=True)
            secrets = tuple(str(settings[key]) for key in SECRET_KEYS if settings.get(key))
        if isinstance(value, dict):
            return {key: ("***" if key in SECRET_KEYS else self._redact(item, secrets)) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item, secrets) for item in value]
        if isinstance(value, str):
            for secret in secrets:
                value = re.sub(rf"(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])", "***", value)
        return value

    def get_settings(self, *, reveal_secrets: bool = False) -> dict[str, Any]:
        with self.db.session() as session:
            rows = session.scalars(select(AppSetting)).all()
            result = {row.key: json.loads(row.value_json) for row in rows}
            if not reveal_secrets:
                for key in SECRET_KEYS:
                    if result.get(key):
                        result[key] = "********"
            return result

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        with self.db.session() as session, session.begin():
            active = session.scalar(select(Job.id).where(Job.status.in_(ACTIVE_JOB_STATES)).limit(1))
            if active:
                raise RuntimeError("active_job")
            for key, value in values.items():
                if value is None or (key in SECRET_KEYS and value == "********"):
                    continue
                row = session.get(AppSetting, key)
                if row is None:
                    session.add(AppSetting(key=key, value_json=json.dumps(value, ensure_ascii=False), is_secret=key in SECRET_KEYS))
                else:
                    row.value_json = json.dumps(value, ensure_ascii=False)
        return self.get_settings()

    def seed_settings(self, defaults: dict[str, Any]) -> None:
        with self.db.session() as session, session.begin():
            for key, value in defaults.items():
                if session.get(AppSetting, key) is None:
                    session.add(AppSetting(key=key, value_json=json.dumps(value, ensure_ascii=False), is_secret=key in SECRET_KEYS))

    def list_voices(self) -> list[dict[str, Any]]:
        with self.db.session() as session:
            rows = session.scalars(select(VoiceProfile).order_by(VoiceProfile.pool_order, VoiceProfile.name)).all()
            return [
                self._voice_dict(row)
                for row in rows
            ]

    def _voice_dict(self, row: VoiceProfile) -> dict[str, Any]:
        sample = bool(
            row.sample_path
            and row.sample_reference_id == row.reference_id
            and (self.storage_root / row.sample_path).is_file()
        )
        return {
            "id": row.id,
            "reference_id": row.reference_id,
            "name": row.name,
            "pool_order": row.pool_order,
            "gender": row.gender,
            "traits": row.traits,
            "enabled": row.enabled,
            "sample_available": sample,
            "sample_reference_id": row.sample_reference_id if sample else None,
            "sample_title": row.sample_title if sample else None,
            "sample_text": row.sample_text if sample else None,
            "sample_url": f"/api/v1/voices/{row.id}/sample" if sample else None,
        }

    def _pending_sample_paths(self, reference_id: str) -> tuple[Path, Path]:
        key = hashlib.sha256(reference_id.encode("utf-8")).hexdigest()
        root = self.storage_root / "voices" / ".validated"
        return root / f"{key}.wav", root / f"{key}.json"

    def validate_voice(self, reference_id: str, base_url: str) -> dict[str, Any]:
        import httpx

        reference_id = reference_id.strip()
        try:
            response = httpx.get(f"{base_url.rstrip('/')}/model/{reference_id}", timeout=20, follow_redirects=True)
            response.raise_for_status()
            model = response.json()
            samples = model.get("samples") or []
            if model.get("state") != "trained" or model.get("dmca_taken_down") or not samples:
                raise RuntimeError("voice_not_usable")
            sample = samples[0]
            audio_url = sample.get("audio")
            if not audio_url:
                raise RuntimeError("voice_sample_missing")
            audio_response = httpx.get(audio_url, timeout=30, follow_redirects=True)
            audio_response.raise_for_status()
            audio = audio_response.content
            if not audio:
                raise RuntimeError("voice_sample_empty")
        except RuntimeError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise RuntimeError(f"voice_validation_failed: {exc}") from exc

        audio_path, metadata_path = self._pending_sample_paths(reference_id)
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        temp_audio = audio_path.with_suffix(".wav.part")
        temp_metadata = metadata_path.with_suffix(".json.part")
        tags = [str(tag) for tag in model.get("tags") or []]
        gender_tags = {"female": "女", "male": "男", "neutral": "中性"}
        gender = next((gender_tags[tag.casefold()] for tag in tags if tag.casefold() in gender_tags), "")
        category_tags = {
            *gender_tags,
            "zh", "en", "ja", "ko", "de", "fr", "es", "ru",
            "chinese", "english", "japanese", "korean", "german", "french", "spanish", "russian",
            "character-voice", "tts", "voice", "speech",
        }
        metadata = {
            "reference_id": reference_id,
            "name": str(model.get("title") or reference_id),
            "gender": gender,
            "traits": "、".join(tag for tag in tags if tag.casefold() not in category_tags),
            "sample_title": str(sample.get("title") or ""),
            "sample_text": str(sample.get("text") or ""),
        }
        temp_audio.write_bytes(audio)
        temp_metadata.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        temp_audio.replace(audio_path)
        temp_metadata.replace(metadata_path)
        return {
            **metadata,
            "status": "trained",
            "sample_available": True,
            "sample_url": f"/api/v1/voices/previews/{audio_path.stem}",
        }

    def get_voice_preview(self, token: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise KeyError(token)
        path = (self.storage_root / "voices" / ".validated" / f"{token}.wav").resolve()
        if self.storage_root not in path.parents or not path.is_file():
            raise KeyError(token)
        return path

    def get_voice_sample(self, voice_id: str) -> Path:
        with self.db.session() as session:
            row = session.get(VoiceProfile, voice_id)
            if row is None or not row.sample_path or row.sample_reference_id != row.reference_id:
                raise KeyError(voice_id)
            path = (self.storage_root / row.sample_path).resolve()
            if self.storage_root not in path.parents or not path.is_file():
                raise KeyError(voice_id)
            return path

    def replace_voices(self, voices: list[dict[str, Any]], *, require_validation: bool = False) -> list[dict[str, Any]]:
        with self.db.session() as session, session.begin():
            active = session.scalar(select(Job.id).where(Job.status.in_(ACTIVE_JOB_STATES)).limit(1))
            if active:
                raise RuntimeError("active_job")
            existing = {row.id: row for row in session.scalars(select(VoiceProfile)).all()}
            incoming_ids = {voice["id"] for voice in voices if voice.get("id") in existing}
            removed_ids = set(existing) - incoming_ids
            in_use = set(session.scalars(
                select(SpeakerAssignment.voice_profile_id)
                .where(SpeakerAssignment.voice_profile_id.in_(removed_ids))
            ).all()) if removed_ids else set()
            if in_use:
                raise RuntimeError("voice_in_use")
            if removed_ids:
                session.execute(delete(VoiceProfile).where(VoiceProfile.id.in_(removed_ids)))
            for voice in voices:
                row = existing.get(voice.get("id"))
                is_new = row is None
                changed_reference = bool(row and row.reference_id != voice["reference_id"])
                pending_audio, pending_metadata = self._pending_sample_paths(voice["reference_id"])
                if require_validation and (is_new or changed_reference) and not (pending_audio.is_file() and pending_metadata.is_file()):
                    raise RuntimeError(f"voice_not_validated: {voice['name']}")
                if row is None:
                    row = VoiceProfile(id=str(uuid.uuid4()))
                    session.add(row)
                needs_sample = row.sample_reference_id != voice["reference_id"] or not row.sample_path or not (self.storage_root / row.sample_path).is_file()
                disabling = row.enabled and not voice.get("enabled", True)
                conflicts = self._voice_conflicts(session, row.id) if disabling and not is_new else []
                if conflicts:
                    raise RuntimeError(f"voice_in_use: {', '.join(conflicts)}")
                if is_new or changed_reference or needs_sample:
                    if pending_audio.is_file() and pending_metadata.is_file():
                        metadata = json.loads(pending_metadata.read_text(encoding="utf-8"))
                        sample_dir = self.storage_root / "voices" / row.id
                        sample_dir.mkdir(parents=True, exist_ok=True)
                        sample_path = sample_dir / "sample.wav"
                        shutil.copyfile(pending_audio, sample_path)
                        row.sample_path = sample_path.relative_to(self.storage_root).as_posix()
                        row.sample_reference_id = voice["reference_id"]
                        row.sample_title = metadata.get("sample_title") or None
                        row.sample_text = metadata.get("sample_text") or None
                    else:
                        row.sample_path = None
                        row.sample_reference_id = None
                        row.sample_title = None
                        row.sample_text = None
                row.reference_id = voice["reference_id"]
                row.name = voice["name"]
                row.bound_speaker = None
                row.pool_order = voice.get("pool_order", 0)
                row.gender = voice.get("gender", "").strip()
                row.traits = voice.get("traits", "").strip()
                row.enabled = voice.get("enabled", True)
        return self.list_voices()

    def _voice_conflicts(self, session, voice_id: str, *, project_id: str | None = None) -> list[str]:
        rows = session.execute(
            select(Project.title, SpeakerAssignment.speaker)
            .join(SpeakerAssignment, SpeakerAssignment.project_id == Project.id)
            .where(SpeakerAssignment.voice_profile_id == voice_id)
            .where(SpeakerAssignment.project_id == project_id if project_id else True)
        ).all()
        return [f"{title} / {speaker}" for title, speaker in rows]

    def get_project_voice_pool(self, project_id: str) -> dict[str, Any]:
        with self.db.session() as session:
            if session.get(Project, project_id) is None:
                raise KeyError(project_id)
            excluded = session.scalars(select(ProjectVoiceExclusion.voice_profile_id).where(ProjectVoiceExclusion.project_id == project_id)).all()
            narrator = session.scalar(select(SpeakerAssignment.voice_profile_id).where(
                SpeakerAssignment.project_id == project_id,
                SpeakerAssignment.speaker == "NARRATOR",
            ))
            return {"excluded_voice_ids": list(excluded), "narrator_voice_profile_id": narrator}

    def update_project_voice_pool(self, project_id: str, excluded_voice_ids: list[str], narrator_voice_profile_id: str | None) -> dict[str, Any]:
        excluded = set(excluded_voice_ids)
        with self.db.session() as session, session.begin():
            if session.get(Project, project_id) is None:
                raise KeyError(project_id)
            active = session.scalar(select(Job.id).where(Job.project_id == project_id, Job.status.in_(ACTIVE_JOB_STATES)).limit(1))
            if active:
                raise RuntimeError("project_running")
            requested = excluded | ({narrator_voice_profile_id} if narrator_voice_profile_id else set())
            voices = {row.id: row for row in session.scalars(select(VoiceProfile).where(VoiceProfile.id.in_(requested))).all()} if requested else {}
            if len(voices) != len(requested):
                raise KeyError("voice")
            if narrator_voice_profile_id and (not voices[narrator_voice_profile_id].enabled or narrator_voice_profile_id in excluded):
                raise RuntimeError("narrator_voice_unavailable")
            conflicts = []
            for voice_id in excluded:
                conflicts.extend(self._voice_conflicts(session, voice_id, project_id=project_id))
            if narrator_voice_profile_id:
                conflicts.extend(
                    f"{title} / {speaker}" for title, speaker in session.execute(
                        select(Project.title, SpeakerAssignment.speaker)
                        .join(SpeakerAssignment, SpeakerAssignment.project_id == Project.id)
                        .where(
                            SpeakerAssignment.project_id == project_id,
                            SpeakerAssignment.voice_profile_id == narrator_voice_profile_id,
                            SpeakerAssignment.speaker != "NARRATOR",
                        )
                    ).all()
                )
            if conflicts:
                raise RuntimeError(f"voice_in_use: {', '.join(conflicts)}")
            old_excluded = set(session.scalars(select(ProjectVoiceExclusion.voice_profile_id).where(ProjectVoiceExclusion.project_id == project_id)).all())
            narrator = session.scalar(select(SpeakerAssignment).where(
                SpeakerAssignment.project_id == project_id,
                SpeakerAssignment.speaker == "NARRATOR",
            ))
            old_narrator = narrator.voice_profile_id if narrator else None
            session.execute(delete(ProjectVoiceExclusion).where(ProjectVoiceExclusion.project_id == project_id))
            session.add_all(ProjectVoiceExclusion(project_id=project_id, voice_profile_id=voice_id) for voice_id in excluded)
            if narrator_voice_profile_id:
                if narrator is None:
                    narrator = SpeakerAssignment(project_id=project_id, speaker="NARRATOR")
                    session.add(narrator)
                narrator.voice_profile_id = narrator_voice_profile_id
                narrator.user_edited = True
            elif narrator is not None:
                session.delete(narrator)
            if old_excluded != excluded or old_narrator != narrator_voice_profile_id:
                session.execute(update(Artifact).where(Artifact.project_id == project_id, Artifact.status == "active").values(status="stale"))
                session.execute(update(AudioSegment).where(AudioSegment.project_id == project_id).values(status="stale"))
                project = session.get(Project, project_id)
                project.status = "ready_for_tts"
                project.updated_at = utcnow()
        return self.get_project_voice_pool(project_id)

    def assign_voices(self, project_id: str, speakers: list[str]) -> dict[str, dict[str, str]]:
        """Resolve explicit choices and chapter-local random choices without persisting randomness."""
        with self.db.session() as session:
            excluded = select(ProjectVoiceExclusion.voice_profile_id).where(ProjectVoiceExclusion.project_id == project_id)
            profiles = session.scalars(
                select(VoiceProfile)
                .where(VoiceProfile.enabled.is_(True), VoiceProfile.id.not_in(excluded))
                .order_by(VoiceProfile.pool_order, VoiceProfile.name)
            ).all()
            if not profiles:
                raise RuntimeError("No Fish voices configured")
            existing_rows = session.scalars(select(SpeakerAssignment).where(SpeakerAssignment.project_id == project_id)).all()
            existing = {row.speaker: row.voice_profile_id for row in existing_rows}
            by_id = {row.id: row for row in profiles}
            reserved = {profile_id for profile_id in existing.values() if profile_id}
            pool = [row for row in profiles if row.id not in reserved]
            resolved: dict[str, VoiceProfile] = {}
            for speaker in speakers:
                profile = resolved.get(speaker) or by_id.get(existing.get(speaker, ""))
                if profile is None and speaker != "NARRATOR":
                    character = next((row for row in existing_rows if row.speaker == speaker), None)
                    preferred = [row for row in pool if character and character.gender and row.gender == character.gender]
                    choices = preferred or pool
                    if not choices:
                        raise RuntimeError(f"No unbound Fish voice available for {speaker}")
                    profile = random.choice(choices)
                if profile is None:
                    raise RuntimeError("narrator_voice_required")
                resolved[speaker] = profile
            return {
                speaker: {"reference_id": resolved[speaker].reference_id, "name": resolved[speaker].name}
                for speaker in resolved
            }

    def list_speaker_assignments(self, project_id: str) -> list[dict[str, str]]:
        with self.db.session() as session:
            rows = session.execute(
                select(SpeakerAssignment, VoiceProfile)
                .outerjoin(VoiceProfile, VoiceProfile.id == SpeakerAssignment.voice_profile_id)
                .where(SpeakerAssignment.project_id == project_id)
                .order_by(SpeakerAssignment.created_at)
            ).all()
            return [
                {"speaker": assignment.speaker, "voice_name": voice.name if voice else "", "reference_id": voice.reference_id if voice else ""}
                for assignment, voice in rows
            ]

    def refresh_characters(self, project_id: str) -> list[dict[str, Any]]:
        with self.db.session() as session, session.begin():
            chapters = session.scalars(select(Chapter).where(
                Chapter.project_id == project_id, Chapter.included.is_(True)
            ).order_by(Chapter.position)).all()
            stats: dict[str, dict[str, Any]] = {}
            seen = 0
            for chapter in chapters:
                if not chapter.active_revision_id:
                    continue
                entries = session.scalars(
                    select(ScriptEntry).where(ScriptEntry.revision_id == chapter.active_revision_id).order_by(ScriptEntry.position)
                ).all()
                for entry in entries:
                    speaker = entry.speaker.strip()
                    if not speaker or speaker == "NARRATOR":
                        continue
                    if speaker not in stats:
                        seen += 1
                        stats[speaker] = {"line_count": 0, "importance": 0, "first_seen": seen}
                    stats[speaker]["line_count"] += 1
                    stats[speaker]["importance"] += len(entry.text.strip())
            rows = {row.speaker: row for row in session.scalars(
                select(SpeakerAssignment).where(SpeakerAssignment.project_id == project_id)
            ).all()}
            for speaker, values in stats.items():
                row = rows.get(speaker)
                if row is None:
                    row = SpeakerAssignment(project_id=project_id, speaker=speaker)
                    session.add(row)
                    rows[speaker] = row
                row.line_count = values["line_count"]
                row.importance = values["importance"]
                row.first_seen = values["first_seen"]
            stale = set(rows) - set(stats) - {"NARRATOR"}
            if stale:
                session.execute(delete(SpeakerAssignment).where(
                    SpeakerAssignment.project_id == project_id,
                    SpeakerAssignment.speaker.in_(stale),
                ))
        return self.list_characters(project_id)

    def list_characters(self, project_id: str) -> list[dict[str, Any]]:
        with self.db.session() as session:
            rows = session.execute(
                select(SpeakerAssignment, VoiceProfile)
                .outerjoin(VoiceProfile, VoiceProfile.id == SpeakerAssignment.voice_profile_id)
                .where(SpeakerAssignment.project_id == project_id, SpeakerAssignment.speaker != "NARRATOR")
                .order_by(SpeakerAssignment.importance.desc(), SpeakerAssignment.first_seen)
            ).all()
            return [{
                "speaker": row.speaker,
                "gender": row.gender,
                "personality": row.personality,
                "line_count": row.line_count,
                "importance": row.importance,
                "voice_profile_id": row.voice_profile_id,
                "voice_name": voice.name if voice else "",
                "user_edited": row.user_edited,
            } for row, voice in rows]

    def character_samples(self, project_id: str, speakers: list[str]) -> dict[str, list[str]]:
        wanted = set(speakers)
        samples = {speaker: [] for speaker in speakers}
        with self.db.session() as session:
            chapters = session.scalars(select(Chapter).where(
                Chapter.project_id == project_id, Chapter.included.is_(True)
            ).order_by(Chapter.position)).all()
            for chapter in chapters:
                if not chapter.active_revision_id:
                    continue
                entries = session.scalars(
                    select(ScriptEntry).where(ScriptEntry.revision_id == chapter.active_revision_id).order_by(ScriptEntry.position)
                ).all()
                for entry in entries:
                    if entry.speaker in wanted and len(samples[entry.speaker]) < 8:
                        samples[entry.speaker].append(entry.text.strip())
        return samples

    def update_characters(self, project_id: str, characters: list[dict[str, Any]], *, automated: bool = False) -> list[dict[str, Any]]:
        with self.db.session() as session, session.begin():
            active = session.scalar(select(Job.id).where(Job.project_id == project_id, Job.status.in_(ACTIVE_JOB_STATES)).limit(1))
            if active and not automated:
                raise RuntimeError("project_running")
            excluded = select(ProjectVoiceExclusion.voice_profile_id).where(ProjectVoiceExclusion.project_id == project_id)
            voices = {row.id: row for row in session.scalars(
                select(VoiceProfile).where(VoiceProfile.enabled.is_(True), VoiceProfile.id.not_in(excluded))
            ).all()}
            narrator_voice_id = session.scalar(select(SpeakerAssignment.voice_profile_id).where(
                SpeakerAssignment.project_id == project_id,
                SpeakerAssignment.speaker == "NARRATOR",
            ))
            for values in characters:
                row = session.scalar(select(SpeakerAssignment).where(
                    SpeakerAssignment.project_id == project_id,
                    SpeakerAssignment.speaker == values["speaker"],
                ))
                if row is None:
                    continue
                voice_id = values.get("voice_profile_id")
                if voice_id and (voice_id not in voices or voice_id == narrator_voice_id):
                    raise RuntimeError("Character voices must come from the unbound Fish voice pool")
                if not automated or not row.gender:
                    row.gender = values.get("gender", "").strip()
                if not automated or not row.personality:
                    row.personality = values.get("personality", "").strip()
                if not automated:
                    row.voice_profile_id = voice_id or None
                    row.user_edited = True
            if not automated:
                session.execute(update(Artifact).where(Artifact.project_id == project_id, Artifact.status == "active").values(status="stale"))
                session.execute(update(AudioSegment).where(AudioSegment.project_id == project_id).values(status="stale"))
                project = session.get(Project, project_id)
                project.status = "ready_for_tts"
                project.updated_at = utcnow()
        return self.list_characters(project_id)

    def pause_active_jobs_for_shutdown(self) -> None:
        with self.db.session() as session, session.begin():
            jobs = session.scalars(select(Job).where(Job.status.in_(ACTIVE_JOB_STATES))).all()
            for job in jobs:
                job.cancel_requested = True
                job.status = "paused" if job.status == "queued" else "pausing"
                project = session.get(Project, job.project_id)
                if project:
                    project.status = job.status

    def update_chapter_status(self, chapter_id: int, status: str) -> None:
        with self.db.session() as session, session.begin():
            chapter = session.get(Chapter, chapter_id)
            if chapter:
                chapter.status = status

    def update_stage(self, job_id: str, chapter_id: int, stage: str, status: str, **values: Any) -> None:
        with self.db.session() as session, session.begin():
            row = session.scalar(select(StageRun).where(StageRun.job_id == job_id, StageRun.chapter_id == chapter_id, StageRun.stage == stage))
            if row is None:
                row = StageRun(job_id=job_id, chapter_id=chapter_id, stage=stage)
                session.add(row)
            row.status = status
            row.attempts = (row.attempts or 0) + (1 if status == "running" else 0)
            row.error = values.get("error")
            row.input_hash = values.get("input_hash", row.input_hash)
            row.output_hash = values.get("output_hash", row.output_hash)
            if status == "running":
                row.started_at = utcnow()
            if status in ("completed", "failed", "interrupted"):
                row.finished_at = utcnow()

    def save_audio_segment(self, *, project_id: str, chapter_id: int, revision_id: str, position: int, fingerprint: str, path: str, status: str, attempts: int, error: str | None = None) -> None:
        with self.db.session() as session, session.begin():
            row = session.scalar(select(AudioSegment).where(AudioSegment.chapter_id == chapter_id, AudioSegment.position == position))
            if row is None:
                row = AudioSegment(project_id=project_id, chapter_id=chapter_id, revision_id=revision_id, position=position, fingerprint=fingerprint, path=path)
                session.add(row)
            row.revision_id = revision_id
            row.fingerprint = fingerprint
            row.path = path
            row.status = status
            row.attempts = attempts
            row.error = error

    def reusable_audio_segment(self, chapter_id: int, position: int, fingerprint: str) -> str | None:
        with self.db.session() as session:
            row = session.scalar(select(AudioSegment).where(AudioSegment.chapter_id == chapter_id, AudioSegment.position == position))
            if row and row.status == "done" and row.fingerprint == fingerprint:
                path = self.storage_root / row.path
                if path.exists() and path.stat().st_size:
                    return row.path
            return None

    def save_artifact(self, project_id: str, chapter_id: int | None, kind: str, absolute_path: Path) -> dict[str, Any]:
        relative = absolute_path.resolve().relative_to(self.storage_root).as_posix()
        digest = hashlib.sha256(absolute_path.read_bytes()).hexdigest()
        with self.db.session() as session, session.begin():
            if kind == "chapter_mp3":
                session.execute(
                    update(Artifact)
                    .where(Artifact.project_id == project_id, Artifact.kind == "book_mp3", Artifact.status == "active")
                    .values(status="stale")
                )
            session.execute(
                update(Artifact)
                .where(Artifact.project_id == project_id, Artifact.chapter_id == chapter_id, Artifact.kind == kind, Artifact.status == "active")
                .values(status="stale")
            )
            row = Artifact(id=str(uuid.uuid4()), project_id=project_id, chapter_id=chapter_id, kind=kind, path=relative, sha256=digest, size=absolute_path.stat().st_size)
            session.add(row)
        return {"id": row.id, "project_id": project_id, "chapter_id": chapter_id, "kind": kind, "path": relative, "sha256": digest, "size": row.size, "status": "active"}

    def list_artifacts(self, project_id: str) -> list[dict[str, Any]]:
        with self.db.session() as session:
            rows = session.scalars(select(Artifact).where(Artifact.project_id == project_id, Artifact.status == "active").order_by(Artifact.created_at.desc())).all()
            return [
                {"id": row.id, "project_id": row.project_id, "chapter_id": row.chapter_id, "kind": row.kind, "path": row.path, "sha256": row.sha256, "size": row.size, "status": row.status}
                for row in rows
            ]

    def get_artifact(self, artifact_id: str) -> tuple[dict[str, Any], Path]:
        with self.db.session() as session:
            row = session.get(Artifact, artifact_id)
            if row is None or row.status != "active":
                raise KeyError(artifact_id)
            data = {"id": row.id, "project_id": row.project_id, "chapter_id": row.chapter_id, "kind": row.kind, "path": row.path}
            return data, (self.storage_root / row.path).resolve()
