"""Offline checks for the handover media port and its honest timing contract."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from youtube_transcript_api import InvalidVideoId, RequestBlocked, TranscriptsDisabled

from backend.services.resolution import download, media, transcribe
from backend.services.shared.errors import ErrorCode, QuantroError, Stage
from backend.services.shared.types import TranscriptStatus

VIDEO_ID = "e50Tt9qJRQk"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
RESULT = {
    "title": "A public speech",
    "transcript": "The first sentence. The next sentence.",
    "source": "youtube_captions",
    "duration": 20.0,
    "segments": [
        {"text": "The first sentence.", "start": 2.0, "end": 5.0},
        {"text": "The next sentence.", "start": 15.0, "end": 20.0},
    ],
}


def test_bot_block_uses_bounded_reader_and_retains_partial_evidence(monkeypatch):
    from backend.services.reporting import remote_youtube

    error = QuantroError(ErrorCode.MEDIA_UNAVAILABLE, Stage.RETRIEVE_POST)
    error.platform_challenge = True
    monkeypatch.setattr(media, "detect_platform", lambda url: "youtube")
    monkeypatch.setattr(media, "_transcript_for_media", AsyncMock(side_effect=error))
    sentence = SimpleNamespace(
        text="Actual available speech", start_seconds=1.0, end_seconds=4.0
    )
    remote = SimpleNamespace(
        submitted=SimpleNamespace(
            transcript=SimpleNamespace(sentences=[sentence]),
            post=SimpleNamespace(title="Public clip"),
        )
    )
    calls = []
    monkeypatch.setattr(
        remote_youtube,
        "build_remote_youtube_report",
        lambda job, url, **kwargs: calls.append(url) or remote,
    )
    post, features = asyncio.run(media.resolve_media(URL))
    assert calls == [URL]
    assert features.transcript.status == TranscriptStatus.PARTIAL
    assert post.media.duration_seconds is None
    assert features.transcript.sentences[0].text == sentence.text
    assert "Gemini" in " ".join(features.warnings)


def test_unavailable_video_does_not_invoke_remote_reader(monkeypatch):
    from backend.services.reporting import remote_youtube

    error = QuantroError(ErrorCode.MEDIA_UNAVAILABLE, Stage.RETRIEVE_POST)
    monkeypatch.setattr(media, "detect_platform", lambda url: "youtube")
    monkeypatch.setattr(media, "_transcript_for_media", AsyncMock(side_effect=error))
    monkeypatch.setattr(
        remote_youtube,
        "build_remote_youtube_report",
        lambda *args: pytest.fail("Unavailable is not a bot challenge"),
    )
    with pytest.raises(QuantroError):
        asyncio.run(media.resolve_media(URL))


@pytest.mark.parametrize("later_code", ["download_failed", "format_unavailable", "missing_api_key"])
def test_initial_bot_challenge_survives_later_audio_failure(monkeypatch, later_code):
    from backend.services.reporting import remote_youtube

    monkeypatch.setenv("RESOLUTION_DOWNLOAD_VIDEO", "true")
    monkeypatch.setattr(media, "download_short_media", AsyncMock(side_effect=download.MediaDownloadError(
        "auth_required", "private signed-token=never-publish", URL
    )))
    monkeypatch.setattr(media, "get_video_transcript", AsyncMock(side_effect=transcribe.TranscriptionError(
        later_code, "private provider response", URL
    )))
    remote = SimpleNamespace(submitted=SimpleNamespace(
        transcript=SimpleNamespace(sentences=[SimpleNamespace(
            text="Observed speech from the public recording.", start_seconds=0.0, end_seconds=4.0
        )]), post=SimpleNamespace(title="Public clip")
    ))
    reader = Mock(return_value=remote)
    monkeypatch.setattr(remote_youtube, "build_remote_youtube_report", reader)
    _, features = asyncio.run(media.resolve_media(URL))
    reader.assert_called_once()
    assert features.transcript.status == TranscriptStatus.PARTIAL
    assert features.transcript.sentences[0].text == "Observed speech from the public recording."
    assert "private provider response" not in features.model_dump_json()


@pytest.mark.parametrize("later_code", ["private", "unavailable", "geo_restricted", "no_audio", "auth_failed", "rate_limited"])
def test_initial_bot_challenge_does_not_mask_different_failure(monkeypatch, later_code):
    from backend.services.reporting import remote_youtube

    monkeypatch.setenv("RESOLUTION_DOWNLOAD_VIDEO", "true")
    monkeypatch.setattr(media, "download_short_media", AsyncMock(side_effect=download.MediaDownloadError(
        "auth_required", "private provider response", URL
    )))
    monkeypatch.setattr(media, "get_video_transcript", AsyncMock(side_effect=transcribe.TranscriptionError(
        later_code, "private provider response", URL
    )))
    monkeypatch.setattr(remote_youtube, "build_remote_youtube_report", lambda *args, **kwargs: pytest.fail("Different failure must not trigger the bot fallback"))
    with pytest.raises(QuantroError) as caught:
        asyncio.run(media.resolve_media(URL))
    assert caught.value.retrieval_reason == later_code


def test_typed_download_failure_uses_independent_remote_reader(monkeypatch):
    from backend.services.reporting import remote_youtube

    monkeypatch.setenv("RESOLUTION_DOWNLOAD_VIDEO", "true")
    monkeypatch.setattr(media, "download_short_media", AsyncMock(side_effect=download.MediaDownloadError(
        "download_failed", "private provider response", URL
    )))
    monkeypatch.setattr(media, "get_video_transcript", AsyncMock(side_effect=transcribe.TranscriptionError(
        "download_failed", "private provider response", URL
    )))
    remote = SimpleNamespace(submitted=SimpleNamespace(
        transcript=SimpleNamespace(sentences=[SimpleNamespace(
            text="Observed speech from the public recording.", start_seconds=0.0, end_seconds=4.0
        )]), post=SimpleNamespace(title="Public clip")
    ))
    reader = Mock(return_value=remote)
    monkeypatch.setattr(remote_youtube, "build_remote_youtube_report", reader)
    _, features = asyncio.run(media.resolve_media(URL))
    reader.assert_called_once()
    assert features.transcript.sentences[0].text == "Observed speech from the public recording."


def test_download_failure_diagnostics_keep_code_without_provider_text(monkeypatch):
    from backend.services.resolution.diagnostics import DiagnosticCapture

    monkeypatch.setenv("RESOLUTION_DOWNLOAD_VIDEO", "true")
    monkeypatch.setattr(media, "download_short_media", AsyncMock(side_effect=download.MediaDownloadError(
        "auth_required", "private signed-token=never-publish", URL
    )))
    monkeypatch.setattr(media, "get_video_transcript", AsyncMock(return_value=RESULT))
    capture = DiagnosticCapture("job_blocked", SimpleNamespace(save_diagnostics=lambda *args: None))
    with capture.bind():
        asyncio.run(media.resolve_media(URL))
    event = capture.events[0]
    assert event["stage"] == "retrieve_video"
    assert event["data"] == {"code": "auth_required", "reason": "platform_challenge"}
    assert "never-publish" not in json.dumps(capture.events)


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch, tmp_path):
    from backend.services.reporting import remote_youtube

    def unavailable(*args, **kwargs):
        raise QuantroError(ErrorCode.MEDIA_UNAVAILABLE, Stage.RETRIEVE_POST)

    monkeypatch.setattr(remote_youtube, "build_remote_youtube_report", unavailable)
    monkeypatch.setattr(
        transcribe, "_fetch_hosted_captions", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(transcribe, "_CAPTION_CACHE_DIR", tmp_path / "captions")
    monkeypatch.setattr(download, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(transcribe, "_transcript_api_blocked_until", 0.0)
    monkeypatch.setattr(transcribe, "_ytdlp_captions_blocked_until", 0.0)
    monkeypatch.setenv("RESOLUTION_DOWNLOAD_VIDEO", "false")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a.mp4",
        "https://youtube.com@127.0.0.1/x",
        "https://www.youtube.com:8000/watch?v=x",
        "file:///tmp/x",
        "https://youtube.com.evil.test/a",
        "https://example.com/a.mp4",
    ],
)
def test_input_guard_rejects_private_or_unsupported_url(url):
    with pytest.raises(download.MediaDownloadError):
        download.detect_platform(url)


def test_cache_keeps_real_timestamps_and_source(monkeypatch):
    provider = lambda video_id: dict(RESULT, source="groq_whisper")
    monkeypatch.setattr(transcribe, "_fetch_youtube_captions_uncached", provider)
    first = transcribe._fetch_youtube_captions_sync(VIDEO_ID)
    monkeypatch.setattr(
        transcribe,
        "_fetch_youtube_captions_uncached",
        lambda video_id: pytest.fail("cache should avoid network"),
    )
    second = transcribe._fetch_youtube_captions_sync(VIDEO_ID)
    assert second == first
    assert second["source"] == "groq_whisper"
    assert second["segments"][1]["start"] == 15.0


def test_legacy_text_cache_is_refreshed(monkeypatch):
    cache = transcribe._CAPTION_CACHE_DIR / f"{VIDEO_ID}.json"
    cache.parent.mkdir()
    cache.write_text(json.dumps({"text": "legacy plain text", "end": 50.0}))
    calls = []
    monkeypatch.setattr(
        transcribe,
        "_fetch_youtube_captions_uncached",
        lambda video_id: calls.append(video_id) or RESULT,
    )
    assert (
        transcribe._fetch_youtube_captions_sync(VIDEO_ID)["segments"]
        == RESULT["segments"]
    )
    assert calls == [VIDEO_ID]


def test_caption_cache_rejects_traversal():
    with pytest.raises(InvalidVideoId):
        transcribe._fetch_youtube_captions_sync("../../secrets")


def test_permanent_caption_failure_cached(monkeypatch):
    calls = []

    def failure(video_id):
        calls.append(video_id)
        raise TranscriptsDisabled(video_id)

    monkeypatch.setattr(transcribe, "_fetch_youtube_captions_uncached", failure)
    with pytest.raises(TranscriptsDisabled):
        transcribe._fetch_youtube_captions_sync(VIDEO_ID)
    with pytest.raises(transcribe.CachedNoCaptions):
        transcribe._fetch_youtube_captions_sync(VIDEO_ID)
    assert len(calls) == 1


def test_both_blocked_caption_methods_back_off_30_minutes(monkeypatch):
    calls = []
    monkeypatch.setattr(transcribe.time, "monotonic", lambda: 100.0)

    def blocked(operation, payload):
        calls.append(operation)
        raise RequestBlocked(payload["video_id"])

    monkeypatch.setattr(transcribe, "_guarded_call", blocked)
    for _ in range(2):
        with pytest.raises(RequestBlocked):
            transcribe._fetch_youtube_captions_uncached(VIDEO_ID)
    assert calls == ["captions_api", "captions_ytdlp"]
    assert transcribe._transcript_api_blocked_until == 1900.0
    assert transcribe._ytdlp_captions_blocked_until == 1900.0
    assert not (transcribe._CAPTION_CACHE_DIR / f"{VIDEO_ID}.json").exists()


def test_caption_api_preserves_snippet_timings(monkeypatch):
    snippets = [
        SimpleNamespace(text="[Music]", start=0, duration=1),
        SimpleNamespace(text="A &amp; B", start=3.25, duration=2.5),
    ]
    selected = SimpleNamespace(fetch=lambda: snippets)
    listing = SimpleNamespace(find_transcript=lambda languages: selected)
    monkeypatch.setattr(
        transcribe,
        "YouTubeTranscriptApi",
        lambda: SimpleNamespace(list=lambda video_id: listing),
    )
    result = transcribe._fetch_with_transcript_api(VIDEO_ID)
    assert result["segments"] == [{"text": "A & B", "start": 3.25, "end": 5.75}]
    assert result["transcript"] == "A & B"


def test_json3_captions_preserve_timing(monkeypatch):
    import yt_dlp

    payload = {
        "events": [
            {"tStartMs": 2500, "dDurationMs": 2100, "segs": [{"utf8": "Real speech"}]}
        ]
    }
    info = {
        "title": "Speech",
        "subtitles": {
            "en": [{"ext": "json3", "url": "https://www.youtube.com/caption"}]
        },
    }

    class Ydl:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, *args, **kwargs):
            return info

        def urlopen(self, url):
            return SimpleNamespace(read=lambda: json.dumps(payload).encode())

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Ydl)
    result = transcribe._fetch_with_ytdlp(VIDEO_ID)
    assert result["segments"] == [{"text": "Real speech", "start": 2.5, "end": 4.6}]


@pytest.mark.asyncio
async def test_groq_requests_verbose_segment_timing(monkeypatch, tmp_path):
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"local test bytes")
    monkeypatch.setenv("GROQ_API_KEY", "unit-test-placeholder")
    calls = []

    class Client:
        def __init__(self, **kwargs):
            self.http = kwargs["http_client"]
            self.audio = SimpleNamespace(
                transcriptions=SimpleNamespace(create=self.create)
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.http.aclose()

        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                model_dump=lambda: {
                    "text": "Timed speech",
                    "duration": 4,
                    "segments": [{"text": "Timed speech", "start": 1.5, "end": 3.75}],
                    "words": [
                        {"word": "Timed", "start": 1.5, "end": 2.0},
                        {"word": "speech", "start": 2.1, "end": 3.75},
                    ],
                }
            )

    monkeypatch.setattr(transcribe, "AsyncGroq", Client)
    result = await transcribe._transcribe_with_groq(audio, URL, "youtube")
    assert calls[0]["response_format"] == "verbose_json"
    assert calls[0]["timestamp_granularities"] == ["word", "segment"]
    assert result["segments"][0]["words"] == [
        {"word": "Timed", "start": 1.5, "end": 2.0},
        {"word": "speech", "start": 2.1, "end": 3.75},
    ]


@pytest.mark.asyncio
async def test_full_audio_adds_chunk_offsets(monkeypatch, tmp_path):
    source = tmp_path / "recording.m4a"
    source.write_bytes(b"audio")
    chunks = [tmp_path / "chunk_000.mp3", tmp_path / "chunk_001.mp3"]
    for chunk in chunks:
        chunk.write_bytes(b"chunk")
    monkeypatch.setattr(transcribe, "_probe_sync", lambda path: (True, 1300.0))
    monkeypatch.setattr(transcribe, "_split_audio_sync", lambda path, directory: chunks)

    async def whisper(path, url, platform):
        return {
            "transcript": path.stem,
            "duration": 5.0,
            "segments": [
                {
                    "text": path.stem,
                    "start": 1.0,
                    "end": 5.0,
                    "words": [{"word": path.stem, "start": 1.5, "end": 4.5}],
                }
            ],
        }

    monkeypatch.setattr(transcribe, "_transcribe_with_groq", whisper)
    result = await transcribe._transcribe_file(source, URL, "youtube", tmp_path)
    assert [segment["start"] for segment in result["segments"]] == [1.0, 1201.0]
    assert [s["words"][0]["start"] for s in result["segments"]] == [1.5, 1201.5]
    assert result["duration"] == 1300.0


@pytest.mark.asyncio
async def test_captions_return_before_media_download(monkeypatch):
    monkeypatch.setattr(
        transcribe, "_fetch_youtube_captions_sync", lambda video_id: dict(RESULT)
    )
    monkeypatch.setattr(
        transcribe,
        "download_short_media",
        AsyncMock(side_effect=AssertionError("no download")),
    )
    result = await transcribe.get_video_transcript(URL)
    assert result["segments"] == RESULT["segments"]


@pytest.mark.asyncio
async def test_precomputed_source_does_not_download_or_retranscribe(monkeypatch):
    monkeypatch.setattr(
        media,
        "download_short_media",
        AsyncMock(side_effect=AssertionError("no download")),
    )
    monkeypatch.setattr(
        media,
        "get_video_transcript",
        AsyncMock(side_effect=AssertionError("no transcription")),
    )
    post, features = await media.resolve_media(
        URL,
        candidate_id="candidate_1",
        transcript_result=RESULT,
        metadata={"duration": 20.0},
    )
    assert post.media.local_path == ""
    assert post.media.playback_url == URL
    assert features.transcript.sentences[1].start_seconds == 15.0
    assert post.published_at is None
    assert post.author.followers_count is None
    assert features.frames == features.visual_text == []


@pytest.mark.asyncio
async def test_blocked_optional_video_download_retains_timed_captions(monkeypatch):
    monkeypatch.setenv("RESOLUTION_DOWNLOAD_VIDEO", "true")
    monkeypatch.setattr(media, "get_video_transcript", AsyncMock(return_value=RESULT))
    monkeypatch.setattr(
        media,
        "download_short_media",
        AsyncMock(
            side_effect=download.MediaDownloadError("auth_required", "blocked", URL)
        ),
    )
    post, features = await media.resolve_media(URL)
    assert features.transcript.sentences[0].start_seconds == 2.0
    assert post.media.local_path == ""
    assert any("blocked" in warning for warning in features.warnings)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "auth_required",
        "private",
        "geo_restricted",
        "unavailable",
        "format_unavailable",
        "download_failed",
    ],
)
@pytest.mark.parametrize("retryable", [False, True])
async def test_whisper_download_failure_remains_safe_retrieval_error(
    monkeypatch, code, retryable
):
    diagnostic = "private provider response with signed-token=unit-test-secret"
    download_error = download.MediaDownloadError(code, diagnostic, URL)
    if retryable:
        download_error.__cause__ = TimeoutError("private network diagnostic")
    transcript_error = transcribe.TranscriptionError(code, diagnostic, URL)
    transcript_error.__cause__ = download_error
    monkeypatch.setattr(
        media, "get_video_transcript", AsyncMock(side_effect=transcript_error)
    )

    with pytest.raises(QuantroError) as caught:
        await media.resolve_media(URL)

    error = caught.value
    assert error.code == ErrorCode.MEDIA_UNAVAILABLE
    assert error.stage == Stage.RETRIEVE_POST
    assert error.retryable is retryable
    assert error.message == "The media could not be retrieved."
    assert diagnostic not in str(error.public_dict())
    assert error.__cause__ is transcript_error
    assert error.retrieval_reason == code
    assert error.platform_challenge is (code == "auth_required")
    assert "retrieval_reason" not in error.public_dict()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", ["download_failed", "format_unavailable", "private", "missing_api_key"]
)
async def test_caption_block_marker_survives_audio_error_normalization(
    monkeypatch, code
):
    def blocked(video_id):
        raise RequestBlocked(video_id)

    monkeypatch.setattr(transcribe, "_fetch_youtube_captions_sync", blocked)
    failure = transcribe.TranscriptionError(code, "private provider diagnostic", URL)
    monkeypatch.setattr(
        transcribe, "transcribe_youtube_audio", AsyncMock(side_effect=failure)
    )
    with pytest.raises(QuantroError) as caught:
        await media._transcript_for_media(URL)
    assert caught.value.platform_challenge is (
        code in {"download_failed", "format_unavailable"}
    )
    assert caught.value.retrieval_reason == code
    assert "private provider diagnostic" not in str(caught.value.public_dict())


@pytest.mark.asyncio
async def test_explicit_caption_only_mode_does_not_download_video(monkeypatch):
    monkeypatch.setattr(media, "get_video_transcript", AsyncMock(return_value=RESULT))
    monkeypatch.setattr(
        media,
        "download_short_media",
        AsyncMock(side_effect=AssertionError("no video download")),
    )
    post, features = await media.resolve_media(URL)
    assert post.media.playback_url == URL
    assert features.transcript.status == TranscriptStatus.PARTIAL
    assert any("coverage" in warning for warning in features.warnings)


@pytest.mark.asyncio
async def test_instagram_reuses_downloaded_file_for_whisper(monkeypatch, tmp_path):
    url = "https://www.instagram.com/reel/ABC123/"
    path = tmp_path / "instagram.mp4"
    path.write_bytes(b"media")
    downloaded = {"file_path": str(path), "title": "An excerpt", "duration": 20.0}
    get_transcript = AsyncMock(return_value={**RESULT, "source": "groq_whisper"})
    monkeypatch.setattr(
        media, "download_short_media", AsyncMock(return_value=downloaded)
    )
    monkeypatch.setattr(media, "get_video_transcript", get_transcript)
    post, features = await media.resolve_media(url)
    assert get_transcript.call_args.args == (url, str(path))
    assert post.media.local_path == str(path)
    assert features.transcript.status == TranscriptStatus.COMPLETE


@pytest.mark.asyncio
async def test_untimed_text_never_fabricates_sentences():
    result = {
        "transcript": "Unlocated plain text",
        "segments": [],
        "duration": 30.0,
        "title": "Recording",
        "source": "groq_whisper",
    }
    _post, features = await media.resolve_media(URL, transcript_result=result)
    assert features.transcript.sentences == []
    assert features.transcript.status == TranscriptStatus.PARTIAL
    assert any("timestamps" in warning for warning in features.warnings)


@pytest.mark.asyncio
async def test_prototype_downloads_submitted_clip_and_reuses_it_for_transcription(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("RESOLUTION_DOWNLOAD_VIDEO", raising=False)
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"media")
    downloaded = {"file_path": str(path), "title": "Submitted speech", "duration": 20.0}
    downloader = AsyncMock(return_value=downloaded)
    speech = AsyncMock(return_value=RESULT)
    monkeypatch.setattr(media, "download_short_media", downloader)
    monkeypatch.setattr(media, "get_video_transcript", speech)
    post, features = await media.resolve_media(URL)
    downloader.assert_awaited_once_with(URL)
    speech.assert_awaited_once_with(URL, str(path), known_info=downloaded)
    assert post.media.local_path == str(path)
    assert features.full_transcript_text == RESULT["transcript"]
    assert "full_transcript_text" not in features.model_dump()


@pytest.mark.asyncio
async def test_hosted_caption_windows_preserve_full_search_text_and_unknown_duration():
    result = {
        **RESULT,
        "source": "serpapi_captions",
        "duration": None,
        "duration_known": False,
        "coverage": "partial",
        "transcript": RESULT["transcript"] + " Final untimed words.",
        "warnings": ["Cue windows are not word endpoints."],
    }
    post, features = await media.resolve_media(URL, transcript_result=result)
    assert post.media.duration_seconds is None
    assert features.duration_seconds == 20.0
    assert features.transcript.status == TranscriptStatus.PARTIAL
    assert features.full_transcript_text.endswith("Final untimed words.")
    assert "Cue windows are not word endpoints." in features.warnings
    assert "Final untimed words" not in " ".join(
        s.text for s in features.transcript.sentences
    )


@pytest.mark.asyncio
async def test_real_ffmpeg_output_is_used_by_whisper(monkeypatch, tmp_path):
    import asyncio
    import math
    import struct
    import subprocess
    import wave
    from pathlib import Path

    ffmpeg = transcribe._find_tool("ffmpeg")
    assert ffmpeg, "FFmpeg must be installed for this media integration check."
    wav = tmp_path / "tone.wav"
    with wave.open(str(wav), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(
            b"".join(
                struct.pack("<h", int(2000 * math.sin(i * 440 * 2 * math.pi / 16000)))
                for i in range(16000)
            )
        )
    video = tmp_path / "clip.mp4"
    await asyncio.to_thread(
        subprocess.run,
        [
            ffmpeg,
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-i",
            str(wav),
            "-c:a",
            "aac",
            str(video),
        ],
        check=True,
    )
    received = []

    async def whisper(path, url, platform):
        received.append(Path(path))
        assert Path(path).suffix == ".mp3"
        assert Path(path).stat().st_size > 0
        audio_present, duration = transcribe._probe_sync(Path(path))
        assert audio_present and duration > 0
        return {
            "segments": [{"text": "Speech response", "start": 0.0, "end": 0.8}],
            "transcript": "Speech response",
            "duration": 1.0,
        }

    monkeypatch.setenv("GROQ_API_KEY", "offline-test")
    monkeypatch.setattr(
        transcribe,
        "_fetch_youtube_captions_sync",
        lambda _: (_ for _ in ()).throw(TranscriptsDisabled(VIDEO_ID)),
    )
    monkeypatch.setattr(transcribe, "_transcribe_with_groq", whisper)
    result = await transcribe.get_video_transcript(URL, str(video))
    assert len(received) == 1 and result["source"] == "groq_whisper"
    assert result["segments"][0]["text"] == "Speech response"


@pytest.mark.asyncio
async def test_contradicted_search_duration_is_not_published():
    result = {
        **RESULT,
        "source": "serpapi_captions",
        "duration": None,
        "duration_known": False,
        "duration_hint_rejected": True,
        "coverage": "partial",
    }
    post, features = await media.resolve_media(
        URL, transcript_result=result, metadata={"duration": 10.0}
    )
    assert post.media.duration_seconds is None
    assert features.duration_seconds == 20.0
    assert features.transcript.status == TranscriptStatus.PARTIAL


def test_private_visual_discovery_hints_reach_features_not_report_fields(monkeypatch):
    from backend.services.reporting import remote_youtube
    error = QuantroError(ErrorCode.MEDIA_UNAVAILABLE, Stage.RETRIEVE_POST)
    error.platform_challenge = True
    monkeypatch.setattr(media, "_transcript_for_media", AsyncMock(side_effect=error))
    def fallback(job, url, *, discovery_hints=None):
        discovery_hints.extend(["OCR: NEWS", "Scene: A speaker at a podium."])
        return SimpleNamespace(submitted=SimpleNamespace(transcript=SimpleNamespace(sentences=[SimpleNamespace(text="Actual speech.",start_seconds=0.,end_seconds=3.)]), post=SimpleNamespace(title="Public video")))
    monkeypatch.setattr(remote_youtube, "build_remote_youtube_report", fallback)
    _, features = asyncio.run(media.resolve_media(URL))
    assert features.discovery_hints == ["OCR: NEWS", "Scene: A speaker at a podium."]
    assert features.visual_text == [] and features.frames == []
    assert "discovery_hints" not in features.model_dump_json() and "OCR: NEWS" not in features.model_dump_json()
    assert "unverified visual search clues" in " ".join(features.warnings).lower()
