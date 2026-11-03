import json
import sqlite3
import unittest
import uuid
import time
import threading
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select

from backend.alexandria.api.schemas import AgentMessage, AutomationCreate, AutomationUpdate
from backend.alexandria.db.models import AgentRun, Chapter, ProjectAutomation
from backend.alexandria.main import create_app
from tests import test_project_agent as fixtures


class AgentCapabilitiesTests(unittest.TestCase):
    setUp = fixtures.ProjectAgentTests.setUp
    tearDown = fixtures.ProjectAgentTests.tearDown
    new_project = fixtures.ProjectAgentTests.new_project
    model = fixtures.ProjectAgentTests.model
    post = fixtures.ProjectAgentTests.post
    wait = fixtures.ProjectAgentTests.wait
    editable = fixtures.ProjectAgentTests.editable

    def test_project_tools_multiple_actions_and_deduplication(self):
        self.model([("update_project", {"changes": {"title": "新标题"}}),
                    ("update_project", {"changes": {"title": "新标题"}}),
                    ("select_chapters", {"included_chapter_ids": [self.cid]}),
                    ("read_source", {"chapter_id": self.cid}), ("read_characters", {}),
                    ("read_logs", {}), ("list_artifacts", {})])
        run, payload = self.post("改标题、保留第1章，并查询资料")
        done = self.wait(run)
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual([action["kind"] for action in done["actions"]], ["project", "chapters"])
        self.assertEqual([action["position"] for action in done["actions"]], [0, 1])
        self.assertEqual(done["action"], done["actions"][0])
        self.assertEqual(self.repo.get_project(self.pid)["title"], "新标题")
        self.assertEqual(self.client.post(self.base + "/messages", json=payload).json()["actions"], done["actions"])

    def test_bilibili_tools_and_control_with_mock_service(self):
        self.model([("bilibili_status", {}), ("extract_bilibili_metadata", {}),
                    ("create_bilibili_task", {"payload": {"action": "publish", "parts": 10,
                        "source": "https://example.org/book", "title": "测试书", "visibility": "public"}})])
        with patch.object(self.agent.bilibili, "status", return_value={"appendable_count": 10}), \
             patch.object(self.agent.bilibili, "account_status", return_value={"logged_in": True, "name": "测试"}), \
             patch.object(self.agent.bilibili, "extract_metadata", return_value={"author": "作者"}):
            run, _ = self.post("公开首发10个分P，来源为https://example.org/book")
            done = self.wait(run)
        self.assertEqual(done["status"], "completed", done)
        job = done["action"]["result"]
        self.assertTrue(job["payload"]["confirm_public"])
        self.assertEqual(job["type"], "bilibili")
        self.model([("control_task", {"job_id": job["id"], "action": "pause"})])
        run, _ = self.post("暂停上传")
        self.assertEqual(self.wait(run)["action"]["result"]["status"], "paused")

    def test_batch_save_is_atomic_and_checks_foreign_ids(self):
        self.editable()
        before = self.repo.get_script(self.cid)
        rows = [{"chapter_id": self.cid, "expected_revision": before["revision"],
                 "entries": [{"speaker": "NARRATOR", "text": "新正文", "instruct": "平静"}]}]
        self.model([("save_scripts_batch", {"changes": rows, "summary": "按要求改写"})] * 2)
        run, _ = self.post("改写本章")
        done = self.wait(run)
        self.assertEqual(len(done["actions"]), 1)
        self.assertEqual(done["action"]["status"], "applied", done)
        self.assertEqual(self.repo.get_script(self.cid)["revision"], 2)
        rows[0]["expected_revision"] = 1
        self.model([("save_scripts_batch", {"changes": rows, "summary": "过期修改"})])
        run, _ = self.post()
        self.assertEqual(self.wait(run)["actions"], [])
        other = self.new_project()
        rows[0]["chapter_id"] = self.repo.list_chapters(other["id"])[0]["id"]
        self.model([("save_scripts_batch", {"changes": rows, "summary": "越界修改"})])
        run, _ = self.post()
        self.assertEqual(self.wait(run)["status"], "failed")

    def test_roles_voice_pool_force_preprocess_and_archive(self):
        self.editable()
        voices = self.repo.list_voices()
        narrator = voices[0]["id"]
        self.model([("refresh_characters", {}), ("update_characters", {"characters": []}),
                    ("update_voice_pool", {"changes": {"excluded_voice_ids": [], "narrator_voice_profile_id": narrator}}),
                    ("update_project", {"changes": {"archived": True}})])
        run, _ = self.post()
        done = self.wait(run)
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(len(done["actions"]), 4)
        self.assertEqual(self.repo.get_project_voice_pool(self.pid)["narrator_voice_profile_id"], narrator)
        self.assertIsNotNone(self.repo.get_project(self.pid)["archived_at"])
        self.model([("create_task", {"task_type": "preprocess", "force_regenerate": True})])
        run, _ = self.post("强制重新预解析")
        self.assertTrue(self.wait(run)["action"]["result"]["payload"]["force_regenerate"])

    def test_reparse_only_exempts_calling_run_and_keeps_receipt(self):
        self.editable()
        (Path(self.temp.name) / "test.txt").write_text("第1章\n新原文", encoding="utf-8")
        running, _ = self.agent.store.submit(self.pid, AgentMessage(request_id=uuid.uuid4(), message="等待"))
        with self.assertRaisesRegex(RuntimeError, "agent_busy"):
            self.repo.reparse_project(self.pid, agent_run_id="another-run")
        self.agent.store.finish(self.pid, running["id"], "completed", "完成")
        self.model([("reparse_project", {})])
        run, _ = self.post("重新识别本书章节")
        done = self.wait(run)
        self.assertEqual(done["action"]["status"], "completed", done)
        self.assertEqual(self.repo.get_script(self.cid)["entries"], [])
        self.assertEqual(self.repo.list_jobs(self.pid)["jobs"], [])

    def test_delete_project_terminates_run_and_cascades_plans(self):
        plan = self.agent.automations.create(self.pid, AutomationCreate(instruction="查询状态", interval_seconds=600))
        self.model([("delete_project", {}), ("create_task", {"task_type": "preprocess"})])
        self.post("永久删除当前项目")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.client.get(self.base).status_code == 404:
                break
            time.sleep(.02)
        else:
            self.fail("Project was not deleted")
        with self.repo.db.session() as session:
            self.assertIsNone(session.get(ProjectAutomation, plan["id"]))
        self.assertEqual(self.client.get(self.base).status_code, 404)

    def test_bilibili_credentials_are_redacted_before_model_and_in_storage(self):
        cookie = self.agent.bilibili.cookie_path
        cookie.parent.mkdir(parents=True, exist_ok=True)
        cookie.write_text(json.dumps({"cookie_info": {"cookies": [{"name": "SESSDATA", "value": "cookie-secret"}]},
                                      "token_info": {"access_token": "token-secret"}}), encoding="utf-8")
        calls = 0
        def respond(messages, info):
            nonlocal calls
            self.assertNotIn("cookie-secret", str(messages))
            self.assertNotIn("token-secret", str(messages))
            calls += 1
            return ModelResponse(parts=[ToolCallPart("bilibili_status", {})] if calls == 1 else [TextPart("完成")])
        self.agent.model_factory = lambda: FunctionModel(respond)
        with patch.object(self.agent.bilibili, "status", return_value={"publication": {"sync_error": "cookie-secret token-secret"}}), \
             patch.object(self.agent.bilibili, "account_status", return_value={"logged_in": True}):
            run, _ = self.post("检查cookie-secret")
            self.assertEqual(self.wait(run)["status"], "completed")
        self.assertNotIn("cookie-secret", self.client.get(self.base).text)
        self.assertNotIn("token-secret", self.client.get(self.base).text)


