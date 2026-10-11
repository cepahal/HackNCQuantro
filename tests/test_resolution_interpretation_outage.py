"""Unavailable unmatched-clip explanations preserve observed evidence and reports."""

from unittest.mock import Mock

import pytest
import test_resolution_pipeline as fixtures
from test_resolution_pipeline import CLIP, SOURCE, WORDS

from backend.services.resolution import analysis, run
from backend.services.shared.errors import ErrorCode, QuantroError, Stage

store = fixtures.store
wired = fixtures.wired


@pytest.mark.parametrize("retryable", [False, True])
@pytest.mark.parametrize("caption", [None, "An attributed claim that was not assessed."])
def test_unmatched_explanation_outage_saves_evidence_and_reuses_report(
    store, wired, monkeypatch, retryable, caption,
):
    wired["candidates"] = []
    wired["leads"] = [{"video_url": SOURCE, "title": "Possible longer recording"}]
    original = run.resolve_media.side_effect

    async def resolve(url, **kwargs):
        post, features = await original(url, **kwargs)
        post.caption = caption
        return post, features

    run.resolve_media.side_effect = resolve
    explain = Mock(side_effect=QuantroError(
        ErrorCode.PROCESSING_FAILED, Stage.COMPARE_CONTEXT_AND_CAPTION,
        "The language assessment is temporarily unavailable.", retryable=retryable,
    ))
    monkeypatch.setattr(run, "interpret_clip", explain)
    compare = Mock(side_effect=AssertionError("No source was accepted for comparison"))
    monkeypatch.setattr(run, "compare_clip", compare)

    first = run.run_analysis("job_unmatched_explanation_outage", CLIP, store=store)
    report = store.get_report(first.report_id)
    assert first.outcome.value == "no_verified_match"
    assert store.get_job(first.job_id)["status"] == "completed"
    assert report["source"] is report["alignment"] is report["context"] is report["comparison"] is None
    assert report["submitted"]["transcript"]["sentences"][0]["text"] == WORDS
    assert report["submitted"]["post"]["caption"] == caption
    assert WORDS in report["summary"]
    assert "automated explanation is unavailable" in report["summary"].lower()
    reliability = report["summary"].split("Reliability:", 1)[1]
    assert "source recording was not confirmed" in reliability
    assert "Factual claims and recording authenticity were not checked" in reliability
    assert "The source transcript is available for comparison" not in report["summary"]
    assert any("Automated explanation was unavailable" in note for note in report["limitations"])
    assert any(note == f"Unverified source candidate: {SOURCE}" for note in report["limitations"])
    events = store.get_diagnostics(first.job_id)
    unavailable = [event for event in events
                   if event["stage"] == Stage.COMPARE_CONTEXT_AND_CAPTION.value
                   and event["status"] == "unavailable"]
    assert len(unavailable) == 1
    assert unavailable[0]["data"]["reason"] == "language_assessment_unavailable"
    assert unavailable[0]["data"]["retryable"] is retryable

    second = run.run_analysis(first.job_id, CLIP, store=store)
    assert second == first
    assert len(store.get_job_reports(first.job_id)) == 1
    assert store.get_diagnostics(first.job_id) == events
    run.resolve_media.assert_awaited_once()
    run.find_source_video.assert_awaited_once()
    explain.assert_called_once()
    compare.assert_not_called()


def test_actual_missing_explanation_key_preserves_unmatched_report(store, wired, monkeypatch):
    from google import genai

    from backend.services.specialist import gemini

    wired["candidates"] = []
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # Ignore a developer's private vault so this remains a real missing-key path.
    monkeypatch.setattr(gemini, "load_settings", Mock())
    client = Mock(side_effect=AssertionError("An unconfigured provider must not be called"))
    monkeypatch.setattr(genai, "Client", client)
    monkeypatch.setattr(run, "interpret_clip", analysis.interpret_clip)
    result = run.run_analysis("job_unmatched_missing_explanation_key", CLIP, store=store)
    report = store.get_report(result.report_id)
    assert result.outcome.value == "no_verified_match"
    assert WORDS in report["summary"]
    assert "automated explanation is unavailable" in report["summary"].lower()
    assert report["comparison"] is None
    client.assert_not_called()


@pytest.mark.parametrize("error", [
    QuantroError(ErrorCode.STORAGE_FAILED, Stage.COMPARE_CONTEXT_AND_CAPTION),
    QuantroError(ErrorCode.PROCESSING_FAILED, Stage.RETRIEVE_POST),
    RuntimeError("Unexpected interpretation bug"),
])
def test_unrelated_interpretation_failures_remain_fatal(store, wired, monkeypatch, error):
    wired["candidates"] = []
    monkeypatch.setattr(run, "interpret_clip", Mock(side_effect=error))
    with pytest.raises(QuantroError) as caught:
        run.run_analysis("job_unmatched_unrelated_failure", CLIP, store=store)
    if isinstance(error, QuantroError):
        assert caught.value.code == error.code
        assert caught.value.stage == error.stage
    assert store.get_job("job_unmatched_unrelated_failure")["status"] == "failed"
    assert store.get_job_reports("job_unmatched_unrelated_failure") == []


def test_extractive_clip_summary_does_not_claim_source_or_veracity():
    _, features = fixtures.media("clip", CLIP)
    summary = analysis._extractive_clip_summary(features)
    assert WORDS in summary
    assert "Takeaways:" in summary and "What this means:" in summary
    assert "source recording was not confirmed" in summary
    assert "authenticity were not checked" in summary
    assert "The source transcript is available" not in summary
    assert "preserves or changes the source meaning" not in summary
