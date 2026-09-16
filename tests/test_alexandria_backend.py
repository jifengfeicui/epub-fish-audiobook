from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

try:
    from backend.alexandria.db.database import Database
    from backend.alexandria.db.models import Artifact, AudioSegment, StageRun
    from backend.alexandria.db.repository import Repository
    from backend.alexandria.scheduler.broker import EventBroker
    from backend.alexandria.scheduler.runner import PipelineRunner
except ImportError:
    Database = Artifact = AudioSegment = StageRun = Repository = EventBroker = PipelineRunner = None


def chapter_rows(count: int) -> list[dict]:
    return [
        {"index": index, "title": f"Chapter {index}", "href": f"{index}.xhtml", "text": f"Text {index}"}
        for index in range(1, count + 1)
    ]


@unittest.skipIf(Database is None, "backend requirements are not installed")
class RepositoryTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.database = Database(root / "alexandria.db")
        self.database.create_schema()
        self.repo = Repository(self.database, root)

    def tearDown(self):
        self.database.engine.dispose()
        self.temp.cleanup()

    def project(self, count: int = 2, **settings):
        return self.repo.create_project(
            title="Book",
            source_filename="book.epub",
            source_path="projects/source/book.epub",
            source_sha256="0" * 64,
            chapters=chapter_rows(count),
            render_start_mode=settings.get("render_start_mode", "after_review_batch"),
            release_batch_size=settings.get("release_batch_size", 3),
        )


