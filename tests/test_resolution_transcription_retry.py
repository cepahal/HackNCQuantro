"""Temporary speech-provider faults retry after three seconds without new jobs."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
import test_resolution_pipeline as pipeline_fixtures

from backend.services.reporting import remote_youtube
from backend.services.resolution import media, run, transcribe
from backend.services.resolution.diagnostics import DiagnosticCapture
from backend.services.shared.errors import ErrorCode, QuantroError, Stage

URL = "https://www.youtube.com/watch?v=e50Tt9qJRQk"
RESULT = {
    "transcript": "The recording supplies enough actual timed speech for discovery.",
    "segments": [{"text": "The recording supplies enough actual timed speech for discovery.",
                  "start": 0.0, "end": 5.0}],
    "source": "groq_whisper", "duration": 5.0,
}


@pytest.fixture(autouse=True)
def no_real_delay(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(media.asyncio, "sleep", sleep)
    return sleep


def capture():
    return DiagnosticCapture("job_retry", SimpleNamespace(save_diagnostics=lambda *args: None))


@pytest.mark.parametrize("failure", [
    transcribe.TranscriptionError("network_error", "token=private network response", URL),
    transcribe.TranscriptionError("rate_limited", "token=private quota response", URL),
    TimeoutError("token=private timeout"),
    ConnectionError("token=private connection"),
])
def test_temporary_transcription_failure_retries_exact_delay(monkeypatch, no_real_delay, failure):
    provider = AsyncMock(side_effect=[failure, RESULT])
    monkeypatch.setattr(media, "get_video_transcript", provider)
    diagnostics = capture()
    with diagnostics.bind():
        result = asyncio.run(media._transcript_for_media(URL))
    assert result == RESULT
    assert provider.await_count == 2
    no_real_delay.assert_awaited_once_with(3)
    assert [event["status"] for event in diagnostics.events] == ["started", "retrying", "completed"]
    assert "private" not in json.dumps(diagnostics.events)


def test_transcription_retry_has_three_attempt_cap_and_preserves_typed_error(monkeypatch, no_real_delay):
    failure = transcribe.TranscriptionError("network_error", "private response", URL)
    provider = AsyncMock(side_effect=failure)
    monkeypatch.setattr(media, "get_video_transcript", provider)
    diagnostics = capture()
    with diagnostics.bind(), pytest.raises(QuantroError) as caught:
        asyncio.run(media._transcript_for_media(URL))
    assert provider.await_count == 3
    assert no_real_delay.await_args_list == [call(3), call(3)]
    assert caught.value.__cause__ is failure
    assert caught.value.retrieval_reason == "network_error"
    assert caught.value.retryable is True
    assert [event["status"] for event in diagnostics.events] == ["started", "retrying", "retrying", "failed"]


@pytest.mark.parametrize("code", [
    "auth_required", "auth_failed", "missing_api_key", "private", "geo_restricted",
    "unavailable", "no_audio", "invalid_audio", "file_too_large", "download_failed",
])
def test_permanent_transcription_failure_is_not_retried_even_with_timeout_cause(monkeypatch, no_real_delay, code):
    failure = transcribe.TranscriptionError(code, "private response", URL)
    failure.__cause__ = TimeoutError("private inner timeout")
    provider = AsyncMock(side_effect=failure)
    monkeypatch.setattr(media, "get_video_transcript", provider)
    with pytest.raises(QuantroError):
        asyncio.run(media._transcript_for_media(URL))
    assert provider.await_count == 1
    no_real_delay.assert_not_awaited()


def native_result():
    return SimpleNamespace(submitted=SimpleNamespace(
        transcript=SimpleNamespace(sentences=[SimpleNamespace(
            text=RESULT["transcript"], start_seconds=0.0, end_seconds=5.0,
        )]), post=SimpleNamespace(title="Public recording"),
    ))


def test_native_temporary_failure_retries_one_window_with_one_completion(monkeypatch, no_real_delay):
    failure = QuantroError(ErrorCode.TRANSCRIPTION_FAILED, Stage.EXTRACT_MEDIA_FEATURES,
                          "Gemini connection failed.", retryable=True)
    reader = Mock(side_effect=[failure, native_result()])
    monkeypatch.setattr(remote_youtube, "build_remote_youtube_report", reader)
    diagnostics = capture()
    with diagnostics.bind():
        result = asyncio.run(media._native_youtube_transcript(
            URL, reason="platform_challenge", discovery_hints=[], warnings=[],
        ))
    assert result["transcript"] == RESULT["transcript"]
    assert reader.call_count == 2
    no_real_delay.assert_awaited_once_with(3)
    assert [event["status"] for event in diagnostics.events] == ["started", "retrying", "completed"]
    assert reader.call_args_list[0].args == reader.call_args_list[1].args


def test_native_exhaustion_records_safe_connection_category_once(monkeypatch, no_real_delay):
    failure = QuantroError(ErrorCode.TRANSCRIPTION_FAILED, Stage.EXTRACT_MEDIA_FEATURES,
                          "Gemini connection failed.", retryable=True)
    reader = Mock(side_effect=failure)
    monkeypatch.setattr(remote_youtube, "build_remote_youtube_report", reader)
    diagnostics = capture()
    with diagnostics.bind(), pytest.raises(QuantroError) as caught:
        asyncio.run(media._native_youtube_transcript(
            URL, reason="platform_challenge", discovery_hints=[], warnings=[],
        ))
    assert caught.value is failure
    assert reader.call_count == 3
    assert no_real_delay.await_args_list == [call(3), call(3)]
    assert [event["status"] for event in diagnostics.events] == ["started", "retrying", "retrying", "failed"]
    assert diagnostics.events[-1]["data"]["message"] == "Gemini connection failed."


@pytest.mark.parametrize("failure", [
    QuantroError(ErrorCode.TRANSCRIPTION_FAILED, Stage.EXTRACT_MEDIA_FEATURES,
                 "Invalid provider schema."),
    QuantroError(ErrorCode.INVALID_URL, Stage.RETRIEVE_POST, retryable=True),
    QuantroError(ErrorCode.MEDIA_UNAVAILABLE, Stage.RETRIEVE_POST, retryable=True),
])
def test_native_permanent_or_unrelated_failure_is_not_retried(monkeypatch, no_real_delay, failure):
    reader = Mock(side_effect=failure)
    monkeypatch.setattr(remote_youtube, "build_remote_youtube_report", reader)
    with pytest.raises(QuantroError):
        asyncio.run(media._native_youtube_transcript(
            URL, reason="platform_challenge", discovery_hints=[], warnings=[],
        ))
    reader.assert_called_once()
    no_real_delay.assert_not_awaited()


def test_sparse_caption_survives_native_retry_exhaustion(monkeypatch, no_real_delay):
    monkeypatch.setenv("RESOLUTION_DOWNLOAD_VIDEO", "false")
    monkeypatch.setattr(media, "get_video_transcript", AsyncMock(return_value={
        "transcript": "as damon simon", "segments": [
            {"text": "as damon simon", "start": 4.0, "end": 13.0}],
        "source": "youtube_captions", "title": "Submitted excerpt",
        "discovery_inadequate": True,
    }))
    reader = Mock(side_effect=QuantroError(
        ErrorCode.TRANSCRIPTION_FAILED, Stage.EXTRACT_MEDIA_FEATURES,
        "Gemini request failed (HTTP 503).", retryable=True,
    ))
    monkeypatch.setattr(remote_youtube, "build_remote_youtube_report", reader)
    _, features = asyncio.run(media.resolve_media(URL))
    assert features.full_transcript_text == "as damon simon"
    assert reader.call_count == 3
    assert no_real_delay.await_args_list == [call(3), call(3)]


@pytest.fixture
def store(tmp_path):
    yield from pipeline_fixtures.store.__wrapped__(tmp_path)


@pytest.fixture
def wired(monkeypatch):
    return pipeline_fixtures.wired.__wrapped__(monkeypatch)


def test_retried_transcription_does_not_duplicate_job_or_report(store, wired, monkeypatch, no_real_delay):
    monkeypatch.setenv("CONTEXT_VIDEO_ENABLED", "false")
    provider = AsyncMock(side_effect=[
        transcribe.TranscriptionError("network_error", "Private connection response", URL), RESULT,
    ])
    monkeypatch.setattr(media, "get_video_transcript", provider)
    original = run.resolve_media.side_effect

    async def resolve(url, **kwargs):
        if not kwargs.get("candidate_id"):
            await media._transcript_for_media(url)
        return await original(url, **kwargs)

    run.resolve_media.side_effect = resolve
    first = run.run_analysis("job_retry_once", pipeline_fixtures.CLIP, store=store)
    second = run.run_analysis("job_retry_once", pipeline_fixtures.CLIP, store=store)
    assert first == second
    assert first.outcome.value == "matched"
    assert len(store.get_job_reports("job_retry_once")) == 1
    assert provider.await_count == 2
    assert run.find_source_video.await_count == 1
    no_real_delay.assert_awaited_once_with(3)
