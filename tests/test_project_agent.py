import asyncio
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select

from backend.alexandria.api.schemas import AgentMessage
from backend.alexandria.db.models import AgentRun, Artifact, AudioSegment
from backend.alexandria.main import create_app


class ProjectAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.temp.name), start_scheduler=False)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.repo = self.app.state.repository
        self.agent = self.app.state.agent
        self.project = self.new_project()
        self.pid = self.project["id"]
        self.cid = self.repo.list_chapters(self.pid)[0]["id"]
        self.base = f"/api/v1/projects/{self.pid}/agent"

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def new_project(self):
        return self.repo.create_project(title="测试", source_filename="test.txt", source_path="test.txt", source_sha256="a" * 64,
                                        chapters=[{"index": 1, "title": "第一章", "href": "1", "text": "原文。"}],
                                        render_start_mode="after_review_batch", release_batch_size=3)

    def model(self, calls):
        pending = list(calls)
        def respond(messages, info):
            call = pending.pop(0) if pending else None
            if isinstance(call, Exception):
                raise call
            return ModelResponse(parts=[ToolCallPart(call[0], call[1])] if call else [TextPart("已处理，请查看操作状态。")])
        self.agent.model_factory = lambda: FunctionModel(respond)

    def post(self, message="处理当前项目", **fields):
        payload = {"request_id": str(uuid.uuid4()), "message": message, **fields}
        response = self.client.post(self.base + "/messages", json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json(), payload

    def wait(self, run):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            response = self.client.get(self.base + "/runs/" + run["id"])
            self.assertEqual(response.status_code, 200, response.text)
            run = response.json()
            if run["status"] != "running":
                return run
            time.sleep(.02)
        self.fail("Agent did not finish")

    def editable(self):
        script = self.repo.save_revision(self.cid, "reviewed", [{"speaker": "NARRATOR", "text": "原文。", "instruct": ""}])
        job = self.repo.create_job(self.pid)
        self.repo.set_job_status(job["id"], "completed")
        return script

    def proposal(self, text="原文。", **fields):
        before = self.repo.get_script(self.cid)
        self.model([("read_script", {"chapter_id": self.cid}), ("propose_script", {
            "chapter_id": self.cid, "expected_revision": before["revision"],
            "entries": [{"speaker": "NARRATOR", "text": text, "instruct": "沉稳"}], "summary": "调整语气"})])
        run, _ = self.post("修改当前章旁白语气", selected_chapter_id=self.cid, **fields)
        return self.wait(run)

    def decide(self, action, approve=True):
        return self.client.post(self.base + "/proposals/" + action["id"], json={"approve": approve})

    def test_task_creation_and_request_and_tool_idempotency(self):
        self.model([("project_status", {}), ("create_task", {"task_type": "preprocess", "from_chapter": 1, "to_chapter": 1}),
                    ("create_task", {"task_type": "preprocess", "from_chapter": 1, "to_chapter": 1})])
        run, payload = self.post("预处理第1章")
        done = self.wait(run)
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["action"]["status"], "completed")
        self.assertEqual(done["action"]["result"]["status"], "queued")
        duplicate = self.client.post(self.base + "/messages", json=payload)
        self.assertEqual(duplicate.json()["id"], run["id"])
        self.assertEqual(len(self.repo.list_jobs(self.pid)["jobs"]), 1)
        payload["message"] = "另一条指令"
        self.assertEqual(self.client.post(self.base + "/messages", json=payload).status_code, 409)

    def test_proposal_confirmation_is_atomic_and_invalidates_audio(self):
        original = self.editable()
        with self.repo.db.session() as session, session.begin():
            session.add_all([
                Artifact(id="chapter", project_id=self.pid, chapter_id=self.cid, kind="chapter_mp3", path="a.mp3", sha256="a", size=1),
                Artifact(id="book", project_id=self.pid, kind="book_mp3", path="b.mp3", sha256="b", size=1),
                AudioSegment(project_id=self.pid, chapter_id=self.cid, revision_id=original["revision_id"], position=1, fingerprint="a", path="a.mp3", status="done"),
            ])
        done = self.proposal()
        self.assertEqual(done["status"], "completed", done)
        action = done["action"]
        self.assertEqual(action["status"], "pending")
        self.assertEqual(self.repo.get_script(self.cid)["revision"], original["revision"])
        confirmed = self.decide(action)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(self.decide(action).json(), confirmed.json())
        saved = self.repo.get_script(self.cid)
        self.assertEqual(saved["revision"], original["revision"] + 1)
        self.assertEqual(saved["kind"], "manual")
        self.assertEqual(saved["entries"][0]["text"], "原文。")
        with self.repo.db.session() as session:
            self.assertTrue(all(row.status == "stale" for row in session.scalars(select(Artifact))))
            self.assertEqual(session.scalar(select(AudioSegment)).status, "stale")

    def test_rejection_and_stale_proposals_do_not_overwrite(self):
        self.editable()
        first = self.proposal()["action"]
        self.assertEqual(self.decide(first, False).json()["status"], "rejected")
        self.assertEqual(self.repo.get_script(self.cid)["revision"], 1)
        self.assertEqual(self.decide(first).status_code, 409)
        second = self.proposal()["action"]
        self.repo.edit_script(self.cid, 1, [{"speaker": "NARRATOR", "text": "人工修改", "instruct": ""}])
        self.assertEqual(self.decide(second).status_code, 409)
        self.assertEqual(self.repo.get_script(self.cid)["entries"][0]["text"], "人工修改")
        self.assertEqual(self.client.get(self.base).json()["runs"][-1]["action"]["status"], "outdated")

    def test_original_text_protected_unless_explicitly_enabled(self):
        self.editable()
        denied = self.proposal(text="改写。")
        self.assertIsNone(denied["action"])
        self.assertEqual(self.repo.get_script(self.cid)["revision"], 1)
        allowed = self.proposal(text="改写。", allow_text_rewrite=True)
        self.assertEqual(allowed["action"]["status"], "pending", allowed)
        self.assertTrue(allowed["action"]["args"]["text_changed"])
        self.assertEqual(self.decide(allowed["action"]).status_code, 200)

    def test_project_isolation_for_reads_controls_and_confirmation(self):
        other = self.new_project()
        ocid = self.repo.list_chapters(other["id"])[0]["id"]
        other_job = self.repo.create_job(other["id"])
        self.model([("read_script", {"chapter_id": ocid})])
        run, _ = self.post()
        self.assertEqual(self.wait(run)["status"], "failed")
        self.model([("control_task", {"job_id": other_job["id"], "action": "cancel"})])
        run, _ = self.post()
        self.wait(run)
        self.assertEqual(self.repo.get_job(other_job["id"])["status"], "queued")
        self.repo.set_job_status(other_job["id"], "running")
        self.model([("project_status", {})])
        query, _ = self.post("查询当前项目")
        self.wait(query)
        with self.repo.db.session() as session:
            self.assertNotIn(other_job["id"], session.get(AgentRun, query["id"]).history_json)
        self.editable()
        action = self.proposal()["action"]
        self.assertEqual(self.client.post(f"/api/v1/projects/{other['id']}/agent/proposals/{action['id']}", json={"approve": True}).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/projects/{other['id']}/agent/runs/" + run["id"]).status_code, 404)
        self.assertEqual(self.client.post(self.base + "/messages", json={"request_id": str(uuid.uuid4()), "message": "读脚本", "selected_chapter_id": ocid}).status_code, 404)

    def test_tool_schema_limits_and_one_mutation_per_turn(self):
        self.model([("create_task", {"task_type": "bilibili"})])
        run, _ = self.post()
        self.wait(run)
        self.assertEqual(self.repo.list_jobs(self.pid)["jobs"], [])
        self.model([("create_task", {"task_type": "preprocess"}), ("create_task", {"task_type": "character_analysis"})])
        run, _ = self.post()
        self.wait(run)
        self.assertEqual(len(self.repo.list_jobs(self.pid)["jobs"]), 1)
        self.agent.model_factory = lambda: FunctionModel(lambda messages, info: ModelResponse(parts=[ToolCallPart("project_status", {})]))
        run, _ = self.post()
        done = self.wait(run)
        self.assertEqual(done["status"], "failed")
        self.assertIn("request_limit", done["output"])

    def test_busy_restart_and_history_persistence(self):
        async def slow(messages, info):
            await asyncio.sleep(.3)
            return ModelResponse(parts=[TextPart("查询完成")])
        self.agent.model_factory = lambda: FunctionModel(slow)
        run, payload = self.post()
        self.assertEqual(self.client.post(self.base + "/messages", json=payload).json()["id"], run["id"])
        self.assertEqual(self.client.post(self.base + "/messages", json={**payload, "request_id": str(uuid.uuid4())}).status_code, 409)
        self.wait(run)
        interrupted, _ = self.agent.store.submit(self.pid, AgentMessage(request_id=uuid.uuid4(), message="待重启"))
        self.agent.store.claim_action(self.pid, interrupted["id"], "task", {"type": "preprocess"})
        self.client.__exit__(None, None, None)
        self.app = create_app(Path(self.temp.name), start_scheduler=False)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.repo, self.agent = self.app.state.repository, self.app.state.agent
        rows = self.client.get(self.base).json()["runs"]
        self.assertEqual(rows[-1]["status"], "interrupted")
        self.assertEqual(rows[-1]["action"]["status"], "interrupted")
        self.assertEqual(rows[0]["output"], "查询完成")
        self.assertEqual(self.repo.list_jobs(self.pid)["jobs"], [])

    def test_secret_redaction_in_requests_history_and_provider_errors(self):
        self.repo.update_settings({"llm_api_key": "secret-test-key"})
        self.model([RuntimeError("https://provider.test?key=secret-test-key")])
        run, _ = self.post("secret-test-key")
        self.wait(run)
        self.assertNotIn("secret-test-key", self.client.get(self.base).text)
        with self.repo.db.session() as session:
            row = session.get(AgentRun, run["id"])
            self.assertNotIn("secret-test-key", row.message + row.output + row.history_json)
        self.repo.update_settings({"llm_api_key": "key"})
        self.assertEqual(self.agent.store.safe({"tool_call_id": "key123", "api_key": "key"}),
                         {"tool_call_id": "***123", "api_key": "***"})

    def test_simultaneous_manual_saves_accept_only_one_revision(self):
        self.editable()
        def save(text):
            try:
                return self.repo.edit_script(self.cid, 1, [{"speaker": "NARRATOR", "text": text, "instruct": ""}])["revision"]
            except RuntimeError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ["一", "二"]))
        self.assertCountEqual(results, [2, "revision_conflict"])

    def test_pause_must_finish_before_proposal_can_apply(self):
        self.editable()
        action = self.proposal()["action"]
        job = self.repo.create_job(self.pid, "character_analysis")
        self.repo.set_job_status(job["id"], "running")
        self.repo.request_pause(job["id"])
        self.assertEqual(self.decide(action).status_code, 409)
        self.repo.set_job_status(job["id"], "paused")
        self.assertEqual(self.decide(action).status_code, 200)

    def test_reused_chapter_number_cannot_apply_old_proposal(self):
        self.editable()
        action = self.proposal()["action"]
        (Path(self.temp.name) / "test.txt").write_text("第1章\n新原文", encoding="utf-8")
        self.repo.reparse_project(self.pid)
        new_id = self.repo.list_chapters(self.pid)[0]["id"]
        self.assertEqual(new_id, self.cid)
        new_script = self.repo.save_revision(new_id, "reviewed", [{"speaker": "NARRATOR", "text": "新原文", "instruct": ""}])
        self.assertEqual(new_script["revision"], 1)
        job = self.repo.create_job(self.pid)
        self.repo.set_job_status(job["id"], "completed")
        self.assertEqual(self.decide(action).status_code, 409)

    def test_timeout_after_submitting_task_preserves_receipt(self):
        calls = 0
        async def respond(messages, info):
            nonlocal calls
            calls += 1
            if calls == 1:
                return ModelResponse(parts=[ToolCallPart("create_task", {"task_type": "preprocess"})])
            await asyncio.sleep(2)
            return ModelResponse(parts=[TextPart("完成")])
        self.agent.model_factory = lambda: FunctionModel(respond)
        timeout = asyncio.timeout
        with patch("backend.alexandria.services.agent.asyncio.timeout", side_effect=lambda seconds: timeout(.15)):
            run, payload = self.post()
            done = self.wait(run)
        self.assertEqual(done["status"], "failed")
        self.assertIn("超时", done["output"])
        self.assertEqual(done["action"]["status"], "completed")
        self.assertEqual(self.client.post(self.base + "/messages", json=payload).json()["id"], run["id"])
        self.assertEqual(len(self.repo.list_jobs(self.pid)["jobs"]), 1)

    def test_old_database_upgrade_preserves_projects(self):
        self.editable()
        self.client.__exit__(None, None, None)
        from alembic import command
        from alembic.config import Config
        from backend.alexandria.main import ROOT
        config = Config(str(ROOT / "backend/alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "backend/alembic"))
        config.set_main_option("sqlalchemy.url", "sqlite:///" + (Path(self.temp.name) / "alexandria.db").as_posix())
        command.downgrade(config, "0006")
        self.app = create_app(Path(self.temp.name), start_scheduler=False)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.repo, self.agent = self.app.state.repository, self.app.state.agent
        self.assertEqual(self.repo.get_script(self.cid)["revision"], 1)
        self.assertEqual(self.client.get(self.base).json(), {"runs": []})
