"""Offline integration checks for the supplied Groq resolver and existing API contract."""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.routes.common import get_tiger_store
from backend.services.resolution import analysis, run
from backend.services.shared.errors import ErrorCode, QuantroError, Stage
from backend.services.shared.types import (
    Author,
    Comparison,
    Engagement,
    MediaAsset,
    MediaFeatures,
    Post,
    Retrieval,
)
from backend.services.storage.tiger import TigerStore

CLIP = "https://www.youtube.com/watch?v=3XPuGqqWLtk"
SOURCE = "https://www.youtube.com/watch?v=e50Tt9qJRQk"
WORDS = "Nobody is going to give you anything that you have not earned through your own hard work."
SUMMARY = "Takeaways: The speaker urges graduates to earn their success.\n\nWhat this means: The surrounding speech emphasizes responsibility.\n\nReliability: This is a transcript comparison, not a fact check."


def media(name, url, candidate_id=None):
    post = Post(
        post_id="post_" + name,
        candidate_id=candidate_id,
        post_url=url,
        platform="youtube",
        title="Commencement address",
        caption=None,
        author=Author(),
        engagement=Engagement(),
        media=MediaAsset(
            media_id=name,
            local_path="",
            mime_type="video/mp4",
            playback_url=url,
            duration_seconds=300.0 if candidate_id else 12.0,
        ),
        retrieval=Retrieval(retrieved_at="2026-10-10T20:00:00Z", provider="captions"),
    )
    times = (
        [(0.0, 12.0, WORDS)]
        if candidate_id is None
        else [
            (
                10.0,
                20.0,
                "The graduates now face the challenge of building their own future.",
            ),
            (100.0, 112.0, WORDS),
            (120.0, 135.0, "We must also make sure that others have a fair chance."),
        ]
    )
    features = MediaFeatures(
        media_id=name,
        duration_seconds=post.media.duration_seconds,
        transcript={
            "status": "complete",
            "processed_ranges": [
                {"start_seconds": 0.0, "end_seconds": post.media.duration_seconds}
            ],
            "sentences": [
                {
                    "sentence_id": f"{name}_{i}",
                    "start_seconds": a,
                    "end_seconds": b,
                    "text": text,
                    "speaker_id": None,
                }
                for i, (a, b, text) in enumerate(times)
            ],
        },
    )
    return post, features


def comparison():
    return Comparison.model_validate(
        {
            "context_assessment": "no_material_difference_found",
            "caption_assessment": "not_provided",
            "assessment_confidence": {
                "level": "moderate",
                "method": "groq_transcript_context",
                "basis": ["Cited speech agrees"],
                "limitations": [],
            },
            "summary": SUMMARY,
            "findings": [
                {
                    "finding_id": "f1",
                    "type": "context",
                    "explanation": "The address emphasizes responsibility.",
                    "evidence": [
                        {
                            "media_id": "source",
                            "sentence_ids": ["source_1"],
                            "start_seconds": 100.0,
                            "end_seconds": 112.0,
                            "quote": WORDS,
                        }
                    ],
                }
            ],
            "limitations": [],
        }
    )


@pytest.fixture
def store(tmp_path):
    value = TigerStore(f"sqlite:///{tmp_path / 'resolver.db'}")
    value.ensure_schema()
    yield value
    value.close()


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDER", "youtube")

    async def resolve(url, **kwargs):
        return (
            media("source", SOURCE, kwargs["candidate_id"])
            if kwargs.get("candidate_id")
            else media("clip", CLIP)
        )

    monkeypatch.setattr(run, "resolve_media", AsyncMock(side_effect=resolve))
    discovery = {
        "status": "success",
        "candidates": [
            {
                "video_id": "e50Tt9qJRQk",
                "video_url": SOURCE,
                "title": "Full commencement address",
                "duration": 300.0,
                "match_score": 96.3,
                "transcript_result": {},
            }
        ],
    }
    monkeypatch.setattr(run, "find_source_video", AsyncMock(return_value=discovery))
    monkeypatch.setattr(run, "compare_clip", lambda *args: comparison())
    monkeypatch.setattr(run, "interpret_clip", lambda *args: SUMMARY)
    monkeypatch.setenv("REPORT_BASE_URL", "https://quantro-ten.vercel.app")
    return discovery


