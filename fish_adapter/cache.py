"""Manifest and cache helpers for resumable Fish rendering."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class ManifestError(ValueError):
    """Raised when an existing manifest cannot be read safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json_write(data: Any, path: Path) -> None:
    """Write JSON beside the destination and replace it atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.remove(temp_name)
        except OSError:
            pass
        raise


def load_manifest(path: Path) -> dict[int, dict[str, Any]]:
    """Load a list-shaped manifest, keyed by its 1-based segment id."""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"Cannot read manifest {path}: {exc}") from exc
    if not isinstance(raw, list):
        raise ManifestError(f"Manifest {path} must contain a JSON array")

    entries: dict[int, dict[str, Any]] = {}
    for position, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise ManifestError(f"Manifest entry {position} must be an object")
        entry_id = entry.get("id")
        if isinstance(entry_id, bool) or not isinstance(entry_id, int) or entry_id < 1:
            raise ManifestError(f"Manifest entry {position} has an invalid id")
        if entry_id in entries:
            raise ManifestError(f"Manifest contains duplicate id {entry_id}")
        entries[entry_id] = dict(entry)
    return entries


def fingerprint_entry(
    item: dict[str, Any],
    voice_id: str,
    model: str,
    tts_settings: dict[str, Any],
) -> str:
    """Hash every input that can change generated audio, excluding secrets."""
    payload = {
        "speaker": item["speaker"],
        "text": item["text"],
        "instruct": item["instruct"],
        "voice_id": voice_id,
        "model": model,
        "tts": tts_settings,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def make_manifest_entry(
    entry_id: int,
    item: dict[str, Any],
    voice_id: str,
    file_name: str,
    fingerprint: str,
    *,
    status: str = "pending",
    attempts: int = 0,
    error: str | None = None,
) -> dict[str, Any]:
    """Build the stable public record for one script item."""
    return {
        "id": entry_id,
        "speaker": item["speaker"],
        "text": item["text"],
        "instruct": item["instruct"],
        "voice_id": voice_id,
        "file": file_name,
        "fingerprint": fingerprint,
        "status": status,
        "attempts": attempts,
        "error": error,
        "updated_at": _utc_now(),
    }


class ManifestStore:
    """Thread-safe manifest store with an atomic write after each update."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._entries: dict[int, dict[str, Any]] = {}

    def load(self) -> dict[int, dict[str, Any]]:
        with self._lock:
            self._entries = load_manifest(self.path)
            return {key: dict(value) for key, value in self._entries.items()}

    def replace(self, entries: Iterable[dict[str, Any]]) -> None:
        ordered = sorted((dict(entry) for entry in entries), key=lambda item: item["id"])
        with self._lock:
            self._entries = {entry["id"]: entry for entry in ordered}
            atomic_json_write(ordered, self.path)

    def update(self, entry_id: int, **changes: Any) -> dict[str, Any]:
        with self._lock:
            if entry_id not in self._entries:
                raise ManifestError(f"Unknown manifest id {entry_id}")
            self._entries[entry_id].update(changes)
            self._entries[entry_id]["updated_at"] = _utc_now()
            ordered = [self._entries[key] for key in sorted(self._entries)]
            atomic_json_write(ordered, self.path)
            return dict(self._entries[entry_id])

    def get(self, entry_id: int) -> dict[str, Any] | None:
        with self._lock:
            entry = self._entries.get(entry_id)
            return dict(entry) if entry is not None else None
