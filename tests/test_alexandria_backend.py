from __future__ import annotations

import tempfile
import threading
import time
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

try:
    from backend.alexandria.db.database import Database
    from backend.alexandria.db.models import Artifact, AudioSegment, StageRun
    from backend.alexandria.db.repository import Repository
    from backend.alexandria.scheduler.broker import EventBroker
    from backend.alexandria.scheduler.runner import PipelineRunner
    from backend.alexandria.services.stages import StageCancelled, StageExecutor
except ImportError:
    Database = Artifact = AudioSegment = StageRun = Repository = EventBroker = PipelineRunner = StageCancelled = StageExecutor = None


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
        self.database.close()
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

    def test_archive_restore_delete_and_active_job_protection(self):
        project = self.project()
        job = self.repo.create_job(project["id"])
        with self.assertRaisesRegex(RuntimeError, "project_running"):
            self.repo.update_project(project["id"], {"archived": True})
        with self.assertRaisesRegex(RuntimeError, "project_running"):
            self.repo.delete_project(project["id"])
        self.repo.cancel_job(job["id"])
        self.repo.update_project(project["id"], {"archived": True})
        self.assertEqual([row["id"] for row in self.repo.list_projects(True)], [project["id"]])
        self.repo.update_project(project["id"], {"archived": False})
        self.assertEqual([row["id"] for row in self.repo.list_projects(False)], [project["id"]])
        self.repo.delete_project(project["id"])
        with self.assertRaises(KeyError):
            self.repo.get_project(project["id"])

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

    def test_voice_assignment_uses_explicit_choice_and_gender_without_persisting_random_choice(self):
        project = self.project()
        voices = self.repo.replace_voices([
            {"reference_id": "narrator", "name": "Narrator", "pool_order": 0, "gender": "female"},
            {"reference_id": "a", "name": "A", "pool_order": 1, "gender": "female"},
            {"reference_id": "b", "name": "B", "pool_order": 2, "gender": "male"},
        ])
        chapter = self.repo.list_chapters(project["id"])[0]
        self.repo.save_revision(chapter["id"], "reviewed", [
            {"speaker": "Alice", "text": "hello", "instruct": ""},
            {"speaker": "Bob", "text": "hello", "instruct": ""},
        ])
        self.repo.refresh_characters(project["id"])
        by_reference = {voice["reference_id"]: voice for voice in voices}
        self.repo.update_project_voice_pool(project["id"], [], by_reference["narrator"]["id"])
        self.repo.update_characters(project["id"], [
            {"speaker": "Alice", "gender": "female", "personality": "", "voice_profile_id": None},
            {"speaker": "Bob", "gender": "male", "personality": "", "voice_profile_id": by_reference["b"]["id"]},
        ])

        assigned = self.repo.assign_voices(project["id"], ["NARRATOR", "Alice", "Alice", "Bob"])

        self.assertEqual(assigned["NARRATOR"]["reference_id"], "narrator")
        self.assertEqual(assigned["Alice"]["reference_id"], "a")
        self.assertEqual(assigned["Bob"]["reference_id"], "b")
        self.assertIsNone(next(item for item in self.repo.list_characters(project["id"]) if item["speaker"] == "Alice")["voice_profile_id"])

    def test_projects_can_choose_different_narrators(self):
        first = self.project()
        second = self.project()
        voices = self.repo.replace_voices([
            {"reference_id": "a", "name": "A", "pool_order": 0},
            {"reference_id": "b", "name": "B", "pool_order": 1},
        ])
        self.repo.update_project_voice_pool(first["id"], [], voices[0]["id"])
        self.repo.update_project_voice_pool(second["id"], [], voices[1]["id"])

        self.assertEqual(self.repo.assign_voices(first["id"], ["NARRATOR"])["NARRATOR"]["reference_id"], "a")
        self.assertEqual(self.repo.assign_voices(second["id"], ["NARRATOR"])["NARRATOR"]["reference_id"], "b")

    def test_disabled_voice_cannot_be_used_and_in_use_disable_is_rejected(self):
        project = self.project()
        voices = self.repo.replace_voices([
            {"reference_id": "narrator", "name": "Narrator", "pool_order": 0},
            {"reference_id": "a", "name": "A", "pool_order": 1},
        ])
        self.repo.update_project_voice_pool(project["id"], [], voices[0]["id"])
        chapter = self.repo.list_chapters(project["id"])[0]
        self.repo.save_revision(chapter["id"], "reviewed", [{"speaker": "Alice", "text": "hello", "instruct": ""}])
        self.repo.refresh_characters(project["id"])
        self.repo.update_characters(project["id"], [{"speaker": "Alice", "gender": "", "personality": "", "voice_profile_id": voices[1]["id"]}])
        with self.assertRaisesRegex(RuntimeError, "voice_in_use"):
            self.repo.replace_voices([
                {"id": voices[0]["id"], "reference_id": "narrator", "name": "Narrator", "enabled": True},
                {"id": voices[1]["id"], "reference_id": "a", "name": "A", "enabled": False},
            ])
        self.repo.update_characters(project["id"], [{"speaker": "Alice", "gender": "", "personality": "", "voice_profile_id": None}])
        self.repo.replace_voices([
            {"id": voices[0]["id"], "reference_id": "narrator", "name": "Narrator", "enabled": True},
            {"id": voices[1]["id"], "reference_id": "a", "name": "A", "enabled": False},
        ])
        with self.assertRaisesRegex(RuntimeError, "No unbound Fish voice available"):
            self.repo.assign_voices(project["id"], ["Alice"])

    def test_project_voice_pool_rejects_bound_voice_and_filters_random_pool(self):
        project = self.project()
        voices = self.repo.replace_voices([
            {"reference_id": "narrator", "name": "Narrator", "pool_order": 0},
            {"reference_id": "a", "name": "A", "pool_order": 1},
            {"reference_id": "b", "name": "B", "pool_order": 2},
        ])
        self.repo.update_project_voice_pool(project["id"], [], voices[0]["id"])
        chapter = self.repo.list_chapters(project["id"])[0]
        self.repo.save_revision(chapter["id"], "reviewed", [{"speaker": "Alice", "text": "hello", "instruct": ""}])
        self.repo.refresh_characters(project["id"])
        self.repo.update_characters(project["id"], [{"speaker": "Alice", "gender": "", "personality": "", "voice_profile_id": voices[1]["id"]}])
        with self.assertRaisesRegex(RuntimeError, "voice_in_use"):
            self.repo.update_project_voice_pool(project["id"], [voices[1]["id"]], voices[0]["id"])
        self.repo.update_characters(project["id"], [{"speaker": "Alice", "gender": "", "personality": "", "voice_profile_id": None}])
        self.repo.update_project_voice_pool(project["id"], [voices[1]["id"]], voices[0]["id"])
        assigned = self.repo.assign_voices(project["id"], ["Alice"])
        self.assertEqual(assigned["Alice"]["reference_id"], "b")

    def test_validate_voice_caches_first_sample_and_allows_new_voice_save(self):
        class Response:
            def __init__(self, payload=None, content=b""):
                self.payload = payload
                self.content = content
            def raise_for_status(self):
                return None
            def json(self):
                return self.payload

        responses = iter([
            Response({
                "state": "trained",
                "title": "Demo",
                "tags": ["female", "young", "zh", "character-voice", "温柔"],
                "samples": [{"title": "Sample", "text": "Hello", "audio": "https://sample.test/a.wav"}],
            }),
            Response(content=b"RIFFsample"),
        ])
        with patch("httpx.get", side_effect=lambda *args, **kwargs: next(responses)):
            result = self.repo.validate_voice("demo-reference", "https://api.fish.audio")
        self.assertEqual(result["status"], "trained")
        self.assertEqual(result["gender"], "女")
        self.assertEqual(result["traits"], "young、温柔")
        self.assertTrue(result["sample_available"])
        saved = self.repo.replace_voices([
            {"reference_id": "demo-reference", "name": "Demo", "pool_order": 0, "enabled": True},
        ], require_validation=True)
        self.assertTrue(saved[0]["sample_available"])
        self.assertEqual(saved[0]["sample_text"], "Hello")

    def test_character_importance_sort_and_automated_updates_preserve_manual_metadata(self):
        project = self.project()
        chapters = self.repo.list_chapters(project["id"])
        self.repo.save_revision(chapters[0]["id"], "reviewed", [
            {"speaker": "Small", "text": "短", "instruct": ""},
            {"speaker": "NARRATOR", "text": "旁白不进入角色池", "instruct": ""},
            {"speaker": "Large", "text": "这是一段更长的台词", "instruct": ""},
        ])
        self.assertEqual([row["speaker"] for row in self.repo.refresh_characters(project["id"])], ["Large", "Small"])
        self.repo.update_characters(project["id"], [{"speaker": "Large", "gender": "女", "personality": "人工资料", "voice_profile_id": None}])
        self.repo.update_characters(project["id"], [{"speaker": "Large", "gender": "男", "personality": "自动资料"}], automated=True)
        large = self.repo.list_characters(project["id"])[0]
        self.assertEqual((large["gender"], large["personality"], large["user_edited"]), ("女", "人工资料", True))

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

    def test_updating_chapter_audio_invalidates_book_artifact(self):
        project = self.project(1)
        chapter = self.repo.list_chapters(project["id"])[0]
        output = self.repo.storage_root / "projects" / project["id"] / "output"
        output.mkdir(parents=True)
        book = output / "book.mp3"
        book.write_bytes(b"book")
        chapter_audio = output / "chapter.mp3"
        chapter_audio.write_bytes(b"chapter")
        self.repo.save_artifact(project["id"], None, "book_mp3", book)

        self.repo.save_artifact(project["id"], chapter["id"], "chapter_mp3", chapter_audio)

        self.assertEqual([row["kind"] for row in self.repo.list_artifacts(project["id"])], ["chapter_mp3"])


