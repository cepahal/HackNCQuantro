"""Offline per-job capture, isolation, redaction, persistence and route regressions."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
import test_resolution_pipeline as fixtures
from fastapi.testclient import TestClient
from test_resolution_pipeline import CLIP, WORDS

from backend.main import app
from backend.routes.common import get_tiger_store
from backend.services.resolution import candidates, run
from backend.services.resolution.diagnostics import (
    DiagnosticCapture,
    diagnostic_response,
    public_data,
    record,
)
from backend.services.resolution.media import _ResolvedFeatures
from backend.services.shared.errors import QuantroError

store = fixtures.store
wired = fixtures.wired


@pytest.fixture
def client(store):
    app.dependency_overrides[get_tiger_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_capture_retains_full_text_candidates_reasons_and_report_without_contract_changes(store, wired, monkeypatch, client):
    original = run.resolve_media.side_effect

    async def resolve(url, **kwargs):
        post, features = await original(url, **kwargs)
        if not kwargs.get("candidate_id"):
            features = _ResolvedFeatures.model_validate(features.model_dump(mode="json"))
            features._full_transcript_text = WORDS + " Additional untimed words."
        return post, features

    run.resolve_media.side_effect = resolve
    wired["search_metadata"] = {"speaker_and_event": "Graduation", "unique_phrase": WORDS, "query": "graduation speech", "title_query": "address"}
    wired["candidates"][0]["transcript_result"] = {"transcript": WORDS, "source": "youtube_captions", "segments": [{"start": 1, "end": 2, "text": WORDS}], "api_key": "hidden-secret", "file_path": "C:/private/file"}
    wired["candidates"].append({"video_id": "12345678901", "video_url": "https://www.youtube.com/watch?v=12345678901", "title": "Weak lead", "match_score": 42.0, "transcript_result": {"transcript": "Different words", "source": "groq_whisper"}})
    result = run.run_analysis("job_diag", CLIP, store=store)
    report = store.get_report(result.report_id)
    assert "diagnostics" not in report
    response = client.get(f"/reports/{result.report_id}/diagnostics")
    assert response.status_code == 200
    data = response.json()
    assert data["capture_status"] == "recorded"
    assert data["job_id"] == "job_diag"
    serialized = json.dumps(data)
    assert "Additional untimed words" in serialized
    assert "youtube_captions" in serialized and "groq_whisper" in serialized
    assert "lexical_score_below_threshold" in serialized and "42.0" in serialized
    assert "graduation speech" in serialized
    assert all(name not in serialized for name in ("hidden-secret", "file_path", "local_path", "C:/private"))
    assert {event["stage"] for event in data["events"]} >= {"retrieve_post", "search_candidates", "build_report", "compare_context_and_caption", "save_report_and_complete_job", "run_analysis"}
    assert data["events"][-1]["status"] == "completed"
    assert all(event["elapsed_seconds"] >= 0 for event in data["events"])
    assert data["terminal_output"].startswith("[run_analysis] started")
    previous = store.get_diagnostics("job_diag")
    run.run_analysis("job_diag", CLIP, store=store)
    assert store.get_diagnostics("job_diag") == previous


def test_diagnostic_storage_failure_does_not_break_pipeline(store, wired, monkeypatch):
    def fail(*args):
        raise RuntimeError("token=private-database-diagnostic")
    monkeypatch.setattr(store, "save_diagnostics", fail)
    result = run.run_analysis("job_storage_optional", CLIP, store=store)
    assert result.outcome.value == "matched"
    assert store.get_report(result.report_id)


def test_older_and_unknown_jobs_are_explicit_and_scoped(store, wired, client):
    result = run.run_analysis("job_old", CLIP, store=store)
    with store._engine.begin() as connection:
        connection.execute(store.analysis_diagnostics.delete())
    assert client.get(f"/reports/{result.report_id}/diagnostics").json() == {
        "job_id": "job_old", "capture_status": "unavailable", "events": [], "terminal_output": "",
    }
    assert client.get("/reports/report_unknown/diagnostics").status_code == 404
    assert client.get("/reports/invalid/diagnostics").status_code == 422


def test_failed_job_exposes_only_its_safe_capture(store, wired, monkeypatch, client):
    monkeypatch.setattr(run, "find_source_video", AsyncMock(side_effect=run.CandidateSearchError("search_failed", "token=never-publish")))
    with pytest.raises(QuantroError):
        run.run_analysis("job_failed_diag", CLIP, store=store)
    response = client.get("/reports/report_job_failed_diag/diagnostics")
    assert response.status_code == 200
    data = response.json()
    assert data["events"][-1]["status"] == "failed"
    assert data["events"][-1]["data"]["code"] == "SEARCH_FAILED"
    assert "never-publish" not in response.text
    assert client.get("/reports/report_other_job/diagnostics").status_code == 404


def test_get_storage_failure_returns_unavailable(store, wired, monkeypatch, client):
    result = run.run_analysis("job_read_failure", CLIP, store=store)
    def fail(*args):
        raise RuntimeError("token=private")
    monkeypatch.setattr(store, "get_diagnostics", fail)
    data = client.get(f"/reports/{result.report_id}/diagnostics").json()
    assert data["capture_status"] == "unavailable" and data["events"] == []


def test_explicit_projection_strips_paths_credentials_queries_and_unknown_keys():
    value = public_data({"url": CLIP + "&token=private", "file_path": "/tmp/private", "token": "secret", "unknown": "private", "title": "token=private", "summary": "C:\\private\\file.txt", "post": {"media": {"local_path": "private", "media_id": "media_1"}}})
    assert value["url"] == CLIP
    assert value["title"] == "[credential omitted]"
    assert value["summary"] == "[private reference omitted]"
    assert value["post"]["media"] == {"media_id": "media_1"}
    assert all(key not in value for key in ("file_path", "token", "unknown"))
    assert public_data({"unavailable_fields": ["media.local_path", "frames.image_path", "FILE", "media.width"]}) == {"unavailable_fields": ["media.width"]}


def test_async_context_keeps_captures_separate(store):
    for job in ("job_a", "job_b"):
        store.try_create_job(job, CLIP)
    async def worker(job):
        with DiagnosticCapture(job, store).bind():
            await asyncio.sleep(0)
            record("run_analysis", "completed", {"job_id": job, "title": job})
    async def both():
        await asyncio.gather(worker("job_a"), worker("job_b"))
    asyncio.run(both())
    for job in ("job_a", "job_b"):
        events = store.get_diagnostics(job)
        assert len(events) == 1 and events[0]["data"]["job_id"] == job


def test_candidate_stage_records_search_metadata_and_unavailable_leads(store, monkeypatch):
    store.try_create_job("job_candidates", CLIP)
    meta = {"speaker_and_event": "Event", "unique_phrase": WORDS, "query": "event phrase", "title_query": "speech"}
    leads = [candidates.Candidate("12345678901", "Available", 300), candidates.Candidate("12345678902", "Unavailable", 400)]
    monkeypatch.setattr(candidates, "extract_search_metadata", AsyncMock(return_value=meta))
    monkeypatch.setattr(candidates, "search_candidates", AsyncMock(return_value=leads))
    monkeypatch.setattr(candidates, "_candidate_captions", AsyncMock(return_value=None))
    monkeypatch.setattr(candidates, "_score_candidate", AsyncMock(side_effect=[{"video_id": "12345678901", "video_url": leads[0].url, "title": "Available", "duration": 300, "match_score": 89.0, "transcript_result": {"transcript": WORDS, "source": "youtube_captions"}}, None]))
    with DiagnosticCapture("job_candidates", store).bind():
        result = asyncio.run(candidates.find_source_video(WORDS))
    assert result["search_metadata"] == meta
    events = store.get_diagnostics("job_candidates")
    assert [event["status"] for event in events] == ["completed", "discovered", "scored", "unavailable"]
    assert "youtube_captions" in json.dumps(diagnostic_response("job_candidates", events))


def test_context_export_data_survives_public_projection_without_private_paths():
    value = public_data({"context_video":{"media_id":"context_1","duration_seconds":12,"local_path":"/tmp/private"},
                         "context_selection":{"start_seconds":10,"end_seconds":22,"strategy":"model_selected",
                                              "before":[{"text":"A preceding sentence.","start":10,"end":12}],
                                              "after_method":"sentences","api_key":"never-publish"}})
    assert value["context_video"] == {"media_id":"context_1","duration_seconds":12}
    assert value["context_selection"]["strategy"] == "model_selected"
    assert value["context_selection"]["before"][0]["text"] == "A preceding sentence."
    assert "never-publish" not in json.dumps(value) and "private" not in json.dumps(value)


def test_timing_scope_distinguishes_stage_duration_from_cumulative_job_elapsed(store):
    import time
    store.try_create_job("job_clocks",CLIP)
    capture = DiagnosticCapture("job_clocks",store)
    capture.record("search_query","completed",{},started=time.monotonic())
    capture.record("run_analysis","completed",{})
    events = diagnostic_response("job_clocks",store.get_diagnostics("job_clocks"))["events"]
    assert [e["timing_kind"] for e in events] == ["stage_duration","job_elapsed"]
    old = diagnostic_response("job_old",[{"stage":"old","status":"completed","elapsed_seconds":1,"data":{}}])
    assert "timing_kind" not in old["events"][0]
