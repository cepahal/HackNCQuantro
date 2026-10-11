"""Offline recovery from captions that omit a distinctive spoken quotation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.services.reporting import remote_youtube
from backend.services.resolution import download, media, transcribe
from backend.services.resolution.diagnostics import DiagnosticCapture
from backend.services.shared.errors import ErrorCode, QuantroError, Stage
from backend.services.shared.types import TranscriptStatus

URL = "https://www.youtube.com/watch?v=rQ6N-sb7SVQ"
SPARSE = {
    "transcript": "as damon simon",
    "segments": [{"text": "as damon simon", "start": 0.0, "end": 2.0}],
    "title": "A submitted excerpt",
    "duration": 2.0,
    "duration_known": False,
    "coverage": "partial",
    "source": "youtube_captions",
    "discovery_inadequate": True,
    "warnings": ["Sparse provider captions remain observed evidence."],
}
SPEECH = "Fool me once, shame on you. You cannot fool me again."


def native_response(text=SPEECH, start=0.0, end=12.0):
    sentences = [SimpleNamespace(
        text=text, start_seconds=start, end_seconds=end,
    )] if text else []
    return SimpleNamespace(submitted=SimpleNamespace(
        transcript=SimpleNamespace(sentences=sentences),
        post=SimpleNamespace(title="Public recording"),
    ))


@pytest.fixture(autouse=True)
def offline_providers(monkeypatch):
    monkeypatch.setenv("RESOLUTION_DOWNLOAD_VIDEO", "false")
    monkeypatch.setattr(media, "get_video_transcript", AsyncMock(return_value=SPARSE))
    monkeypatch.setattr(media, "download_short_media", AsyncMock(
        side_effect=AssertionError("No unexpected media download"),
    ))
    monkeypatch.setattr(media, "fetch_youtube_oembed", AsyncMock(return_value={}))
    monkeypatch.setattr(remote_youtube, "build_remote_youtube_report", Mock(
        return_value=native_response(),
    ))


def test_sparse_captions_use_one_native_window_and_remain_partial():
    post, features = asyncio.run(media.resolve_media(URL))
    remote_youtube.build_remote_youtube_report.assert_called_once()
    assert features.full_transcript_text == SPEECH
    assert features.transcript.sentences[0].text == SPEECH
    assert features.transcript.status == TranscriptStatus.PARTIAL
    assert post.media.duration_seconds is None
    assert post.media.local_path == ""
    assert "Sparse provider captions" in " ".join(features.warnings)
    assert "Gemini transcript words" in " ".join(features.warnings)
    assert "discovery_inadequate" not in features.model_dump_json()


def test_real_caption_marker_reaches_independent_reader(monkeypatch, tmp_path):
    """Exercise actual caption normalization and the marker, without paid calls."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("RESOLUTION_DOWNLOAD_VIDEO", "true")
    monkeypatch.setattr(media, "get_video_transcript", transcribe.get_video_transcript)
    monkeypatch.setattr(media, "download_short_media", AsyncMock(side_effect=
        download.MediaDownloadError("auth_required", "Private provider response", URL)))
    monkeypatch.setattr(transcribe, "_CAPTION_CACHE_DIR", tmp_path / "captions")
    native_captions = Mock(return_value={
        key: value for key, value in SPARSE.items()
        if key not in {"discovery_inadequate", "warnings"}
    })
    monkeypatch.setattr(transcribe, "_fetch_youtube_captions_uncached", native_captions)
    audio = AsyncMock(side_effect=AssertionError("Groq is not configured"))
    monkeypatch.setattr(transcribe, "transcribe_youtube_audio", audio)
    post, features = asyncio.run(media.resolve_media(URL))
    native_captions.assert_called_once_with("rQ6N-sb7SVQ")
    remote_youtube.build_remote_youtube_report.assert_called_once()
    audio.assert_not_awaited()
    assert features.full_transcript_text == SPEECH
    assert features.transcript.status == TranscriptStatus.PARTIAL
    assert post.media.duration_seconds is None
    assert "Very little speech" in " ".join(features.warnings)


@pytest.mark.parametrize("reason", ["auth_required", "download_failed", "format_unavailable"])
def test_sparse_captions_recover_after_download_challenge(monkeypatch, reason):
    monkeypatch.setenv("RESOLUTION_DOWNLOAD_VIDEO", "true")
    monkeypatch.setattr(media, "download_short_media", AsyncMock(side_effect=
        download.MediaDownloadError(reason, "Private provider response", URL)))
    post, features = asyncio.run(media.resolve_media(URL))
    remote_youtube.build_remote_youtube_report.assert_called_once()
    assert features.full_transcript_text == SPEECH
    assert post.media.duration_seconds is None
    assert "Private provider response" not in features.model_dump_json()


