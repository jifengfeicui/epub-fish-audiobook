import contextlib
import io
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import mock_open, patch

import httpx
from openai import OpenAI

import app.llm_json
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from generate_script import annotations_to_entries, annotation_response_format, process_chunk
from review_script import review_batch, review_source_segments
import review_script
from script_segments import annotation_batches, dialogue_prefixes, spoken_source, split_annotation_segments
from script_validation import compare_text_fidelity


FIXTURE = Path(__file__).parent / 'fixtures' / 'courage_introduction.txt'
SHORT_SOURCE = '青年：开头“不可能‘。\n\n哲人：那是”世界“。\n\n青年：还有问题。\n\n继续说下去。\n\n哲人：请讲。'


class DialogueSegmentsTests(unittest.TestCase):
    def test_continuations_keep_original_turn_context_across_batches(self):
        source = FIXTURE.read_text(encoding='utf-8')
        rows = split_annotation_segments(source)
        entries = [dict(speaker=row.get('expected_speaker', '青年'), text=row['text'], instruct='') for row in rows]
        reviewed = review_source_segments(entries, source)
        for index in (13, 14):
            self.assertIn('青年：非常感谢', rows[index]['source_context'])
            self.assertIn('的确，孩子眼中的世界', rows[index]['source_context'])
            self.assertEqual(reviewed[index]['source_context'], rows[index]['source_context'])
            self.assertNotIn('expected_speaker', reviewed[index])
        batches = annotation_batches(rows, 500, max_segments=1)
        self.assertEqual(batches[14][0]['source_context'], rows[14]['source_context'])
        source = SHORT_SOURCE + '\n青年：' + '论证。' * 1800 + '目标台词。\n哲人：答复。'
        long_rows = split_annotation_segments(source)
        for row in long_rows:
            self.assertLessEqual(len(row['source_context']), 2000)
            self.assertIn(row['text'].strip(), row['source_context'])
            if row.get('speaker_hint') == '青年':
                self.assertTrue(row['source_context'].startswith('青年：'))

    def test_narrator_recheck_corrects_speech_but_keeps_actual_action(self):
        source = SHORT_SOURCE.replace('继续说下去。', '世界是简单的。\n\n青年皱起了眉头。')
        rows = split_annotation_segments(source)
        entries = [dict(speaker=row.get('expected_speaker', '青年'), text=row['text'], instruct='平静') for row in rows]
        records = review_source_segments(entries, source)
        requests = []

        def completion(client, **kwargs):
            requests.append(kwargs)
            target, _ = json.JSONDecoder().raw_decode(kwargs['messages'][-1]['content'])
            annotations = [dict(segment_id=row['segment_id'], speaker=row.get('expected_speaker', 'NARRATOR'), instruct='平静') for row in target]
            if len(requests) == 2:
                self.assertEqual(len(target), 2)
                self.assertIn('青年：还有问题', target[0]['source_context'])
                annotations[0]['speaker'] = '青年'
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(annotations)), finish_reason='stop')])

        with patch('review_script.create_json_completion', side_effect=completion), patch('review_script.open', mock_open()), contextlib.redirect_stdout(io.StringIO()):
            result = review_batch(None, 'test', entries, 1, 1, source_segments=records, user_prompt_template='{batch}', max_retries=0)
        self.assertEqual(len(requests), 2)
        self.assertEqual([row['speaker'] for row in result], ['青年', '哲人', '青年', '青年', 'NARRATOR', '哲人'])
        self.assertEqual([row['text'] for row in result], [row['text'] for row in entries])

    def test_failed_narrator_recheck_does_not_replace_existing_output(self):
        rows = split_annotation_segments(SHORT_SOURCE)
        entries = [dict(speaker=row.get('expected_speaker', '青年'), text=row['text'], instruct='') for row in rows]
        calls = []

        def completion(client, **kwargs):
            target = kwargs['response_format']['json_schema']['schema']['prefixItems']
            calls.append(target)
            annotations = [dict(segment_id=row['properties']['segment_id']['const'], speaker=row['properties']['speaker'].get('const', 'NARRATOR'), instruct='') for row in target]
            if len(calls) > 1:
                annotations = []
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(annotations)), finish_reason='stop')])

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'source.txt').write_text(SHORT_SOURCE, encoding='utf-8')
            (root / 'script.json').write_text(json.dumps(entries), encoding='utf-8')
            (root / 'config.json').write_text(json.dumps({'llm': {'api_key': 'test'}}), encoding='utf-8')
            output = root / 'reviewed.json'
            output.write_text('existing output', encoding='utf-8')
            args = ['review_script', '--script', str(root / 'script.json'), '--source', str(root / 'source.txt'), '--output', str(output), '--config', str(root / 'config.json')]
            with patch.object(sys, 'argv', args), patch('review_script.create_json_completion', side_effect=completion), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    review_script.main()
            self.assertEqual(output.read_text(encoding='utf-8'), 'existing output')
        self.assertEqual(len(calls), 4)

    def test_real_introduction_has_all_42_turns_and_no_mixed_segment(self):
        source = FIXTURE.read_text(encoding='utf-8')
        self.assertEqual(Counter(match['name'] for match in dialogue_prefixes(source)), {'青年': 21, '哲人': 21})
        segments = split_annotation_segments(source)
        self.assertEqual(''.join(row['text'] for row in segments), spoken_source(source))
        self.assertEqual({row['turn_id'] for row in segments}, set(range(43)))
        self.assertTrue(all(len(row['text']) <= 500 for row in segments))
        self.assertTrue(all('青年：' not in row['text'] and '哲人：' not in row['text'] for row in segments))
        for match in dialogue_prefixes(source):
            row = next(row for row in segments if row['source_start'] == match.end())
            self.assertEqual(row['expected_speaker'], match['name'])
        for row in segments:
            if row['turn_id'] == 0:
                self.assertEqual(row['expected_speaker'], 'NARRATOR')

    def test_bad_quotes_do_not_cross_paragraph_or_explicit_turn(self):
        rows = split_annotation_segments(SHORT_SOURCE)
        self.assertEqual([row.get('expected_speaker') for row in rows], ['青年', '哲人', '青年', None, '哲人'])
        self.assertEqual(rows[3]['speaker_hint'], '青年')
        self.assertEqual(rows[1]['text'].strip(), '那是”世界“。')
        prose = split_annotation_segments('“坏掉的引号。\n\n他回答：“正常台词。”\n她转身离去。')
        self.assertFalse(any('坏掉' in row['text'] and '正常' in row['text'] for row in prose))
        self.assertEqual(''.join(row['text'] for row in prose), '“坏掉的引号。\n\n他回答：“正常台词。”\n她转身离去。')

    def test_prose_quotes_nested_quotes_and_english_remain_lossless(self):
        for source in ['林轩皱眉。他说道：“你引用的「勇气」是什么？”她没有回答。',
                       'He said, "I cannot leave." She nodded.\n"Go," he replied.',
                       '地下人想到‘你们’时，仍在自言自语。']:
            with self.subTest(source=source):
                rows = split_annotation_segments(source)
                self.assertEqual(''.join(row['text'] for row in rows), source)
        self.assertTrue(any(row['text'] == '“你引用的「勇气」是什么？”' for row in split_annotation_segments('他说：“你引用的「勇气」是什么？”')))

    def test_colons_in_titles_times_urls_and_narration_are_not_removed(self):
        source = '被讨厌的勇气：阿德勒的哲学课\n12:30 出发。\nhttps://example.test\nhttps://example.test/two\n他说：不要走。\n'
        self.assertEqual(spoken_source(source), source)
        self.assertTrue(spoken_source(source + SHORT_SOURCE).startswith(source))
        # Once labels are removed, deleting real dialogue still fails fidelity.
        rows = split_annotation_segments(SHORT_SOURCE)
        entries = [dict(speaker=row.get('expected_speaker', '青年'), text=row['text'], instruct='') for row in rows]
        self.assertTrue(compare_text_fidelity(SHORT_SOURCE, entries).exact)
        self.assertFalse(compare_text_fidelity(SHORT_SOURCE, entries[:-1]).exact)
        modified = [dict(row) for row in entries]
        modified[0]['text'] = modified[0]['text'].replace('开头', '')
        self.assertFalse(compare_text_fidelity(SHORT_SOURCE, modified).exact)
        attribution = '他说：走吧。\n她问：为什么？\n他说：天黑了。\n她问：然后呢？'
        self.assertEqual(spoken_source(attribution), attribution)

    def test_long_dialogue_stays_bounded_and_keeps_role_across_batches(self):
        source = SHORT_SOURCE + '\n\n青年：' + '这是一段较长的话。' * 200
        rows = split_annotation_segments(source)
        self.assertTrue(all(len(row['text'].strip()) <= 500 for row in rows))
        batches = annotation_batches(rows, 150, max_segments=3)
        self.assertTrue(all(len(batch) <= 3 and sum(len(row['text']) for row in batch) <= 150 for batch in batches))
        self.assertEqual(''.join(row['text'] for batch in batches for row in batch), spoken_source(source))
        self.assertTrue(all(row['expected_speaker'] == '青年' for batch in batches for row in batch if '这是一段' in row['text']))
        for text in ['甲' * 500 + '\n\n', '\n' * 600 + '甲']:
            capped = split_annotation_segments(text)
            self.assertTrue(all(len(row['text']) <= 500 for row in capped))
            self.assertTrue(compare_text_fidelity(text, capped).exact)

    def test_explicit_role_is_both_schema_constraint_and_local_validation(self):
        rows = split_annotation_segments(SHORT_SOURCE)
        annotations = [dict(segment_id=row['segment_id'], speaker=row.get('expected_speaker', '青年'), instruct='') for row in rows]
        schema = annotation_response_format(rows)['json_schema']['schema']
        self.assertEqual(schema['prefixItems'][0]['properties']['speaker'], {'const': '青年'})
        annotations[0]['speaker'] = '哲人'
        with self.assertRaisesRegex(ValueError, 'explicit speaker'):
            annotations_to_entries(rows, annotations)

    def test_review_maps_constraints_to_full_source_and_rejects_mixed_legacy_entries(self):
        rows = split_annotation_segments(SHORT_SOURCE)
        entries = [dict(speaker='NARRATOR', text=row['text'], instruct='') for row in rows]
        records = review_source_segments(entries, SHORT_SOURCE)
        self.assertEqual(records[1]['expected_speaker'], '哲人')
        self.assertEqual(records[3]['speaker_hint'], '青年')
        with self.assertRaisesRegex(ValueError, 'crosses explicit'):
            review_source_segments([dict(speaker='青年', text=''.join(row['text'] for row in rows), instruct='')], SHORT_SOURCE)
        with self.assertRaisesRegex(ValueError, 'rerun Generate'):
            review_source_segments([dict(speaker='青年', text=SHORT_SOURCE, instruct='')], SHORT_SOURCE)

    def test_generate_review_retries_wrong_roles_in_deepseek_json_mode(self):
        requests = []
        invalid = [True]

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            if body['response_format']['type'] == 'json_schema':
                return httpx.Response(400, json={'error': {'message': 'This response_format type is unavailable now'}})
            prompt = body['messages'][-1]['content']
            rows, _ = json.JSONDecoder().raw_decode(prompt)
            annotations = [dict(segment_id=row['segment_id'], speaker=row.get('expected_speaker', row.get('speaker_hint') or 'NARRATOR'), instruct='Calm, clear voice.') for row in rows]
            if invalid[0]:
                annotations[0]['speaker'] = '哲人'
                invalid[0] = False
            return httpx.Response(200, json={'id': 'test', 'object': 'chat.completion', 'created': 0, 'model': 'test',
                'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': json.dumps({'items': annotations}, ensure_ascii=False)}}]})

        with OpenAI(api_key='test', http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
            with patch('generate_script.open', mock_open()), patch('review_script.open', mock_open()), contextlib.redirect_stdout(io.StringIO()):
                entries = process_chunk(client, 'test', SHORT_SOURCE, 1, 1, user_prompt_template='{chunk}')
                records = review_source_segments(entries, SHORT_SOURCE)
                invalid[0] = True
                reviewed = review_batch(client, 'test', entries, 1, 1, source_segments=records, user_prompt_template='{batch}\n{context}', first_person_speaker='地下人')
                self.assertEqual([row['speaker'] for row in reviewed], ['青年', '哲人', '青年', '青年', '哲人'])
                invalid[0] = True
                self.assertIsNone(review_batch(client, 'test', entries, 1, 1, source_segments=records, user_prompt_template='{batch}', max_retries=0))
        self.assertTrue(compare_text_fidelity(SHORT_SOURCE, reviewed).exact)
        self.assertTrue(any('地下人' in body['messages'][0]['content'] or '地下人' in body['messages'][-1]['content'] for body in requests))

    def test_both_review_cli_modes_receive_source_constraints_and_failed_review_preserves_output(self):
        entries = [dict(speaker=row.get('expected_speaker', '青年'), text=row['text'], instruct='')
                   for row in split_annotation_segments(SHORT_SOURCE)]
        for window in (0, 2):
            with self.subTest(window=window), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / 'source.txt').write_text(SHORT_SOURCE, encoding='utf-8')
                (root / 'script.json').write_text(json.dumps(entries, ensure_ascii=False), encoding='utf-8')
                (root / 'config.json').write_text(json.dumps({'llm': {'api_key': 'test'}, 'generation': {'review_batch_size': 2}}), encoding='utf-8')
                output = root / 'reviewed.json'
                output.write_text('existing output', encoding='utf-8')
                requests = []

                def completion(client, **kwargs):
                    requests.append(kwargs)
                    schema = kwargs['response_format']['json_schema']['schema']
                    annotations = [dict(segment_id=row['properties']['segment_id']['const'],
                                        speaker=row['properties']['speaker'].get('const', '青年'), instruct='Clear, calm voice.')
                                   for row in schema['prefixItems']]
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(annotations)), finish_reason='stop')])

                args = ['review_script', '--script', str(root / 'script.json'), '--output', str(output),
                        '--source', str(root / 'source.txt'), '--config', str(root / 'config.json'), '--context-window', str(window)]
                with patch.object(sys, 'argv', args), patch('review_script.create_json_completion', side_effect=completion), contextlib.redirect_stdout(io.StringIO()):
                    review_script.main()
                reviewed = json.loads(output.read_text(encoding='utf-8'))
                self.assertEqual([row['speaker'] for row in reviewed], ['青年', '哲人', '青年', '青年', '哲人'])
                self.assertTrue(any('source_context' in request['messages'][-1]['content'] for request in requests))
                output.write_text('existing output', encoding='utf-8')

                def invalid_completion(client, **kwargs):
                    response = completion(client, **kwargs)
                    rows = json.loads(response.choices[0].message.content)
                    rows[0]['speaker'] = 'NARRATOR'
                    response.choices[0].message.content = json.dumps(rows)
                    return response

                with patch.object(sys, 'argv', args), patch('review_script.create_json_completion', side_effect=invalid_completion), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        review_script.main()
                self.assertEqual(output.read_text(encoding='utf-8'), 'existing output')
