"""Bounded hosted captions for the original transcript and fuzzy matching flow."""

from __future__ import annotations

import html
import json
import math
import re
import ssl
import time
from typing import Any

import httpx

from ..shared.config import environment

ENDPOINT = "https://serpapi.com/search.json"
TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CUES = 10_000
MAX_TEXT_CHARACTERS = 240_000
MAX_SECONDS = 86_400.0
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")


class CaptionProviderError(Exception):
    """A fixed message; provider URLs and credentials never cross this boundary."""


def _key(settings: dict[str, str]) -> str | None:
    for provider, credential in (
        ("SEARCH_FALLBACK_PROVIDER", "SEARCH_FALLBACK_API_KEY"),
        ("SEARCH_PROVIDER", "SEARCH_PROVIDER_API_KEY"),
    ):
        if settings.get(provider, "").strip().lower() == "serpapi" and (
            key := settings.get(credential, "").strip()
        ):
            return key
    return None


def _seconds(value: Any, *, milliseconds: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("Invalid caption timing.")
    limit = MAX_SECONDS * 1_000 if milliseconds else MAX_SECONDS
    if not 0 <= value <= limit or not math.isfinite(value):
        raise ValueError("Invalid caption timing.")
    return float(value / 1_000 if milliseconds else value)


def _parse_transcript(
    raw: Any, *, known_duration: float | None = None
) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("error"):
        raise ValueError("Caption provider failed.")
    rows = raw.get("transcript")
    if not isinstance(rows, list) or not 0 < len(rows) <= MAX_CUES:
        raise ValueError("Caption cues are missing or exceed the limit.")
    duration = _seconds(known_duration) if known_duration is not None else None
    if duration == 0:
        duration = None
    cues: list[dict[str, Any]] = []
    characters = 0
    previous = -1.0
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("snippet"), str):
            raise TypeError("Invalid caption cue.")
        start = _seconds(row.get("start_ms"), milliseconds=True)
        text = re.sub(r"\s+", " ", html.unescape(row["snippet"])).strip()
        characters += len(text)
        if not text or characters > MAX_TEXT_CHARACTERS or start < previous:
            raise ValueError("Caption wording or ordering is invalid.")
        end = row.get("end_ms")
        if end is not None:
            end = _seconds(end, milliseconds=True)
            if end <= start:
                raise ValueError("Invalid caption endpoint.")
        cues.append({"text": text, "start": start, "end": end})
        previous = start

    duration_hint_rejected = duration is not None and any(
        cue["start"] >= duration or (cue["end"] is not None and cue["end"] > duration)
        for cue in cues
    )
    if duration_hint_rejected:
        duration = None

    next_starts: list[float | None] = [None] * len(cues)
    following: float | None = None
    for index in range(len(cues) - 2, -1, -1):
        if cues[index + 1]["start"] > cues[index]["start"]:
            following = cues[index + 1]["start"]
        next_starts[index] = following

    segments: list[dict[str, Any]] = []
    windows = False
    untimed = False
    for index, cue in enumerate(cues):
        end = cue["end"]
        if end is None:
            end = next_starts[index] if next_starts[index] is not None else duration
            windows = windows or end is not None
        if end is None:
            untimed = True
            continue
        segments.append({"text": cue["text"], "start": cue["start"], "end": end})

    warnings = [
        "Hosted caption text and timestamps are provider data, not independently verified audio or recording duration.",
        "Caption coverage is partial; full recording coverage has not been independently confirmed.",
    ]
    if duration_hint_rejected:
        warnings.append(
            "Recording duration metadata contradicts caption timing and was discarded; actual duration remains unknown."
        )
    if windows:
        warnings.append(
            "Estimated caption cue windows run from a provider start marker to the next strictly later marker, or a known recording end; these are not measured word or speech endpoints."
        )
    if untimed:
        warnings.append(
            "Final caption text was retained for transcript matching, but its timed interval was omitted because its endpoint and recording duration are unknown."
        )
    return {
        "title": "",
        "transcript": " ".join(cue["text"] for cue in cues),
        "segments": segments,
        "duration": duration,
        "duration_known": duration is not None,
        "duration_hint_rejected": duration_hint_rejected,
        "source": "serpapi_captions",
        "coverage": "partial",
        "timing_basis": "caption_cue_windows"
        if windows
        else "provider_caption_intervals",
        "warnings": warnings,
    }


def fetch_caption_transcript(
    video_id: str, *, known_duration: float | None = None
) -> dict[str, Any] | None:
    """Use an explicitly configured SerpAPI pair after native captions fail."""
    if not isinstance(video_id, str) or _VIDEO_ID.fullmatch(video_id) is None:
        raise ValueError("Invalid YouTube video identifier.")
    try:
        key = _key(environment())
    except (OSError, ImportError):
        raise CaptionProviderError("Hosted captions could not be configured.") from None
    if key is None:
        return None
    try:
        started = time.monotonic()
        with (
            httpx.Client(
                timeout=TIMEOUT_SECONDS,
                follow_redirects=False,
                trust_env=False,
                verify=ssl.create_default_context(),
            ) as client,
            client.stream(
                "GET",
                ENDPOINT,
                params={
                    "engine": "youtube_video_transcript",
                    "v": video_id,
                    "language_code": "en",
                    "api_key": key,
                },
            ) as response,
        ):
            response.raise_for_status()
            if "json" not in response.headers.get("content-type", "").lower():
                raise ValueError("Invalid caption response type.")
            declared = response.headers.get("content-length")
            if declared is not None and int(declared) > MAX_RESPONSE_BYTES:
                raise ValueError("Caption response exceeds the byte limit.")
            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=16_384):
                if (
                    time.monotonic() - started > TIMEOUT_SECONDS
                    or len(body) + len(chunk) > MAX_RESPONSE_BYTES
                ):
                    raise ValueError("Caption response exceeded its limit.")
                body.extend(chunk)
        return _parse_transcript(json.loads(body), known_duration=known_duration)
    except (httpx.HTTPError, ValueError, TypeError, OSError):
        raise CaptionProviderError("Hosted captions were unavailable.") from None
