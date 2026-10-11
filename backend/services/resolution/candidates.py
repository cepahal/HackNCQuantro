"""Find long-form YouTube source leads with Exa and observed speech.

The text score is a retrieval heuristic, not a probability of authenticity.
All usable candidates retain their provider transcripts and real timings so
the caller can verify a passage before treating a source lead as a match.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import ssl
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, NotRequired, TypedDict
from urllib.parse import urlsplit

import httpx
from rapidfuzz import fuzz

from ..shared.post_identity import post_identity
from .diagnostics import record
from .download import _guarded_call, fetch_media_info
from .exa_search import search_source_links
from .transcribe import (
    _fetch_youtube_captions_sync,
    extract_youtube_id,
    transcribe_youtube_audio,
)

logger = logging.getLogger(__name__)

MATCH_THRESHOLD = 75.0
MIN_DURATION_S = 30.0
MAX_CANDIDATES = 12
MAX_WHISPER_CANDIDATES = 6
MAX_SEARCH_QUERIES = 6
WHISPER_FALLBACK_MAX_S = 90 * 60
_SEARCH_RESULTS = 12
_LLM_TRANSCRIPT_CHARS = 6000
_PUNCT = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")
_TITLE_NOISE = re.compile(r"[#@]\w+|\bshorts?\b", re.IGNORECASE)
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_ISO_DURATION = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
_SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
_SERPAPI_TIMEOUT_S = 12.0
_SERPAPI_MAX_BYTES = 2 * 1024 * 1024
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}


class CandidateSearchError(Exception):
    """Publicly safe failure for an empty transcript or unavailable search."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"error": True, "code": self.code, "message": self.message}


class SearchMetadata(TypedDict):
    speaker_and_event: str
    unique_phrase: str
    query: str
    title_query: str
    visual_queries: NotRequired[list[str]]
    secondary_phrase: NotRequired[str]


class ScoredCandidate(TypedDict):
    video_id: str
    video_url: str
    title: str
    duration: float | None
    match_score: float
    transcript_result: dict[str, Any]


class MatchResult(TypedDict):
    status: Literal["success", "low_confidence", "not_found"]
    match_score: float
    video_id: str | None
    video_url: str | None
    title: str | None
    candidates: list[ScoredCandidate]
    leads: list[dict[str, Any]]
    warnings: list[str]
    search_metadata: NotRequired[SearchMetadata]


@dataclass(frozen=True)
class Candidate:
    video_id: str
    title: str
    duration: float | None

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


def normalize_text(text: str) -> str:
    """Match the prototype: lowercase, remove punctuation, collapse spaces."""
    return _SPACES.sub(" ", _PUNCT.sub("", text.lower())).strip()


def _fallback_phrase(normalized: str, n_words: int = 8) -> str:
    words = normalized.split()
    if len(words) <= n_words:
        return " ".join(words)
    start = max(
        range(len(words) - n_words + 1),
        key=lambda index: sum(len(word) for word in words[index : index + n_words]),
    )
    return " ".join(words[start : start + n_words])


def clean_title(title: str | None) -> str:
    cleaned = normalize_text(_TITLE_NOISE.sub(" ", title or ""))
    return cleaned if len(cleaned.split()) >= 2 else ""


def _secondary_phrase(transcript: str, phrase: str) -> str:
    """Select one substantive observed passage disjoint from the primary anchor."""
    words = normalize_text(transcript[:_LLM_TRANSCRIPT_CHARS]).split()
    anchor = normalize_text(phrase).split()
    positions = [index for index in range(len(words) - len(anchor) + 1)
                 if words[index:index + len(anchor)] == anchor] if anchor else []
    if len(anchor) < 6 or not positions:
        return ""
    windows = [words[index:index + 8] for index in range(len(words) - 7)
               if all(index + 8 <= position or index >= position + len(anchor)
                      for position in positions)]
    if not windows:
        return ""
    secondary = max(windows, key=lambda window: sum(map(len, window)))
    return " ".join(secondary) if len(set(secondary)) >= 6 and sum(map(len, secondary)) >= 40 else ""


