"""Bounded Exa discovery using the existing guarded web-search adapter."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..external_apis.exa_search import ExaSearch, _publisher_youtube_url
from .diagnostics import record

_MAX_RESULTS = 12


async def search_source_links(
    *, title: str, transcript: str, limit: int = _MAX_RESULTS
) -> list[dict[str, str]]:
    """Return observed YouTube links; transcript alignment still verifies leads."""
    key = os.getenv("EXA_API_KEY", "").strip()
    if not key or type(limit) is not int or limit <= 0:
        return []
    title = title[:500] if isinstance(title, str) else ""
    transcript = transcript[:6000] if isinstance(transcript, str) else ""
    # Import only local text helpers after candidates has initialized its seam.
    from .candidates import (
        _fallback_phrase,
        _secondary_phrase,
        build_query,
        clean_title,
        normalize_text,
    )

    normalized = normalize_text(transcript)
    phrase = _fallback_phrase(normalized)
    secondary = _secondary_phrase(normalized, phrase)
    queries = list(
        dict.fromkeys(
            query
            for query in (
                clean_title(title),
                build_query("", phrase),
                build_query("", secondary),
            )
            if query
        )
    )[:3]
    if not queries:
        return []
    limit = min(limit, _MAX_RESULTS)
    started = time.monotonic()
    record("exa_source_search", "started", {"status": "searching", "queries": queries})

    def search() -> list[dict[str, str]]:
        provider = ExaSearch(key)
        try:
            rows = provider.find_posts(queries, limit)
        finally:
            provider.close()
        found = []
        seen = set()
        for row in rows[:_MAX_RESULTS] if isinstance(rows, list) else []:
            if (
                not isinstance(row, (tuple, list))
                or not row
                or not isinstance(row[0], dict)
            ):
                continue
            metadata: dict[str, Any] = row[0]
            url = metadata.get("post_url")
            if not isinstance(url, str):
                continue
            try:
                if urlsplit(url).path.startswith("/shorts/"):
                    continue
            except ValueError:
                continue
            canonical = _publisher_youtube_url(url)
            if canonical is None or canonical in seen:
                continue
            seen.add(canonical)
            found_title = metadata.get("title")
            found.append(
                {
                    "url": canonical,
                    "title": found_title[:500] if isinstance(found_title, str) else "",
                }
            )
            if len(found) == limit:
                break
        return found

    try:
        async with asyncio.timeout(50):
            links = await asyncio.to_thread(search)
    except Exception as error:  # noqa: BLE001 - Optional discovery preserves other search paths.
        status = (
            error.response.status_code
            if isinstance(error, httpx.HTTPStatusError)
            else None
        )
        code = (
            str(status)
            if type(status) is int and 100 <= status <= 599
            else (
                "timeout"
                if isinstance(error, (TimeoutError, httpx.TimeoutException))
                else "provider_error"
            )
        )
        record(
            "exa_source_search",
            "failed",
            {"status": "unavailable", "code": code},
            started=started,
        )
        return []
    record(
        "exa_source_search",
        "completed",
        {
            "status": "leads_found" if links else "no_grounded_leads",
            "candidate_count": len(links),
            "candidates": [
                {"video_url": row["url"], "title": row["title"]} for row in links
            ],
        },
        started=started,
    )
    return links
