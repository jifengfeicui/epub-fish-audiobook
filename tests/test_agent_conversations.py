import unittest
import uuid

from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart
from pydantic_ai.models.function import FunctionModel

from backend.alexandria.api.schemas import AgentMessage
from tests import test_project_agent as fixtures


class AgentConversationTests(unittest.TestCase):
    setUp = fixtures.ProjectAgentTests.setUp
    tearDown = fixtures.ProjectAgentTests.tearDown
    new_project = fixtures.ProjectAgentTests.new_project
    model = fixtures.ProjectAgentTests.model
    post = fixtures.ProjectAgentTests.post
    wait = fixtures.ProjectAgentTests.wait

    def create(self):
        response = self.client.post(self.base + "/conversations", json={})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def test_create_list_rename_and_project_isolation(self):
        first, second = self.create(), self.create()
        response = self.client.patch(self.base + "/conversations/" + first, json={"title": "  上传安排  "})
        self.assertEqual(response.json()["title"], "上传安排")
        rows = self.client.get(self.base + "/conversations").json()["conversations"]
        self.assertEqual({row["id"] for row in rows}, {self.pid, first, second})
        self.assertEqual(self.client.get(self.base, params={"conversation_id": second}).json()["runs"], [])
        self.assertEqual(self.client.patch(self.base + "/conversations/" + first, json={"title": " "}).status_code, 422)
        other = self.new_project()
        foreign = f"/api/v1/projects/{other['id']}/agent"
        self.assertEqual(self.client.get(foreign, params={"conversation_id": first}).status_code, 404)
        self.assertEqual(self.client.patch(foreign + "/conversations/" + first, json={"title": "越界"}).status_code, 404)
        self.assertEqual(self.client.post(foreign + "/messages", json={"conversation_id": first,
            "message": "越界", "request_id": str(uuid.uuid4())}).status_code, 404)

    def test_context_and_receipts_are_scoped_to_conversation(self):
        first, second = self.create(), self.create()
        self.model([])
        run, payload = self.post("第一会话的秘密标记", conversation_id=first)
        self.wait(run)
        self.assertEqual(self.client.get(self.base, params={"conversation_id": first}).json()["runs"][0]["id"], run["id"])
        self.assertEqual(self.client.get(self.base, params={"conversation_id": second}).json()["runs"], [])
        self.assertEqual(self.client.post(self.base + "/messages", json={**payload, "conversation_id": second}).status_code, 409)
        def respond(messages, info):
            self.assertNotIn("第一会话的秘密标记", str(messages))
            return ModelResponse(parts=[TextPart("独立上下文")])
        self.agent.model_factory = lambda: FunctionModel(respond)
        run, _ = self.post("第二会话", conversation_id=second)
        self.assertEqual(self.wait(run)["output"], "独立上下文")
        self.assertEqual(self.client.get(self.base + "/conversations").json()["conversations"][0]["title"], "第二会话")

    def test_old_capability_history_remains_visible_without_replaying_to_model(self):
        run, _ = self.agent.store.submit(self.pid, AgentMessage(request_id=uuid.uuid4(), message="旧版消息"))
        self.agent.store.finish(self.pid, run["id"], "completed", "旧版拒绝", [
            {"kind": "request", "instructions": "不支持B站、定时", "parts": [{"part_kind": "user-prompt", "content": "旧版消息"}]},
            {"kind": "response", "parts": [{"part_kind": "text", "content": "旧版拒绝"}]}])
        def respond(messages, info):
            self.assertNotIn("旧版拒绝", str(messages))
            self.assertNotIn("旧版消息", str(messages))
            self.assertIn("create_automation", {tool.name for tool in info.function_tools})
            return ModelResponse(parts=[TextPart("使用当前工具")])
        self.agent.model_factory = lambda: FunctionModel(respond)
        new, _ = self.post("新指令")
        self.assertEqual(self.wait(new)["output"], "使用当前工具")
        self.assertEqual(self.client.get(self.base).json()["runs"][0]["output"], "旧版拒绝")

    def test_current_history_keeps_messages_but_does_not_replay_private_thinking(self):
        self.agent.model_factory = lambda: FunctionModel(lambda messages, info: ModelResponse(
            parts=[ThinkingPart("不应回放的推理"), TextPart("第一轮实际回答")]))
        run, _ = self.post("记住此会话")
        self.wait(run)
        def respond(messages, info):
            self.assertIn("第一轮实际回答", str(messages))
            self.assertFalse(any(isinstance(part, ThinkingPart) for message in messages for part in message.parts))
            return ModelResponse(parts=[TextPart("第二轮")])
        self.agent.model_factory = lambda: FunctionModel(respond)
        run, _ = self.post("继续")
        self.assertEqual(self.wait(run)["output"], "第二轮")

    def test_running_project_blocks_writes_from_other_conversations(self):
        first, second = self.create(), self.create()
        run, _ = self.agent.store.submit(self.pid, AgentMessage(request_id=uuid.uuid4(), message="等待", conversation_id=first))
        result = self.client.get(self.base, params={"conversation_id": second}).json()
        self.assertTrue(result["project_busy"])
        self.assertEqual(result["runs"], [])
        self.assertEqual(self.client.post(self.base + "/messages", json={"conversation_id": second,
            "message": "另一个请求", "request_id": str(uuid.uuid4())}).status_code, 409)
        self.agent.store.finish(self.pid, run["id"], "completed", "完成")

    def test_scheduled_wakeups_have_their_own_context(self):
        self.model([])
        self.wait(self.post("交互对话不能污染定时任务")[0])
        from backend.alexandria.api.schemas import AutomationCreate
        from datetime import datetime, timezone
        plan = self.agent.automations.create(self.pid, AutomationCreate(instruction="查看任务状态", interval_seconds=600,
            first_run_at=datetime.now(timezone.utc)))
        pid, run = self.agent.automations.claim_due()[0]
        self.assertEqual(run["conversation_id"], plan["id"])
        self.assertEqual(self.agent.store.history(pid, run["id"]), [])
        self.agent.store.finish(pid, run["id"], "completed", "完成")
        self.assertEqual(len(self.client.get(self.base).json()["runs"]), 1)
        self.assertEqual(self.client.get(self.base, params={"conversation_id": plan["id"]}).json()["runs"][0]["id"], run["id"])
