"""Bounded SerpAPI caption start markers for locating a source video window.

Caption text and cue starts are provider data, not independently observed audio,
speaker identity, recording duration or verified alignment. Some real responses
contain no cue end times, so this private DTO deliberately promises none.
"""

import html
import json
import math
import ssl
import time
from typing import Annotated, Any
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from ..reporting.remote_youtube import canonical_youtube_url
from ..shared.errors import ErrorCode, QuantroError, Stage
from .tigerdata_storage import StorageError, _environment

ENDPOINT = "https://serpapi.com/search.json"
TIMEOUT_SECONDS = 12.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CUES = 10_000
MAX_TEXT_CHARACTERS = 240_000
MAX_START_SECONDS = 86_400.0
CAPTION_LOCATOR_LIMITATION = (
    "Provider caption start markers were used only to locate a video window; "
    "caption text, timing, speaker identity and source alignment are not independently verified."
)


class TimedCaptionCue(BaseModel):
    """Private locator DTO: no fabricated cue ending or complete transcript claim."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    start_seconds: Annotated[float, Field(strict=True, ge=0, le=MAX_START_SECONDS)]
    text: Annotated[StrictStr, Field(min_length=1, max_length=MAX_TEXT_CHARACTERS)]


def _key(settings: dict[str, str]) -> str | None:
    if settings.get("SEARCH_FALLBACK_PROVIDER", "").strip().lower() == "serpapi":
        key = settings.get("SEARCH_FALLBACK_API_KEY", "").strip()
        if key:
            return key
    if settings.get("SEARCH_PROVIDER", "").strip().lower() == "serpapi":
        return settings.get("SEARCH_PROVIDER_API_KEY", "").strip() or None
    return None


def _parse_cues(raw: Any) -> list[TimedCaptionCue]:
    if not isinstance(raw, dict) or raw.get("error"):
        raise ValueError("Caption provider failed.")
    rows = raw.get("transcript")
    if not isinstance(rows, list) or not 0 < len(rows) <= MAX_CUES:
        raise ValueError("Caption cues are missing or exceed the limit.")
    result: list[TimedCaptionCue] = []
    characters = 0
    previous_start = -1.0
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("Invalid caption cue.")
        start = row.get("start_ms")
        text = row.get("snippet")
        if (isinstance(start, bool) or not isinstance(start, (int, float))
                or not 0 <= start <= MAX_START_SECONDS * 1_000 or not math.isfinite(start)
                or not isinstance(text, str)):
            raise ValueError("Invalid caption cue data.")
        wording = html.unescape(text)
        characters += len(wording)
        if not wording.strip() or characters > MAX_TEXT_CHARACTERS:
            raise ValueError("Caption text is empty or exceeds the limit.")
        seconds = start / 1_000.0
        if seconds < previous_start:
            raise ValueError("Caption start markers are not ordered.")
        # Preserve actual provider words. HTML-like text stays literal, and
        # absent end_ms is never inferred from the next start_ms.
        result.append(TimedCaptionCue(start_seconds=seconds, text=wording))
        previous_start = seconds
    return result


def fetch_youtube_transcript(url: str) -> list[TimedCaptionCue] | None:
    """Fetch one bounded caption locator response using an explicit SerpAPI key.

    With no configured SerpAPI provider/key pair, return None without HTTP. Any
    configured provider failure is safe and explicit; callers may disclose it
    and continue with independent native video observation.
    """
    try:
        key = _key(_environment())
    except StorageError:
        raise QuantroError(ErrorCode.SEARCH_FAILED, Stage.SEARCH_CANDIDATES,
                           "YouTube caption location could not be configured.", retryable=True) from None
    if key is None:
        return None
    canonical = canonical_youtube_url(url)
    video_id = parse_qs(urlsplit(canonical).query)["v"][0]
    try:
        started = time.monotonic()
        with (
            httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False,
                         trust_env=False, verify=ssl.create_default_context()) as client,
            client.stream("GET", ENDPOINT, params={
                "engine": "youtube_video_transcript", "v": video_id,
                "language_code": "en", "api_key": key,
            }) as response,
        ):
            response.raise_for_status()
            if "json" not in response.headers.get("content-type", "").lower():
                raise ValueError("Invalid caption response type.")
            declared = response.headers.get("content-length")
            if declared is not None and int(declared) > MAX_RESPONSE_BYTES:
                raise ValueError("Caption response exceeds the byte limit.")
            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=16_384):
                if (time.monotonic() - started > TIMEOUT_SECONDS
                        or len(body) + len(chunk) > MAX_RESPONSE_BYTES):
                    raise ValueError("Caption response exceeded its limit.")
                body.extend(chunk)
        return _parse_cues(json.loads(body))
    except (httpx.HTTPError, ValueError, TypeError, OSError):
        # HTTP diagnostics include credential-bearing provider URLs. Never
        # expose, log or chain the original exception.
        raise QuantroError(ErrorCode.SEARCH_FAILED, Stage.SEARCH_CANDIDATES,
                           "YouTube caption location was unavailable; no caption timing was verified.",
                           retryable=True) from None