class FakeExecutor:
    def __init__(self, repo: Repository):
        self.repo = repo
        self.review_four_started = threading.Event()
        self.render_one_started = threading.Event()
        self.overlapped = False
        self.generated = []
        self.reviewed = []
        self.rendered = []
        self.analyzed = 0
        self.merged = 0

    def generate(self, chapter_id, project, log, should_stop=lambda: False):
        self.generated.append(chapter_id)
        return self.repo.save_revision(chapter_id, "generated", [{"speaker": "NARRATOR", "text": "text", "instruct": ""}], self.generate_input_hash(chapter_id, project))

    def generate_input_hash(self, chapter_id, project):
        return f"generate-{chapter_id}"

    def review_input_hash(self, generated, project):
        return f"review-{generated['content_hash']}"

    def review(self, chapter_id, project, generated, log, should_stop=lambda: False):
        self.reviewed.append(chapter_id)
        chapter = self.repo.get_chapter_record(chapter_id)
        if chapter.position == 4:
            self.review_four_started.set()
            self.overlapped = self.render_one_started.wait(1)
        return self.repo.save_revision(chapter_id, "reviewed", generated["entries"], self.review_input_hash(generated, project))

    def render(self, chapter_id, project, log, should_stop=lambda: False):
        self.rendered.append(chapter_id)
        chapter = self.repo.get_chapter_record(chapter_id)
        if chapter.position == 1:
            self.render_one_started.set()
            self.review_four_started.wait(1)
        path = self.repo.storage_root / "projects" / project["id"] / "output" / f"{chapter.position}.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mp3")
        self.repo.save_artifact(project["id"], chapter_id, "chapter_mp3", path)
        return path

    def analyze_characters(self, project):
        self.analyzed += 1
        return self.repo.list_characters(project["id"])

    def merge_book(self, project, log):
        self.merged += 1
        path = self.repo.storage_root / "projects" / project["id"] / "output" / "book.mp3"
        path.write_bytes(b"book")
        self.repo.save_artifact(project["id"], None, "book_mp3", path)
        return path