@pytest.mark.parametrize("reason", ["private", "unavailable", "geo_restricted", "file_too_large"])
def test_terminal_download_preserves_sparse_caption_without_reader(monkeypatch, reason):
    monkeypatch.setenv("RESOLUTION_DOWNLOAD_VIDEO", "true")
    monkeypatch.setattr(media, "download_short_media", AsyncMock(side_effect=
        download.MediaDownloadError(reason, "Private provider response", URL)))
    _, features = asyncio.run(media.resolve_media(URL))
    remote_youtube.build_remote_youtube_report.assert_not_called()
    assert features.full_transcript_text == SPARSE["transcript"]


@pytest.mark.parametrize("reason", [
    "private", "unavailable", "geo_restricted", "no_audio",
    "auth_failed", "rate_limited", "file_too_large",
])
def test_terminal_audio_recovery_preserves_caption_without_reader(monkeypatch, reason):
    monkeypatch.setattr(media, "get_video_transcript", AsyncMock(return_value={
        **SPARSE, "discovery_recovery_error": reason,
    }))
    _, features = asyncio.run(media.resolve_media(URL))
    remote_youtube.build_remote_youtube_report.assert_not_called()
    assert features.full_transcript_text == SPARSE["transcript"]
    assert "discovery_recovery_error" not in features.model_dump_json()


def test_existing_media_does_not_start_native_reader(monkeypatch, tmp_path):
    path = tmp_path / "recording.mp4"
    path.write_bytes(b"local media fixture")
    monkeypatch.setenv("RESOLUTION_DOWNLOAD_VIDEO", "true")
    monkeypatch.setattr(media, "download_short_media", AsyncMock(return_value={
        "file_path": str(path), "duration": 20.0,
    }))
    post, features = asyncio.run(media.resolve_media(URL))
    remote_youtube.build_remote_youtube_report.assert_not_called()
    assert post.media.local_path == str(path)
    assert features.full_transcript_text == SPARSE["transcript"]


def test_candidate_supplied_sparse_transcript_does_not_start_native_reader():
    _, features = asyncio.run(media.resolve_media(
        URL, candidate_id="candidate_1", transcript_result=SPARSE,
    ))
    remote_youtube.build_remote_youtube_report.assert_not_called()
    assert features.full_transcript_text == SPARSE["transcript"]


def test_unmarked_caption_does_not_start_native_reader(monkeypatch):
    monkeypatch.setattr(media, "get_video_transcript", AsyncMock(return_value={
        key: value for key, value in SPARSE.items() if key != "discovery_inadequate"
    }))
    asyncio.run(media.resolve_media(URL))
    remote_youtube.build_remote_youtube_report.assert_not_called()


@pytest.mark.parametrize("failure", [
    QuantroError(ErrorCode.TRANSCRIPTION_FAILED, Stage.EXTRACT_MEDIA_FEATURES),
    RuntimeError("Private provider response token=never-publish"),
])
def test_optional_native_outage_preserves_sparse_evidence(monkeypatch, failure):
    monkeypatch.setattr(remote_youtube, "build_remote_youtube_report", Mock(side_effect=failure))
    capture = DiagnosticCapture("job_sparse", SimpleNamespace(save_diagnostics=lambda *args: None))
    with capture.bind():
        _, features = asyncio.run(media.resolve_media(URL))
    remote_youtube.build_remote_youtube_report.assert_called_once()
    assert features.full_transcript_text == SPARSE["transcript"]
    assert "available captions were retained" in " ".join(features.warnings)
    assert "Private provider response" not in str(capture.events)


@pytest.mark.parametrize("text,start,end", [
    ("as damon simon", 0.0, 12.0),
    ("Only two", 0.0, 12.0),
    ("", 0.0, 12.0),
    (SPEECH, float("nan"), 12.0),
    (SPEECH, 12.0, 1.0),
])
def test_reader_must_add_valid_timed_words(monkeypatch, text, start, end):
    monkeypatch.setattr(remote_youtube, "build_remote_youtube_report", Mock(
        return_value=native_response(text, start, end),
    ))
    _, features = asyncio.run(media.resolve_media(URL))
    remote_youtube.build_remote_youtube_report.assert_called_once()
    assert features.full_transcript_text == SPARSE["transcript"]
    assert features.transcript.sentences[0].end_seconds == 2.0
    assert "Gemini supplied" not in " ".join(features.warnings)


def test_explicit_window_preserves_absolute_provider_times(monkeypatch):
    monkeypatch.setattr(remote_youtube, "build_remote_youtube_report", Mock(
        return_value=native_response(start=91.0, end=103.0),
    ))
    post, features = asyncio.run(media.resolve_media(URL + "&t=1m30s"))
    assert remote_youtube.build_remote_youtube_report.call_args.kwargs["window_start_seconds"] == 90
    assert features.transcript.sentences[0].start_seconds == 91.0
    assert features.transcript.sentences[0].end_seconds == 103.0
    assert post.media.duration_seconds is None
    assert "window from 90 to 150 seconds" in " ".join(features.warnings)


def test_invalid_window_is_rejected_before_native_provider():
    with pytest.raises(QuantroError) as caught:
        asyncio.run(media.resolve_media(URL + "&t=bad"))
    assert caught.value.code == ErrorCode.INVALID_URL
    remote_youtube.build_remote_youtube_report.assert_not_called()