def build_query(speaker_and_event: str, unique_phrase: str) -> str:
    return " ".join(f'"{part}"' for part in (speaker_and_event, unique_phrase) if part)


def _visual_queries(hints: Iterable[str] | None, phrase: str) -> list[str]:
    """Bounded scene/OCR search clues, never speaker or alignment evidence."""
    if not isinstance(hints, (list, tuple)):
        return []
    queries: list[str] = []
    for hint in hints[:4]:
        if not isinstance(hint, str) or len(hint) > 160 or re.search(r"https?://|www\.", hint, re.IGNORECASE):
            continue
        clue = normalize_text(re.sub(r"^(?:OCR|Scene):\s*", "", hint))
        if not 2 <= len(clue.split()) <= 24:
            continue
        query = " ".join(part for part in (clue, build_query("", phrase)) if part)
        if query not in queries:
            queries.append(query)
        if len(queries) == 2:
            break
    return queries


def _parse_iso_duration(value: str) -> float | None:
    match = _ISO_DURATION.fullmatch(value or "")
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, seconds = (int(group or 0) for group in match.groups())
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def _duration(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


async def extract_search_metadata(
    transcript: str, title: str | None = ""
) -> SearchMetadata:
    """Use observed speech and the supplied title without an LLM search request."""
    normalized = normalize_text(transcript)
    title_query = clean_title(title)
    phrase = _fallback_phrase(normalized)
    return {
        "speaker_and_event": title_query,
        "unique_phrase": phrase,
        "query": build_query(title_query, phrase),
        "title_query": title_query,
    }



def _search_data_api_sync(query: str, api_key: str, limit: int) -> list[Candidate]:
    from googleapiclient.discovery import build

    with build("youtube", "v3", developerKey=api_key, cache_discovery=False) as youtube:
        found = (
            youtube.search()
            .list(q=query, part="id", type="video", maxResults=min(limit, 50))
            .execute()
        )
        ids = [item.get("id", {}).get("videoId") for item in found.get("items", [])]
        ids = [
            video_id
            for video_id in ids
            if isinstance(video_id, str) and _VIDEO_ID.fullmatch(video_id)
        ]
        if not ids:
            return []
        details = (
            youtube.videos()
            .list(id=",".join(ids), part="snippet,contentDetails")
            .execute()
        )
    by_id = {item["id"]: item for item in details.get("items", []) if item.get("id")}
    return [
        Candidate(
            video_id=video_id,
            title=by_id[video_id].get("snippet", {}).get("title") or "",
            duration=_parse_iso_duration(
                by_id[video_id].get("contentDetails", {}).get("duration", "")
            ),
        )
        for video_id in ids
        if video_id in by_id
    ]


def _search_ytdlp_sync(query: str, limit: int) -> list[Candidate]:
    info = _guarded_call("search", {"query": query, "limit": limit}) or {}
    candidates = []
    for entry in info.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        video_id = entry.get("id")
        if not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id):
            continue
        candidates.append(
            Candidate(
                video_id, entry.get("title") or "", _duration(entry.get("duration"))
            )
        )
    return candidates


def _serpapi_key() -> str | None:
    if os.getenv("SEARCH_FALLBACK_PROVIDER", "").strip().lower() == "serpapi":
        key = os.getenv("SEARCH_FALLBACK_API_KEY", "").strip()
        if key:
            return key
    if os.getenv("SEARCH_PROVIDER", "").strip().lower() == "serpapi":
        return os.getenv("SEARCH_PROVIDER_API_KEY", "").strip() or None
    return None


