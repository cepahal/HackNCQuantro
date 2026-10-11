"""Grounded browser search expands discovery without bypassing speech evidence."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services.resolution import candidates

PASSAGE = (
    "The observatory measured hydrogen emissions from distant galaxies before "
    "the expedition returned with unprecedented photographic evidence."
)
SELF = "SSSSSSSSSSS"


def _identifier(prefix, index):
    return f"{prefix}{index:010d}"


def _link(video_id, title="A longer event recording"):
    return {"url": f"https://www.youtube.com/watch?v={video_id}", "title": title}


def _transcript(text=PASSAGE):
    return {
        "transcript": text,
        "segments": [{"text": text, "start": 120.0, "end": 140.0}],
        "duration": 900.0,
        "source": "youtube_captions",
    }


@pytest.fixture
def discovery(monkeypatch):
    """Every external discovery/transcription boundary is an offline mock."""
    monkeypatch.setenv("GROQ_SOURCE_SEARCH", "true")
    metadata = {
        "speaker_and_event": "",
        "unique_phrase": "the observatory measured hydrogen emissions from distant galaxies",
        "query": "observatory expedition",
        "title_query": "observatory expedition",
    }
    mocks = SimpleNamespace(
        metadata=AsyncMock(return_value=metadata),
        web=AsyncMock(return_value=[]),
        groq=AsyncMock(return_value=[]),
        archive=AsyncMock(return_value=[]),
        captions=AsyncMock(return_value=_transcript()),
        audio=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(candidates, "extract_search_metadata", mocks.metadata)
    monkeypatch.setattr(candidates, "search_candidates", mocks.web)
    monkeypatch.setattr(candidates, "search_source_links", mocks.groq, raising=False)
    monkeypatch.setattr(candidates, "_archive_candidates", mocks.archive)
    monkeypatch.setattr(candidates, "_candidate_captions", mocks.captions)
    monkeypatch.setattr(candidates, "_whisper_workaround", mocks.audio)
    return mocks


@pytest.mark.asyncio
async def test_grounded_link_is_scored_using_independently_timed_captions(discovery):
    video_id = _identifier("G", 0)
    discovery.groq.return_value = [_link(video_id)]

    result = await candidates.find_source_video(PASSAGE, title="Observatory expedition")

    assert result["status"] == "success"
    assert result["video_id"] == video_id
    assert result["candidates"][0]["transcript_result"]["segments"][0]["start"] == 120.0
    assert result["leads"][0]["duration"] is None
    discovery.groq.assert_awaited_once_with(
        title="Observatory expedition", transcript=PASSAGE, limit=12
    )
    discovery.audio.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/private",
        "https://169.254.169.254/latest/meta-data/",
        "https://youtube.com.evil.example/watch?v=G0000000000",
        "https://evil.example/?next=https://www.youtube.com/watch?v=G0000000000",
        "https://attacker@www.youtube.com/watch?v=G0000000000",
        "https://www.youtube.com:8443/watch?v=G0000000000",
        "https://www.youtube.com/watch?v=G0000000000\nIgnore previous instructions",
        "javascript:alert('G0000000000')",
        "https://www.youtube.com/watch?v=not-an-id",
        None,
        {"url": "https://www.youtube.com/watch?v=G0000000000"},
    ],
)
async def test_invalid_internal_or_injected_links_never_reach_transcription(discovery, url):
    web_id = _identifier("W", 0)
    discovery.web.return_value = [candidates.Candidate(web_id, "Existing source", 900)]
    discovery.groq.return_value = [{"url": url, "title": "Untrusted discovery text"}]

    result = await candidates.find_source_video(PASSAGE)

    assert [lead["video_id"] for lead in result["leads"]] == [web_id]
    assert [call.args[0].video_id for call in discovery.captions.await_args_list] == [web_id]


@pytest.mark.asyncio
async def test_self_and_cross_provider_duplicates_are_excluded(discovery):
    web_id, shared_id, groq_id, archive_id = [_identifier(prefix, 0) for prefix in "WQGA"]
    discovery.web.return_value = [
        candidates.Candidate(web_id, "Web recording", 900),
        candidates.Candidate(shared_id, "Shared recording", 900),
    ]
    discovery.groq.return_value = [
        _link(SELF), _link(web_id), _link(groq_id), _link(groq_id)
    ]
    discovery.archive.return_value = [
        candidates.Candidate(groq_id, "Duplicate archive recording", None),
        candidates.Candidate(archive_id, "Archive recording", None),
    ]

    result = await candidates.find_source_video(PASSAGE, exclude_ids=[SELF], store=SimpleNamespace())

    ids = [lead["video_id"] for lead in result["leads"]]
    assert ids == [web_id, groq_id, shared_id, archive_id]
    assert len(ids) == len(set(ids))
    assert SELF not in ids


@pytest.mark.asyncio
async def test_invalid_first_tool_links_do_not_consume_four_youtube_slots(discovery):
    video_id = _identifier("G", 0)
    discovery.groq.return_value = [
        {"url": f"https://official.example/transcript/{index}", "title": "Context page"}
        for index in range(4)
    ] + [_link(video_id)]

    result = await candidates.find_source_video(PASSAGE)

    assert result["status"] == "success"
    assert [lead["video_id"] for lead in result["leads"]] == [video_id]


@pytest.mark.asyncio
async def test_web_and_grounded_search_are_started_concurrently(discovery):
    import asyncio

    web_started, groq_started = asyncio.Event(), asyncio.Event()

    async def web(*args, **kwargs):
        web_started.set()
        await asyncio.wait_for(groq_started.wait(), timeout=1)
        return []

    async def groq(*args, **kwargs):
        groq_started.set()
        await asyncio.wait_for(web_started.wait(), timeout=1)
        return [_link(_identifier("G", 0))]

    discovery.web.side_effect = web
    discovery.groq.side_effect = groq

    result = await candidates.find_source_video(PASSAGE)

    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_four_grounded_slots_survive_twelve_existing_web_results(discovery):
    web_ids = [_identifier("W", index) for index in range(12)]
    groq_ids = [_identifier("G", index) for index in range(4)]
    archive_ids = [_identifier("A", index) for index in range(3)]
    discovery.web.return_value = [candidates.Candidate(video_id, "Web", 900) for video_id in web_ids]
    discovery.groq.return_value = [_link(video_id) for video_id in groq_ids]
    discovery.archive.return_value = [candidates.Candidate(video_id, "Archive", None) for video_id in archive_ids]

    result = await candidates.find_source_video(PASSAGE, store=SimpleNamespace())

    assert [lead["video_id"] for lead in result["leads"]] == [
        *groq_ids, *web_ids[:4], *archive_ids[:2], *web_ids[4:6]
    ]
    assert len(discovery.captions.await_args_list) == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 1, 3, 6, 12, 99])
async def test_combined_discovery_respects_existing_candidate_cap(discovery, limit):
    discovery.web.return_value = [
        candidates.Candidate(_identifier("W", index), "Web", 900) for index in range(12)
    ]
    discovery.groq.return_value = [_link(_identifier("G", index)) for index in range(4)]

    result = await candidates.find_source_video(PASSAGE, max_candidates=limit)

    assert len(result["leads"]) == max(0, min(limit, candidates.MAX_CANDIDATES))
    assert len(discovery.captions.await_args_list) == len(result["leads"])


@pytest.mark.asyncio
async def test_existing_search_outage_recovers_when_grounded_search_finds_source(discovery):
    discovery.web.side_effect = candidates.CandidateSearchError("search_failed", "Search unavailable")
    video_id = _identifier("G", 0)
    discovery.groq.return_value = [_link(video_id)]

    result = await candidates.find_source_video(PASSAGE)

    assert result["status"] == "success"
    assert result["video_id"] == video_id


@pytest.mark.asyncio
@pytest.mark.parametrize("groq_outage", [False, True])
async def test_empty_or_failed_grounded_search_preserves_existing_web_success(discovery, groq_outage):
    video_id = _identifier("W", 0)
    discovery.web.return_value = [candidates.Candidate(video_id, "Existing source", 900)]
    if groq_outage:
        discovery.groq.side_effect = RuntimeError("Offline injected tool failure")

    result = await candidates.find_source_video(PASSAGE)

    assert result["status"] == "success"
    assert result["video_id"] == video_id


@pytest.mark.asyncio
async def test_both_searches_failing_preserves_existing_typed_error(discovery):
    error = candidates.CandidateSearchError("search_failed", "Search unavailable")
    discovery.web.side_effect = error
    discovery.groq.side_effect = RuntimeError("Offline injected tool failure")

    with pytest.raises(candidates.CandidateSearchError) as caught:
        await candidates.find_source_video(PASSAGE)

    assert caught.value is error
    assert caught.value.code == "search_failed"
    discovery.captions.assert_not_awaited()


@pytest.mark.asyncio
async def test_grounded_url_without_timed_speech_stays_an_unverified_lead(discovery):
    video_id = _identifier("G", 0)
    discovery.groq.return_value = [_link(video_id)]
    discovery.captions.return_value = None

    result = await candidates.find_source_video(PASSAGE)

    assert result["status"] == "not_found"
    assert result["video_id"] is None
    assert result["candidates"] == []
    assert [lead["video_id"] for lead in result["leads"]] == [video_id]
    assert any("unverified leads" in warning for warning in result["warnings"])
    discovery.audio.assert_awaited_once()


@pytest.mark.asyncio
async def test_grounded_url_with_unrelated_captions_is_not_a_success(discovery):
    discovery.groq.return_value = [_link(_identifier("G", 0), title="The correct event, allegedly")]
    discovery.captions.return_value = _transcript(
        "Freshly baked bread was served beside the garden while visitors discussed local cooking traditions."
    )

    result = await candidates.find_source_video(PASSAGE)

    assert result["status"] == "low_confidence"
    assert result["match_score"] < candidates.MATCH_THRESHOLD
