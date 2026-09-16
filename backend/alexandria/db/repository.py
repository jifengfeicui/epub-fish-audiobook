from __future__ import annotations

import hashlib
import json
import re
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

    def list_projects(self) -> list[dict[str, Any]]:
        with self.db.session() as session:
            projects = session.scalars(
                select(Project)
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
            counts[chapter.status] = counts.get(chapter.status, 0) + 1
        return {
            "id": project.id,
            "title": project.title,
            "source_filename": project.source_filename,
            "status": project.status,
            "chapter_count": len(project.chapters),
            "chapter_status_counts": counts,
            "created_at": _iso(project.created_at),
            "updated_at": _iso(project.updated_at),
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
                "workers": settings.workers,
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

    def list_chapters(self, project_id: str) -> list[dict[str, Any]]:
        with self.db.session() as session:
            rows = session.scalars(select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.position)).all()
            return [self.chapter_dict(row) for row in rows]

    def list_selected_chapters(self, project_id: str) -> list[dict[str, Any]]:
        project = self.get_project(project_id)
        start = project["settings"]["from_chapter"] or 1
        end = project["settings"]["to_chapter"] or project["chapter_count"]
        return [row for row in self.list_chapters(project_id) if start <= row["position"] <= end]

    @staticmethod
    def chapter_dict(chapter: Chapter) -> dict[str, Any]:
        return {
            "id": chapter.id,
            "project_id": chapter.project_id,
            "position": chapter.position,
            "title": chapter.title,
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
        return result

    def create_job(self, project_id: str) -> dict[str, Any]:
        with self._job_lock:
            return self._create_job(project_id)

    def _create_job(self, project_id: str) -> dict[str, Any]:
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
            job = Job(
                id=str(uuid.uuid4()),
                project_id=project_id,
                status="queued",
                render_start_mode=project.settings.render_start_mode,
                release_batch_size=project.settings.release_batch_size,
            )
            project.status = "queued"
            session.add(job)
        self.add_event(job.id, project_id, "job.queued", {"status": "queued"})
        return self.get_job(job.id)

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
                {"id": row.id, "reference_id": row.reference_id, "name": row.name, "bound_speaker": row.bound_speaker, "pool_order": row.pool_order}
                for row in rows
            ]

    def replace_voices(self, voices: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
                if row is None:
                    row = VoiceProfile(id=str(uuid.uuid4()))
                    session.add(row)
                row.reference_id = voice["reference_id"]
                row.name = voice["name"]
                row.bound_speaker = voice.get("bound_speaker") or None
                row.pool_order = voice.get("pool_order", 0)
        return self.list_voices()

    def assign_voices(self, project_id: str, speakers: list[str]) -> dict[str, dict[str, str]]:
        with self.db.session() as session, session.begin():
            profiles = session.scalars(select(VoiceProfile).order_by(VoiceProfile.pool_order, VoiceProfile.name)).all()
            if not profiles:
                raise RuntimeError("No Fish voices configured")
            existing_rows = session.scalars(select(SpeakerAssignment).where(SpeakerAssignment.project_id == project_id)).all()
            existing = {row.speaker: row.voice_profile_id for row in existing_rows}
            by_id = {row.id: row for row in profiles}
            bound = {row.bound_speaker: row for row in profiles if row.bound_speaker}
            reserved = {row.id for row in bound.values()}
            pool = [row for row in profiles if row.id not in reserved]
            pool_position = sum(1 for profile_id in existing.values() if profile_id not in reserved)
            for speaker in speakers:
                if speaker in existing:
                    continue
                profile = bound.get(speaker)
                if profile is None:
                    if not pool:
                        raise RuntimeError(f"No unbound Fish voice available for {speaker}")
                    profile = pool[pool_position % len(pool)]
                    pool_position += 1
                session.add(SpeakerAssignment(project_id=project_id, speaker=speaker, voice_profile_id=profile.id))
                existing[speaker] = profile.id
            return {
                speaker: {"reference_id": by_id[existing[speaker]].reference_id, "name": by_id[existing[speaker]].name}
                for speaker in speakers
            }

    def list_speaker_assignments(self, project_id: str) -> list[dict[str, str]]:
        with self.db.session() as session:
            rows = session.execute(
                select(SpeakerAssignment, VoiceProfile)
                .join(VoiceProfile, VoiceProfile.id == SpeakerAssignment.voice_profile_id)
                .where(SpeakerAssignment.project_id == project_id)
                .order_by(SpeakerAssignment.created_at)
            ).all()
            return [
                {"speaker": assignment.speaker, "voice_name": voice.name, "reference_id": voice.reference_id}
                for assignment, voice in rows
            ]

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
            data = {"id": row.id, "kind": row.kind, "path": row.path}
            return data, (self.storage_root / row.path).resolve()
