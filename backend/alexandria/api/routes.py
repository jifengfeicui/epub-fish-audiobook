from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import aiofiles
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from backend.alexandria.api.schemas import CharacterListUpdate, JobCreate, ProjectUpdate, ProjectVoicePoolUpdate, ScriptUpdate, SettingsUpdate, VoiceListUpdate, VoiceValidationInput
from backend.alexandria.db.repository import Repository
from backend.alexandria.domain.pipeline import RENDER_START_MODES


router = APIRouter(prefix="/api/v1")


def _repo(request: Request) -> Repository:
    return request.app.state.repository


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="Resource not found")
    mapping = {
        "active_job": (409, "Another audiobook job is already running"),
        "resumable_job": (409, "This project has a paused or interrupted job; resume it instead"),
        "revision_conflict": (409, "The script changed; reload before saving"),
        "project_running": (409, "Pause the project before editing its script"),
        "script_not_editable": (409, "Scripts can only be edited while a job is paused or completed"),
        "not_running": (409, "The job is not running"),
        "not_resumable": (409, "The job cannot be resumed"),
        "invalid_chapter_range": (422, "The selected chapter range is invalid"),
        "voice_in_use": (409, "This voice is assigned to a project and cannot be removed"),
        "render_not_ready": (409, "All selected chapters must finish preprocessing before TTS can start"),
        "merge_not_ready": (409, "Every chapter must have valid audio before the book can be merged"),
        "voice_not_usable": (422, "Fish reference_id is not a trained voice with a sample"),
        "voice_sample_missing": (422, "Fish voice has no downloadable sample"),
        "voice_sample_empty": (422, "Fish returned an empty voice sample"),
        "narrator_voice_required": (409, "Select a narrator voice in project settings before starting TTS"),
        "narrator_voice_unavailable": (409, "The project narrator must be an enabled voice in the project pool"),
    }
    message = str(exc)
    key = next((item for item in mapping if message == item or message.startswith(f"{item}:")), None)
    status, detail = mapping.get(key, (400, message))
    if key and message != key:
        detail = f"{detail}: {message.split(':', 1)[1].strip()}"
    return HTTPException(status_code=status, detail=detail)


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/projects")
def list_projects(request: Request, archived: bool = False):
    return _repo(request).list_projects(archived=archived)


@router.post("/projects", status_code=201)
async def create_project(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(""),
    render_start_mode: str = Form("after_review_batch"),
    release_batch_size: int = Form(3),
    from_chapter: int | None = Form(None),
    to_chapter: int | None = Form(None),
):
    if render_start_mode not in RENDER_START_MODES:
        raise HTTPException(status_code=422, detail="Invalid render start mode")
    if not 1 <= release_batch_size <= 20:
        raise HTTPException(status_code=422, detail="Release batch size must be between 1 and 20")
    filename = Path(file.filename or "book.epub").name
    if Path(filename).suffix.lower() not in (".epub", ".txt"):
        raise HTTPException(status_code=422, detail="Only EPUB and TXT files are supported")
    project_id = str(uuid.uuid4())
    repository = _repo(request)
    source_dir = repository.storage_root / "projects" / project_id / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    source_path = source_dir / filename
    digest = hashlib.sha256()
    try:
        async with aiofiles.open(source_path, "wb") as output:
            while block := await file.read(1024 * 1024):
                digest.update(block)
                await output.write(block)
        from tools.render_book import inspect_book

        chapters = inspect_book(source_path)
        if not chapters:
            raise ValueError("No readable chapters found")
        last_chapter = len(chapters)
        if (from_chapter is not None and from_chapter < 1) or (to_chapter is not None and to_chapter < 1):
            raise HTTPException(status_code=422, detail="Chapter range values must be positive")
        selected_start = from_chapter or 1
        selected_end = to_chapter or last_chapter
        if selected_start > selected_end or selected_end > last_chapter:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid chapter range {selected_start}-{selected_end}; book has {last_chapter} chapters",
            )
        relative_path = source_path.relative_to(repository.storage_root).as_posix()
        return repository.create_project(
            project_id=project_id,
            title=(title.strip() or Path(filename).stem),
            source_filename=filename,
            source_path=relative_path,
            source_sha256=digest.hexdigest(),
            chapters=chapters,
            render_start_mode=render_start_mode,
            release_batch_size=release_batch_size,
            from_chapter=from_chapter,
            to_chapter=to_chapter,
        )
    except HTTPException:
        source_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        source_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Could not import book: {exc}") from exc