class PipelineTests(RepositoryTestCase):
    def test_stage_process_logs_utf8(self):
        messages = []
        StageExecutor(self.repo, Path(__file__).parents[1])._run(
            [sys.executable, "-c", "print('\u9752\u5c71')"],
            messages.append,
        )
        self.assertEqual(messages, ["\u9752\u5c71"])

    def test_stage_process_stops_when_job_is_cancelled(self):
        executor = StageExecutor(self.repo, Path(__file__).parents[1])
        stop = threading.Event()
        timer = threading.Timer(0.2, stop.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(StageCancelled):
                executor._run(
                    [sys.executable, "-u", "-c", "import time; print('started'); time.sleep(10)"],
                    lambda _message: None,
                    stop.is_set,
                )
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 2)

    def test_preprocess_stops_before_tts_and_render_only_runs_tts(self):
        project = self.project(2)
        executor = FakeExecutor(self.repo)
        preprocess = self.repo.create_job(project["id"], "preprocess")
        PipelineRunner(self.repo, executor, EventBroker()).run(preprocess["id"])

        self.assertEqual(len(executor.generated), 2)
        self.assertEqual(len(executor.reviewed), 2)
        self.assertEqual(executor.rendered, [])
        self.assertEqual(executor.merged, 0)
        self.assertEqual(self.repo.get_project(project["id"])["status"], "ready_for_tts")

        render = self.repo.create_job(project["id"], "render")
        PipelineRunner(self.repo, executor, EventBroker()).run(render["id"])
        self.assertEqual(len(executor.generated), 2)
        self.assertEqual(len(executor.reviewed), 2)
        self.assertEqual(len(executor.rendered), 2)
        self.assertEqual(executor.merged, 1)
        self.assertEqual(self.repo.get_job(render["id"])["status"], "completed")

    def test_partial_render_does_not_merge_book(self):
        project = self.project(2)
        self.repo.update_project(project["id"], {"from_chapter": 1, "to_chapter": 1})
        for chapter in self.repo.list_chapters(project["id"]):
            self.repo.save_revision(chapter["id"], "reviewed", [{"speaker": "NARRATOR", "text": "text", "instruct": ""}])
        job = self.repo.create_job(project["id"], "render")
        executor = FakeExecutor(self.repo)

        PipelineRunner(self.repo, executor, EventBroker()).run(job["id"])

        self.assertEqual(executor.rendered, [self.repo.list_chapters(project["id"])[0]["id"]])
        self.assertEqual(executor.merged, 0)

    def test_merge_job_requires_all_chapter_audio_and_merges_all_chapters(self):
        project = self.project(2)
        with self.assertRaisesRegex(RuntimeError, "merge_not_ready"):
            self.repo.create_job(project["id"], "merge")
        chapters = self.repo.list_chapters(project["id"])
        for chapter in chapters:
            path = self.repo.storage_root / "projects" / project["id"] / "output" / f"{chapter['position']}.mp3"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"mp3")
            self.repo.save_artifact(project["id"], chapter["id"], "chapter_mp3", path)

        job = self.repo.create_job(project["id"], "merge")
        executor = FakeExecutor(self.repo)
        PipelineRunner(self.repo, executor, EventBroker()).run(job["id"])

        self.assertEqual(executor.rendered, [])
        self.assertEqual(executor.merged, 1)
        self.assertEqual(self.repo.get_job(job["id"])["status"], "completed")

    def test_merge_book_ignores_selected_tts_range(self):
        project = self.project(2)
        self.repo.update_project(project["id"], {"from_chapter": 2, "to_chapter": 2})
        expected = []
        for chapter in self.repo.list_chapters(project["id"]):
            path = self.repo.storage_root / "projects" / project["id"] / "output" / f"{chapter['position']}.mp3"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"mp3")
            expected.append(path)
            self.repo.save_artifact(project["id"], chapter["id"], "chapter_mp3", path)
        merged_files = []

        def concat(files, output):
            merged_files.extend(files)
            output.write_bytes(b"book")

        with patch("tools.render_book.concat_mp3", concat):
            StageExecutor(self.repo, Path(__file__).parents[1]).merge_book(project, lambda _message: None)

        self.assertEqual(merged_files, expected)

    def test_render_requires_every_selected_chapter_to_be_preprocessed(self):
        project = self.project(2)
        chapter = self.repo.list_chapters(project["id"])[0]
        self.repo.save_revision(chapter["id"], "reviewed", [{"speaker": "NARRATOR", "text": "text", "instruct": ""}])
        with self.assertRaisesRegex(RuntimeError, "render_not_ready"):
            self.repo.create_job(project["id"], "render")

    def test_pause_after_generate_does_not_start_review(self):
        project = self.project(1)
        job = self.repo.create_job(project["id"])
        executor = FakeExecutor(self.repo)
        generate = executor.generate

        def generate_then_pause(chapter_id, project_data, log, should_stop=lambda: False):
            result = generate(chapter_id, project_data, log)
            self.repo.request_pause(job["id"])
            return result

        executor.generate = generate_then_pause
        PipelineRunner(self.repo, executor, EventBroker()).run(job["id"])

        chapter_id = self.repo.list_chapters(project["id"])[0]["id"]
        self.assertIsNone(self.repo.latest_revision(chapter_id, "reviewed"))
        self.assertEqual(self.repo.get_job(job["id"])["status"], "paused")

    def test_render_stop_discards_in_flight_results_and_stops_submitting(self):
        from fish_adapter.renderer import FishRenderer, RenderResult

        project = self.project(1)
        chapter = self.repo.list_chapters(project["id"])[0]
        self.repo.save_revision(chapter["id"], "reviewed", [
            {"speaker": "NARRATOR", "text": f"line {index}", "instruct": ""}
            for index in range(8)
        ])
        voices = self.repo.replace_voices([{
            "reference_id": "narrator",
            "name": "Narrator",
            "pool_order": 0,
        }])
        self.repo.update_project_voice_pool(project["id"], [], voices[0]["id"])
        self.repo.update_settings({"fish_api_key": "secret", "fish_workers": 2})
        executor = StageExecutor(self.repo, Path(__file__).parents[1])
        stop = threading.Event()
        release = threading.Event()
        both_started = threading.Event()
        all_returned = threading.Event()
        calls = 0
        returned = 0
        lock = threading.Lock()

        def blocked_synthesize(_renderer, _text, _voice):
            nonlocal calls, returned
            with lock:
                calls += 1
                if calls == 2:
                    both_started.set()
            release.wait(2)
            with lock:
                returned += 1
                if returned == 2:
                    all_returned.set()
            return RenderResult(audio=b"mp3", attempts=1)

        errors = []
        with patch.object(FishRenderer, "synthesize", blocked_synthesize):
            thread = threading.Thread(
                target=lambda: self._capture_error(
                    errors,
                    lambda: executor.render(chapter["id"], project, lambda _message: None, stop.is_set),
                )
            )
            thread.start()
            self.assertTrue(both_started.wait(2))
            stop.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            release.set()
            self.assertTrue(all_returned.wait(2))

        self.assertEqual(calls, 2)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], StageCancelled)
        with self.database.session() as session:
            self.assertEqual(session.query(AudioSegment).count(), 0)
        self.assertEqual(list((self.repo.storage_root / "projects" / project["id"] / "audio").rglob("*.mp3")), [])

    def test_render_stop_during_write_removes_audio(self):
        from fish_adapter.renderer import FishRenderer, RenderResult

        project = self.project(1)
        chapter = self.repo.list_chapters(project["id"])[0]
        self.repo.save_revision(chapter["id"], "reviewed", [{"speaker": "NARRATOR", "text": "line", "instruct": ""}])
        voices = self.repo.replace_voices([{
            "reference_id": "narrator",
            "name": "Narrator",
            "pool_order": 0,
        }])
        self.repo.update_project_voice_pool(project["id"], [], voices[0]["id"])
        self.repo.update_settings({"fish_api_key": "secret", "fish_workers": 1})
        stop = threading.Event()

        def write_then_stop(path, audio):
            path.write_bytes(audio)
            stop.set()

        with (
            patch.object(FishRenderer, "synthesize", return_value=RenderResult(audio=b"mp3", attempts=1)),
            patch("fish_adapter.fish_adapter._write_audio_atomic", write_then_stop),
            self.assertRaises(StageCancelled),
        ):
            StageExecutor(self.repo, Path(__file__).parents[1]).render(
                chapter["id"], project, lambda _message: None, stop.is_set
            )

        with self.database.session() as session:
            self.assertEqual(session.query(AudioSegment).count(), 0)
        self.assertEqual(list((self.repo.storage_root / "projects" / project["id"] / "audio").rglob("*.mp3")), [])

    def test_paused_and_cancelled_render_mark_stage_interrupted_and_chapter_reviewed(self):
        for stop_method, expected_status in (("request_pause", "paused"), ("cancel_job", "cancelled")):
            with self.subTest(stop_method=stop_method):
                project = self.project(1)
                chapter = self.repo.list_chapters(project["id"])[0]
                self.repo.save_revision(chapter["id"], "reviewed", [{"speaker": "NARRATOR", "text": "text", "instruct": ""}])
                job = self.repo.create_job(project["id"], "render")
                executor = FakeExecutor(self.repo)
                started = threading.Event()

                def render_until_stopped(_chapter_id, _project, _log, should_stop=lambda: False):
                    started.set()
                    while not should_stop():
                        time.sleep(0.01)
                    raise StageCancelled("Stage cancelled")

                executor.render = render_until_stopped
                thread = threading.Thread(target=PipelineRunner(self.repo, executor, EventBroker()).run, args=(job["id"],))
                thread.start()
                self.assertTrue(started.wait(2))
                getattr(self.repo, stop_method)(job["id"])
                thread.join(2)

                self.assertFalse(thread.is_alive())
                self.assertEqual(self.repo.get_job(job["id"])["status"], expected_status)
                self.assertEqual(self.repo.list_chapters(project["id"])[0]["status"], "reviewed")
                with self.database.session() as session:
                    stage = session.query(StageRun).filter_by(job_id=job["id"]).one()
                    self.assertEqual(stage.status, "interrupted")

    @staticmethod
    def _capture_error(errors, callback):
        try:
            callback()
        except Exception as exc:
            errors.append(exc)


if __name__ == "__main__":
    unittest.main()
