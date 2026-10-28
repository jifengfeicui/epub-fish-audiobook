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

from backend.alexandria.api.schemas import AgentMessage
from backend.alexandria.main import create_app


async def validate(settings_db: Path):
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
                chapters=[{"index": 1, "title": "第一章", "href": "1", "text": "他望向窗外。"}],
                render_start_mode="after_review_batch", release_batch_size=3)
            pid = project["id"]
            cid = repo.list_chapters(pid)[0]["id"]
            repo.save_revision(cid, "reviewed", [{"speaker": "NARRATOR", "text": "他望向窗外。", "instruct": "平静"}])
            initial = repo.create_job(pid)
            repo.set_job_status(initial["id"], "completed")

            async def run(message):
                submitted = service.submit(pid, AgentMessage(request_id=uuid.uuid4(), message=message, selected_chapter_id=cid))
                await service.tasks[submitted["id"]]
                result = service.store.get(pid, submitted["id"])
                if result["status"] != "completed":
                    raise RuntimeError(result["output"])
                return result

            query = await run("查询当前项目有哪些章节和任务，请调用 project_status 后汇报，不修改任何内容。")
            if query["action"]:
                raise RuntimeError("查询意外产生写操作")
            print("PASS: real model project-status query")
            task = await run("仅提交当前项目第1章的预处理任务（preprocess，范围1到1），不强制重新生成，不需要等待任务完成。")
            if not task["action"] or task["action"]["kind"] != "task" or task["action"]["status"] != "completed":
                raise RuntimeError("真实模型未提交预期任务：" + task["output"])
            repo.set_job_status(task["action"]["result"]["id"], "completed")
            print("PASS: real model task submission (scheduler disabled)")
            proposal = await run("先读取第1章已保存脚本，逐字保留正文和说话人，仅把所有条目的 instruct 改为‘沉稳、舒缓’，创建修改提案供我在网页确认。")
            action = proposal["action"]
            if not action or action["kind"] != "script" or action["status"] != "pending":
                raise RuntimeError("真实模型未生成待确认提案：" + proposal["output"])
            if repo.get_script(cid)["revision"] != 1:
                raise RuntimeError("确认前脚本被修改")
            service.store.decide(pid, action["id"], True)
            saved = repo.get_script(cid)
            if saved["revision"] != 2 or saved["entries"][0]["text"] != "他望向窗外。" or not saved["entries"][0]["instruct"]:
                raise RuntimeError("提案保存结果不符合预期")
            print("PASS: real model proposal, explicit confirmation, preserved text")
            print("Validation used temporary data only; credentials were not persisted.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings-db", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(validate(args.settings_db))


if __name__ == "__main__":
    main()
