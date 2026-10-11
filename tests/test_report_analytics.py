"""Analytics stay evidence-bound, cached in Tiger, and separate from source verification."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
import test_resolution_pipeline as fixtures
from fastapi.testclient import TestClient

from backend.main import app
from backend.routes.common import get_tiger_store
from backend.services.reporting import report_interpretation as insights
from backend.services.resolution import candidates, run
from backend.services.shared.types import InterpretationDraft, Outcome, ReportV2
from backend.services.storage.tiger import TigerStore

store = fixtures.store
wired = fixtures.wired


@pytest.fixture
def saved(store, wired):
    result = run.run_analysis("job_analytics", fixtures.CLIP, store=store)
    return result.report_id, ReportV2.model_validate(store.get_report(result.report_id))


@pytest.fixture
def client(store):
    app.dependency_overrides[get_tiger_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


def draft(evidence):
    refs = []
    for item in evidence["sentences"]:
        if item["media_id"] not in {ref["media_id"] for ref in refs}:
            refs.append({"media_id": item["media_id"], "sentence_id": item["sentence_id"]})
    return InterpretationDraft(context_gap="The clip urges graduates to earn their success. The surrounding words also call for fair opportunities.",
                               summary="The speaker discusses responsibility and opportunity.", references=refs)


@pytest.fixture
def generated(monkeypatch):
    generate = Mock(side_effect=lambda evidence: (draft(evidence), "gemini-test"))
    monkeypatch.setattr(insights, "_generate", generate)
    return generate


def test_interpretation_uses_saved_context_caches_in_tiger_and_keeps_report_unchanged(saved, store, generated):
    report_id, report = saved
    before = store.get_report(report_id)
    first = insights.report_interpretation(report_id, report, store)
    second = insights.report_interpretation(report_id, report, store)
    assert first == second and first.status == "ready"
    assert generated.call_count == 1
    assert {ref.media_id for ref in first.references} == {report.submitted.post.media.media_id, report.source.post.media.media_id}
    # Independent store instance simulates a process restart. No fresh provider request.
    reopened = TigerStore(str(store._engine.url))
    try:
        assert insights.report_interpretation(report_id, report, reopened) == first
    finally:
        reopened.close()
    assert generated.call_count == 1
    assert store.get_report(report_id) == before
    assert store.get_diagnostics(report.job_id)[-1]["data"]["interpretation"]["summary"] == first.summary


def test_concurrent_page_requests_make_only_one_provider_call(saved, store, generated):
    report_id, report = saved
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: insights.report_interpretation(report_id, report, store), range(4)))
    assert generated.call_count == 1
    assert all(result == results[0] for result in results)


def test_provider_failure_is_cached_safe_unavailable_not_a_failed_report(saved, store, monkeypatch):
    generate = Mock(side_effect=RuntimeError("api_key=private-provider-error"))
    monkeypatch.setattr(insights, "_generate", generate)
    report_id, report = saved
    first = insights.report_interpretation(report_id, report, store)
    assert first.status == "unavailable" and first.summary is None and first.references == []
    assert insights.report_interpretation(report_id, report, store) == first
    assert generate.call_count == 1
    assert "private-provider-error" not in first.model_dump_json()
    assert store.get_job(report.job_id)["status"] == "completed"
    events = store.get_diagnostics(report.job_id)
    events[-1]["data"]["interpretation"]["generated_at"] = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    store.save_diagnostics(report.job_id, events)
    insights.report_interpretation(report_id, report, store)
    assert generate.call_count == 2


def test_empty_transcript_never_calls_gemini(saved, store, generated):
    report_id, report = saved
    report.submitted.transcript.sentences = []
    report.source = report.context = report.alignment = None
    report.outcome = Outcome.NO_VERIFIED_MATCH
    assert insights.report_interpretation(report_id, report, store).status == "unavailable"
    generated.assert_not_called()


def test_payload_is_bounded_and_stale_source_context_is_excluded(saved):
    _, report = saved
    report.limitations = ["x" * 50000] * 20
    report.submitted.post.caption = "y" * 50000
    evidence = insights._evidence(report)
    assert len(str(evidence)) < 22000
    report.alignment.source_media_id = "unrelated"
    assert {item["media_id"] for item in insights._evidence(report)["sentences"]} == {report.submitted.post.media.media_id}


@pytest.mark.parametrize("change", ["fabricated_ref", "missing_source_ref", "too_verbose", "unsafe", "leak"])
def test_gemini_response_rejects_fabricated_evidence_or_unsafe_prose(saved, monkeypatch, change):
    _, report = saved
    evidence = insights._evidence(report)
    response = draft(evidence).model_dump()
    if change == "fabricated_ref":
        response["references"][0]["sentence_id"] = "not_recorded"
    elif change == "missing_source_ref":
        response["references"] = response["references"][:1]
    elif change == "too_verbose":
        response["summary"] = "word " * 31
    elif change == "unsafe":
        response["summary"] = "<script>text</script>"
    else:
        response["summary"] = "api_key=private"
    client = Mock()
    monkeypatch.setattr(insights, "client_and_model", lambda stage: (client, "unused"))
    request = Mock(return_value=response)
    monkeypatch.setattr(insights, "generate_json", request)
    with pytest.raises(ValueError):
        insights._generate(evidence)
    assert request.call_args.kwargs["max_attempts"] == 1
    client.close.assert_called_once()


def test_interpretation_route_roundtrips_and_unknown_report_does_not_call_provider(saved, client, generated):
    report_id, _ = saved
    result = client.post(f"/reports/{report_id}/interpretation", json={})
    assert result.status_code == 200 and result.json()["status"] == "ready"
    assert client.post("/reports/report_missing/interpretation", json={}).status_code == 404
    assert generated.call_count == 1


def test_source_selection_rechecks_original_input_through_existing_verification(saved, wired, store, client, monkeypatch):
    report_id, original = saved
    target = "https://www.youtube.com/watch?v=12345678901"
    original.limitations.append(f"Unverified source candidate: {target}")
    # Direct database update is test setup only: production reports remain immutable.
    from sqlalchemy import update
    with store._engine.begin() as connection:
        connection.execute(update(store.reports).where(store.reports.c.report_id == report_id).values(report_json=original.model_dump(mode="json")))
    before = store.get_report(report_id)
    selected = dict(wired)
    selected["candidates"] = [{**wired["candidates"][0], "video_id": "12345678901", "video_url": target}]
    check = AsyncMock(return_value=selected)
    monkeypatch.setattr(run, "check_source_video", check)
    response = client.post(f"/reports/{report_id}/source", json={"url": target, "job_id": "job_selected"})
    assert response.status_code == 200 and response.json()["outcome"] == "matched"
    assert check.await_args.args == (fixtures.WORDS, target)
    assert run.find_source_video.await_count == 1  # Original discovery only.
    assert store.get_job("job_selected")["url"] == fixtures.CLIP
    assert store.get_report(report_id) == before
    retry = client.post(f"/reports/{report_id}/source", json={"url": target, "job_id": "job_selected"})
    assert retry.json()["report_id"] == response.json()["report_id"]
    assert check.await_count == 1


@pytest.mark.parametrize("url", ["http://127.0.0.1/secret", "file:///etc/passwd", fixtures.CLIP, "https://www.youtube.com/watch?v=xxxxxxxxxxx"])
def test_source_selection_rejects_undiscovered_or_submitted_urls_without_writes(saved, store, client, url):
    report_id, _ = saved
    response = client.post(f"/reports/{report_id}/source", json={"url": url, "job_id": "job_rejected"})
    assert response.status_code == 400 and response.json()["error"]["code"] == "INVALID_URL"
    assert store.get_job("job_rejected") is None


def test_selected_candidate_keeps_whisper_caption_fallback_and_no_search_call(monkeypatch):
    score = {"video_id": "12345678901", "video_url": "https://www.youtube.com/watch?v=12345678901",
             "title": "Selected candidate recording", "duration": 300, "match_score": 90.0,
             "transcript_result": {"transcript": fixtures.WORDS}}
    scorer = AsyncMock(return_value=score)
    monkeypatch.setattr(candidates, "_score_candidate", scorer)
    result = asyncio.run(candidates.check_source_video(fixtures.WORDS, score["video_url"]))
    assert result["candidates"] == [score]
    assert scorer.await_args.args[0].url == score["video_url"]
    assert scorer.await_args.args[1] == candidates.normalize_text(fixtures.WORDS)


def test_low_scoring_selected_candidate_cannot_be_promoted(saved, wired, store, client, monkeypatch):
    report_id, original = saved
    monkeypatch.setattr(run, "check_source_video", AsyncMock(return_value={"candidates": [{**wired["candidates"][0], "match_score": 12}], "leads": []}))
    result = client.post(f"/reports/{report_id}/source", json={"url": original.source.post.post_url, "job_id": "job_low_selected"})
    assert result.status_code == 200 and result.json()["outcome"] == "no_verified_match"
    assert store.get_report(result.json()["report_id"])["source"] is None