def _serpapi_video_id(row: dict[str, Any]) -> str | None:
    declared = row.get("video_id")
    if declared is not None and (
        not isinstance(declared, str) or not _VIDEO_ID.fullmatch(declared)
    ):
        return None
    link = row.get("link")
    if link is None:
        return declared
    if not isinstance(link, str) or len(link) > 2048:
        return None
    try:
        parsed = urlsplit(link)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in _YOUTUBE_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443 if parsed.scheme == "https" else 80}
            or any(character.isspace() for character in link)
        ):
            return None
        video_id = extract_youtube_id(link)
        return video_id if declared in {None, video_id} else None
    except ValueError:
        return None


def _serpapi_duration(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) not in {2, 3} or not all(
        part.isascii() and part.isdigit() for part in parts
    ):
        return None
    numbers = [int(part) for part in parts]
    if any(number >= 60 for number in numbers[1:]):
        return None
    seconds = sum(number * 60**index for index, number in enumerate(reversed(numbers)))
    return _duration(seconds)


def _search_serpapi_sync(query: str, api_key: str, limit: int) -> list[Candidate]:
    """One bounded YouTube-engine request; never forward provider result URLs."""
    if limit <= 0:
        return []
    started = time.monotonic()
    with (
        httpx.Client(
            timeout=_SERPAPI_TIMEOUT_S,
            follow_redirects=False,
            trust_env=False,
            verify=ssl.create_default_context(),
        ) as client,
        client.stream(
            "GET",
            _SERPAPI_ENDPOINT,
            params={"engine": "youtube", "search_query": query, "api_key": api_key},
        ) as response,
    ):
        response.raise_for_status()
        if "json" not in response.headers.get("content-type", "").lower():
            raise ValueError("Invalid search response type.")
        declared_size = response.headers.get("content-length")
        if declared_size is not None and int(declared_size) > _SERPAPI_MAX_BYTES:
            raise ValueError("Search response exceeds the byte limit.")
        body = bytearray()
        for chunk in response.iter_bytes(chunk_size=16_384):
            if (
                time.monotonic() - started > _SERPAPI_TIMEOUT_S
                or len(body) + len(chunk) > _SERPAPI_MAX_BYTES
            ):
                raise ValueError("Search response exceeded its limit.")
            body.extend(chunk)
    data = json.loads(body)
    if not isinstance(data, dict) or data.get("error"):
        raise ValueError("Search provider failed.")
    rows = data.get("video_results", [])
    if not isinstance(rows, list):
        raise TypeError("Invalid search results.")
    candidates = []
    for row in rows[:_SEARCH_RESULTS]:
        if not isinstance(row, dict) or not (video_id := _serpapi_video_id(row)):
            continue
        title = row.get("title")
        candidates.append(
            Candidate(
                video_id,
                title[:500] if isinstance(title, str) else "",
                _serpapi_duration(row.get("length")),
            )
        )
        if len(candidates) >= max(0, min(limit, _SEARCH_RESULTS)):
            break
    return candidates


async def _search(query: str, limit: int) -> list[Candidate]:
    api_key = os.environ.get("YOUTUBE_API_KEY")
    found = None
    try:
        if api_key:
            found = await asyncio.to_thread(
                _search_data_api_sync, query, api_key, limit
            )
        else:
            found = await asyncio.to_thread(_search_ytdlp_sync, query, limit)
    except Exception as exc:  # noqa: BLE001 - An explicitly configured fallback may recover.
        logger.info("Direct YouTube search unavailable (%s).", type(exc).__name__)
    if found:
        return found
    if key := _serpapi_key():
        try:
            return await asyncio.to_thread(_search_serpapi_sync, query, key, limit)
        except Exception as exc:  # noqa: BLE001 - Never expose credential-bearing HTTP errors.
            logger.info(
                "Configured YouTube search fallback unavailable (%s).",
                type(exc).__name__,
            )
    if found is not None:
        return found
    raise CandidateSearchError(
        "search_failed", "YouTube source search is temporarily unavailable."
    ) from None


