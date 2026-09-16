from __future__ import annotations

import queue
import threading
from typing import Any

from backend.alexandria.db.repository import Repository
from backend.alexandria.domain.pipeline import AFTER_ALL_REVIEWS
from backend.alexandria.scheduler.broker import EventBroker
from backend.alexandria.services.stages import StageExecutor


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
        chapters = self.repo.list_selected_chapters(job["project_id"])
        render_queue: queue.Queue[list[int] | None] = queue.Queue()
        errors: list[Exception] = []

        def render_consumer() -> None:
            try:
                while True:
                    batch = render_queue.get()
                    if batch is None:
                        return
                    if self.repo.is_stop_requested(job_id):
                        return
                    self._event(job, "render.batch_started", {"chapter_ids": batch})
                    for chapter_id in batch:
                        if self.repo.is_stop_requested(job_id) or errors:
                            return
                        chapter = self.repo.get_chapter_record(chapter_id)
                        self.repo.update_stage(job_id, chapter_id, "render", "running")
                        self.repo.update_chapter_status(chapter_id, "rendering")
                        try:
                            output = self.executor.render(chapter_id, project, lambda message: self._log(job, "tts", message))
                        except Exception as exc:
                            self.repo.update_stage(job_id, chapter_id, "render", "failed", error=str(exc))
                            self.repo.update_chapter_status(chapter_id, "failed")
                            raise
                        self.repo.update_stage(job_id, chapter_id, "render", "completed")
                        self.repo.update_chapter_status(chapter_id, "done")
                        self._event(job, "chapter.rendered", {"chapter_id": chapter_id, "position": chapter.position, "output": str(output)})
                    self._event(job, "render.batch_completed", {"chapter_ids": batch})
            except Exception as exc:
                errors.append(exc)

        consumer = threading.Thread(target=render_consumer, name=f"tts-{job_id}", daemon=True)
        consumer.start()
        release: list[int] = []
        all_reviewed: list[int] = []

        try:
            for chapter_data in chapters:
                if self.repo.is_stop_requested(job_id) or errors:
                    break
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
                    generation_input_hash = self.executor.generate_input_hash(chapter_id, project)
                    if generated and generated["input_hash"] == generation_input_hash:
                        self._event(job, "chapter.generate_reused", {"chapter_id": chapter_id, "position": chapter.position})
                    else:
                        self.repo.update_stage(job_id, chapter_id, "generate", "running", input_hash=generation_input_hash)
                        self.repo.update_chapter_status(chapter_id, "generating")
                        self._event(job, "chapter.generating", {"chapter_id": chapter_id, "position": chapter.position})
                        try:
                            generated = self.executor.generate(chapter_id, project, lambda message: self._log(job, "review", message))
                        except Exception as exc:
                            self.repo.update_stage(job_id, chapter_id, "generate", "failed", error=str(exc))
                            self.repo.update_chapter_status(chapter_id, "failed")
                            raise
                        self.repo.update_stage(job_id, chapter_id, "generate", "completed", output_hash=generated["content_hash"])
                    if self.repo.is_stop_requested(job_id) or errors:
                        break
                    review_input_hash = self.executor.review_input_hash(generated, project)
                    self.repo.update_stage(job_id, chapter_id, "review", "running", input_hash=review_input_hash)
                    self.repo.update_chapter_status(chapter_id, "reviewing")
                    try:
                        script = self.executor.review(chapter_id, project, generated, lambda message: self._log(job, "review", message))
                    except Exception as exc:
                        self.repo.update_stage(job_id, chapter_id, "review", "failed", error=str(exc))
                        self.repo.update_chapter_status(chapter_id, "failed")
                        raise
                    self.repo.update_stage(job_id, chapter_id, "review", "completed", output_hash=script["content_hash"])
                    self.repo.update_chapter_status(chapter_id, "reviewed")
                self._event(job, "chapter.reviewed", {"chapter_id": chapter_id, "position": chapter.position})
                if self.repo.is_stop_requested(job_id) or errors:
                    break
                all_reviewed.append(chapter_id)
                if job["render_start_mode"] != AFTER_ALL_REVIEWS:
                    release.append(chapter_id)
                    if len(release) >= job["release_batch_size"]:
                        self._event(job, "render.batch_released", {"chapter_ids": release})
                        render_queue.put(release)
                        release = []

            if not errors and not self.repo.is_stop_requested(job_id):
                if job["render_start_mode"] == AFTER_ALL_REVIEWS:
                    if all_reviewed:
                        self._event(job, "render.batch_released", {"chapter_ids": all_reviewed})
                        render_queue.put(all_reviewed)
                elif release:
                    self._event(job, "render.batch_released", {"chapter_ids": release})
                    render_queue.put(release)
        except Exception as exc:
            errors.append(exc)
        finally:
            render_queue.put(None)
            consumer.join()

        if errors:
            self.repo.set_job_status(job_id, "paused", str(errors[0]))
            return
        if self.repo.is_stop_requested(job_id):
            current = self.repo.get_job(job_id)
            if current["status"] != "cancelled":
                self.repo.set_job_status(job_id, "paused")
            return
        try:
            self.executor.merge_book(project, lambda message: self._log(job, "merge", message))
        except Exception as exc:
            self.repo.set_job_status(job_id, "paused", str(exc))
            return
        self.repo.set_job_status(job_id, "completed")