class RepositoryTests(RepositoryTestCase):
    def test_first_stage_run_starts_with_one_attempt(self):
        project = self.project(1)
        job = self.repo.create_job(project["id"])
        chapter = self.repo.list_chapters(project["id"])[0]

        self.repo.update_stage(job["id"], chapter["id"], "generate", "running")

        with self.database.session() as session:
            stage = session.query(StageRun).one()
            self.assertEqual(stage.attempts, 1)

    def test_defaults_and_single_active_job(self):
        first = self.project()
        second = self.project()
        self.assertEqual(first["settings"]["release_batch_size"], 3)
        self.repo.create_job(first["id"])
        with self.assertRaisesRegex(RuntimeError, "active_job"):
            self.repo.create_job(second["id"])

    def test_pause_resume_cancel_and_restart_interruption(self):
        project = self.project()
        job = self.repo.create_job(project["id"])
        self.repo.set_job_status(job["id"], "running")
        self.assertEqual(self.repo.request_pause(job["id"])["status"], "pausing")
        self.repo.set_job_status(job["id"], "paused")
        self.assertEqual(self.repo.resume_job(job["id"])["status"], "queued")
        self.repo.set_job_status(job["id"], "running")
        self.database.mark_running_interrupted()
        self.assertEqual(self.repo.get_job(job["id"])["status"], "interrupted")
        self.assertEqual(self.repo.get_project(project["id"])["status"], "interrupted")
        self.assertEqual(self.repo.cancel_job(job["id"])["status"], "cancelled")
        self.assertEqual(self.repo.set_job_status(job["id"], "paused")["status"], "cancelled")

    def test_masks_and_redacts_secrets_and_replays_events(self):
        self.repo.update_settings({"fish_api_key": "secret", "llm_api_key": "other"})
        self.assertEqual(self.repo.get_settings()["fish_api_key"], "********")
        project = self.project()
        job = self.repo.create_job(project["id"])
        event = self.repo.add_event(job["id"], project["id"], "log", {"fish_api_key": "secret", "nested": {"llm_api_key": "other"}, "message": "request secret failed"})
        self.assertEqual(event["payload"]["fish_api_key"], "***")
        self.assertEqual(event["payload"]["nested"]["llm_api_key"], "***")
        self.assertNotIn("secret", event["payload"]["message"])
        replay = self.repo.list_events(project["id"], after=event["event_id"] - 1)
        self.assertEqual(replay[-1]["event_id"], event["event_id"])

    def test_concurrent_event_publication_follows_database_order(self):
        project = self.project()
        job = self.repo.create_job(project["id"])
        published = []
        self.repo.event_publisher = lambda _project_id, event: published.append(event["event_id"])
        threads = [
            threading.Thread(
                target=self.repo.add_event,
                args=(job["id"], project["id"], "test", {"number": number}),
            )
            for number in range(12)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(published, sorted(published))

    def test_voice_assignment_is_stable_as_new_speakers_appear(self):
        project = self.project()
        self.repo.replace_voices([
            {"reference_id": "narrator", "name": "Narrator", "bound_speaker": "NARRATOR", "pool_order": 0},
            {"reference_id": "a", "name": "A", "pool_order": 1},
            {"reference_id": "b", "name": "B", "pool_order": 2},
        ])
        first = self.repo.assign_voices(project["id"], ["NARRATOR", "Alice"])
        second = self.repo.assign_voices(project["id"], ["Alice", "Bob"])
        self.assertEqual(first["Alice"], second["Alice"])
        self.assertEqual(second["Bob"]["reference_id"], "b")

    def test_script_edit_preserves_text_and_invalidates_only_its_audio(self):
        project = self.project()
        chapters = self.repo.list_chapters(project["id"])
        first = self.repo.save_revision(chapters[0]["id"], "reviewed", [{"speaker": " NARRATOR ", "text": "  exact text\n", "instruct": " calm "}])
        second = self.repo.save_revision(chapters[1]["id"], "reviewed", [{"speaker": "NARRATOR", "text": "other", "instruct": ""}])
        job = self.repo.create_job(project["id"])
        self.repo.set_job_status(job["id"], "completed")
        with self.database.session() as session, session.begin():
            session.add_all([
                AudioSegment(project_id=project["id"], chapter_id=chapters[0]["id"], revision_id=first["revision_id"], position=1, fingerprint="a", path="a.mp3", status="done"),
                AudioSegment(project_id=project["id"], chapter_id=chapters[1]["id"], revision_id=second["revision_id"], position=1, fingerprint="b", path="b.mp3", status="done"),
                Artifact(id="chapter-a", project_id=project["id"], chapter_id=chapters[0]["id"], kind="chapter_mp3", path="a.mp3", sha256="a", size=1),
                Artifact(id="chapter-b", project_id=project["id"], chapter_id=chapters[1]["id"], kind="chapter_mp3", path="b.mp3", sha256="b", size=1),
                Artifact(id="book", project_id=project["id"], chapter_id=None, kind="book_mp3", path="book.mp3", sha256="c", size=2),
            ])
        edited = self.repo.edit_script(chapters[0]["id"], first["revision"], [{"speaker": "NARRATOR", "text": "  exact text\n", "instruct": ""}])
        self.assertEqual(edited["entries"][0]["text"], "  exact text\n")
        with self.database.session() as session:
            statuses = {row.id: row.status for row in session.query(Artifact).all()}
            segments = {row.chapter_id: row.status for row in session.query(AudioSegment).all()}
        self.assertEqual(statuses, {"chapter-a": "stale", "chapter-b": "active", "book": "stale"})
        self.assertEqual(segments, {chapters[0]["id"]: "stale", chapters[1]["id"]: "done"})

    def test_script_edit_requires_paused_or_completed_job(self):
        project = self.project()
        chapter = self.repo.list_chapters(project["id"])[0]
        script = self.repo.save_revision(chapter["id"], "reviewed", [{"speaker": "NARRATOR", "text": "text", "instruct": ""}])
        job = self.repo.create_job(project["id"])
        self.repo.cancel_job(job["id"])
        with self.assertRaisesRegex(RuntimeError, "script_not_editable"):
            self.repo.edit_script(chapter["id"], script["revision"], script["entries"])


class FakeExecutor:
    def __init__(self, repo: Repository):
        self.repo = repo
        self.review_four_started = threading.Event()
        self.render_one_started = threading.Event()
        self.overlapped = False

    def generate(self, chapter_id, project, log):
        return self.repo.save_revision(chapter_id, "generated", [{"speaker": "NARRATOR", "text": "text", "instruct": ""}], self.generate_input_hash(chapter_id, project))

    def generate_input_hash(self, chapter_id, project):
        return f"generate-{chapter_id}"

    def review_input_hash(self, generated, project):
        return f"review-{generated['content_hash']}"

    def review(self, chapter_id, project, generated, log):
        chapter = self.repo.get_chapter_record(chapter_id)
        if chapter.position == 4:
            self.review_four_started.set()
            self.overlapped = self.render_one_started.wait(1)
        return self.repo.save_revision(chapter_id, "reviewed", generated["entries"], self.review_input_hash(generated, project))

    def render(self, chapter_id, project, log):
        chapter = self.repo.get_chapter_record(chapter_id)
        if chapter.position == 1:
            self.render_one_started.set()
            self.review_four_started.wait(1)
        path = self.repo.storage_root / "projects" / project["id"] / "output" / f"{chapter.position}.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mp3")
        self.repo.save_artifact(project["id"], chapter_id, "chapter_mp3", path)
        return path

    def merge_book(self, project, log):
        path = self.repo.storage_root / "projects" / project["id"] / "output" / "book.mp3"
        path.write_bytes(b"book")
        self.repo.save_artifact(project["id"], None, "book_mp3", path)
        return path


class PipelineTests(RepositoryTestCase):
    def test_review_and_tts_channels_overlap_after_three_chapters(self):
        project = self.project(4, release_batch_size=3)
        job = self.repo.create_job(project["id"])
        executor = FakeExecutor(self.repo)
        PipelineRunner(self.repo, executor, EventBroker()).run(job["id"])
        self.assertTrue(executor.overlapped)
        self.assertEqual(self.repo.get_job(job["id"])["status"], "completed")
        event_types = [event["type"] for event in self.repo.list_events(project["id"])]
        self.assertLess(event_types.index("render.batch_released"), event_types.index("render.batch_started"))

    def test_pause_after_generate_does_not_start_review(self):
        project = self.project(1)
        job = self.repo.create_job(project["id"])
        executor = FakeExecutor(self.repo)
        generate = executor.generate

        def generate_then_pause(chapter_id, project_data, log):
            result = generate(chapter_id, project_data, log)
            self.repo.request_pause(job["id"])
            return result

        executor.generate = generate_then_pause
        PipelineRunner(self.repo, executor, EventBroker()).run(job["id"])

        chapter_id = self.repo.list_chapters(project["id"])[0]["id"]
        self.assertIsNone(self.repo.latest_revision(chapter_id, "reviewed"))
        self.assertEqual(self.repo.get_job(job["id"])["status"], "paused")


if __name__ == "__main__":
    unittest.main()
