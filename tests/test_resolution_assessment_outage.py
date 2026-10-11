"""An optional language outage must preserve retrieved and aligned evidence."""

import json
from unittest.mock import Mock

import pytest
import test_resolution_pipeline as fixtures
from test_resolution_pipeline import CLIP, SOURCE, WORDS

from backend.services.resolution import analysis, run
from backend.services.shared.errors import ErrorCode, QuantroError, Stage

store = fixtures.store
wired = fixtures.wired


@pytest.mark.parametrize("matched", [False, True])
@pytest.mark.parametrize("caption", [None, "An unverified attributed claim."])
@pytest.mark.parametrize("retryable", [False, True])
def test_assessment_outage_keeps_report_and_observed_evidence(
    store, wired, monkeypatch, matched, caption, retryable,
):
    if not matched:
        wired["candidates"] = []
        wired["leads"] = [{"video_url": SOURCE, "title": "Possible longer recording"}]
    original_resolve = run.resolve_media.side_effect

    async def resolve(url, **kwargs):
        post, features = await original_resolve(url, **kwargs)
        if not kwargs.get("candidate_id"):
            post.caption = caption
        return post, features

    run.resolve_media.side_effect = resolve
    explanation = Mock(side_effect=QuantroError(
        ErrorCode.PROCESSING_FAILED,
        Stage.COMPARE_CONTEXT_AND_CAPTION,
        "The language assessment is temporarily unavailable.",
        retryable=retryable,
    ))
    unused = Mock(side_effect=AssertionError("No second assessment call is needed"))
    monkeypatch.setattr(run, "compare_clip" if matched else "interpret_clip", explanation)
    monkeypatch.setattr(run, "interpret_clip" if matched else "compare_clip", unused)

    result = run.run_analysis("job_assessment_outage", CLIP, store=store)
    report = store.get_report(result.report_id)
    assert store.get_job(result.job_id)["status"] == "completed"
    assert report["submitted"]["transcript"]["sentences"][0]["text"] == WORDS
    assert report["submitted"]["post"]["caption"] == caption
    assert WORDS in report["summary"]
    assert "Reliability:" in report["summary"]
    assert "unavailable" in report["summary"].lower()
    assert report.get("context_video") is None
    if matched:
        assert result.outcome.value == "matched"
        assert report["source"]["post"]["post_url"] == SOURCE
        assert report["source"]["transcript"]["sentences"][1]["text"] == WORDS
        assert report["alignment"]["status"] == "verified"
        assert report["alignment"]["matches"][0]["source_start_seconds"] == 100.0
        assert report["context"]["windows"][0]["sentences"]
        comparison = report["comparison"]
        assert comparison["context_assessment"] == "not_assessed"
        assert comparison["caption_assessment"] == ("not_assessed" if caption else "not_provided")
        assert comparison["assessment_confidence"]["level"] == "not_assessed"
        assert comparison["findings"] == []
    else:
        assert result.outcome.value == "no_verified_match"
        assert report["source"] is report["alignment"] is report["context"] is report["comparison"] is None
        assert any(note == f"Unverified source candidate: {SOURCE}" for note in report["limitations"])
        assert "not confirmed" in report["summary"].lower() or "not verified" in report["summary"].lower()

    unavailable = [
        event for event in store.get_diagnostics(result.job_id)
        if event["stage"] == Stage.COMPARE_CONTEXT_AND_CAPTION.value
        and event["status"] == "unavailable"
    ]
    assert len(unavailable) == 1
    assert unavailable[0]["data"]["reason"] == "language_assessment_unavailable"
    assert unavailable[0]["data"]["retryable"] is retryable
    assert not any(value in json.dumps(report) for value in ("local_path", "api_key", "file://"))
    assert run.run_analysis(result.job_id, CLIP, store=store) == result
    assert len(store.get_job_reports(result.job_id)) == 1
    explanation.assert_called_once()
    unused.assert_not_called()


@pytest.mark.parametrize("matched", [False, True])
def test_actual_missing_groq_key_keeps_report_without_network(
    store, wired, monkeypatch, matched,
):
    if not matched:
        wired["candidates"] = []
    monkeypatch.setenv("GROQ_API_KEY", "")
    client = Mock(side_effect=AssertionError("Missing credentials must not make a network call"))
    monkeypatch.setattr(analysis, "Groq", client)
    # Local vault credentials may be loaded on import; prevent a real fallback call.
    monkeypatch.setattr(analysis, "client_and_model", Mock(side_effect=QuantroError(
        ErrorCode.PROCESSING_FAILED, Stage.COMPARE_CONTEXT_AND_CAPTION,
        "The fallback analysis provider is not configured.",
    )))
    monkeypatch.setattr(run, "compare_clip", analysis.compare_clip)
    monkeypatch.setattr(run, "interpret_clip", analysis.interpret_clip)
    result = run.run_analysis("job_groq_not_configured", CLIP, store=store)
    report = store.get_report(result.report_id)
    assert store.get_job(result.job_id)["status"] == "completed"
    assert WORDS in report["summary"]
    if matched:
        assert report["source"]["post"]["post_url"] == SOURCE
        assert report["comparison"]["context_assessment"] == "not_assessed"
    else:
        assert report["comparison"] is None
    client.assert_not_called()


@pytest.mark.parametrize("matched", [False, True])
@pytest.mark.parametrize("error", [
    QuantroError(ErrorCode.STORAGE_FAILED, Stage.COMPARE_CONTEXT_AND_CAPTION),
    QuantroError(ErrorCode.PROCESSING_FAILED, Stage.RETRIEVE_POST),
    RuntimeError("Unexpected internal assessment bug"),
])
def test_unrelated_errors_are_fatal(store, wired, monkeypatch, matched, error):
    if not matched:
        wired["candidates"] = []
    monkeypatch.setattr(
        run, "compare_clip" if matched else "interpret_clip", Mock(side_effect=error),
    )
    with pytest.raises(QuantroError) as caught:
        run.run_analysis("job_assessment_unrelated_error", CLIP, store=store)
    if isinstance(error, QuantroError):
        assert caught.value.code == error.code
        assert caught.value.stage == error.stage
    assert store.get_job("job_assessment_unrelated_error")["status"] == "failed"
    assert store.get_job_reports("job_assessment_unrelated_error") == []
