"""CLI adapter from Alexandria annotated scripts to Fish Audio MP3 chunks."""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable

try:
    from .cache import (
        ManifestError,
        ManifestStore,
        fingerprint_entry,
        make_manifest_entry,
    )
    from .emotion_mapper import build_fish_text
    from .renderer import (
        FishAuthenticationError,
        FishRenderer,
        FishTTSException,
    )
except ImportError:  # Supports `python fish_adapter/fish_adapter.py`.
    from cache import ManifestError, ManifestStore, fingerprint_entry, make_manifest_entry
    from emotion_mapper import build_fish_text
    from renderer import FishAuthenticationError, FishRenderer, FishTTSException


LOGGER = logging.getLogger("fish_adapter")
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
DEFAULT_SCRIPT_PATH = PROJECT_ROOT / "annotated_script.json"
DEFAULT_VOICES_PATH = PACKAGE_DIR / "voices.json"
DEFAULT_CONFIG_PATH = PACKAGE_DIR / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "api": {
        "base_url": "https://api.fish.audio",
        "model": "s2.1-pro-free",
        "api_key_env": "FISH_API_KEY",
    },
    "tts": {
        "format": "mp3",
        "sample_rate": 44100,
        "mp3_bitrate": 128,
        "temperature": 0.7,
        "top_p": 0.7,
        "latency": "normal",
        "condition_on_previous_chunks": True,
        "timeout_seconds": 300,
    },
    "render": {
        "workers": 2,
        "max_retries": 5,
        "retry_backoff_seconds": [2, 5, 10, 20, 40],
        "output_dir": "output",
    },
}