@router.get("/projects/{project_id}")
def get_project(project_id: str, request: Request):
    try:
        return _repo(request).get_project(project_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.patch("/projects/{project_id}")
def update_project(project_id: str, payload: ProjectUpdate, request: Request):
    try:
        return _repo(request).update_project(project_id, payload.model_dump(exclude_unset=True))
    except Exception as exc:
        raise _http_error(exc) from exc


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str, request: Request):
    try:
        _repo(request).delete_project(project_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/projects/{project_id}/reparse")
def reparse_project(project_id: str, request: Request):
    try:
        return _repo(request).reparse_project(project_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/projects/{project_id}/chapters")
def list_chapters(project_id: str, request: Request):
    try:
        _repo(request).get_project(project_id)
        return _repo(request).list_chapters(project_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/chapters/{chapter_id}/script")
def get_script(chapter_id: int, request: Request):
    try:
        return _repo(request).get_script(chapter_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.patch("/chapters/{chapter_id}/script")
def update_script(chapter_id: int, payload: ScriptUpdate, request: Request):
    try:
        return _repo(request).edit_script(
            chapter_id,
            payload.expected_revision,
            [entry.model_dump() for entry in payload.entries],
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/projects/{project_id}/jobs", status_code=201)
def create_job(project_id: str, payload: JobCreate, request: Request):
    try:
        return _repo(request).create_job(project_id, payload.type)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    try:
        return _repo(request).get_job(job_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/jobs/{job_id}/pause")
def pause_job(job_id: str, request: Request):
    try:
        return _repo(request).request_pause(job_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/jobs/{job_id}/resume")
@router.post("/jobs/{job_id}/retry")
def resume_job(job_id: str, request: Request):
    try:
        return _repo(request).resume_job(job_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request):
    try:
        return _repo(request).cancel_job(job_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/projects/{project_id}/events")
def list_events(project_id: str, request: Request, after: int = 0):
    return _repo(request).list_events(project_id, after=after)


@router.get("/projects/{project_id}/artifacts")
def list_artifacts(project_id: str, request: Request):
    return _repo(request).list_artifacts(project_id)


@router.get("/artifacts/{artifact_id}/play")
def play_artifact(artifact_id: str, request: Request):
    repository = _repo(request)
    try:
        _artifact, path = repository.get_artifact(artifact_id)
    except Exception as exc:
        raise _http_error(exc) from exc
    if repository.storage_root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Artifact file not found")
    return FileResponse(path, media_type="audio/mpeg", content_disposition_type="inline")


@router.get("/projects/{project_id}/speaker-assignments")
def list_speaker_assignments(project_id: str, request: Request):
    try:
        _repo(request).get_project(project_id)
        return _repo(request).list_speaker_assignments(project_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/projects/{project_id}/characters")
def list_characters(project_id: str, request: Request):
    try:
        _repo(request).get_project(project_id)
        return _repo(request).refresh_characters(project_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.put("/projects/{project_id}/characters")
def update_characters(project_id: str, payload: CharacterListUpdate, request: Request):
    try:
        return _repo(request).update_characters(
            project_id,
            [character.model_dump() for character in payload.characters],
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: str, request: Request):
    repository = _repo(request)
    try:
        artifact, path = repository.get_artifact(artifact_id)
    except Exception as exc:
        raise _http_error(exc) from exc
    if repository.storage_root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Artifact file not found")
    return FileResponse(path, filename=path.name, media_type="audio/mpeg")


@router.get("/settings")
def get_settings(request: Request):
    return _repo(request).get_settings()


@router.put("/settings")
def update_settings(payload: SettingsUpdate, request: Request):
    try:
        return _repo(request).update_settings(payload.model_dump(exclude_unset=True))
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/voices")
def list_voices(request: Request):
    return _repo(request).list_voices()


@router.put("/voices")
def replace_voices(payload: VoiceListUpdate, request: Request):
    try:
        repository = _repo(request)
        return repository.replace_voices([voice.model_dump() for voice in payload.voices], require_validation=True)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/voices/validate")
def validate_voice(payload: VoiceValidationInput, request: Request):
    repository = _repo(request)
    settings = repository.get_settings(reveal_secrets=True)
    try:
        return repository.validate_voice(payload.reference_id, settings.get("fish_base_url", "https://api.fish.audio"))
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/voices/{voice_id}/sample")
def voice_sample(voice_id: str, request: Request):
    try:
        return FileResponse(_repo(request).get_voice_sample(voice_id), media_type="audio/wav", content_disposition_type="inline")
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/voices/previews/{token}")
def voice_preview(token: str, request: Request):
    try:
        return FileResponse(_repo(request).get_voice_preview(token), media_type="audio/wav", content_disposition_type="inline")
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/projects/{project_id}/voice-pool")
def get_project_voice_pool(project_id: str, request: Request):
    try:
        return _repo(request).get_project_voice_pool(project_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.put("/projects/{project_id}/voice-pool")
def update_project_voice_pool(project_id: str, payload: ProjectVoicePoolUpdate, request: Request):
    try:
        return _repo(request).update_project_voice_pool(
            project_id,
            payload.excluded_voice_ids,
            payload.narrator_voice_profile_id,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.websocket("/ws/projects/{project_id}")
async def project_events(websocket: WebSocket, project_id: str, after: int = 0):
    repository: Repository = websocket.app.state.repository
    broker = websocket.app.state.broker
    try:
        repository.get_project(project_id)
    except KeyError:
        await websocket.close(code=4404)
        return
    await broker.connect(project_id, websocket)
    try:
        cursor = after
        while True:
            events = repository.list_events(project_id, after=cursor)
            for event in events:
                await websocket.send_json(event)
                cursor = event["event_id"]
            if len(events) < 500:
                break
        await broker.activate(project_id, websocket, cursor)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        broker.disconnect(project_id, websocket)