async def search_candidates(
    meta: SearchMetadata,
    exclude_ids: Iterable[str] | None = None,
    max_candidates: int = MAX_CANDIDATES,
) -> list[Candidate]:
    """Interleave query ranks, deduplicate and exclude short/self recordings."""
    excluded = set(exclude_ids or ())

    async def run(query: str) -> list[Candidate]:
        started = time.monotonic()
        try:
            returned = await _search(query, _SEARCH_RESULTS)
        except Exception:
            record("search_query", "failed", {"query": query, "reason": "search_provider_unavailable"}, started=started)
            raise
        kept = [
            candidate
            for candidate in returned
            if candidate.video_id not in excluded
            and (candidate.duration is None or candidate.duration >= MIN_DURATION_S)
        ]
        record("search_query", "completed", {"query": query, "returned_count": len(returned), "kept_count": len(kept)}, started=started)
        return kept

    # Speaker/event and repost titles are hypotheses. Also search the validated
    # speech anchor independently so a wrong attribution cannot constrain every query.
    phrase = meta["unique_phrase"]
    anchor_query = build_query("", phrase) if 6 <= len(normalize_text(phrase).split()) <= 8 else ""
    secondary = meta.get("secondary_phrase", "")
    secondary_query = build_query("", secondary) if len(normalize_text(secondary).split()) == 8 else ""
    # A nonempty list of irrelevant exact-quote hits must not starve relaxed
    # discovery. Prefer the independent observed secondary anchor over an
    # attributed speaker/title; acceptance still requires timed alignment.
    relaxed_phrase = normalize_text(secondary or phrase)
    relaxed_query = relaxed_phrase if 6 <= len(relaxed_phrase.split()) <= 8 else ""
    queries = list(
        dict.fromkeys(query for query in (meta["query"], meta["title_query"], anchor_query, secondary_query,
                                         relaxed_query, *meta.get("visual_queries", [])[:2]) if query)
    )[:MAX_SEARCH_QUERIES]
    result_lists = await asyncio.gather(
        *(run(query) for query in queries), return_exceptions=True
    )
    successful = [result for result in result_lists if isinstance(result, list)]
    merged: list[Candidate] = []
    seen: set[str] = set()
    for rank in range(max((len(result) for result in successful), default=0)):
        for results in successful:
            if rank < len(results) and results[rank].video_id not in seen:
                seen.add(results[rank].video_id)
                merged.append(results[rank])
    if not merged:
        loose = " ".join(
            part for part in (meta["speaker_and_event"], meta["unique_phrase"]) if part
        )
        if loose and loose not in queries and len(queries) < MAX_SEARCH_QUERIES:
            try:
                for candidate in await run(loose):
                    if candidate.video_id not in seen:
                        seen.add(candidate.video_id)
                        merged.append(candidate)
                successful.append([])
            except CandidateSearchError:
                pass
        if not successful:
            raise CandidateSearchError(
                "search_failed", "YouTube source search is temporarily unavailable."
            )
    return merged[: max(0, min(max_candidates, MAX_CANDIDATES))]


async def _whisper_workaround(candidate: Candidate) -> dict[str, Any] | None:
    duration = candidate.duration
    if duration is None:
        try:
            duration = _duration(
                (await fetch_media_info(candidate.url)).get("duration")
            )
        except Exception as exc:  # noqa: BLE001 - An unavailable lead must not abort other candidates.
            logger.warning(
                "Candidate %s duration unavailable (%s).",
                candidate.video_id,
                type(exc).__name__,
            )
            return None
        # An unknown live/VOD duration cannot bypass the maximum audio budget.
        if duration is None:
            return None
    if duration < MIN_DURATION_S or duration > WHISPER_FALLBACK_MAX_S:
        return None
    logger.info(
        "Captions unavailable for %s; using cached, chunked Whisper fallback.",
        candidate.video_id,
    )
    try:
        return await transcribe_youtube_audio(candidate.video_id)
    except Exception as exc:  # noqa: BLE001 - An unavailable lead must not abort other candidates.
        logger.warning(
            "Candidate %s transcription unavailable (%s).",
            candidate.video_id,
            type(exc).__name__,
        )
        return None


