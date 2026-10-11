"""Fault tests for independent native-video recovery; no external calls."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.services.reporting import remote_youtube
from backend.services.resolution import download, media, transcribe
from backend.services.resolution.diagnostics import DiagnosticCapture
from backend.services.shared.errors import ErrorCode, QuantroError, Stage
from backend.services.shared.types import TranscriptStatus

URL = "https://www.youtube.com/watch?v=e50Tt9qJRQk"


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setenv("RESOLUTION_DOWNLOAD_VIDEO", "false")
    monkeypatch.setattr(media, "fetch_youtube_oembed", AsyncMock(return_value={}))
    monkeypatch.setattr(remote_youtube, "build_remote_youtube_report", Mock(
        side_effect=AssertionError("No unintended provider call")))


def native_response(start=0.0):
    return SimpleNamespace(submitted=SimpleNamespace(
        transcript=SimpleNamespace(sentences=[SimpleNamespace(
            text="Observed speech supplied by the independent reader.",
            start_seconds=start + 1.0, end_seconds=start + 5.0,
        )]), post=SimpleNamespace(title="Public recording"),
    ))


@pytest.mark.parametrize("reason", ["auth_required", "download_failed", "format_unavailable"])
def test_typed_acquisition_failure_uses_independent_reader_once(monkeypatch, reason):
    monkeypatch.setattr(media, "get_video_transcript", AsyncMock(side_effect=
        transcribe.TranscriptionError(reason, "Private provider response", URL)))
    reader = Mock(return_value=native_response())
    monkeypatch.setattr(remote_youtube, "build_remote_youtube_report", reader)
    post, features = asyncio.run(media.resolve_media(URL))
    reader.assert_called_once()
    assert post.media.local_path == ""
    assert post.media.duration_seconds is None
    assert features.transcript.status == TranscriptStatus.PARTIAL
    assert features.transcript.sentences[0].start_seconds == 1.0
    assert "Private provider response" not in features.model_dump_json()


@pytest.mark.parametrize("initial", ["private", "unavailable", "geo_restricted", "file_too_large"])
def test_terminal_initial_download_cannot_be_overridden_by_later_failure(monkeypatch, initial):
    monkeypatch.setenv("RESOLUTION_DOWNLOAD_VIDEO", "true")
    monkeypatch.setattr(media, "download_short_media", AsyncMock(side_effect=
        download.MediaDownloadError(initial, "private", URL)))
    monkeypatch.setattr(media, "get_video_transcript", AsyncMock(side_effect=
        transcribe.TranscriptionError("download_failed", "private", URL)))
    with pytest.raises(QuantroError):
        asyncio.run(media.resolve_media(URL))
    remote_youtube.build_remote_youtube_report.assert_not_called()


@pytest.mark.parametrize("reason", ["private", "unavailable", "geo_restricted", "no_audio",
                                    "auth_failed", "rate_limited", "file_too_large"])
def test_terminal_failure_wins_over_incorrect_challenge_flag(monkeypatch, reason):
    error = QuantroError(ErrorCode.MEDIA_UNAVAILABLE, Stage.RETRIEVE_POST)
    error.platform_challenge = True
    error.retrieval_reason = reason
    monkeypatch.setattr(media, "_transcript_for_media", AsyncMock(side_effect=error))
    with pytest.raises(QuantroError):
        asyncio.run(media.resolve_media(URL))
    remote_youtube.build_remote_youtube_report.assert_not_called()


@pytest.mark.parametrize("reason", ["missing_api_key", "rate_limited", "transcription_failed"])
def test_existing_media_asr_failure_does_not_start_another_provider(monkeypatch, tmp_path, reason):
    path = tmp_path / "recording.mp4"
    path.write_bytes(b"local-media-fixture")
    monkeypatch.setenv("RESOLUTION_DOWNLOAD_VIDEO", "true")
    monkeypatch.setattr(media, "download_short_media", AsyncMock(return_value={"file_path": str(path)}))
    monkeypatch.setattr(media, "get_video_transcript", AsyncMock(side_effect=
        transcribe.TranscriptionError(reason, "private", URL)))
    with pytest.raises(QuantroError):
        asyncio.run(media.resolve_media(URL))
    remote_youtube.build_remote_youtube_report.assert_not_called()


def test_untyped_failure_is_not_treated_as_media_acquisition(monkeypatch):
    monkeypatch.setattr(media, "get_video_transcript", AsyncMock(side_effect=RuntimeError("private")))
    with pytest.raises(QuantroError):
        asyncio.run(media.resolve_media(URL))
    remote_youtube.build_remote_youtube_report.assert_not_called()


@pytest.mark.parametrize("suffix,offset", [("&t=1m30s", 90), ("&start=31", 31), ("#t=45", 45)])
def test_explicit_window_is_forwarded_with_absolute_times(monkeypatch, suffix, offset):
    monkeypatch.setattr(media, "get_video_transcript", AsyncMock(side_effect=
        transcribe.TranscriptionError("auth_required", "private", URL)))
    reader = Mock(return_value=native_response(offset))
    monkeypatch.setattr(remote_youtube, "build_remote_youtube_report", reader)
    capture = DiagnosticCapture("job_window", SimpleNamespace(save_diagnostics=lambda *args: None))
    with capture.bind():
        post, features = asyncio.run(media.resolve_media(URL + suffix))
    assert reader.call_args.kwargs["window_start_seconds"] == offset
    assert features.transcript.sentences[0].start_seconds == offset + 1
    assert post.media.duration_seconds is None
    assert features.transcript.status == TranscriptStatus.PARTIAL
    assert f"window from {offset} to {offset + 60} seconds" in " ".join(features.warnings)
    completed = next(event for event in capture.events
                     if event["stage"] == "native_youtube_fallback" and event["status"] == "completed")
    assert completed["data"]["start_seconds"] == offset
    assert completed["data"]["end_seconds"] == offset + 60


@pytest.mark.parametrize("suffix", ["&t=abc", "&t=1&start=2", "&t=86401", "#t="])
def test_invalid_window_is_rejected_before_independent_provider(monkeypatch, suffix):
    monkeypatch.setattr(media, "get_video_transcript", AsyncMock(side_effect=
        transcribe.TranscriptionError("auth_required", "private", URL)))
    with pytest.raises(QuantroError) as caught:
        asyncio.run(media.resolve_media(URL + suffix))
    assert caught.value.code == ErrorCode.INVALID_URL
    remote_youtube.build_remote_youtube_report.assert_not_called()


def test_remote_outage_keeps_original_error_without_retry(monkeypatch):
    original = transcribe.TranscriptionError("download_failed", "private-original", URL)
    monkeypatch.setattr(media, "get_video_transcript", AsyncMock(side_effect=original))
    reader = Mock(side_effect=QuantroError(ErrorCode.TRANSCRIPTION_FAILED, Stage.EXTRACT_MEDIA_FEATURES))
    monkeypatch.setattr(remote_youtube, "build_remote_youtube_report", reader)
    with pytest.raises(QuantroError) as caught:
        asyncio.run(media.resolve_media(URL))
    reader.assert_called_once()
    assert caught.value.retrieval_reason == "download_failed"
    assert "private-original" not in str(caught.value)
