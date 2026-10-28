import json
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select

from backend.alexandria.db.database import Database
from backend.alexandria.db.models import SpeakerAssignment
from backend.alexandria.db.repository import Repository
from backend.alexandria.main import _defaults
from backend.alexandria.services.stages import StageExecutor


class DialogueSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / 'test.db')
        self.db.create_schema()
        self.repo = Repository(self.db, self.root)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_legacy_defaults_upgrade_per_field_and_restart_preserves_customizations(self):
        defaults = _defaults()
        legacy = json.loads((Path(__file__).parent / 'fixtures' / 'legacy_prompts.json').read_text(encoding='utf-8'))
        legacy['user_prompt'] = '我的自定义模板：{chunk}'
        self.repo.seed_settings({'prompts': legacy})
        self.repo.seed_settings(defaults)
        prompts = self.repo.get_settings()['prompts']
        self.assertEqual(prompts['user_prompt'], legacy['user_prompt'])
        for key in ['system_prompt', 'review_system_prompt', 'review_user_prompt']:
            self.assertEqual(prompts[key], defaults['prompts'][key])
        self.repo.seed_settings(defaults)
        self.assertEqual(self.repo.get_settings()['prompts'], prompts)
        self.db.close()
        self.db = Database(self.root / 'test.db')
        self.repo = Repository(self.db, self.root)
        self.repo.seed_settings(defaults)
        self.assertEqual(self.repo.get_settings()['prompts'], prompts)

    def test_changed_script_invalidates_only_automatic_character_metadata_and_keeps_voice(self):
        project = self.repo.create_project(title='Book', source_filename='book.txt', source_path='book.txt', source_sha256='0' * 64,
            chapters=[{'index': 1, 'title': 'Chapter', 'href': '1', 'text': '原文'}],
            render_start_mode='after_review_batch', release_batch_size=3)
        chapter_id = self.repo.list_chapters(project['id'])[0]['id']
        original = [dict(speaker=name, text='旧台词。', instruct='') for name in ['青年', '哲人']]
        self.repo.save_revision(chapter_id, 'reviewed', original)
        self.repo.refresh_characters(project['id'])
        voices = self.repo.replace_voices([{'reference_id': 'sample', 'name': 'Sample', 'pool_order': 0}])
        with self.db.session() as session, session.begin():
            rows = session.scalars(select(SpeakerAssignment).where(SpeakerAssignment.project_id == project['id'])).all()
            for row in rows:
                row.gender = '男'
                row.personality = '旧性格'
                row.user_edited = row.speaker == '哲人'
                row.voice_profile_id = voices[0]['id']
        new = [dict(row, text='新台词。') for row in original]
        self.repo.save_revision(chapter_id, 'generated', new)
        rows = {row['speaker']: row for row in self.repo.refresh_characters(project['id'])}
        self.assertEqual((rows['青年']['gender'], rows['青年']['personality']), ('', ''))
        self.assertEqual((rows['哲人']['gender'], rows['哲人']['personality']), ('男', '旧性格'))
        self.assertTrue(all(row['voice_profile_id'] == voices[0]['id'] for row in rows.values()))
        self.assertEqual(len(self.repo.list_characters(project['id'])), 2)
        self.assertEqual(self.repo.get_script(chapter_id)['revision'], 2)

    def test_parser_changes_invalidate_both_stage_fingerprints(self):
        self.repo.seed_settings(_defaults())
        project = self.repo.create_project(title='Book', source_filename='book.txt', source_path='book.txt', source_sha256='0' * 64,
            chapters=[{'index': 1, 'title': 'Chapter', 'href': '1', 'text': '原文'}],
            render_start_mode='after_review_batch', release_batch_size=3)
        chapter_id = self.repo.list_chapters(project['id'])[0]['id']
        executor = StageExecutor(self.repo, Path(__file__).resolve().parents[1])
        generated = {'content_hash': 'script'}
        before = (executor.generate_input_hash(chapter_id, project), executor.review_input_hash(generated, project))
        original_hash = executor._file_hash
        executor._file_hash = lambda path: 'changed-parser' if path.name == 'script_segments.py' else original_hash(path)
        after = (executor.generate_input_hash(chapter_id, project), executor.review_input_hash(generated, project))
        self.assertNotEqual(before[0], after[0])
        self.assertNotEqual(before[1], after[1])
