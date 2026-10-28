from __future__ import annotations

import json
import uuid

from sqlalchemy import select, update

from backend.alexandria.api.schemas import ScriptUpdate
from backend.alexandria.db.models import AgentAction, AgentRun, Chapter
from backend.alexandria.db.repository import Repository, _iso


class AgentStore:
    """One conversation per project; write receipts survive client retries and restarts."""

    def __init__(self, repo: Repository):
        self.repo = repo

    def safe(self, value, secrets=()):
        value = self.repo._redact(value)
        settings = self.repo.get_settings(reveal_secrets=True)
        keys = [settings.get("fish_api_key", ""), settings.get("llm_api_key", "")]
        keys.extend(profile.get("api_key", "") for profile in settings.get("llm_profiles", []))
        keys = sorted(set(keys).union(secrets) - {""}, key=len, reverse=True)

        def redact(item):
            if isinstance(item, dict):
                return {key: redact(content) for key, content in item.items()}
            if isinstance(item, list):
                return [redact(content) for content in item]
            if isinstance(item, str):
                for key in keys:
                    item = item.replace(key, "***")
            return item

        return redact(value)

    def recover(self):
        with self.repo.db.session() as session, session.begin():
            session.execute(update(AgentRun).where(AgentRun.status == "running").values(
                status="interrupted", output="服务已停止，本轮已中断。已提交的任务请查看项目进度，不会自动重复执行。"))
            session.execute(update(AgentAction).where(AgentAction.status == "executing").values(
                status="interrupted", result_json=json.dumps({"message": "执行结果待核对，请检查任务记录；不会自动重放。"})))

    def _run(self, session, project_id, run_id):
        run = session.get(AgentRun, run_id)
        if run is None or run.project_id != project_id:
            raise KeyError(run_id)
        return run

    def _dict(self, session, run):
        action = session.scalar(select(AgentAction).where(AgentAction.run_id == run.id))
        return {
            "id": run.id, "request_id": run.request_id, "message": run.message,
            "status": run.status, "output": run.output, "created_at": _iso(run.created_at),
            "selected_chapter_id": run.selected_chapter_id, "allow_text_rewrite": run.allow_text_rewrite,
            "action": self.action_dict(action) if action else None,
        }

    @staticmethod
    def action_dict(action):
        return {"id": action.id, "kind": action.kind, "status": action.status,
                "args": json.loads(action.args_json), "result": json.loads(action.result_json)}

    def get(self, project_id, run_id):
        with self.repo.db.session() as session:
            return self._dict(session, self._run(session, project_id, run_id))

    def conversation(self, project_id):
        self.repo.get_project(project_id)
        with self.repo.db.session() as session:
            # shortcut: display the latest 50 turns; add pagination when older history needs browsing.
            runs = session.scalars(select(AgentRun).where(AgentRun.project_id == project_id)
                                   .order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(50)).all()
            return {"runs": [self._dict(session, run) for run in reversed(runs)]}

    def submit(self, project_id, payload):
        self.repo.get_project(project_id)
        safe_message = self.safe(payload.message)
        with self.repo.db.session() as session, session.begin():
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            existing = session.scalar(select(AgentRun).where(
                AgentRun.project_id == project_id, AgentRun.request_id == str(payload.request_id)))
            if existing:
                if (existing.message != safe_message or existing.selected_chapter_id != payload.selected_chapter_id
                        or existing.allow_text_rewrite != payload.allow_text_rewrite):
                    raise RuntimeError("agent_request_conflict")
                return self._dict(session, existing), False
            if session.scalar(select(AgentRun.id).where(AgentRun.project_id == project_id, AgentRun.status == "running")):
                raise RuntimeError("agent_busy")
            run = AgentRun(id=str(uuid.uuid4()), project_id=project_id, request_id=str(payload.request_id),
                           message=safe_message, selected_chapter_id=payload.selected_chapter_id,
                           allow_text_rewrite=payload.allow_text_rewrite, status="running", output="", history_json="[]")
            session.add(run)
            session.flush()
            return self._dict(session, run), True

    def finish(self, project_id, run_id, status, output, history=None):
        safe_output = self.safe(output)
        safe_history = self.safe(history or [])
        with self.repo.db.session() as session, session.begin():
            run = self._run(session, project_id, run_id)
            if run.status == "running":
                run.status, run.output = status, safe_output
                run.history_json = json.dumps(safe_history, ensure_ascii=False)

    def history(self, project_id, run_id):
        with self.repo.db.session() as session:
            rows = session.scalars(select(AgentRun).where(
                AgentRun.project_id == project_id, AgentRun.id != run_id, AgentRun.status == "completed")
                .order_by(AgentRun.created_at.desc()).limit(6)).all()
            # Include confirmation outcomes so old proposals are never mistaken for applied edits.
            return [(json.loads(row.history_json), self._dict(session, row)["action"]) for row in reversed(rows)]

    def claim_action(self, project_id, run_id, kind, args, status="executing"):
        safe_args = self.safe(args)
        if kind == "script" and safe_args != args:
            raise RuntimeError("脚本含有敏感配置内容，无法生成提案")
        args = safe_args
        with self.repo.db.session() as session, session.begin():
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            run = self._run(session, project_id, run_id)
            if run.status != "running":
                raise RuntimeError("agent_interrupted")
            action = session.scalar(select(AgentAction).where(AgentAction.run_id == run_id))
            if action:
                if action.kind == kind and json.loads(action.args_json) == args and action.status in ("completed", "pending"):
                    return self.action_dict(action), False
                raise RuntimeError("agent_one_action")
            action = AgentAction(id=str(uuid.uuid4()), run_id=run_id, kind=kind, status=status,
                                 args_json=json.dumps(args, ensure_ascii=False), result_json="{}")
            session.add(action)
            session.flush()
            return self.action_dict(action), True

    def action_result(self, action_id, status, result):
        result = self.safe(result)
        with self.repo.db.session() as session, session.begin():
            action = session.get(AgentAction, action_id)
            action.status, action.result_json = status, json.dumps(result, ensure_ascii=False)

    def decide(self, project_id, action_id, approve):
        conflict = False
        with self.repo.db.session() as session, session.begin():
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            action = session.get(AgentAction, action_id)
            if action is None:
                raise KeyError(action_id)
            run = self._run(session, project_id, action.run_id)
            if action.kind != "script" or run.status == "running":
                raise RuntimeError("agent_busy")
            if action.status != "pending":
                if action.status == ("applied" if approve else "rejected"):
                    return self.action_dict(action)
                raise RuntimeError("agent_proposal_closed")
            if not approve:
                action.status = "rejected"
            else:
                args = json.loads(action.args_json)
                if self.repo.get_chapter_record(args["chapter_id"]).project_id != project_id:
                    raise KeyError(args["chapter_id"])
                payload = ScriptUpdate.model_validate(args)
                try:
                    if session.get(Chapter, args["chapter_id"]).active_revision_id != args["before_revision_id"]:
                        raise RuntimeError("revision_conflict")
                    result = self.repo._edit_script(session, args["chapter_id"], payload.expected_revision,
                                                    [entry.model_dump() for entry in payload.entries])
                except RuntimeError as exc:
                    if str(exc) != "revision_conflict":
                        raise
                    action.status = "outdated"
                    conflict = True
                else:
                    action.status = "applied"
                    action.result_json = json.dumps({"chapter_id": result["chapter_id"], "revision": result["revision"]})
            response = self.action_dict(action)
        if conflict:
            raise RuntimeError("revision_conflict")
        return response
