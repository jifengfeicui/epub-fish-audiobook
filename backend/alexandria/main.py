from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from alembic import command
from alembic.config import Config

from backend.alexandria.api.routes import router
from backend.alexandria.db.database import Database
from backend.alexandria.db.repository import Repository
from backend.alexandria.scheduler.broker import EventBroker
from backend.alexandria.scheduler.manager import Scheduler
from backend.alexandria.scheduler.runner import PipelineRunner
from backend.alexandria.services.stages import StageExecutor


ROOT = Path(__file__).resolve().parents[2]


def _prompts(path: Path) -> tuple[str, str]:
    try:
        first, second = path.read_text(encoding="utf-8").split("---SEPARATOR---")
        return first.strip(), second.strip()
    except (OSError, ValueError):
        return "", ""


def _defaults() -> dict:
    generate_system, generate_user = _prompts(ROOT / "default_prompts.txt")
    review_system, review_user = _prompts(ROOT / "review_prompts.txt")
    return {
        "llm_base_url": "http://localhost:11434/v1",
        "llm_api_key": "local",
        "llm_model": "local-model",
        "fish_base_url": "https://api.fish.audio",
        "fish_api_key": "",
        "fish_model": "s2.1-pro-free",
        "generation": {"chunk_size": 3000, "max_tokens": 4096, "review_batch_size": 25},
        "prompts": {
            "system_prompt": generate_system,
            "user_prompt": generate_user,
            "review_system_prompt": review_system,
            "review_user_prompt": review_user,
        },
        "fish_tts": {"format": "mp3", "sample_rate": 44100, "mp3_bitrate": 128, "temperature": 0.7, "top_p": 0.7, "latency": "normal", "condition_on_previous_chunks": True, "timeout_seconds": 300},
    }


def _seed_voices(repository: Repository) -> None:
    if repository.list_voices():
        return
    path = ROOT / "fish_adapter" / "voice_pool.json"
    if not path.exists():
        return
    raw = json.loads(path.read_text(encoding="utf-8"))
    voices: list[dict] = []
    seen: set[str] = set()
    for speaker, voice in raw.get("bindings", {}).items():
        reference_id = voice.get("reference_id")
        if reference_id and reference_id not in seen:
            voices.append({"reference_id": reference_id, "name": voice.get("name", speaker), "bound_speaker": speaker, "pool_order": len(voices)})
            seen.add(reference_id)
    for voice in raw.get("pool", []):
        reference_id = voice.get("reference_id")
        if reference_id and reference_id not in seen:
            voices.append({"reference_id": reference_id, "name": voice.get("name", reference_id), "bound_speaker": None, "pool_order": len(voices)})
            seen.add(reference_id)
    if voices:
        repository.replace_voices(voices)


def create_app(data_dir: Path | None = None, *, start_scheduler: bool = True) -> FastAPI:
    storage_root = (data_dir or Path(os.environ.get("ALEXANDRIA_DATA_DIR", ROOT / "data"))).resolve()
    database = Database(storage_root / "alexandria.db")
    alembic = Config(str(ROOT / "backend" / "alembic.ini"))
    alembic.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
    alembic.set_main_option("sqlalchemy.url", f"sqlite:///{database.path.as_posix()}")
    command.upgrade(alembic, "head")
    interrupted_jobs = database.mark_running_interrupted()
    repository = Repository(database, storage_root)
    repository.seed_settings(_defaults())
    _seed_voices(repository)
    broker = EventBroker()
    repository.event_publisher = broker.publish
    for job_id, project_id in interrupted_jobs:
        repository.add_event(job_id, project_id, "job.interrupted", {
            "status": "interrupted",
            "error": "Service restarted while the job was running",
        })
    executor = StageExecutor(repository, ROOT)
    runner = PipelineRunner(repository, executor, broker)
    scheduler = Scheduler(repository, runner)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        broker.bind_loop(asyncio.get_running_loop())
        if start_scheduler:
            scheduler.start()
        yield
        scheduler.stop()
        database.engine.dispose()

    application = FastAPI(title="Alexandria Fish Audiobook", version="1.0", lifespan=lifespan)
    application.state.database = database
    application.state.repository = repository
    application.state.broker = broker
    application.state.scheduler = scheduler
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(router)

    dist = ROOT / "frontend" / "dist"
    if (dist / "assets").exists():
        application.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @application.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str):
        index = dist / "index.html"
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        raise HTTPException(status_code=503, detail="Frontend is not built. Run npm --prefix frontend run build")

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.alexandria.main:app", host=os.environ.get("ALEXANDRIA_HOST", "127.0.0.1"), port=4200, workers=1)
