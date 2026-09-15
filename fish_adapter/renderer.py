"""Fish Audio HTTP client with bounded retries."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

try:
    import requests
    from requests import RequestException
except ImportError:  # pragma: no cover - the project requirements include requests
    requests = None  # type: ignore[assignment]

    class RequestException(Exception):
        """Fallback exception type used when requests is not installed."""


class FishTTSException(RuntimeError):
    """Base error carrying the number of HTTP attempts already made."""

    def __init__(self, message: str, *, attempts: int = 0, status_code: int | None = None):
        super().__init__(message)
        self.attempts = attempts
        self.status_code = status_code


class FishAuthenticationError(FishTTSException):
    """Raised for an invalid or unauthorized API key."""


class FishNetworkError(FishTTSException):
    """Raised after retryable network errors are exhausted."""


class FishHTTPError(FishTTSException):
    """Raised for non-success Fish HTTP responses."""


@dataclass(frozen=True)
class RenderResult:
    audio: bytes
    attempts: int


class FishRenderer:
    """Render one script item per Fish API request."""

    RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
    DEFAULT_BACKOFF_SECONDS = (2, 5, 10, 20, 40)

    def __init__(
        self,
        config: dict[str, Any],
        api_key: str,
        *,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_key:
            raise ValueError("Fish API key is empty")
        self.config = config
        self.api_key = api_key
        api_config = config["api"]
        tts_config = config["tts"]
        self.endpoint = f"{str(api_config['base_url']).rstrip('/')}/v1/tts"
        self.model = str(api_config["model"])
        self.timeout = float(tts_config["timeout_seconds"])
        render_config = config["render"]
        self.max_retries = int(render_config["max_retries"])
        backoff = render_config.get("retry_backoff_seconds", self.DEFAULT_BACKOFF_SECONDS)
        self.backoff_seconds = tuple(float(value) for value in backoff) or (0.0,)
        self.session = session
        if self.session is None:
            if requests is None:
                raise RuntimeError("requests is required; install app/requirements.txt")
            self.session = requests.Session()
        self.sleep = sleep

    def build_payload(self, fish_text: str, reference_id: str) -> dict[str, Any]:
        tts = self.config["tts"]
        return {
            "text": fish_text,
            "reference_id": reference_id,
            "temperature": tts["temperature"],
            "top_p": tts["top_p"],
            "format": tts["format"],
            "sample_rate": tts["sample_rate"],
            "mp3_bitrate": tts["mp3_bitrate"],
            "latency": tts["latency"],
            "condition_on_previous_chunks": tts["condition_on_previous_chunks"],
        }

    def _delay_before_retry(self, attempt: int) -> None:
        index = min(attempt - 1, len(self.backoff_seconds) - 1)
        self.sleep(max(0.0, self.backoff_seconds[index]))

    @staticmethod
    def _response_error_detail(response: Any) -> str:
        try:
            detail = str(getattr(response, "text", "")).strip()
        except Exception:
            detail = ""
        if not detail:
            return "no response body"
        return detail[:240].replace("\r", " ").replace("\n", " ")

    def synthesize(self, fish_text: str, reference_id: str) -> RenderResult:
        """Call Fish, retry transient failures, and return binary audio bytes."""
        payload = self.build_payload(fish_text, reference_id)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "model": self.model,
        }

        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.session.post(
                    self.endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )
            except (RequestException, TimeoutError, OSError) as exc:
                if attempt <= self.max_retries:
                    self._delay_before_retry(attempt)
                    continue
                raise FishNetworkError(
                    f"Fish request failed after {attempt} attempts: {exc}",
                    attempts=attempt,
                ) from exc

            status = int(getattr(response, "status_code", 0))
            if status == 401:
                raise FishAuthenticationError(
                    "Fish API rejected the API key (HTTP 401)",
                    attempts=attempt,
                    status_code=status,
                )

            if status in self.RETRYABLE_STATUS_CODES or 500 <= status <= 599:
                if attempt <= self.max_retries:
                    self._delay_before_retry(attempt)
                    continue
                detail = self._response_error_detail(response)
                raise FishHTTPError(
                    f"Fish request failed with HTTP {status} after {attempt} attempts: {detail}",
                    attempts=attempt,
                    status_code=status,
                )

            if status < 200 or status >= 300:
                detail = self._response_error_detail(response)
                raise FishHTTPError(
                    f"Fish request failed with HTTP {status}: {detail}",
                    attempts=attempt,
                    status_code=status,
                )

            audio = bytes(getattr(response, "content", b""))
            content_type = str(getattr(response, "headers", {}).get("content-type", "")).lower()
            if not audio:
                raise FishHTTPError(
                    "Fish returned an empty audio response",
                    attempts=attempt,
                    status_code=status,
                )
            if "json" in content_type or audio.lstrip().startswith(b"{"):
                detail = self._response_error_detail(response)
                raise FishHTTPError(
                    f"Fish returned a JSON error instead of audio: {detail}",
                    attempts=attempt,
                    status_code=status,
                )
            return RenderResult(audio=audio, attempts=attempt)