class AdapterError(ValueError):
    """Raised for local input/configuration errors before rendering."""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise AdapterError(f"{label} not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(f"Cannot read {label} {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AdapterError(f"{label} must contain a JSON object: {path}")
    return data


def load_script(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise AdapterError(f"Script not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(f"Cannot read script {path}: {exc}") from exc

    if not isinstance(raw, list):
        raise AdapterError("Script must be a JSON array of speaker/text/instruct objects")
    if not raw:
        raise AdapterError("Script must contain at least one entry")

    script: list[dict[str, str]] = []
    for position, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise AdapterError(f"Script entry {position} must be an object")
        missing = [field for field in ("speaker", "text", "instruct") if field not in entry]
        if missing:
            raise AdapterError(
                f"Script entry {position} is missing required field(s): {', '.join(missing)}"
            )
        speaker = entry["speaker"]
        text = entry["text"]
        instruct = entry["instruct"]
        if not isinstance(speaker, str) or not speaker.strip():
            raise AdapterError(f"Script entry {position} has an empty or invalid speaker")
        if not isinstance(text, str) or not text.strip():
            raise AdapterError(f"Script entry {position} has empty or invalid text")
        if not isinstance(instruct, str):
            raise AdapterError(f"Script entry {position} has an invalid instruct")
        script.append({"speaker": speaker, "text": text, "instruct": instruct})
    return script


def load_voices(path: Path) -> dict[str, dict[str, Any]]:
    raw = load_json_object(path, "voices file")
    voices: dict[str, dict[str, Any]] = {}
    for speaker, value in raw.items():
        if not isinstance(speaker, str) or not speaker:
            raise AdapterError("Every voices.json key must be a non-empty speaker name")
        if not isinstance(value, dict):
            raise AdapterError(f"Voice entry '{speaker}' must be an object")
        reference_id = value.get("reference_id")
        if not isinstance(reference_id, str) or not reference_id.strip():
            raise AdapterError(f"Voice entry '{speaker}' needs a non-empty reference_id")
        if "name" in value and not isinstance(value["name"], str):
            raise AdapterError(f"Voice entry '{speaker}' has an invalid name")
        voices[speaker] = dict(value)
    return voices


def load_config(path: Path) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    if path.exists():
        raw = load_json_object(path, "config")
        config = _deep_merge(config, raw)
    else:
        LOGGER.warning("Config not found at %s; using built-in defaults", path)

    local_path = path.with_name("config.local.json")
    if local_path != path and local_path.exists():
        local = load_json_object(local_path, "local config")
        config = _deep_merge(config, local)

    try:
        api = config["api"]
        tts = config["tts"]
        render = config["render"]
        if not isinstance(api["base_url"], str) or not api["base_url"].strip():
            raise ValueError("api.base_url must be a non-empty string")
        if not isinstance(api["model"], str) or not api["model"].strip():
            raise ValueError("api.model must be a non-empty string")
        if not isinstance(api["api_key_env"], str) or not api["api_key_env"].strip():
            raise ValueError("api.api_key_env must be a non-empty string")
        if "api_key" in api and (
            not isinstance(api["api_key"], str) or not api["api_key"].strip()
        ):
            raise ValueError("api.api_key must be a non-empty string when provided")
        if not isinstance(tts["format"], str) or not tts["format"].strip():
            raise ValueError("tts.format must be a non-empty string")
        if int(tts["sample_rate"]) <= 0 or int(tts["mp3_bitrate"]) <= 0:
            raise ValueError("tts.sample_rate and tts.mp3_bitrate must be positive")
        if float(tts["timeout_seconds"]) <= 0:
            raise ValueError("tts.timeout_seconds must be positive")
        if int(render["workers"]) < 1 or int(render["max_retries"]) < 0:
            raise ValueError("render.workers must be >= 1 and max_retries must be >= 0")
        if not isinstance(render["output_dir"], str) or not render["output_dir"].strip():
            raise ValueError("render.output_dir must be a non-empty string")
        backoff = render.get("retry_backoff_seconds", [])
        if not isinstance(backoff, list) or any(float(value) < 0 for value in backoff):
            raise ValueError("render.retry_backoff_seconds must be a list of non-negative numbers")
    except (KeyError, TypeError, ValueError) as exc:
        raise AdapterError(f"Invalid config {path}: {exc}") from exc
    return config


def _resolve_api_key(config: dict[str, Any]) -> str:
    """Resolve a Fish key without exposing its value in errors or logs."""
    api = config["api"]
    api_key_env = api["api_key_env"]
    environment_key = os.environ.get(api_key_env, "").strip()
    if environment_key:
        return environment_key
    configured_key = api.get("api_key", "")
    if isinstance(configured_key, str) and configured_key.strip():
        return configured_key.strip()
    raise AdapterError(
        f"Fish API key is missing; set {api_key_env} or api.api_key in config.local.json"
    )


def _output_file_name(entry_id: int, audio_format: str) -> str:
    extension = audio_format.strip().lower().lstrip(".")
    if not extension:
        extension = "mp3"
    return f"{entry_id:06d}.{extension}"


def _write_audio_atomic(output_path: Path, audio: bytes) -> None:
    if not audio:
        raise AdapterError("Fish returned empty audio")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{output_path.stem}.", suffix=".part", dir=str(output_path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(audio)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, output_path)
    except Exception:
        try:
            os.remove(temp_name)
        except OSError:
            pass
        raise


def _resolve_output_dir(config_path: Path, configured: str, override: str | None) -> Path:
    if override:
        candidate = Path(override).expanduser()
        return candidate.resolve()
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        candidate = config_path.parent / candidate
    return candidate.resolve()


def _selected_ids(total: int, only: Iterable[int] | None) -> list[int]:
    if only is None:
        return list(range(1, total + 1))
    selected = sorted(set(int(value) for value in only))
    invalid = [value for value in selected if value < 1 or value > total]
    if invalid:
        raise AdapterError(
            f"--only id(s) out of range: {', '.join(str(value) for value in invalid)}"
        )
    return selected


def render_script(
    script: list[dict[str, str]],
    voices: dict[str, dict[str, Any]],
    config: dict[str, Any],
    output_dir: Path,
    *,
    only: Iterable[int] | None = None,
    session: Any | None = None,
    sleep: Any | None = None,
) -> dict[str, int]:
    """Render a script and return completed/skipped/failed counts."""
    selected = _selected_ids(len(script), only)
    selected_set = set(selected)
    force_set = set(selected) if only is not None else set()
    missing = sorted({item["speaker"] for item in script if item["speaker"] not in voices})
    if missing:
        raise AdapterError(
            "Missing voices for speaker(s): " + ", ".join(missing)
        )

    api_key = _resolve_api_key(config)

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_store = ManifestStore(output_dir / "manifest.json")
    previous = manifest_store.load()
    model = config["api"]["model"]
    tts_settings = config["tts"]
    records: list[dict[str, Any]] = []
    work: list[tuple[int, dict[str, str], str, str, Path]] = []
    counts = {"completed": 0, "skipped": 0, "failed": 0}

    for entry_id, item in enumerate(script, start=1):
        voice_id = voices[item["speaker"]]["reference_id"]
        fingerprint = fingerprint_entry(item, voice_id, model, tts_settings)
        file_name = _output_file_name(entry_id, tts_settings["format"])
        output_path = output_dir / file_name
        old = previous.get(entry_id)
        valid_cache = (
            old is not None
            and old.get("status") == "done"
            and old.get("fingerprint") == fingerprint
            and output_path.exists()
            and output_path.stat().st_size > 0
        )
        reusable = entry_id not in force_set and valid_cache
        if reusable:
            status = "done"
            attempts = int(old.get("attempts", 0)) if old else 0
            record = make_manifest_entry(
                entry_id, item, voice_id, file_name, fingerprint,
                status=status, attempts=attempts, error=None,
            )
            counts["skipped"] += 1
        elif entry_id not in selected_set:
            record = make_manifest_entry(
                entry_id, item, voice_id, file_name, fingerprint,
                status="done" if valid_cache else "pending",
                attempts=int(old.get("attempts", 0)) if old else 0,
                error=old.get("error") if old and not valid_cache else None,
            )
        else:
            record = make_manifest_entry(entry_id, item, voice_id, file_name, fingerprint)
            work.append((entry_id, item, voice_id, fingerprint, output_path))
        records.append(record)

    manifest_store.replace(records)
    if not work:
        counts["completed"] = 0
        return counts

    renderer_kwargs = {"session": session}
    if sleep is not None:
        renderer_kwargs["sleep"] = sleep
    renderer = FishRenderer(config, api_key, **renderer_kwargs)
    abort = threading.Event()

    def render_one(job: tuple[int, dict[str, str], str, str, Path]) -> tuple[int, bool]:
        entry_id, item, voice_id, _fingerprint, output_path = job
        if abort.is_set():
            manifest_store.update(entry_id, status="error", error="aborted after authentication failure", attempts=0)
            return entry_id, False
        try:
            fish_text = build_fish_text(item["text"], item["instruct"])
            result = renderer.synthesize(fish_text, voice_id)
            _write_audio_atomic(output_path, result.audio)
            manifest_store.update(entry_id, status="done", attempts=result.attempts, error=None)
            LOGGER.info("Rendered %06d (%s) in %d attempt(s)", entry_id, item["speaker"], result.attempts)
            return entry_id, True
        except FishAuthenticationError as exc:
            abort.set()
            manifest_store.update(entry_id, status="error", attempts=exc.attempts, error=str(exc))
            LOGGER.error("Authentication failed on %06d; stopping new requests", entry_id)
            return entry_id, False
        except FishTTSException as exc:
            manifest_store.update(entry_id, status="error", attempts=exc.attempts, error=str(exc))
            LOGGER.error("Failed %06d: %s", entry_id, exc)
            return entry_id, False
        except Exception as exc:
            manifest_store.update(entry_id, status="error", attempts=0, error=str(exc))
            LOGGER.error("Failed %06d: %s", entry_id, exc)
            return entry_id, False

    workers = max(1, int(config["render"]["workers"]))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(render_one, job) for job in work]
        for future in as_completed(futures):
            _entry_id, success = future.result()
            if success:
                counts["completed"] += 1
            else:
                counts["failed"] += 1

    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render an Alexandria script with Fish Audio")
    parser.add_argument("--script", type=Path, default=DEFAULT_SCRIPT_PATH)
    parser.add_argument("--voices", type=Path, default=DEFAULT_VOICES_PATH)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--only", type=int, nargs="+", metavar="ID")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--max-retries", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        script = load_script(args.script)
        voices = load_voices(args.voices)
        config = load_config(args.config)
        if args.workers is not None:
            if args.workers < 1:
                raise AdapterError("--workers must be >= 1")
            config["render"]["workers"] = args.workers
        if args.max_retries is not None:
            if args.max_retries < 0:
                raise AdapterError("--max-retries must be >= 0")
            config["render"]["max_retries"] = args.max_retries
        output_dir = _resolve_output_dir(args.config.resolve(), config["render"]["output_dir"], args.output)
        counts = render_script(script, voices, config, output_dir, only=args.only)
    except (AdapterError, ManifestError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print(
        f"Fish rendering complete: {counts['completed']} generated, "
        f"{counts['skipped']} skipped, {counts['failed']} failed"
    )
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