_CAPTIONS_NOT_FETCHED = object()


def _timed_candidate_text(transcript_result: Any) -> str:
    """Score observed timed speech only; text without usable bounds needs fallback."""
    if not isinstance(transcript_result, dict):
        return ""
    segments = transcript_result.get("segments")
    if not isinstance(segments, list):
        return ""
    texts = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        start, end, text = segment.get("start"), segment.get("end"), segment.get("text")
        if (type(start) in (int, float) and type(end) in (int, float)
                and math.isfinite(start) and math.isfinite(end) and 0 <= start < end
                and isinstance(text, str) and text.strip()):
            texts.append(text)
    return normalize_text(" ".join(texts))


async def _candidate_captions(candidate: Candidate) -> dict[str, Any] | None:
    try:
        return await asyncio.to_thread(
            _fetch_youtube_captions_sync,
            candidate.video_id,
            known_duration=candidate.duration,
        )
    except Exception:  # noqa: BLE001 - Caption failures trigger the bounded audio fallback.
        return None


async def _score_candidate(
    candidate: Candidate, short_norm: str, *, caption_result: Any = _CAPTIONS_NOT_FETCHED,
    store: Any = None,
    allow_whisper: bool = True,
) -> ScoredCandidate | None:
    # None is an already-attempted failure, not permission to fetch captions again.
    transcript_result = (await _candidate_captions(candidate)
                         if caption_result is _CAPTIONS_NOT_FETCHED else caption_result)
    long_norm = _timed_candidate_text(transcript_result)
    if not long_norm and callable(getattr(store, "get_cached_source", None)):
        # Reuse only a validated prior snapshot, never imply current URL bytes were read.
        def cached_transcript():
            from ..pipeline.feature_cache import extraction_signature
            from ..pipeline.run_ingest import EXTRACTION_OPTIONS
            from ..shared.types import MediaFeatures, Post

            options = {**EXTRACTION_OPTIONS, "extract_frames": False,
                       "extract_visual_text": False, "max_frames": 0}
            cached = store.get_cached_source(candidate.url, extraction_signature(options))
            if not isinstance(cached, dict):
                return None
            post = Post.model_validate(cached["post_json"])
            features = MediaFeatures.model_validate(cached["features_json"])
            acquired = datetime.fromisoformat(cached["retrieved_at"])
            original = datetime.fromisoformat(post.retrieval.retrieved_at)
            if (post_identity(post.post_url) != post_identity(candidate.url)
                    or post.media.media_id != features.media_id
                    or acquired.tzinfo is None or original.tzinfo is None
                    or acquired != original
                    or not 0 <= (datetime.now(UTC) - acquired).total_seconds() <= 3600
                    or re.fullmatch(r"[a-fA-F0-9]{64}", cached.get("media_sha256", "")) is None):
                return None
            sentences = features.transcript.sentences
            if not sentences or any(not s.text.strip() or s.end_seconds <= s.start_seconds for s in sentences):
                return None
            return {"transcript": " ".join(s.text for s in sentences),
                    "segments": [{"text": s.text, "start": s.start_seconds,
                                  "end": s.end_seconds,
                                  "words": [{"word": w.text, "start": w.start_seconds,
                                             "end": w.end_seconds} for w in s.words]}
                                 for s in sentences],
                    "source": "cached_source_snapshot", "coverage": features.transcript.status.value,
                    "retrieved_at": cached["retrieved_at"], "current_url_bytes_verified": False}
        try:
            transcript_result = await asyncio.to_thread(cached_transcript)
        except Exception:  # noqa: BLE001 - Optional storage failure must preserve Whisper fallback.
            transcript_result = None
        long_norm = _timed_candidate_text(transcript_result)
    if not long_norm:
        # A provider returning an empty transcript is also a caption failure.
        if not allow_whisper:
            return None
        transcript_result = await _whisper_workaround(candidate)
        if not isinstance(transcript_result, dict):
            return None
        long_norm = _timed_candidate_text(transcript_result)
        if not long_norm:
            return None
    score = float(await asyncio.to_thread(fuzz.partial_ratio, short_norm, long_norm))
    return {
        "video_id": candidate.video_id,
        "video_url": candidate.url,
        "title": candidate.title,
        "duration": candidate.duration,
        "match_score": score,
        "transcript_result": transcript_result,
    }


