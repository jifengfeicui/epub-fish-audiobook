"""Exercise a real tool-calling model against disposable data, never production jobs.

Credentials are read from a supplied SQLite database in read-only mode and used
only in memory. They are not copied into the disposable database.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import tempfile
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from backend.alexandria.api.schemas import AgentMessage
from backend.alexandria.main import create_app


async def validate(settings_db: Path, schedule_only=False, compare_project=None):
    with sqlite3.connect(settings_db.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        settings = {key: json.loads(value) for key, value in connection.execute("SELECT key, value_json FROM app_settings")}
    if settings.get("llm_profiles"):
        profile = next(item for item in settings["llm_profiles"] if item["id"] == settings["active_llm_profile_id"])
        settings.update({"llm_base_url": profile["base_url"], "llm_api_key": profile["api_key"], "llm_model": profile["model"]})
    if not settings.get("llm_base_url") or not settings.get("llm_model"):
        raise ValueError("所选数据库没有可用的 LLM 配置")

    with tempfile.TemporaryDirectory(prefix="alexandria-agent-validation-") as directory:
        app = create_app(Path(directory), start_scheduler=False)
        async with app.router.lifespan_context(app):
            repo, service = app.state.repository, app.state.agent
            original_settings = repo.get_settings

            def in_memory_settings(*, reveal_secrets=False):
                result = original_settings(reveal_secrets=reveal_secrets)
                result.update({key: settings[key] for key in ("llm_base_url", "llm_api_key", "llm_model")})
                result["llm_profiles"] = settings.get("llm_profiles", [])
                result["fish_api_key"] = settings.get("fish_api_key", "")
                return result

            repo.get_settings = in_memory_settings
            project = repo.create_project(
                title="Agent disposable verification", source_filename="test.txt", source_path="test.txt", source_sha256="a" * 64,
                chapters=[{"index": i, "title": f"第{i}章", "href": str(i), "text": "他望向窗外。"} for i in range(1, 13 if schedule_only else 2)],
                render_start_mode="after_review_batch", release_batch_size=3)
            pid = project["id"]
            cid = repo.list_chapters(pid)[0]["id"]
            repo.save_revision(cid, "reviewed", [{"speaker": "NARRATOR", "text": "他望向窗外。", "instruct": "平静"}])
            initial = repo.create_job(pid)
            repo.set_job_status(initial["id"], "completed")

            async def run(message, project_id=pid, chapter_id=cid):
                submitted = service.submit(project_id, AgentMessage(request_id=uuid.uuid4(), message=message, selected_chapter_id=chapter_id))
                await service.tasks[submitted["id"]]
                result = service.store.get(project_id, submitted["id"])
                if result["status"] != "completed":
                    raise RuntimeError(result["output"])
                return result

            if schedule_only:
                for chapter in repo.list_chapters(pid):
                    if chapter["id"] != cid:
                        repo.save_revision(chapter["id"], "reviewed", [{"speaker": "NARRATOR", "text": "他望向窗外。", "instruct": "平静"}])
                    audio = Path(directory) / f"{chapter['id']}.mp3"
                    audio.write_bytes(b"mock-audio-not-for-playback")
                    repo.save_artifact(pid, chapter["id"], "chapter_mp3", audio)
                    repo.update_chapter_status(chapter["id"], "completed")
                if compare_project:
                    from backend.alexandria.db.models import AgentRun
                    service.store.conversations(pid)
                    with sqlite3.connect(settings_db.resolve().as_uri() + "?mode=ro", uri=True) as connection:
                        old = connection.execute("SELECT message, output, history_json FROM agent_runs WHERE project_id=? AND status='completed' ORDER BY created_at DESC LIMIT 6", (compare_project,)).fetchall()
                    with repo.db.session() as session, session.begin():
                        for message, output, history in reversed(old):
                            session.add(AgentRun(id=str(uuid.uuid4()), project_id=pid, conversation_id=pid, request_id=str(uuid.uuid4()),
                                message=message, output=output, history_json=history, status="completed"))
                now = datetime.now(timezone.utc)
                service.automations.now = lambda: now
                state = {"appendable_count": 10, "continuous_audio_count": 12, "prepared_count": 12, "published_count": 2,
                    "publication": {"bvid": "BV1234567890", "remote_mismatch": False},
                    "chapters": [{**chapter, "bilibili_state": "published" if index < 2 else "ready"}
                                 for index, chapter in enumerate(repo.list_chapters(pid))]}
                with patch.object(service.bilibili, "status", return_value=state), patch.object(service.bilibili, "account_status", return_value={"logged_in": True}):
                    result = await run("每10分钟触发一次b站上传 每次10个稿件 我要执行")
                    print(json.dumps({"case": "historical" if compare_project else "clean", "status": result["status"],
                        "actions": [item["kind"] for item in result["actions"]], "output": result["output"]}, ensure_ascii=False), flush=True)
                    plans = service.automations.list(pid)
                    if len(plans) != 1 or plans[0]["interval_seconds"] != 600 or repo.list_jobs(pid)["jobs"][-1]["status"] != "completed":
                        raise RuntimeError("真实模型未创建延迟首轮计划，或意外立即提交任务")
                    if service.automations.claim_due():
                        raise RuntimeError("首轮没有等待一个间隔")
                    now += timedelta(minutes=10)
                    await service.automations.tick()
                    scheduled = service.automations.list(pid)[0]["last_run_id"]
                    await service.tasks[scheduled]
                    woke = service.store.get(pid, scheduled)
                    print(json.dumps({"case": "wakeup", "status": woke["status"], "actions": [item["kind"] for item in woke["actions"]], "output": woke["output"]}, ensure_ascii=False), flush=True)
                    submitted = [item for item in woke["actions"] if item["kind"] == "task" and item["status"] == "completed"]
                    if len(submitted) != 1 or submitted[0]["result"]["payload"].get("action") != "append" or submitted[0]["result"]["payload"].get("parts") != 10:
                        raise RuntimeError("唤醒未提交正确的10分P追加任务")
                    if len(service.automations.list(pid)) != 1:
                        raise RuntimeError("唤醒重复创建了计划")
                    async def wake_again():
                        nonlocal now
                        now += timedelta(minutes=10)
                        await service.automations.tick()
                        last = service.automations.list(pid)[0]["last_run_id"]
                        await service.tasks[last]
                        return service.store.get(pid, last)
                    busy = await wake_again()
                    if busy["status"] != "completed" or any(item["kind"] == "task" for item in busy["actions"]):
                        raise RuntimeError("任务未完成时重复提交或执行失败：" + busy["output"])
                    repo.set_job_status(submitted[0]["result"]["id"], "completed")
                    state.update(appendable_count=0, published_count=12)
                    for chapter in state["chapters"]:
                        chapter["bilibili_state"] = "published"
                    empty = await wake_again()
                    if empty["status"] != "completed" or any(item["kind"] == "task" for item in empty["actions"]) or not service.automations.list(pid)[0]["enabled"]:
                        raise RuntimeError("无内容时未正常跳过或擅自停用计划：" + empty["output"])
                    print("PASS: real model schedule and delayed wakeup, mocked Bilibili, temporary jobs only", flush=True)
                    print("PASS: real model busy and no-content wakeups skip without duplicating jobs or disabling schedule", flush=True)
                return

            query = await run("查询当前项目有哪些章节和任务，请调用 project_status 后汇报，不修改任何内容。")
            if query["action"]:
                raise RuntimeError("查询意外产生写操作")
            print("PASS: real model project-status query")
            task = await run("仅提交当前项目第1章的预处理任务（preprocess，范围1到1），不强制重新生成，不需要等待任务完成。")
            if not task["action"] or task["action"]["kind"] != "task" or task["action"]["status"] != "completed":
                raise RuntimeError("真实模型未提交预期任务：" + task["output"])
            repo.set_job_status(task["action"]["result"]["id"], "completed")
            print("PASS: real model task submission (scheduler disabled)")
            edit = await run("先读取第1章已保存脚本，逐字保留正文和说话人，仅把所有条目的 instruct 改为‘沉稳、舒缓’，直接保存。")
            action = edit["action"]
            if not action or action["kind"] != "script" or action["status"] != "applied":
                raise RuntimeError("真实模型未直接保存：" + edit["output"])
            saved = repo.get_script(cid)
            if saved["revision"] != 2 or saved["entries"][0]["text"] != "他望向窗外。" or not saved["entries"][0]["instruct"]:
                raise RuntimeError("保存结果不符合预期")
            print("PASS: real model direct script edit, preserved text")

            prefix = '被讨厌的勇气：“自我启发之父”阿德勒的哲学课'
            batch_project = repo.create_project(
                title="被讨厌的勇气", source_filename="test.txt", source_path="test.txt", source_sha256="b" * 64,
                chapters=[{"index": i + 1, "title": f"第{i + 1}章", "href": str(i), "text": "正文。"} for i in range(3)],
                render_start_mode="after_review_batch", release_batch_size=3)
            batch_pid = batch_project["id"]
            batch_ids = [chapter["id"] for chapter in repo.list_chapters(batch_pid)]
            for index, chapter_id in enumerate(batch_ids):
                repo.save_revision(chapter_id, "reviewed", [{"speaker": "NARRATOR", "instruct": "平静",
                    "text": (prefix + "\n" if index < 2 else "") + "正文。"}])
            job = repo.create_job(batch_pid)
            repo.set_job_status(job["id"], "completed")
            batch = await run(f'每个章节前都有重复的\n“{prefix} ”\n批量去掉它', batch_pid, batch_ids[0])
            action = batch["action"]
            if not action or action["kind"] != "script_batch" or action["status"] != "applied":
                raise RuntimeError("真实模型未直接完成批量删除：" + batch["output"])
            if action["result"]["modified_count"] != 2 or action["result"]["skipped_count"] != 1:
                raise RuntimeError("批量修改数量不符合预期")
            for index, chapter_id in enumerate(batch_ids):
                script = repo.get_script(chapter_id)
                if script["revision"] != (2 if index < 2 else 1) or script["entries"] != [
                    {**script["entries"][0], "speaker": "NARRATOR", "instruct": "平静", "text": "\n正文。" if index < 2 else "正文。"}]:
                    raise RuntimeError("批量修改未保留剩余正文、角色或语气")
            print("PASS: real model original batch instruction, direct save without permission flags, two edits and one skip")
            print("Validation used temporary data only; credentials were not persisted.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings-db", type=Path, required=True)
    parser.add_argument("--schedule-only", action="store_true")
    parser.add_argument("--compare-project")
    args = parser.parse_args()
    asyncio.run(validate(args.settings_db, args.schedule_only, args.compare_project))


if __name__ == "__main__":
    main()