class ProjectAutomationTests(unittest.TestCase):
    setUp = fixtures.ProjectAgentTests.setUp
    tearDown = fixtures.ProjectAgentTests.tearDown
    new_project = fixtures.ProjectAgentTests.new_project
    model = fixtures.ProjectAgentTests.model
    post = fixtures.ProjectAgentTests.post
    wait = fixtures.ProjectAgentTests.wait

    def clock(self):
        self.now = datetime(2026, 10, 29, 2, tzinfo=timezone.utc)
        self.agent.automations.now = lambda: self.now

    def plan(self, **fields):
        return self.agent.automations.create(self.pid, AutomationCreate(instruction="查询项目进度", interval_seconds=600, **fields))

    def test_api_validation_and_project_isolation(self):
        base = f"/api/v1/projects/{self.pid}/automations"
        for body in ({"instruction": "  ", "interval_seconds": 600}, {"instruction": "查状态", "interval_seconds": 0},
                     {"instruction": "查状态", "interval_seconds": 600, "first_run_at": "2026-10-29T10:00:00"}):
            self.assertEqual(self.client.post(base, json=body).status_code, 422)
        other = self.new_project()
        foreign = self.repo.list_chapters(other["id"])[0]["id"]
        self.assertEqual(self.client.post(base, json={"instruction": "查状态", "interval_seconds": 600,
                                                     "selected_chapter_id": foreign}).status_code, 404)
        plan = self.client.post(base, json={"instruction": "查状态", "interval_seconds": 600}).json()
        other_base = f"/api/v1/projects/{other['id']}/automations/{plan['id']}"
        self.assertEqual(self.client.patch(other_base, json={"enabled": False}).status_code, 404)
        self.assertEqual(self.client.delete(other_base).status_code, 404)
        self.assertEqual(self.client.patch(base + "/" + plan["id"], json={"enabled": False}).status_code, 200)
        self.assertEqual(self.client.delete(base + "/" + plan["id"]).status_code, 204)
        self.assertEqual(self.client.get(base).json(), {"automations": []})

    def test_delayed_first_run_atomic_claim_and_busy_coalescing(self):
        self.clock()
        plan = self.plan()
        self.assertEqual(datetime.fromisoformat(plan["next_run_at"]), self.now + timedelta(minutes=10))
        self.assertEqual(self.agent.automations.claim_due(), [])
        self.now += timedelta(minutes=40)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: self.agent.automations.claim_due(), range(2)))
        self.assertEqual(sum(map(len, claims)), 1)
        pid, run = next(claim[0] for claim in claims if claim)
        self.now += timedelta(minutes=30)
        self.assertEqual(self.agent.automations.claim_due(), [])
        self.agent.store.finish(pid, run["id"], "completed", "查询完成")
        follow = self.agent.automations.claim_due()
        self.assertEqual(len(follow), 1)
        self.assertEqual(self.agent.automations.claim_due(), [])

    def test_agent_creates_schedule_and_wakeup_submits_ten_append_parts(self):
        self.clock()
        self.model([("create_automation", {"instruction": "查询B站状态，给当前稿件追加最多10个已准备分P；有未完成任务时等待。",
                                           "interval_seconds": 600})])
        run, _ = self.post("每10分钟追加10个分P", selected_chapter_id=self.cid)
        done = self.wait(run)
        self.assertEqual(done["action"]["kind"], "automation_create", done)
        plan = self.agent.automations.list(self.pid)[0]
        self.assertEqual(plan["selected_chapter_id"], self.cid)
        self.assertEqual(self.repo.list_jobs(self.pid)["jobs"], [])
        self.now += timedelta(minutes=10)
        calls = 0
        def respond(messages, info):
            nonlocal calls
            calls += 1
            prompts = [str(part.content) for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
            self.assertIn("定时唤醒", prompts[-1])
            self.assertIn(plan["id"], prompts[-1])
            if calls == 1:
                return ModelResponse(parts=[ToolCallPart("bilibili_status", {})])
            if calls == 2:
                state = next(part.content for part in messages[-1].parts if isinstance(part, ToolReturnPart))
                self.assertEqual(state["appendable_count"], 10)
                return ModelResponse(parts=[ToolCallPart("create_bilibili_task", {"payload": {"action": "append", "parts": 10}})])
            return ModelResponse(parts=[TextPart("本轮已提交10个分P追加任务")])
        self.agent.model_factory = lambda: FunctionModel(respond)
        with patch.object(self.agent.bilibili, "status", return_value={"appendable_count": 10, "publication": {"bvid": "BV1234567890"}}), \
             patch.object(self.agent.bilibili, "account_status", return_value={"logged_in": True}):
            self.client.portal.call(self.agent.automations.tick)
            scheduled = self.agent.automations.list(self.pid)[0]
            result = self.wait({"id": scheduled["last_run_id"]})
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["automation_id"], plan["id"])
        job = result["action"]["result"]
        self.assertEqual(job["payload"]["parts"], 10)
        self.assertEqual(job["payload"]["action"], "append")
        self.now += timedelta(minutes=30)
        pid, later_run = self.agent.automations.claim_due()[0]
        self.agent.store.finish(pid, later_run["id"], "completed", "任务尚未结束，本轮跳过")
        self.assertEqual(len(self.repo.list_jobs(self.pid)["jobs"]), 1)
        self.repo.set_job_status(job["id"], "failed", "远端结果不确定")
        self.assertEqual(self.agent.automations.claim_due(), [])
        paused = self.agent.automations.list(self.pid)[0]
        self.assertFalse(paused["enabled"])
        self.assertIn("不确定", paused["paused_reason"])

    def test_failure_pause_resume_and_disabled_plan(self):
        self.clock()
        plan = self.plan(first_run_at=self.now)
        pid, run = self.agent.automations.claim_due()[0]
        self.agent.store.finish(pid, run["id"], "failed", "模型请求失败")
        self.agent.automations.claim_due()
        self.assertFalse(self.agent.automations.list(pid)[0]["enabled"])
        self.agent.automations.update(pid, plan["id"], AutomationUpdate(enabled=True))
        resumed = self.agent.automations.list(pid)[0]
        self.assertEqual(resumed["last_run_id"], run["id"])
        self.assertEqual(resumed["last_run"]["output"], "模型请求失败")
        self.assertEqual(self.agent.automations.claim_due(), [])
        self.now += timedelta(minutes=10)
        self.assertEqual(len(self.agent.automations.claim_due()), 1)
        self.agent.automations.update(pid, plan["id"], AutomationUpdate(enabled=False))
        self.now += timedelta(days=1)
        self.assertEqual(self.agent.automations.claim_due(), [])

    def test_restart_retains_plan_and_pauses_interrupted_run(self):
        self.clock()
        plan = self.plan(first_run_at=self.now)
        self.agent.automations.claim_due()
        self.client.__exit__(None, None, None)
        from fastapi.testclient import TestClient
        self.app = create_app(Path(self.temp.name), start_scheduler=False)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.repo, self.agent = self.app.state.repository, self.app.state.agent
        self.agent.automations.claim_due()
        restored = self.agent.automations.list(self.pid)[0]
        self.assertEqual(restored["id"], plan["id"])
        self.assertFalse(restored["enabled"])
        self.assertIn("中断", restored["paused_reason"])

    def test_explicit_timezone_and_rescheduled_next_run(self):
        self.clock()
        start = datetime.fromisoformat("2026-10-29T10:10:00+08:00")
        plan = self.plan(first_run_at=start)
        saved = self.agent.automations.list(self.pid)[0]
        self.assertEqual(saved["next_run_at"], "2026-10-29T02:10:00+00:00")
        self.now += timedelta(minutes=9)
        self.assertEqual(self.agent.automations.claim_due(), [])
        self.agent.automations.update(self.pid, plan["id"], AutomationUpdate(next_run_at=start + timedelta(minutes=1)))
        self.now += timedelta(minutes=1)
        self.assertEqual(self.agent.automations.claim_due(), [])
        self.now += timedelta(minutes=1)
        self.assertEqual(len(self.agent.automations.claim_due()), 1)

    def test_schedule_survives_idle_restart_without_catchup_storm(self):
        self.clock()
        plan = self.plan()
        self.client.__exit__(None, None, None)
        from fastapi.testclient import TestClient
        self.app = create_app(Path(self.temp.name), start_scheduler=False)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.repo, self.agent = self.app.state.repository, self.app.state.agent
        self.now += timedelta(days=1)
        self.agent.automations.now = lambda: self.now
        claims = self.agent.automations.claim_due()
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0][1]["automation_id"], plan["id"])
        self.assertEqual(self.agent.automations.claim_due(), [])

    def test_stale_current_chapter_does_not_target_reused_id(self):
        self.clock()
        self.plan(selected_chapter_id=self.cid, first_run_at=self.now)
        with self.repo.db.session() as session, session.begin():
            session.get(Chapter, self.cid).source_hash = "b" * 64
        self.assertEqual(self.agent.automations.claim_due(), [])
        self.assertFalse(self.agent.automations.list(self.pid)[0]["enabled"])

    def test_scheduled_runs_cannot_recursively_create_plans(self):
        self.clock()
        self.plan(first_run_at=self.now)
        self.model([("create_automation", {"instruction": "递归计划", "interval_seconds": 60})])
        self.client.portal.call(self.agent.automations.tick)
        run_id = self.agent.automations.list(self.pid)[0]["last_run_id"]
        self.wait({"id": run_id})
        self.assertEqual(len(self.agent.automations.list(self.pid)), 1)

    def test_lifespan_wakes_agent_while_business_runner_is_busy(self):
        self.client.__exit__(None, None, None)
        from fastapi.testclient import TestClient
        self.app = create_app(Path(self.temp.name), start_scheduler=True)
        self.repo, self.agent = self.app.state.repository, self.app.state.agent
        entered, release, woke = threading.Event(), threading.Event(), threading.Event()
        def slow_business_job(job_id):
            entered.set()
            release.wait(5)
            self.repo.set_job_status(job_id, "completed")
        def respond(messages, info):
            woke.set()
            return ModelResponse(parts=[TextPart("后台任务仍在执行，本轮已查询")])
        self.agent.model_factory = lambda: FunctionModel(respond)
        self.client = TestClient(self.app)
        with patch.object(self.app.state.scheduler.runner, "run", side_effect=slow_business_job):
            self.client.__enter__()
            try:
                self.repo.create_job(self.pid)
                self.assertTrue(entered.wait(2))
                self.plan(first_run_at=datetime.now(timezone.utc))
                self.assertTrue(woke.wait(3), "Automation was blocked by the business runner")
            finally:
                release.set()

    def test_duplicate_upload_from_later_wakeup_is_rejected_and_plan_pauses(self):
        self.clock()
        plan = self.plan(first_run_at=self.now)
        self.model([("create_bilibili_task", {"payload": {"action": "append", "parts": 10}})])
        self.client.portal.call(self.agent.automations.tick)
        first = self.agent.automations.list(self.pid)[0]["last_run_id"]
        self.wait({"id": first})
        self.now += timedelta(minutes=10)
        self.model([("create_bilibili_task", {"payload": {"action": "append", "parts": 10}})])
        self.client.portal.call(self.agent.automations.tick)
        second = self.agent.automations.list(self.pid)[0]["last_run_id"]
        self.wait({"id": second})
        self.agent.automations.claim_due()
        self.assertEqual(len(self.repo.list_jobs(self.pid)["jobs"]), 1)
        paused = self.agent.automations.list(self.pid)[0]
        self.assertEqual(paused["id"], plan["id"])
        self.assertFalse(paused["enabled"])
        self.assertIn("job_exists", paused["paused_reason"])

    def test_upgrade_preserves_legacy_pending_actions_and_allows_multiple(self):
        run, _ = self.agent.store.submit(self.pid, AgentMessage(request_id=uuid.uuid4(), message="旧提案"))
        old, _ = self.agent.store.claim_action(self.pid, run["id"], "script", {"summary": "旧提案"}, "pending")
        self.agent.store.finish(self.pid, run["id"], "completed", "等待确认")
        self.client.__exit__(None, None, None)
        from alembic import command
        from alembic.config import Config
        from backend.alexandria.main import ROOT
        config = Config(str(ROOT / "backend/alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "backend/alembic"))
        database = Path(self.temp.name) / "alexandria.db"
        config.set_main_option("sqlalchemy.url", "sqlite:///" + database.as_posix())
        command.downgrade(config, "0008")
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("select count(*) from agent_actions").fetchone()[0], 1)
        from fastapi.testclient import TestClient
        self.app = create_app(Path(self.temp.name), start_scheduler=False)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.repo, self.agent = self.app.state.repository, self.app.state.agent
        restored = self.agent.store.get(self.pid, run["id"])
        self.assertEqual(restored["conversation_id"], self.pid)
        self.assertEqual(self.agent.store.conversations(self.pid)[0]["title"], "历史对话")
        self.assertEqual(restored["action"]["id"], old["id"])
        self.assertEqual(restored["action"]["status"], "pending")
        new, _ = self.agent.store.submit(self.pid, AgentMessage(request_id=uuid.uuid4(), message="新操作"))
        for title in ("一", "二"):
            action, _ = self.agent.store.claim_action(self.pid, new["id"], "project", {"title": title})
            self.agent.store.action_result(action["id"], "completed", {"title": title})
        self.assertEqual(len(self.agent.store.get(self.pid, new["id"])["actions"]), 2)
