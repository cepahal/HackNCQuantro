"""Offline checks for the handover media port and its honest timing contract."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(transcribe, "_CAPTION_CACHE_DIR", tmp_path / "captions")
    monkeypatch.setattr(download, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(transcribe, "_transcript_api_blocked_until", 0.0)
    monkeypatch.setattr(transcribe, "_ytdlp_captions_blocked_until", 0.0)
    monkeypatch.delenv("RESOLUTION_DOWNLOAD_VIDEO", raising=False)


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
                }
            )

    monkeypatch.setattr(transcribe, "AsyncGroq", Client)
    result = await transcribe._transcribe_with_groq(audio, URL, "youtube")
    assert calls[0]["response_format"] == "verbose_json"
    assert calls[0]["timestamp_granularities"] == ["segment"]
    assert result["segments"][0] == {"text": "Timed speech", "start": 1.5, "end": 3.75}


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
            "segments": [{"text": path.stem, "start": 1.0, "end": 5.0}],
        }

    monkeypatch.setattr(transcribe, "_transcribe_with_groq", whisper)
    result = await transcribe._transcribe_file(source, URL, "youtube", tmp_path)
    assert [segment["start"] for segment in result["segments"]] == [1.0, 1201.0]
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
    ["auth_required", "private", "geo_restricted", "unavailable", "format_unavailable", "download_failed"],
)
@pytest.mark.parametrize("retryable", [False, True])
async def test_whisper_download_failure_remains_safe_retrieval_error(monkeypatch, code, retryable):
    diagnostic = "private provider response with signed-token=unit-test-secret"
    download_error = download.MediaDownloadError(code, diagnostic, URL)
    if retryable:
        download_error.__cause__ = TimeoutError("private network diagnostic")
    transcript_error = transcribe.TranscriptionError(code, diagnostic, URL)
    transcript_error.__cause__ = download_error
    monkeypatch.setattr(media, "get_video_transcript", AsyncMock(side_effect=transcript_error))

    with pytest.raises(QuantroError) as caught:
        await media.resolve_media(URL)

    error = caught.value
    assert error.code == ErrorCode.MEDIA_UNAVAILABLE
    assert error.stage == Stage.RETRIEVE_POST
    assert error.retryable is retryable
    assert error.message == "The media could not be retrieved."
    assert diagnostic not in str(error.public_dict())
    assert error.__cause__ is transcript_error


@pytest.mark.asyncio
async def test_youtube_default_does_not_download_video(monkeypatch):
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
