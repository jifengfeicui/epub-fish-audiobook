from __future__ import annotations

from typing import Any

from backend.alexandria.db.repository import Repository
from backend.alexandria.scheduler.broker import EventBroker
from backend.alexandria.services.stages import StageCancelled, StageExecutor


class PipelineRunner:
    def __init__(self, repository: Repository, executor: StageExecutor, broker: EventBroker):
        self.repo = repository
        self.executor = executor
        self.broker = broker

    def _event(self, job: dict[str, Any], event_type: str, payload: dict[str, Any]) -> None:
        self.repo.add_event(job["id"], job["project_id"], event_type, payload)

    def _log(self, job: dict[str, Any], channel: str, message: str) -> None:
        self._event(job, "log", {"channel": channel, "message": message})

    def run(self, job_id: str) -> None:
        job = self.repo.set_job_status(job_id, "running")
        if job["status"] != "running":
            return
        project = self.repo.get_project(job["project_id"])
        try:
            if job["type"] == "render":
                self._render(job, project)
            else:
                self._preprocess(job, project)
        except Exception as exc:
            if not isinstance(exc, StageCancelled):
                self._log(job, job["type"], str(exc))
            if self.repo.is_stop_requested(job_id):
                current = self.repo.get_job(job_id)
                if current["status"] != "cancelled":
                    self.repo.set_job_status(job_id, "paused")
            else:
                self.repo.set_job_status(job_id, "paused", str(exc))
            return
        if self.repo.is_stop_requested(job_id):
            current = self.repo.get_job(job_id)
            if current["status"] != "cancelled":
                self.repo.set_job_status(job_id, "paused")
            return
        self.repo.set_job_status(job_id, "completed")
        if job["type"] == "preprocess":
            self.repo.set_project_status(project["id"], "ready_for_tts")

    def _preprocess(self, job: dict[str, Any], project: dict[str, Any]) -> None:
        for chapter_data in self.repo.list_selected_chapters(project["id"]):
            if self.repo.is_stop_requested(job["id"]):
                raise StageCancelled("Stage cancelled")
            chapter_id = chapter_data["id"]
            chapter = self.repo.get_chapter_record(chapter_id)
            script = self.repo.get_script(chapter_id)
            generated = self.repo.latest_revision(chapter_id, "generated")
            reviewed_is_current = (
                script["kind"] == "reviewed"
                and generated is not None
                and script["input_hash"] == self.executor.review_input_hash(generated, project)
            )
            if script["kind"] != "manual" and not reviewed_is_current:
                generation_hash = self.executor.generate_input_hash(chapter_id, project)
                if generated and generated["input_hash"] == generation_hash:
                    self._event(job, "chapter.generate_reused", {"chapter_id": chapter_id, "position": chapter.position})
                else:
                    self.repo.update_stage(job["id"], chapter_id, "generate", "running", input_hash=generation_hash)
                    self.repo.update_chapter_status(chapter_id, "generating")
                    self._event(job, "chapter.generating", {"chapter_id": chapter_id, "position": chapter.position})
                    try:
                        generated = self.executor.generate(
                            chapter_id,
                            project,
                            lambda message: self._log(job, "generate", message),
                            lambda: self.repo.is_stop_requested(job["id"]),
                        )
                    except Exception as exc:
                        if not isinstance(exc, StageCancelled):
                            self.repo.update_stage(job["id"], chapter_id, "generate", "failed", error=str(exc))
                            self.repo.update_chapter_status(chapter_id, "failed")
                        raise
                    self.repo.update_stage(job["id"], chapter_id, "generate", "completed", output_hash=generated["content_hash"])
                if self.repo.is_stop_requested(job["id"]):
                    raise StageCancelled("Stage cancelled")
                review_hash = self.executor.review_input_hash(generated, project)
                self.repo.update_stage(job["id"], chapter_id, "review", "running", input_hash=review_hash)
                self.repo.update_chapter_status(chapter_id, "reviewing")
                try:
                    script = self.executor.review(
                        chapter_id,
                        project,
                        generated,
                        lambda message: self._log(job, "review", message),
                        lambda: self.repo.is_stop_requested(job["id"]),
                    )
                except Exception as exc:
                    if not isinstance(exc, StageCancelled):
                        self.repo.update_stage(job["id"], chapter_id, "review", "failed", error=str(exc))
                        self.repo.update_chapter_status(chapter_id, "failed")
                    raise
                self.repo.update_stage(job["id"], chapter_id, "review", "completed", output_hash=script["content_hash"])
            self.repo.update_chapter_status(chapter_id, "reviewed")
            self._event(job, "chapter.reviewed", {"chapter_id": chapter_id, "position": chapter.position})

        self.repo.refresh_characters(project["id"])
        self.executor.analyze_characters(project)
        self._event(job, "characters.completed", {"count": len(self.repo.list_characters(project["id"]))})

    def _render(self, job: dict[str, Any], project: dict[str, Any]) -> None:
        for chapter_data in self.repo.list_selected_chapters(project["id"]):
            if self.repo.is_stop_requested(job["id"]):
                raise StageCancelled("Stage cancelled")
            chapter_id = chapter_data["id"]
            chapter = self.repo.get_chapter_record(chapter_id)
            self.repo.update_stage(job["id"], chapter_id, "render", "running")
            self.repo.update_chapter_status(chapter_id, "rendering")
            try:
                output = self.executor.render(chapter_id, project, lambda message: self._log(job, "tts", message))
            except Exception as exc:
                self.repo.update_stage(job["id"], chapter_id, "render", "failed", error=str(exc))
                self.repo.update_chapter_status(chapter_id, "failed")
                raise
            self.repo.update_stage(job["id"], chapter_id, "render", "completed")
            self.repo.update_chapter_status(chapter_id, "done")
            self._event(job, "chapter.rendered", {"chapter_id": chapter_id, "position": chapter.position, "output": str(output)})
        self.executor.merge_book(project, lambda message: self._log(job, "merge", message))