async def _archive_candidates(store, phrase: str, excluded: set[str], *, transcript: str = "") -> list[Candidate]:
    """Archive metadata supplies leads only, never recording text or timing."""
    lookup = getattr(store, "find_transcript_posts", None)
    if not callable(lookup) or not phrase.strip():
        return []
    queries = [phrase[:512]]
    # A separate observed passage can survive a single ASR error in the anchor.
    # Never derive archive queries from attributed titles or invented metadata.
    if secondary := _secondary_phrase(transcript, phrase):
        queries.append(secondary)
    try:
        excluded_identities = [post_identity(f"https://www.youtube.com/watch?v={video_id}")
                               for video_id in sorted(excluded) if _VIDEO_ID.fullmatch(video_id)]
        rows = await asyncio.to_thread(lookup, list(dict.fromkeys(queries)), MAX_CANDIDATES, excluded_identities)
    except Exception:  # noqa: BLE001 - Optional archive failure must preserve web discovery.
        return []
    found = []
    seen = set(excluded)
    for row in rows[:MAX_CANDIDATES] if isinstance(rows, list) else []:
        if not isinstance(row, (tuple, list)) or len(row) != 2 or not isinstance(row[0], dict):
            continue
        metadata = row[0]
        url = metadata.get("post_url")
        if not isinstance(url, str):
            continue
        try:
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.hostname not in _YOUTUBE_HOSTS
                    or parsed.username or parsed.password or parsed.port not in (None, 443)):
                continue
        except ValueError:
            continue
        video_id = extract_youtube_id(url)
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        found.append(Candidate(video_id, str(metadata.get("title") or ""), None))
    return found


