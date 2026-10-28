from pathlib import Path
from unittest.mock import patch

from sqlalchemy import event, select

from backend.alexandria.db.models import AudioSegment, ScriptRevision
from backend.alexandria.scheduler.broker import EventBroker
from backend.alexandria.scheduler.runner import PipelineRunner
from backend.alexandria.services.stages import StageExecutor
from test_alexandria_backend import FakeExecutor, RepositoryTestCase


class PreprocessRangeTests(RepositoryTestCase):
    def setup_pipeline(self, count=3, start=1, end=None):
        project = self.project(count)
        chapters = self.repo.list_chapters(project["id"])
        job = self.repo.create_job(project["id"], "preprocess", {
            "force_regenerate": True, "from_included_position": start, "to_included_position": end or count,
        })
        executor = FakeExecutor(self.repo)
        return project, chapters, job, executor, PipelineRunner(self.repo, executor, EventBroker())

    def finish_character_analysis(self, project, runner):
        analysis = self.repo.get_project(project["id"])["latest_job"]
        self.assertEqual(analysis["type"], "character_analysis")
        runner.run(analysis["id"])

    def test_forced_range_replaces_cached_and_manual_scripts_and_keeps_history(self):
        project = self.project(3)
        chapters = self.repo.list_chapters(project["id"])
        executor = FakeExecutor(self.repo)
        originals = []
        for chapter in chapters:
            generated = executor.generate(chapter["id"], project, lambda _: None)
            originals.append(executor.review(chapter["id"], project, generated, lambda _: None))
        manual = self.repo.save_revision(chapters[1]["id"], "manual", [{"speaker": "NARRATOR", "text": "manual", "instruct": ""}])
        executor.generated.clear()
        executor.reviewed.clear()
        job = self.repo.create_job(project["id"], "preprocess", {
            "force_regenerate": True, "from_included_position": 1, "to_included_position": 2,
        })
        self.assertEqual(job["progress"], {"completed": 0, "total": 2})
        self.assertEqual(self.repo.get_project(project["id"])["latest_job"]["progress"], job["progress"])
        runner = PipelineRunner(self.repo, executor, EventBroker())
        runner.run(job["id"])
        selected = [chapter["id"] for chapter in chapters[:2]]
        self.assertEqual(executor.generated, selected)
        self.assertEqual(executor.reviewed, selected)
        self.assertEqual(self.repo.get_script(chapters[2]["id"]), originals[2])
        self.assertEqual(self.repo.get_script(chapters[1]["id"])["kind"], "reviewed")
        self.assertGreater(self.repo.get_script(chapters[1]["id"])["revision"], manual["revision"])
        with self.database.session() as session:
            self.assertIsNotNone(session.get(ScriptRevision, manual["revision_id"]))
        self.assertEqual(self.repo.get_job(job["id"])["progress"], {"completed": 2, "total": 2})
        self.finish_character_analysis(project, runner)
        self.assertEqual(self.repo.get_project(project["id"])["status"], "ready_for_tts")

    def test_legacy_task_still_reuses_reviewed_and_manual_scripts(self):
        project = self.project(2)
        chapters = self.repo.list_chapters(project["id"])
        executor = FakeExecutor(self.repo)
        generated = executor.generate(chapters[0]["id"], project, lambda _: None)
        executor.review(chapters[0]["id"], project, generated, lambda _: None)
        manual = self.repo.save_revision(chapters[1]["id"], "manual", [{"speaker": "NARRATOR", "text": "edited", "instruct": ""}])
        executor.generated.clear()
        executor.reviewed.clear()
        job = self.repo.create_job(project["id"])
        PipelineRunner(self.repo, executor, EventBroker()).run(job["id"])
        self.assertEqual(executor.generated, [])
        self.assertEqual(executor.reviewed, [])
        self.assertEqual(self.repo.get_script(chapters[1]["id"]), manual)
        self.assertEqual(self.repo.get_job(job["id"])["progress"], {"completed": 2, "total": 2})

    def test_partial_success_does_not_mark_other_chapters_ready_and_can_render_selected_range(self):
        project, chapters, job, executor, runner = self.setup_pipeline(start=2, end=2)
        runner.run(job["id"])
        self.assertEqual(executor.generated, [chapters[1]["id"]])
        self.assertEqual(executor.reviewed, [chapters[1]["id"]])
        self.assertEqual([chapter["status"] for chapter in self.repo.list_chapters(project["id"])], ["pending", "reviewed", "pending"])
        self.finish_character_analysis(project, runner)
        self.assertEqual(self.repo.get_project(project["id"])["status"], "ready")
        render = self.repo.create_job(project["id"], "render", {"from_included_position": 2, "to_included_position": 2})
        self.assertEqual(render["status"], "queued")
        self.repo.cancel_job(render["id"])
        with self.assertRaisesRegex(RuntimeError, "render_not_ready"):
            self.repo.create_job(project["id"], "render")

    def test_snapshot_does_not_shift_or_include_newly_enabled_chapters(self):
        project = self.project(5)
        chapters = self.repo.list_chapters(project["id"])
        self.repo.set_chapter_inclusion(project["id"], [chapter["id"] for chapter in chapters[1:]])
        job = self.repo.create_job(project["id"], "preprocess", {
            "force_regenerate": True, "from_included_position": 2, "to_included_position": 3,
            "chapter_ids": [chapters[4]["id"]],
        })
        self.assertEqual(job["payload"]["chapter_ids"], [chapters[2]["id"], chapters[3]["id"]])
        self.repo.request_pause(job["id"])
        self.repo.set_chapter_inclusion(project["id"], [chapter["id"] for chapter in chapters if chapter["id"] != chapters[2]["id"]])
        self.repo.resume_job(job["id"])
        executor = FakeExecutor(self.repo)
        PipelineRunner(self.repo, executor, EventBroker()).run(job["id"])
        self.assertEqual(executor.generated, [chapters[3]["id"]])
        self.assertEqual(self.repo.get_job(job["id"])["progress"], {"completed": 1, "total": 1})

    def test_resume_after_generate_only_runs_review(self):
        project, chapters, job, executor, runner = self.setup_pipeline(count=1)
        generate = executor.generate

        def pause_after_generate(*args, **kwargs):
            result = generate(*args, **kwargs)
            self.repo.request_pause(job["id"])
            return result

        with patch.object(executor, "generate", side_effect=pause_after_generate):
            runner.run(job["id"])
        self.assertEqual(self.repo.get_job(job["id"])["status"], "paused")
        self.assertEqual(self.repo.get_job(job["id"])["progress"], {"completed": 0, "total": 1})
        self.repo.resume_job(job["id"])
        runner.run(job["id"])
        self.assertEqual(executor.generated, [chapters[0]["id"]])
        self.assertEqual(executor.reviewed, [chapters[0]["id"]])
        self.assertEqual(self.repo.get_job(job["id"])["status"], "completed")

    def test_retry_skips_completed_chapters_and_reuses_own_generation(self):
        project, chapters, job, executor, runner = self.setup_pipeline(count=2)
        review = executor.review

        def fail_second(chapter_id, *args, **kwargs):
            if chapter_id == chapters[1]["id"]:
                raise RuntimeError("review failed")
            return review(chapter_id, *args, **kwargs)

        with patch.object(executor, "review", side_effect=fail_second):
            runner.run(job["id"])
        self.assertEqual(self.repo.get_job(job["id"])["status"], "failed")
        self.assertEqual(self.repo.get_job(job["id"])["progress"], {"completed": 1, "total": 2})
        with self.assertRaisesRegex(RuntimeError, "job_exists"):
            self.repo.create_job(project["id"], "preprocess", {"force_regenerate": True})
        self.repo.retry_job(job["id"])
        runner.run(job["id"])
        self.assertEqual(executor.generated, [chapter["id"] for chapter in chapters])
        self.assertEqual(executor.reviewed, [chapter["id"] for chapter in chapters])
        self.assertEqual(self.repo.get_job(job["id"])["progress"], {"completed": 2, "total": 2})

    def test_config_changes_after_pause_only_rerun_affected_stages(self):
        for changed in ("generation", "review"):
            with self.subTest(changed=changed):
                project, chapters, job, executor, runner = self.setup_pipeline(count=1)
                real_hashes = StageExecutor(self.repo, Path(__file__).parents[1])
                executor.generate_input_hash = real_hashes.generate_input_hash
                executor.review_input_hash = real_hashes.review_input_hash
                review = executor.review

                def pause_after_review(*args, **kwargs):
                    result = review(*args, **kwargs)
                    self.repo.request_pause(job["id"])
                    return result

                with patch.object(executor, "review", side_effect=pause_after_review):
                    runner.run(job["id"])
                self.assertEqual(self.repo.get_job(job["id"])["status"], "paused")
                self.repo.update_settings({"prompts": {
                    "system_prompt" if changed == "generation" else "review_system_prompt": "changed",
                }})
                self.repo.resume_job(job["id"])
                runner.run(job["id"])
                self.assertEqual(len(executor.generated), 2 if changed == "generation" else 1)
                self.assertEqual(len(executor.reviewed), 2)
                self.finish_character_analysis(project, runner)
                self.repo.update_settings({"prompts": {}})

    def test_replacement_invalidates_only_selected_audio_and_rolls_back_on_error(self):
        project = self.project(2)
        chapters = self.repo.list_chapters(project["id"])
        revisions = [self.repo.save_revision(chapter["id"], "manual", [{"speaker": "NARRATOR", "text": "old", "instruct": ""}]) for chapter in chapters]
        artifacts = []
        paths = []
        for index, chapter_id in enumerate([chapters[0]["id"], chapters[1]["id"], None]):
            path = self.repo.storage_root / f"audio-{index}.mp3"
            path.write_bytes(b"old-audio")
            paths.append(path)
            artifacts.append(self.repo.save_artifact(project["id"], chapter_id, "chapter_mp3" if chapter_id else "book_mp3", path))
        self.repo.save_audio_segment(project_id=project["id"], chapter_id=chapters[0]["id"], revision_id=revisions[0]["revision_id"], position=1, fingerprint="old", path=paths[0].name, status="done", attempts=1)

        def fail_audio_update(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.startswith("UPDATE audio_segments"):
                raise RuntimeError("write failed")

        event.listen(self.database.engine, "before_cursor_execute", fail_audio_update)
        try:
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                self.repo.save_revision(chapters[0]["id"], "generated", [{"speaker": "NARRATOR", "text": "new", "instruct": ""}])
        finally:
            event.remove(self.database.engine, "before_cursor_execute", fail_audio_update)
        self.assertEqual(self.repo.get_script(chapters[0]["id"]), revisions[0])
        self.assertEqual(len(self.repo.list_artifacts(project["id"])), 3)
        with self.database.session() as session:
            self.assertEqual([revision.id for revision in session.scalars(select(ScriptRevision).where(ScriptRevision.chapter_id == chapters[0]["id"])).all()], [revisions[0]["revision_id"]])

        job = self.repo.create_job(project["id"], "preprocess", {"force_regenerate": True, "from_included_position": 1, "to_included_position": 1})
        PipelineRunner(self.repo, FakeExecutor(self.repo), EventBroker()).run(job["id"])
        self.assertEqual([artifact["id"] for artifact in self.repo.list_artifacts(project["id"])], [artifacts[1]["id"]])
        self.assertEqual(self.repo.get_script(chapters[1]["id"]), revisions[1])
        self.assertTrue(all(path.is_file() for path in paths))
        with self.database.session() as session:
            self.assertEqual(session.scalars(select(AudioSegment)).one().status, "stale")
