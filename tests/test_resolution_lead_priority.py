"""Unverified lead presentation cannot rewrite transcript scores or matches."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from backend.services.resolution import candidates

WEB = "W0000000000"
BUSH = "B0000000000"
OTHER = "G0000000000"
TITLE = "George W. Bush Fool me once, shame on you"
GARBLED = "as damon simon"


def score(candidate, value):
    return {
        "video_id": candidate.video_id, "video_url": candidate.url,
        "title": candidate.title, "duration": candidate.duration,
        "match_score": value,
        "transcript_result": {
            "transcript": "Independently observed words remain unchanged by lead presentation.",
            "segments": [{"text": "Independently observed words remain unchanged by lead presentation.",
                          "start": 100.0, "end": 110.0}],
            "source": "youtube_captions", "duration": 900.0,
        },
    }


def wire(monkeypatch, *, web_score, extra_browser=False):
    web = candidates.Candidate(WEB, "An unrelated interview with a higher lexical score", 900.0)
    bush = candidates.Candidate(BUSH, "George W. Bush Fool me once gaffe full speech", None)
    other = candidates.Candidate(OTHER, "A cooking demonstration", None)
    monkeypatch.setattr(candidates, "extract_search_metadata", AsyncMock(return_value={
        "speaker_and_event": TITLE, "unique_phrase": GARBLED,
        "query": TITLE, "title_query": TITLE,
    }))
    monkeypatch.setattr(candidates, "search_candidates", AsyncMock(return_value=[web]))
    browser = [other, bush] if extra_browser else [bush]
    monkeypatch.setattr(candidates, "search_source_links", AsyncMock(return_value=[
        {"url": candidate.url, "title": candidate.title} for candidate in browser
    ]))
    monkeypatch.setattr(candidates, "_archive_candidates", AsyncMock(return_value=[]))
    monkeypatch.setattr(candidates, "_candidate_captions", AsyncMock(return_value={}))
    monkeypatch.setattr(candidates, "_whisper_workaround", AsyncMock(
        side_effect=AssertionError("No live audio transcription"),
    ))
    values = {WEB: web_score, BUSH: 23.0, OTHER: 40.0}

    async def scored(candidate, short_norm, **kwargs):
        assert short_norm == GARBLED
        return score(candidate, values[candidate.video_id])

    monkeypatch.setattr(candidates, "_score_candidate", AsyncMock(side_effect=scored))
    return web, bush


@pytest.mark.parametrize("web_score", [68.0, 96.0])
def test_browser_lead_priority_preserves_best_score_and_matching_order(monkeypatch, web_score):
    web, bush = wire(monkeypatch, web_score=web_score)
    result = asyncio.run(candidates.find_source_video(GARBLED, title=TITLE))
    assert result["leads"][0]["video_id"] == (BUSH if web_score < 75 else WEB)
    assert result["video_id"] == WEB
    assert result["match_score"] == web_score
    assert result["status"] == ("low_confidence" if web_score < 75 else "success")
    assert [candidate["video_id"] for candidate in result["candidates"]] == [WEB, BUSH]
    assert result["candidates"] == [score(web, web_score), score(bush, 23.0)]
    assert "alignment" not in result
    assert not any("match_score" in lead or "alignment" in lead for lead in result["leads"])
    candidates._whisper_workaround.assert_not_awaited()


def test_submitted_title_orders_browser_leads_without_changing_score_ranking(monkeypatch):
    wire(monkeypatch, web_score=68.0, extra_browser=True)
    result = asyncio.run(candidates.find_source_video(GARBLED, title=TITLE))
    assert [lead["video_id"] for lead in result["leads"]] == [BUSH, OTHER, WEB]
    assert [candidate["video_id"] for candidate in result["candidates"]] == [WEB, OTHER, BUSH]
    assert [candidate["match_score"] for candidate in result["candidates"]] == [68.0, 40.0, 23.0]
    assert result["status"] == "low_confidence"
    assert result["match_score"] == 68.0
    assert "alignment" not in result