async def find_source_video(
    transcript: str,
    title: str | None = None,
    exclude_ids: Iterable[str] | None = None,
    *,
    max_candidates: int = MAX_CANDIDATES,
    threshold: float = MATCH_THRESHOLD,
    store: Any = None,
    discovery_hints: Iterable[str] | None = None,
) -> MatchResult:
    """Score every top candidate; keep complete timed transcripts for alignment.

    A success is a promising lexical lead. Only the caller's timed alignment can
    establish a report match; a score alone must never create source timestamps.
    """
    short_norm = normalize_text(transcript or "")
    if not short_norm:
        raise CandidateSearchError("no_transcript", "Transcript is empty.")
    started = time.monotonic()
    meta = await extract_search_metadata(transcript, title)
    if secondary := _secondary_phrase(short_norm, meta.get("unique_phrase", "")):
        meta["secondary_phrase"] = secondary
    if discovery_hints and (visual_queries := _visual_queries(discovery_hints, meta.get("unique_phrase", ""))):
        meta["visual_queries"] = visual_queries
    record("build_search_queries", "completed", dict(meta), started=started)
    started = time.monotonic()
    excluded = set(exclude_ids or ())
    searches = [
        search_candidates(meta, excluded, max_candidates),
        search_source_links(title=title or "", transcript=transcript, limit=12),
    ]
    if store is not None:
        searches.append(_archive_candidates(
            store, meta["unique_phrase"] or _fallback_phrase(short_norm),
            excluded, transcript=short_norm,
        ))
    results = await asyncio.gather(*searches, return_exceptions=True)
    web, exa = results[:2]
    local = results[2] if len(results) > 2 and isinstance(results[2], list) else []
    grounded = []
    grounded_ids = set(excluded)
    for row in exa[:12] if isinstance(exa, list) else []:
        if not isinstance(row, dict):
            continue
        video_id = _serpapi_video_id({"link": row.get("url", row.get("link", ""))})
        if video_id is not None and video_id not in grounded_ids:
            grounded_ids.add(video_id)
            found_title = row.get("title")
            grounded.append(Candidate(
                video_id, found_title[:500] if isinstance(found_title, str) else "", None,
            ))
            if len(grounded) == 4:
                break
    if isinstance(web, Exception) and not local and not grounded:
        raise web
    web = web if isinstance(web, list) else []
    candidates = []
    seen = set(excluded)
    # Reserve independent search positions so twelve lexical hits cannot hide
    # an Exa search lead. All still require timed speech alignment.
    for candidate in [*web[:4], *grounded, *local[:2], *web[4:], *local[2:]]:
        if candidate.video_id not in seen:
            seen.add(candidate.video_id)
            candidates.append(candidate)
    candidates = candidates[:max(0, min(max_candidates, MAX_CANDIDATES))]
    record("search_candidates", "discovered", {"search_metadata": meta, "candidate_count": len(candidates),
           "candidates": [{"video_id": item.video_id, "video_url": item.url, "title": item.title, "duration": item.duration} for item in candidates]}, started=started)
    leads = [
        {
            "video_id": candidate.video_id,
            "video_url": candidate.url,
            "title": candidate.title,
            "duration": candidate.duration,
        }
        for candidate in candidates
    ]
    # Only lightweight caption lookups overlap. Audio/Whisper remains serial below.
    caption_slots = asyncio.Semaphore(2)
    async def prefetch(candidate):
        async with caption_slots:
            started = time.monotonic()
            return await _candidate_captions(candidate), started
    prefetched = await asyncio.gather(*(prefetch(candidate) for candidate in candidates))
    scored: list[ScoredCandidate] = []
    missing: list[tuple[Candidate, float]] = []
    for candidate, (caption_result, started) in zip(candidates, prefetched):
        data = {"video_id": candidate.video_id, "video_url": candidate.url, "title": candidate.title, "duration": candidate.duration}
        try:
            kwargs = {"caption_result": caption_result, "allow_whisper": False}
            if store is not None:
                kwargs["store"] = store
            result = await _score_candidate(candidate, short_norm, **kwargs)
        except Exception as exc:  # noqa: BLE001 - One failed lead must not abort source search.
            logger.warning(
                "Skipping candidate %s (%s).", candidate.video_id, type(exc).__name__
            )
            record("candidate_transcript", "unavailable", {**data, "reason": "candidate_transcript_or_scoring_failed"}, started=started)
            continue
        if result is not None:
            scored.append(result)
            record("candidate_transcript", "scored", dict(result), started=started)
        else:
            missing.append((candidate, started))
    # Expanded caption discovery must not expand paid/audio fallback work.
    audio_attempts = 0
    audio_budget_skipped = 0
    for candidate, started in missing:
        result = None
        eligible = candidate.duration is None or MIN_DURATION_S <= candidate.duration <= WHISPER_FALLBACK_MAX_S
        reason = "audio_duration_outside_budget" if not eligible else "no_usable_candidate_transcript"
        if eligible and audio_attempts < MAX_WHISPER_CANDIDATES:
            # Unknown-duration probes count too; repeated failures cannot expand work.
            audio_attempts += 1
            try:
                result = await _score_candidate(candidate, short_norm, caption_result=None)
            except Exception as exc:  # noqa: BLE001 - One audio failure must not abort other leads.
                logger.warning("Skipping audio candidate %s (%s).", candidate.video_id, type(exc).__name__)
        elif eligible:
            reason = "audio_fallback_budget_exhausted"
            audio_budget_skipped += 1
        data = {"video_id": candidate.video_id, "video_url": candidate.url,
                "title": candidate.title, "duration": candidate.duration}
        if result is not None:
            scored.append(result)
            record("candidate_transcript", "scored", dict(result), started=started)
        else:
            record("candidate_transcript", "unavailable", {**data, "reason": reason}, started=started)
    # Two phases must still resolve equal scores by original discovery rank.
    ranks = {candidate.video_id: index for index, candidate in enumerate(candidates)}
    scored.sort(key=lambda candidate: (-candidate["match_score"], ranks[candidate["video_id"]]))
    scored_ids = {candidate["video_id"] for candidate in scored}
    leads_by_id = {lead["video_id"]: lead for lead in leads}
    leads = [leads_by_id[candidate["video_id"]] for candidate in scored] + [
        lead for lead in leads if lead["video_id"] not in scored_ids
    ]
    if grounded:
        # Presentation only: garbled captions must not promote an unrelated
        # lexical hit above the independent Exa search leads. Matching
        # still uses the unchanged timed-transcript scores and alignment.
        exa_ids = {candidate.video_id for candidate in grounded}
        scores = {candidate["video_id"]: candidate["match_score"] for candidate in scored}
        title_clue = clean_title(title)
        leads.sort(key=lambda lead: (
            scores.get(lead["video_id"], 0) >= threshold,
            lead["video_id"] in exa_ids,
            fuzz.token_set_ratio(title_clue, clean_title(lead["title"])) if title_clue else 0,
        ), reverse=True)
    unavailable_count = len(candidates) - len(scored)
    warnings = (
        [
            f"Transcripts were unavailable for {unavailable_count} source candidate(s); these remain unverified leads."
        ]
        if unavailable_count
        else []
    )
    if audio_budget_skipped:
        warnings.append(f"Audio transcription fallback was limited to {MAX_WHISPER_CANDIDATES} source candidates; additional unavailable transcripts remain unverified leads.")
    if meta.get("visual_queries"):
        warnings.append("Visual search clues are provider estimates; they do not verify speaker identity, the event or a source match.")
    for item in scored:
        snapshot = item.get("transcript_result", {})
        if snapshot.get("source") == "cached_source_snapshot":
            warnings.append(f"Reused a prior recording transcript snapshot retrieved at {snapshot['retrieved_at']}; current URL bytes were not re-downloaded.")
    if not scored:
        empty: MatchResult = {
            "status": "not_found",
            "match_score": 0.0,
            "video_id": None,
            "video_url": None,
            "title": None,
            "candidates": [],
            "leads": leads,
            "warnings": warnings,
        }
        if meta:
            empty["search_metadata"] = meta
        return empty
    best = scored[0]
    return {
        "status": "success" if best["match_score"] >= threshold else "low_confidence",
        "match_score": best["match_score"],
        "video_id": best["video_id"],
        "video_url": best["video_url"],
        "title": best["title"],
        "candidates": scored,
        "leads": leads,
        "warnings": warnings,
        "search_metadata": meta,
    }


async def check_source_video(transcript: str, url: str) -> MatchResult:
    """Recheck one saved discovery lead using the same captions/Whisper scorer."""
    video_id = extract_youtube_id(url)
    if not video_id or not normalize_text(transcript):
        raise CandidateSearchError("invalid_candidate", "Candidate or transcript is unavailable.")
    candidate = Candidate(video_id, "Selected candidate recording", None)
    scored = await _score_candidate(candidate, normalize_text(transcript))
    return {"status": "success" if scored and scored["match_score"] >= MATCH_THRESHOLD else "not_found",
            "match_score": scored["match_score"] if scored else 0.0,
            "video_id": video_id, "video_url": candidate.url, "title": candidate.title,
            "candidates": [scored] if scored else [],
            "leads": [{"video_id": video_id, "video_url": candidate.url, "title": candidate.title, "duration": None}],
            "warnings": [] if scored else ["The selected recording's transcript could not be retrieved."]}
