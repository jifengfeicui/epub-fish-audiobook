from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import timedelta, timezone

from sqlalchemy import func, select

from backend.alexandria.api.schemas import AgentMessage, AutomationCreate, AutomationUpdate
from backend.alexandria.db.models import AgentAction, AgentRun, Chapter, Job, Project, ProjectAutomation, utcnow
from backend.alexandria.db.repository import _iso


class ProjectAutomations:
    def __init__(self, agent):
        self.agent = agent
        self.repo = agent.repo
        self.now = utcnow

    @staticmethod
    def as_dict(row):
        return {"id": row.id, "project_id": row.project_id, "instruction": row.instruction,
                "interval_seconds": row.interval_seconds, "enabled": row.enabled,
                "next_run_at": _iso(row.next_run_at), "selected_chapter_id": row.selected_chapter_id,
                "last_run_id": row.last_run_id, "paused_reason": row.paused_reason,
                "created_at": _iso(row.created_at)}

    def _get(self, session, project_id, automation_id):
        row = session.get(ProjectAutomation, automation_id)
        if row is None or row.project_id != project_id:
            raise KeyError(automation_id)
        return row

    def list(self, project_id):
        self.repo.get_project(project_id)
        with self.repo.db.session() as session:
            result = []
            for row in session.scalars(select(ProjectAutomation).where(
                    ProjectAutomation.project_id == project_id).order_by(ProjectAutomation.created_at, ProjectAutomation.id)):
                run = session.get(AgentRun, row.last_run_id) if row.last_run_id else None
                result.append({**self.as_dict(row), "last_run": {
                    "id": run.id, "status": run.status, "output": run.output, "created_at": _iso(run.created_at)
                } if run else None})
            return result

    def create(self, project_id, payload: AutomationCreate):
        self.repo.get_project(project_id)
        with self.repo.db.session() as session, session.begin():
            chapter = session.get(Chapter, payload.selected_chapter_id) if payload.selected_chapter_id else None
            if payload.selected_chapter_id and (chapter is None or chapter.project_id != project_id):
                raise KeyError(payload.selected_chapter_id)
            row = ProjectAutomation(id=str(uuid.uuid4()), project_id=project_id,
                                    instruction=self.agent.store.safe(payload.instruction),
                                    interval_seconds=payload.interval_seconds, enabled=True,
                                    next_run_at=(payload.first_run_at.astimezone(timezone.utc) if payload.first_run_at
                                                 else self.now() + timedelta(seconds=payload.interval_seconds)),
                                    selected_chapter_id=payload.selected_chapter_id,
                                    selected_chapter_hash=chapter.source_hash if chapter else None,
                                    paused_reason="")
            session.add(row)
            session.flush()
            return self.as_dict(row)

    def update(self, project_id, automation_id, payload: AutomationUpdate):
        values = payload.model_dump(exclude_unset=True, exclude_none=True)
        if "instruction" in values:
            values["instruction"] = self.agent.store.safe(values["instruction"])
        if "next_run_at" in values:
            values["next_run_at"] = values["next_run_at"].astimezone(timezone.utc)
        with self.repo.db.session() as session, session.begin():
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = self._get(session, project_id, automation_id)
            for key, value in values.items():
                setattr(row, key, value)
            if "interval_seconds" in values or values.get("enabled"):
                row.next_run_at = values.get("next_run_at") or self.now() + timedelta(seconds=row.interval_seconds)
            if values.get("enabled"):
                row.paused_reason = ""
                # Resuming is an explicit decision after checking an earlier failed run.
                row.acknowledged_run_id = row.last_run_id
                row.acknowledged_at = utcnow()
            return self.as_dict(row)

    def delete(self, project_id, automation_id):
        with self.repo.db.session() as session, session.begin():
            session.delete(self._get(session, project_id, automation_id))
        return {"deleted": True, "automation_id": automation_id}

    def pause(self, project_id, automation_id, reason):
        with self.repo.db.session() as session, session.begin():
            row = self._get(session, project_id, automation_id)
            row.enabled, row.paused_reason = False, self.agent.store.safe(reason)
            return self.as_dict(row)

    def _failure(self, session, row):
        if row.selected_chapter_id:
            chapter = session.get(Chapter, row.selected_chapter_id)
            if chapter is None or chapter.project_id != row.project_id or chapter.source_hash != row.selected_chapter_hash:
                return "目标章节已变化，请重新创建计划"
        previous = session.get(AgentRun, row.last_run_id) if row.last_run_id else None
        unchecked = previous and previous.id != row.acknowledged_run_id
        if unchecked and previous.status in ("failed", "interrupted"):
            return previous.output or "上次运行失败或中断，请核对已执行的操作"
        if unchecked and previous.status != "running":
            for action in session.scalars(select(AgentAction).where(AgentAction.run_id == previous.id)):
                result = json.loads(action.result_json)
                if action.status in ("executing", "failed", "interrupted"):
                    return result.get("message") or "上次操作结果待核对"
        # A job may fail after later wakeups have already queried its progress.
        jobs = session.execute(select(Job, AgentRun.created_at).join(AgentAction, Job.id == func.json_extract(
            AgentAction.result_json, "$.id")).join(AgentRun, AgentRun.id == AgentAction.run_id).where(
                AgentRun.automation_id == row.id, AgentAction.kind.in_(("task", "control")),
                Job.project_id == row.project_id, Job.status.in_(("failed", "interrupted"))))
        for job, submitted_at in jobs:
            failed_at = job.finished_at or submitted_at
            if row.acknowledged_at is None or failed_at.replace(tzinfo=timezone.utc) > row.acknowledged_at.replace(tzinfo=timezone.utc):
                return job.error or "计划提交的任务失败或中断"
        return ""

    def claim_due(self):
        now = self.now().astimezone(timezone.utc)
        runs = []
        with self.repo.db.session() as session, session.begin():
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            rows = session.scalars(select(ProjectAutomation).where(ProjectAutomation.enabled.is_(True))
                                   .order_by(ProjectAutomation.next_run_at, ProjectAutomation.id)).all()
            for row in rows:
                reason = self._failure(session, row)
                if reason:
                    row.enabled, row.paused_reason = False, self.agent.store.safe(reason)
                    continue
                if row.next_run_at.replace(tzinfo=timezone.utc) > now:
                    continue
                if session.scalar(select(AgentRun.id).where(AgentRun.project_id == row.project_id,
                                                            AgentRun.status == "running")):
                    continue
                project = session.get(Project, row.project_id)
                if project.archived_at is not None:
                    continue
                payload = AgentMessage(request_id=uuid.uuid4(), message=row.instruction,
                                       selected_chapter_id=row.selected_chapter_id)
                run = self.agent.store.create_run(session, row.project_id, payload, row.instruction, row.id)
                row.last_run_id = run.id
                row.next_run_at = now + timedelta(seconds=row.interval_seconds)
                runs.append((row.project_id, self.agent.store._dict(session, run)))
        return runs

    async def tick(self):
        for project_id, run in self.claim_due():
            self.agent.start_run(project_id, run)

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception as exc:
                logging.getLogger(__name__).error("Project automation tick failed: %s", self.agent.store.safe(str(exc)))
            await asyncio.sleep(1)
