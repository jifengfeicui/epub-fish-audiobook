from __future__ import annotations

import json
import uuid

from sqlalchemy import func, select, update

from app.script_validation import validate_script_entries
from backend.alexandria.api.schemas import ScriptUpdate
from backend.alexandria.db.models import AgentAction, AgentConversation, AgentRun, Chapter
from backend.alexandria.db.repository import Repository, _iso


class AgentStore:
    """Project-scoped conversations and persistent, idempotent write receipts."""

    def __init__(self, repo: Repository):
        self.repo = repo
        self.secret_values = lambda: ()

    def safe(self, value, secrets=()):
        value = self.repo._redact(value)
        settings = self.repo.get_settings(reveal_secrets=True)
        keys = [settings.get("fish_api_key", ""), settings.get("llm_api_key", "")]
        keys.extend(profile.get("api_key", "") for profile in settings.get("llm_profiles", []))
        keys.extend(self.secret_values())
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
        actions = [self.action_dict(action) for action in session.scalars(
            select(AgentAction).where(AgentAction.run_id == run.id).order_by(AgentAction.position))]
        return {
            "id": run.id, "request_id": run.request_id, "message": run.message,
            "status": run.status, "output": run.output, "created_at": _iso(run.created_at),
            "selected_chapter_id": run.selected_chapter_id, "allow_text_rewrite": run.allow_text_rewrite,
            "auto_apply_scripts": run.auto_apply_scripts,
            "automation_id": run.automation_id,
            "conversation_id": run.conversation_id,
            "actions": actions, "action": actions[0] if actions else None,
        }

    @staticmethod
    def action_dict(action):
        return {"id": action.id, "position": action.position, "kind": action.kind, "status": action.status,
                "args": json.loads(action.args_json), "result": json.loads(action.result_json)}

    def get(self, project_id, run_id):
        with self.repo.db.session() as session:
            return self._dict(session, self._run(session, project_id, run_id))

    def _conversation(self, session, project_id, conversation_id=None):
        cid = str(conversation_id or project_id)
        row = session.get(AgentConversation, cid)
        if row is None and conversation_id is None:
            row = AgentConversation(id=cid, project_id=project_id, title="新对话")
            session.add(row)
            session.flush()
        if row is None or row.project_id != project_id:
            raise KeyError(cid)
        return row

    @staticmethod
    def _conversation_dict(row):
        return {"id": row.id, "title": row.title, "created_at": _iso(row.created_at)}

    def conversations(self, project_id):
        self.repo.get_project(project_id)
        with self.repo.db.session() as session, session.begin():
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            self._conversation(session, project_id)
            latest = select(AgentRun.conversation_id, func.max(AgentRun.created_at).label("last_at")).group_by(
                AgentRun.conversation_id).subquery()
            rows = session.scalars(select(AgentConversation).outerjoin(latest, latest.c.conversation_id == AgentConversation.id)
                .where(AgentConversation.project_id == project_id)
                .order_by(func.coalesce(latest.c.last_at, AgentConversation.created_at).desc(), AgentConversation.id)).all()
            return [self._conversation_dict(row) for row in rows]

    def create_conversation(self, project_id, title="新对话"):
        self.repo.get_project(project_id)
        with self.repo.db.session() as session, session.begin():
            row = AgentConversation(id=str(uuid.uuid4()), project_id=project_id, title=self.safe(title))
            session.add(row)
            session.flush()
            return self._conversation_dict(row)

    def rename_conversation(self, project_id, conversation_id, title):
        with self.repo.db.session() as session, session.begin():
            row = self._conversation(session, project_id, conversation_id)
            row.title = self.safe(title)
            return self._conversation_dict(row)

    def conversation(self, project_id, conversation_id=None):
        conversations = self.conversations(project_id)
        with self.repo.db.session() as session:
            row = self._conversation(session, project_id, conversation_id)
            # shortcut: display the latest 50 turns; add pagination when older history needs browsing.
            runs = session.scalars(select(AgentRun).where(AgentRun.project_id == project_id, AgentRun.conversation_id == row.id)
                                   .order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(50)).all()
            busy = session.scalar(select(AgentRun.id).where(AgentRun.project_id == project_id, AgentRun.status == "running"))
            return {"runs": [self._dict(session, run) for run in reversed(runs)], "conversation_id": row.id,
                    "conversations": conversations, "project_busy": bool(busy)}

    def submit(self, project_id, payload):
        self.repo.get_project(project_id)
        safe_message = self.safe(payload.message)
        with self.repo.db.session() as session, session.begin():
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            conversation = self._conversation(session, project_id, payload.conversation_id)
            existing = session.scalar(select(AgentRun).where(
                AgentRun.project_id == project_id, AgentRun.request_id == str(payload.request_id)))
            if existing:
                if (existing.message != safe_message or existing.selected_chapter_id != payload.selected_chapter_id
                        or existing.allow_text_rewrite != payload.allow_text_rewrite or existing.conversation_id != conversation.id):
                    raise RuntimeError("agent_request_conflict")
                return self._dict(session, existing), False
            if session.scalar(select(AgentRun.id).where(AgentRun.project_id == project_id, AgentRun.status == "running")):
                raise RuntimeError("agent_busy")
            run = self.create_run(session, project_id, payload, safe_message)
            return self._dict(session, run), True

    def create_run(self, session, project_id, payload, safe_message, automation_id=None):
        if automation_id:
            # Scheduled wakeups have their own context, independent of interactive conversations.
            conversation = session.get(AgentConversation, automation_id)
            if conversation is None:
                conversation = AgentConversation(id=automation_id, project_id=project_id, title="定时任务：" + safe_message[:70])
                session.add(conversation)
                session.flush()
        else:
            conversation = self._conversation(session, project_id, payload.conversation_id)
        if conversation.title == "新对话":
            conversation.title = safe_message[:40]
        run = AgentRun(id=str(uuid.uuid4()), project_id=project_id, request_id=str(payload.request_id),
                       conversation_id=conversation.id,
                       message=safe_message, selected_chapter_id=payload.selected_chapter_id,
                       allow_text_rewrite=payload.allow_text_rewrite, auto_apply_scripts=True,
                       automation_id=automation_id, status="running", output="", history_json="[]")
        session.add(run)
        session.flush()
        return run

    def finish(self, project_id, run_id, status, output, history=None):
        safe_output = self.safe(output)
        safe_history = self.safe(history or [])
        with self.repo.db.session() as session, session.begin():
            run = self._run(session, project_id, run_id)
            if run.status == "running":
                run.status, run.output = status, safe_output
                run.history_json = json.dumps(safe_history, ensure_ascii=False)

    def history(self, project_id, run_id, instructions=None):
        with self.repo.db.session() as session:
            current = self._run(session, project_id, run_id)
            rows = session.scalars(select(AgentRun).where(
                AgentRun.project_id == project_id, AgentRun.conversation_id == current.conversation_id,
                AgentRun.id != run_id, AgentRun.status == "completed")
                .order_by(AgentRun.created_at.desc()).limit(6)).all()
            # Include confirmation outcomes so old proposals are never mistaken for applied edits.
            result = []
            for row in reversed(rows):
                messages = json.loads(row.history_json)
                # Keep old conversations visible, but never replay obsolete capability policies.
                if instructions is not None and not any((message.get("instructions") or "").strip() == instructions.strip() for message in messages):
                    continue
                result.append((messages, self._dict(session, row)["actions"]))
            return result

    def claim_action(self, project_id, run_id, kind, args, status="executing"):
        safe_args = self.safe(args)
        if kind in ("script", "script_batch") and safe_args != args:
            raise RuntimeError("脚本含有敏感配置内容，无法生成提案")
        args = safe_args
        with self.repo.db.session() as session, session.begin():
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            run = self._run(session, project_id, run_id)
            if run.status != "running":
                raise RuntimeError("agent_interrupted")
            actions = session.scalars(select(AgentAction).where(AgentAction.run_id == run_id)
                                      .order_by(AgentAction.position)).all()
            for action in actions:
                if action.kind == kind and json.loads(action.args_json) == args:
                    if action.status not in ("completed", "pending", "applied"):
                        raise RuntimeError("该操作已失败或中断，不会自动重放")
                    return self.action_dict(action), False
            if any(action.status in ("failed", "interrupted", "executing") for action in actions):
                raise RuntimeError("此前操作失败或结果待核对，停止后续写操作")
            action = AgentAction(id=str(uuid.uuid4()), run_id=run_id, kind=kind, status=status,
                                 position=len(actions),
                                 args_json=json.dumps(args, ensure_ascii=False), result_json="{}")
            session.add(action)
            session.flush()
            return self.action_dict(action), True

    def action_result(self, action_id, status, result):
        result = self.safe(result)
        with self.repo.db.session() as session, session.begin():
            action = session.get(AgentAction, action_id)
            action.status, action.result_json = status, json.dumps(result, ensure_ascii=False)

    def _apply_scripts(self, session, project_id, action):
        args = json.loads(action.args_json)
        changes = args["changes"] if action.kind == "script_batch" else [args]
        saved = []
        for change in changes:
            chapter = session.get(Chapter, change["chapter_id"])
            if chapter is None or chapter.project_id != project_id:
                raise KeyError(change["chapter_id"])
            if chapter.active_revision_id != change["before_revision_id"]:
                raise RuntimeError("revision_conflict")
            payload = ScriptUpdate.model_validate(change)
            entries = [entry.model_dump() for entry in payload.entries]
            errors = validate_script_entries(entries)
            if errors:
                raise ValueError("; ".join(errors))
            result = self.repo._edit_script(session, chapter.id, payload.expected_revision, entries)
            saved.append({"chapter_id": chapter.id, "revision": result["revision"]})
        action.status = "applied"
        result = ({"chapters": saved, "modified_count": len(saved), "skipped_count": args["skipped_count"]}
                  if action.kind == "script_batch" else saved[0])
        action.result_json = json.dumps(result)

    def apply_direct(self, project_id, action_id):
        # Revisions and the successful receipt commit together, even if the process stops next.
        try:
            with self.repo.db.session() as session, session.begin():
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                action = session.get(AgentAction, action_id)
                if action is None:
                    raise KeyError(action_id)
                run = self._run(session, project_id, action.run_id)
                if action.status == "applied":
                    return self.action_dict(action)
                if action.kind not in ("script", "script_batch") or action.status != "executing" or run.status != "running":
                    raise RuntimeError("agent_interrupted")
                if not run.auto_apply_scripts:
                    raise RuntimeError("agent_script_permission_revoked")
                self._apply_scripts(session, project_id, action)
                return self.action_dict(action)
        except Exception as exc:
            with self.repo.db.session() as session, session.begin():
                action = session.get(AgentAction, action_id)
                run = session.get(AgentRun, action.run_id) if action else None
                if action and run.project_id == project_id and action.status == "executing":
                    action.status = "failed"
                    action.result_json = json.dumps({"message": self.safe(str(exc))})
            raise

    def decide(self, project_id, action_id, approve):
        conflict = False
        with self.repo.db.session() as session, session.begin():
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            action = session.get(AgentAction, action_id)
            if action is None:
                raise KeyError(action_id)
            run = self._run(session, project_id, action.run_id)
            if action.kind not in ("script", "script_batch") or run.status == "running":
                raise RuntimeError("agent_busy")
            if action.status != "pending":
                if action.status == ("applied" if approve else "rejected"):
                    return self.action_dict(action)
                raise RuntimeError("agent_proposal_closed")
            if not approve:
                action.status = "rejected"
            else:
                try:
                    # A stale chapter must roll back earlier chapters in the same batch.
                    with session.begin_nested():
                        self._apply_scripts(session, project_id, action)
                except RuntimeError as exc:
                    if str(exc) != "revision_conflict":
                        raise
                    action.status = "outdated"
                    conflict = True
            response = self.action_dict(action)
        if conflict:
            raise RuntimeError("revision_conflict")
        return response
