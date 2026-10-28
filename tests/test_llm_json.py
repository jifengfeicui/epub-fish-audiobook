import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch

import httpx
from openai import AuthenticationError, BadRequestError, OpenAI

from app.llm_json import create_json_completion
from backend.alexandria.db.database import Database
from backend.alexandria.db.repository import Repository
from backend.alexandria.services.bilibili import BilibiliService
from backend.alexandria.services.stages import StageExecutor

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from generate_script import annotation_response_format, process_chunk, split_annotation_segments
from review_script import review_batch


def completion(content):
    return httpx.Response(200, json={
        "id": "test", "object": "chat.completion", "created": 0, "model": "test-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(content)}, "finish_reason": "stop"}],
    })


def unsupported():
    return httpx.Response(400, json={"error": {
        "message": "This response_format type is unavailable now (request_id: test)",
        "type": "invalid_request_error", "code": "invalid_request_error",
    }})


class JsonCompletionTests(unittest.TestCase):
    def client(self, handler):
        requests = []

        def send(request):
            body = json.loads(request.content)
            requests.append(body)
            return handler(body)

        client = OpenAI(api_key="test-key", base_url="https://llm.example.test/v1", max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(send)))
        self.addCleanup(client.close)
        return client, requests

    def test_supported_schema_is_unchanged(self):
        client, requests = self.client(lambda _: completion([]))
        messages = [{"role": "system", "content": "Return a JSON array."}, {"role": "user", "content": "Annotate."}]
        schema = annotation_response_format([])
        response = create_json_completion(client, model="local-model", messages=messages, response_format=schema, max_tokens=100)
        self.assertEqual(json.loads(response.choices[0].message.content), [])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["response_format"], schema)
        self.assertEqual(requests[0]["messages"], messages)

    def test_reproduces_deepseek_error_then_wraps_arrays_and_remembers_per_client(self):
        annotations = [{"segment_id": "s0001", "speaker": "NARRATOR", "instruct": ""}]
        client, requests = self.client(lambda body: unsupported() if body["response_format"]["type"] == "json_schema" else completion({"items": annotations}))
        messages = [{"role": "system", "content": "Return only a JSON array."}, {"role": "user", "content": "Annotate."}]
        schema = annotation_response_format([{"segment_id": "s0001", "text": "原文。"}])
        for _ in range(2):
            response = create_json_completion(client, model="deepseek-test", messages=messages, response_format=schema,
                                              max_tokens=123, temperature=0.2, reasoning_effort="none")
            self.assertEqual(json.loads(response.choices[0].message.content), annotations)
        self.assertEqual([request["response_format"]["type"] for request in requests], ["json_schema", "json_object", "json_object"])
        self.assertEqual(messages[0]["content"], "Return only a JSON array.")
        self.assertEqual(schema["json_schema"]["schema"]["type"], "array")
        fallback = requests[1]
        self.assertIn('"items"', fallback["messages"][0]["content"])
        self.assertIn('"s0001"', fallback["messages"][0]["content"])
        self.assertEqual(fallback["max_tokens"], 123)
        self.assertEqual(fallback["reasoning_effort"], "none")
        other, other_requests = self.client(lambda _: completion(annotations))
        create_json_completion(other, model="other", messages=messages, response_format=schema)
        self.assertEqual(other_requests[0]["response_format"]["type"], "json_schema")

    def test_object_output_and_messages_without_system_are_supported(self):
        metadata = {"author": "作者", "publisher": "", "description": "简介"}
        client, requests = self.client(lambda body: unsupported() if body["response_format"]["type"] == "json_schema" else completion(metadata))
        response = create_json_completion(client, model="test", messages=[{"role": "user", "content": "Return metadata."}], response_format={
            "type": "json_schema", "json_schema": {"name": "book_metadata", "schema": {"type": "object", "properties": {key: {"type": "string"} for key in metadata}}},
        })
        self.assertEqual(json.loads(response.choices[0].message.content), metadata)
        self.assertEqual(requests[1]["messages"][0]["role"], "system")
        self.assertNotIn('"items"', requests[1]["messages"][0]["content"])

    def test_unrelated_errors_do_not_downgrade(self):
        for status, message, exception in ((400, "Invalid max_tokens", BadRequestError), (401, "Invalid API key", AuthenticationError)):
            with self.subTest(status=status):
                client, requests = self.client(lambda _: httpx.Response(status, json={"error": {"message": message}}))
                with self.assertRaises(exception):
                    create_json_completion(client, model="test", messages=[], response_format=annotation_response_format([]))
                self.assertEqual(len(requests), 1)

    def test_invalid_array_envelope_is_rejected(self):
        for invalid in ({"items": "not an array"}, {"other": []}, {"items": [], "extra": "unexpected"}):
            with self.subTest(invalid=invalid):
                client, _ = self.client(lambda body: unsupported() if body["response_format"]["type"] == "json_schema" else completion(invalid))
                with self.assertRaisesRegex(ValueError, "items array"):
                    create_json_completion(client, model="test", messages=[], response_format=annotation_response_format([]))

    def test_generate_and_review_use_json_mode_without_rewriting_source(self):
        source = "原文第一句。原文第二句。"
        annotations = [{"segment_id": segment["segment_id"], "speaker": "NARRATOR", "instruct": ""}
                       for segment in split_annotation_segments(source)]
        client, requests = self.client(lambda body: unsupported() if body["response_format"]["type"] == "json_schema" else completion({"items": annotations}))
        with patch("generate_script.open", mock_open()), patch("review_script.open", mock_open()), contextlib.redirect_stdout(io.StringIO()):
            entries = process_chunk(client, "deepseek-test", source, 1, 1, max_retries=0)
            reviewed = review_batch(client, "deepseek-test", entries, 1, 1, max_retries=0)
        self.assertEqual("".join(entry["text"] for entry in entries), source)
        self.assertEqual("".join(entry["text"] for entry in reviewed), source)
        self.assertEqual([request["response_format"]["type"] for request in requests], ["json_schema", "json_object", "json_object"])

    def test_generate_and_review_still_reject_missing_or_rewritten_segments(self):
        for invalid in ([], [{"segment_id": "s0001", "speaker": "NARRATOR", "instruct": "", "text": "改写"}]):
            with self.subTest(invalid=invalid):
                client, _ = self.client(lambda body: unsupported() if body["response_format"]["type"] == "json_schema" else completion({"items": invalid}))
                with patch("generate_script.open", mock_open()), patch("review_script.open", mock_open()), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(process_chunk(client, "deepseek-test", "原文。", 1, 1, max_retries=0), [])
                    self.assertIsNone(review_batch(client, "deepseek-test", [{"speaker": "NARRATOR", "text": "原文。", "instruct": ""}], 1, 1, max_retries=0))

    def test_character_analysis_and_book_metadata_also_support_json_mode(self):
        def reply(body):
            if body["response_format"]["type"] == "json_schema":
                return unsupported()
            if '"items"' in body["messages"][0]["content"]:
                return completion({"items": [{"speaker": "Alice", "gender": "女", "personality": "温柔"}]})
            return completion({"author": "作者", "publisher": "出版社", "description": "简介"})

        client, requests = self.client(reply)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "test.db")
            try:
                database.create_schema()
                repository = Repository(database, root)
                project = repository.create_project(
                    title="Book", source_filename="book.txt", source_path="book.txt", source_sha256="0" * 64,
                    chapters=[{"index": 1, "title": "Chapter", "href": "1", "text": "Hello"}],
                    render_start_mode="after_review_batch", release_batch_size=3,
                )
                chapter = repository.list_chapters(project["id"])[0]
                repository.save_revision(chapter["id"], "reviewed", [{"speaker": "Alice", "text": "Hello", "instruct": ""}])
                with patch("openai.OpenAI", return_value=client):
                    characters = StageExecutor(repository, APP_DIR.parent).analyze_characters(project)
                    metadata = BilibiliService(repository).extract_metadata(project["id"])
                self.assertEqual(characters[0]["gender"], "女")
                self.assertEqual(metadata, {"author": "作者", "publisher": "出版社", "description": "简介"})
                self.assertEqual([request["response_format"]["type"] for request in requests], ["json_schema", "json_object", "json_object"])
            finally:
                database.close()