def test_matched_roundtrip_and_idempotency(store, wired):
    first = run.run_analysis("job_new", CLIP, store=store)
    second = run.run_analysis("job_new", CLIP, store=store)
    assert first == second
    assert first.outcome.value == "matched"
    assert first.report_url.startswith("https://quantro-ten.vercel.app/reports/")
    report = store.get_report(first.report_id)
    assert report["source"]["post"]["post_url"] == SOURCE
    assert report["alignment"]["matches"][0]["source_start_seconds"] == 100.0
    assert report["summary"] == SUMMARY
    assert len(store.get_job_reports("job_new")) == 1
    assert run.find_source_video.await_count == 1
    assert not any(
        marker in json.dumps(report)
        for marker in ("local_path", "/tmp/", "api_key", "file://")
    )


def test_all_matching_nodes_receive_previous_outputs(store, wired, monkeypatch):
    matched = wired["candidates"][0]
    matched["transcript_result"] = {"transcript": WORDS, "segments": []}
    seen = []
    original = run.resolve_media.side_effect

    async def resolve(url, **kwargs):
        seen.append((url, kwargs))
        return await original(url, **kwargs)

    run.resolve_media.side_effect = resolve
    comparison_calls = []
    monkeypatch.setattr(
        run, "compare_clip", lambda *args: comparison_calls.append(args) or comparison()
    )
    result = run.run_analysis("job_connected", CLIP, store=store)
    run.find_source_video.assert_awaited_once_with(
        WORDS, title="Commencement address", exclude_ids=["3XPuGqqWLtk"]
    )
    assert seen[1][0] == SOURCE
    assert seen[1][1]["transcript_result"] is matched["transcript_result"]
    assert seen[1][1]["metadata"] is matched
    assert comparison_calls[0][2].windows[0].sentences
    report = store.get_report(result.report_id)
    assert report["comparison"]["summary"] == SUMMARY
    assert report["context"]["windows"][0]["sentences"]
    assert store.get_job("job_connected")["status"] == "completed"


@pytest.mark.parametrize(
    "reason", ["auth_required", "private", "unavailable", "geo_restricted"]
)
def test_exhausted_retrieval_fallbacks_fail_without_legacy_matcher(
    store, wired, reason
):
    failure = QuantroError(ErrorCode.MEDIA_UNAVAILABLE, Stage.RETRIEVE_POST)
    failure.retrieval_reason = reason
    run.resolve_media.side_effect = failure
    with pytest.raises(QuantroError) as caught:
        run.run_analysis("job_no_access", CLIP, store=store)
    assert caught.value.code == ErrorCode.MEDIA_UNAVAILABLE
    assert store.get_job("job_no_access")["status"] == "failed"
    assert store.get_job_reports("job_no_access") == []
    run.find_source_video.assert_not_awaited()


def test_no_match_keeps_reading_and_leads(store, wired):
    wired["candidates"] = []
    wired["leads"] = [{"video_url": SOURCE, "title": "Possible recording"}]
    result = run.run_analysis("job_unmatched", CLIP, store=store)
    report = store.get_report(result.report_id)
    assert result.outcome.value == "no_verified_match"
    assert (
        report["source"]
        is report["alignment"]
        is report["context"]
        is report["comparison"]
        is None
    )
    assert report["submitted"]["transcript"]["sentences"]
    assert "What this means:" in report["summary"]
    assert any(
        item == f"Unverified source candidate: {SOURCE}"
        for item in report["limitations"]
    )


def test_failed_search_is_error_not_no_match(store, wired):
    run.find_source_video.side_effect = run.CandidateSearchError(
        "search_failed", "Search unavailable"
    )
    with pytest.raises(QuantroError) as failure:
        run.run_analysis("job_search_failed", CLIP, store=store)
    assert failure.value.code.value == "SEARCH_FAILED"
    assert store.get_job("job_search_failed")["status"] == "failed"
    assert store.get_job_reports("job_search_failed") == []


def test_failed_first_candidate_does_not_hide_second(store, wired):
    bad = dict(
        wired["candidates"][0], video_url="https://www.youtube.com/watch?v=abcdefghijk"
    )
    wired["candidates"].insert(0, bad)
    original = run.resolve_media.side_effect

    async def resolve(url, **kwargs):
        if url == bad["video_url"]:
            raise RuntimeError("unavailable")
        return await original(url, **kwargs)

    run.resolve_media.side_effect = resolve
    result = run.run_analysis("job_skip", CLIP, store=store)
    assert result.outcome.value == "matched"
    assert any("candidate 1" in warning for warning in result.warnings)


def test_delivery_failure_preserves_public_report(store, wired, monkeypatch):
    monkeypatch.setattr(
        run,
        "send_summary",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("send failed")),
    )
    result = run.run_analysis("job_send", CLIP, "chat1", store=store)
    assert result.delivery_status == "failed"
    app.dependency_overrides[get_tiger_store] = lambda: store
    try:
        response = TestClient(app).get(f"/reports/{result.report_id}")
        assert response.status_code == 200
        assert response.json()["schema_version"] == "2.1"
    finally:
        app.dependency_overrides.clear()


