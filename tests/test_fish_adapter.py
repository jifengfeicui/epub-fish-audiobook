from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fish_adapter.cache import fingerprint_entry, load_manifest
from fish_adapter.emotion_mapper import build_fish_text
from fish_adapter.fish_adapter import (
    AdapterError,
    DEFAULT_CONFIG,
    load_config,
    load_script,
    load_voices,
    render_script,
)
from fish_adapter.renderer import (
    FishAuthenticationError,
    FishHTTPError,
    FishRenderer,
)


class FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"audio", text: str = "", headers=None):
        self.status_code = status_code
        self.content = content
        self.text = text
        self.headers = headers or {"content-type": "audio/mpeg"}


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def make_test_config():
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["render"]["workers"] = 1
    config["render"]["max_retries"] = 0
    return config


class EmotionMapperTests(unittest.TestCase):
    def test_rich_instruction_is_wrapped_without_rewriting_spoken_text(self):
        text = "你到底想干什么？"
        instruct = "Angry but restrained, voice tense and threatening."
        self.assertEqual(
            build_fish_text(text, instruct),
            "[Angry but restrained, voice tense and threatening.]\n你到底想干什么？",
        )

    def test_empty_instruction_returns_text_unchanged(self):
        text = "夜色越来越深。\n"
        self.assertEqual(build_fish_text(text, "  \n"), text)