def test_active_job_and_changed_url_are_not_overwritten(store, wired):
    store.try_create_job("job_busy", CLIP)
    with pytest.raises(QuantroError) as error:
        run.run_analysis("job_busy", CLIP, store=store)
    assert error.value.http_status == 409
    assert store.get_job("job_busy")["status"] == "running"
    with pytest.raises(QuantroError):
        run.run_analysis("job_busy", SOURCE, store=store)
    assert store.get_job("job_busy")["url"] == CLIP


@pytest.mark.parametrize(
    "url",
    [
        "not-a-url",
        "file:///etc/passwd",
        "http://127.0.0.1/clip.mp4",
        "https://example.com/",
    ],
)
def test_invalid_input_never_writes_job(store, url):
    with pytest.raises(QuantroError):
        run.run_analysis("job_bad", url, store=store)
    assert store.get_job("job_bad") is None


def test_near_duplicate_short_cannot_be_context_source(store, wired, monkeypatch):
    original = run.resolve_media.side_effect

    async def resolve(url, **kwargs):
        post, features = await original(url, **kwargs)
        if kwargs.get("candidate_id"):
            features.duration_seconds = 23.0
        return post, features

    run.resolve_media.side_effect = resolve
    assert (
        run.run_analysis("job_duplicate", CLIP, store=store).outcome.value
        == "no_verified_match"
    )


def test_comparison_citations_and_timestamps_are_checked(monkeypatch):
    post, clip = media("clip", CLIP)
    _, source = media("source", SOURCE, "candidate_1")
    context = run.build_context(run.build_alignment(clip, source, 96.3), source)
    raw = comparison().model_dump(mode="json")
    raw["findings"][0]["evidence"][0].update(start_seconds=0.0, end_seconds=1.0)
    monkeypatch.setattr(analysis, "_json_response", lambda _: raw)
    checked = analysis.compare_clip(post, clip, context)
    assert checked.findings[0].evidence[0].start_seconds == 100.0
    raw["findings"][0]["evidence"][0]["quote"] = "Invented words"
    with pytest.raises(QuantroError):
        analysis.compare_clip(post, clip, context)


def test_unknown_followers_remain_null(store, wired):
    result = run.run_analysis("job_null", CLIP, store=store)
    author = store.get_report(result.report_id)["submitted"]["post"]["author"]
    assert (
        author["followers_count"]
        is author["following_count"]
        is author["posts_count"]
        is None
    )


def test_comparison_rejects_reordered_quote(monkeypatch):
    post, clip = media("clip", CLIP)
    _, source = media("source", SOURCE, "candidate_1")
    context = run.build_context(run.build_alignment(clip, source, 96.3), source)
    raw = comparison().model_dump(mode="json")
    raw["findings"][0]["evidence"][0].update(
        sentence_ids=["source_2", "source_1"],
        quote=source.transcript.sentences[2].text + " " + WORDS,
    )
    monkeypatch.setattr(analysis, "_json_response", lambda _: raw)
    with pytest.raises(QuantroError):
        analysis.compare_clip(post, clip, context)


def test_incomplete_context_cannot_claim_no_difference(monkeypatch):
    post, clip = media("clip", CLIP)
    _, source = media("source", SOURCE, "candidate_1")
    context = run.build_context(run.build_alignment(clip, source, 96.3), source)
    context.windows[0].coverage = "partial"
    monkeypatch.setattr(
        analysis, "_json_response", lambda _: comparison().model_dump(mode="json")
    )
    result = analysis.compare_clip(post, clip, context)
    assert result.context_assessment.value == "uncertain"
    assert result.assessment_confidence.level.value == "low"
    assert "Incomplete coverage" in result.summary


def test_invalid_citation_gets_one_repair_without_relaxing_checks(monkeypatch):
    post, clip = media("clip", CLIP)
    _, source = media("source", SOURCE, "candidate_1")
    context = run.build_context(run.build_alignment(clip, source, 96.3), source)
    bad = comparison().model_dump(mode="json")
    bad["findings"][0]["evidence"][0]["quote"] = "Invented speech"
    responses = iter([bad, comparison().model_dump(mode="json")])
    calls = []

    def respond(prompt):
        calls.append(prompt)
        return next(responses)

    monkeypatch.setattr(analysis, "_json_response", respond)
    result = analysis.compare_clip(post, clip, context)
    assert result.findings[0].evidence[0].quote == WORDS
    assert len(calls) == 2 and "preceding attempt failed validation" in calls[1]