class RendererTests(unittest.TestCase):
    def test_request_shape_and_response(self):
        session = FakeSession([FakeResponse(200, b"mp3-bytes")])
        delays = []
        renderer = FishRenderer(make_test_config(), "secret", session=session, sleep=delays.append)
        result = renderer.synthesize("[Calm]\n你好。", "voice-123")

        self.assertEqual(result.audio, b"mp3-bytes")
        self.assertEqual(result.attempts, 1)
        url, kwargs = session.calls[0]
        self.assertEqual(url, "https://api.fish.audio/v1/tts")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(kwargs["headers"]["model"], "s2.1-pro-free")
        self.assertEqual(kwargs["json"]["reference_id"], "voice-123")
        self.assertEqual(kwargs["json"]["text"], "[Calm]\n你好。")
        self.assertEqual(delays, [])

    def test_retryable_statuses_use_backoff(self):
        session = FakeSession([
            FakeResponse(500, text="temporary"),
            FakeResponse(429, text="rate limited"),
            FakeResponse(200, b"mp3-bytes"),
        ])
        delays = []
        renderer = FishRenderer(make_test_config() | {"render": {
            **make_test_config()["render"],
            "max_retries": 5,
        }}, "secret", session=session, sleep=delays.append)
        result = renderer.synthesize("text", "voice")

        self.assertEqual(result.attempts, 3)
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(delays, [2.0, 5.0])

    def test_unauthorized_response_does_not_retry(self):
        session = FakeSession([FakeResponse(401, text="invalid key")])
        renderer = FishRenderer(make_test_config(), "secret", session=session, sleep=lambda _: self.fail("slept"))
        with self.assertRaises(FishAuthenticationError):
            renderer.synthesize("text", "voice")
        self.assertEqual(len(session.calls), 1)

    def test_non_audio_json_response_is_rejected(self):
        session = FakeSession([FakeResponse(
            200,
            b'{"error":"bad request"}',
            text='{"error":"bad request"}',
            headers={"content-type": "application/json"},
        )])
        renderer = FishRenderer(make_test_config(), "secret", session=session)
        with self.assertRaises(FishHTTPError):
            renderer.synthesize("text", "voice")


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"FISH_API_KEY": "secret"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def _script(self):
        return [
            {"speaker": "NARRATOR", "text": "夜色越来越深。", "instruct": "Quiet, somber narration."},
            {"speaker": "林轩", "text": "你来了。", "instruct": "Calm and confident."},
        ]

    def _voices(self):
        return {
            "NARRATOR": {"reference_id": "voice-narrator"},
            "林轩": {"reference_id": "voice-linxuan"},
        }

    def test_missing_voice_fails_before_http(self):
        session = FakeSession([])
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(AdapterError) as context:
                render_script(self._script(), {"NARRATOR": {"reference_id": "voice"}}, make_test_config(), Path(temp), session=session)
        self.assertIn("林轩", str(context.exception))
        self.assertEqual(session.calls, [])

    def test_missing_unselected_voice_is_still_reported_during_preflight(self):
        session = FakeSession([])
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(AdapterError) as context:
                render_script(
                    self._script(),
                    {"NARRATOR": {"reference_id": "voice"}},
                    make_test_config(),
                    Path(temp),
                    only=[1],
                    session=session,
                )
        self.assertIn("林轩", str(context.exception))
        self.assertEqual(session.calls, [])

    def test_cache_fingerprint_and_only_force_regeneration(self):
        session = FakeSession([
            FakeResponse(200, b"first"),
            FakeResponse(200, b"second"),
            FakeResponse(200, b"third"),
            FakeResponse(200, b"fourth"),
        ])
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            config = make_test_config()
            first = render_script(self._script(), self._voices(), config, output, session=session)
            self.assertEqual(first["completed"], 2)
            self.assertEqual(len(session.calls), 2)

            second = render_script(self._script(), self._voices(), config, output, session=session)
            self.assertEqual(second["skipped"], 2)
            self.assertEqual(len(session.calls), 2)

            changed = self._script()
            changed[1]["instruct"] = "Cold anger, voice tight."
            third = render_script(changed, self._voices(), config, output, session=session)
            self.assertEqual(third["completed"], 1)
            self.assertEqual(len(session.calls), 3)

            forced = render_script(changed, self._voices(), config, output, only=[1], session=session)
            self.assertEqual(forced["completed"], 1)
            self.assertEqual(len(session.calls), 4)

            manifest = load_manifest(output / "manifest.json")
            self.assertEqual(manifest[1]["status"], "done")
            self.assertEqual(manifest[1]["file"], "000001.mp3")
            self.assertEqual((output / "000001.mp3").read_bytes(), b"fourth")
            self.assertEqual((output / "000002.mp3").read_bytes(), b"third")

    def test_manifest_is_list_shaped_and_audio_is_atomic_result(self):
        session = FakeSession([FakeResponse(200, b"audio")])
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            render_script([self._script()[0]], self._voices(), make_test_config(), output, session=session)
            raw = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertIsInstance(raw, list)
            self.assertEqual(raw[0]["attempts"], 1)
            self.assertFalse(list(output.glob("*.part")))

    def test_load_script_rejects_empty_input(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "empty.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(AdapterError):
                load_script(path)

    def test_invalid_voice_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "voices.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(AdapterError):
                load_voices(path)

    def test_missing_api_key_fails_before_http(self):
        session = FakeSession([])
        with patch.dict(os.environ, {}, clear=True):
            with tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(AdapterError):
                    render_script(self._script(), self._voices(), make_test_config(), Path(temp), session=session)
        self.assertEqual(session.calls, [])

    def test_local_config_supplies_api_key_and_environment_overrides_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"api": {"model": "s2.1-pro-free", "api_key": "file-secret"}}),
                encoding="utf-8",
            )
            (root / "config.local.json").write_text(
                json.dumps({"api": {"api_key": "local-secret"}}),
                encoding="utf-8",
            )

            config = load_config(config_path)
            self.assertEqual(config["api"]["model"], "s2.1-pro-free")
            self.assertEqual(config["api"]["api_key"], "local-secret")

            session = FakeSession([FakeResponse(200, b"audio")])
            with patch.dict(os.environ, {}, clear=True):
                render_script(
                    [self._script()[0]], self._voices(), config, root / "output", session=session
                )
            self.assertEqual(
                session.calls[0][1]["headers"]["Authorization"], "Bearer local-secret"
            )

            override_session = FakeSession([FakeResponse(200, b"audio")])
            with patch.dict(os.environ, {"FISH_API_KEY": "env-secret"}, clear=True):
                render_script(
                    [self._script()[0]],
                    self._voices(),
                    config,
                    root / "override-output",
                    session=override_session,
                )
            self.assertEqual(
                override_session.calls[0][1]["headers"]["Authorization"], "Bearer env-secret"
            )

    def test_invalid_local_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config.json").write_text("{}", encoding="utf-8")
            (root / "config.local.json").write_text(
                json.dumps({"api": {"api_key": ""}}), encoding="utf-8"
            )
            with self.assertRaises(AdapterError):
                load_config(root / "config.json")

    def test_invalid_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps({"render": {"output_dir": ""}}), encoding="utf-8")
            with self.assertRaises(AdapterError):
                load_config(path)

    def test_fingerprint_changes_for_voice_and_model(self):
        item = self._script()[0]
        settings = make_test_config()["tts"]
        base = fingerprint_entry(item, "voice-a", "model-a", settings)
        self.assertNotEqual(base, fingerprint_entry(item, "voice-b", "model-a", settings))
        self.assertNotEqual(base, fingerprint_entry(item, "voice-a", "model-b", settings))


class PromptTests(unittest.TestCase):
    def test_prompt_files_keep_separator_and_chinese_rules(self):
        root = Path(__file__).resolve().parents[1]
        for name, marker in (
            ("default_prompts.txt", "CHINESE DIALOGUE AND NARRATION"),
            ("review_prompts.txt", "CHINESE REVIEW EXAMPLE"),
        ):
            content = (root / name).read_text(encoding="utf-8")
            self.assertEqual(content.count("---SEPARATOR---"), 1)
            self.assertIn(marker, content)


if __name__ == "__main__":
    unittest.main()
