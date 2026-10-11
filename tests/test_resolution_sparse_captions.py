"""Sparse captions retain evidence while one independent audio pass can improve it."""

from unittest.mock import AsyncMock, Mock

import pytest

from backend.services.resolution import transcribe

VIDEO_ID = "R0ukW4AzcSs"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
SPARSE = "as damon simon"
RECOVERED = "Fool me once shame on you fool me twice cannot get fooled again."


def _result(text, *, source="youtube_captions", segments=None):
    return {
        "title": "",
        "transcript": text,
        "segments": (
            [{"text": text, "start": 0.0, "end": 8.0}] if segments is None else segments
        ),
        "duration": 12.0,
        "source": source,
    }


@pytest.fixture(autouse=True)
def _offline_providers(monkeypatch, tmp_path):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(transcribe, "_CAPTION_CACHE_DIR", tmp_path / "captions")
    monkeypatch.setattr(
        transcribe,
        "download_short_media",
        AsyncMock(
            side_effect=AssertionError("No live downloads in sparse-caption tests")
        ),
    )


def _captions(monkeypatch, text=SPARSE, *, segments=None):
    result = _result(text, segments=segments)
    monkeypatch.setattr(
        transcribe, "_fetch_youtube_captions_sync", Mock(return_value=result)
    )
    return result


@pytest.mark.asyncio
async def test_sparse_captions_use_independent_audio_instead_of_returning_same_cache(
    monkeypatch,
):
    monkeypatch.setenv("GROQ_API_KEY", "offline-test-key")
    _captions(monkeypatch)
    audio = AsyncMock(return_value=_result(RECOVERED, source="groq_whisper"))
    monkeypatch.setattr(transcribe, "transcribe_youtube_audio", audio)
    cache_write = Mock()
    monkeypatch.setattr(transcribe, "_write_caption_cache", cache_write)

    result = await transcribe.get_video_transcript(URL)

    audio.assert_awaited_once_with(VIDEO_ID, skip_caption_cache=True)
    assert result["source"] == "groq_whisper"
    assert result["transcript"] == RECOVERED
    assert result.get("discovery_inadequate") is not True
    cache_write.assert_called_once()
    assert cache_write.call_args.args[1]["transcript"] == RECOVERED


@pytest.mark.asyncio
async def test_short_utterance_remains_available_without_audio_provider(monkeypatch):
    _captions(monkeypatch, "Thank you.")
    audio = AsyncMock(side_effect=AssertionError("No configured audio provider"))
    monkeypatch.setattr(transcribe, "transcribe_youtube_audio", audio)

    result = await transcribe.get_video_transcript(URL)

    assert result["transcript"] == "Thank you."
    assert result["segments"][0]["text"] == "Thank you."
    assert result["discovery_inadequate"] is True
    audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_caption_transcript_does_not_add_an_audio_request(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "offline-test-key")
    _captions(monkeypatch, RECOVERED)
    audio = AsyncMock(side_effect=AssertionError("Usable captions already exist"))
    monkeypatch.setattr(transcribe, "transcribe_youtube_audio", audio)

    result = await transcribe.get_video_transcript(URL)

    assert result["transcript"] == RECOVERED
    assert result["source"] == "youtube_captions"
    audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_media_uses_local_audio_once(monkeypatch, tmp_path):
    monkeypatch.setenv("GROQ_API_KEY", "offline-test-key")
    _captions(monkeypatch)
    local_media = tmp_path / "recording.mp4"
    local_media.write_bytes(b"offline mock media")
    local = AsyncMock(return_value=_result(RECOVERED, source="groq_whisper"))
    remote = AsyncMock(
        side_effect=AssertionError("Do not download available local media")
    )
    monkeypatch.setattr(transcribe, "_transcribe_file", local)
    monkeypatch.setattr(transcribe, "transcribe_youtube_audio", remote)

    result = await transcribe.get_video_transcript(URL, str(local_media))

    assert result["transcript"] == RECOVERED
    local.assert_awaited_once()
    assert local.await_args.args[:3] == (local_media, URL, "youtube")
    remote.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_supplied_file_keeps_sparse_caption_without_download(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("GROQ_API_KEY", "offline-test-key")
    _captions(monkeypatch)
    audio = AsyncMock(
        side_effect=AssertionError("Stale supplied files must not add a retry")
    )
    monkeypatch.setattr(transcribe, "transcribe_youtube_audio", audio)

    result = await transcribe.get_video_transcript(URL, str(tmp_path / "removed.mp4"))

    assert result["transcript"] == SPARSE
    assert result["discovery_inadequate"] is True
    audio.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["auth_required", "rate_limited", "no_audio"])
async def test_typed_audio_failure_preserves_caption_evidence(monkeypatch, code):
    monkeypatch.setenv("GROQ_API_KEY", "offline-test-key")
    _captions(monkeypatch)
    audio = AsyncMock(
        side_effect=transcribe.TranscriptionError(
            code,
            "Provider detail with offline-test-key must not be echoed",
            URL,
            "youtube",
        )
    )
    monkeypatch.setattr(transcribe, "transcribe_youtube_audio", audio)

    result = await transcribe.get_video_transcript(URL)

    assert result["transcript"] == SPARSE
    assert result["segments"][0]["text"] == SPARSE
    assert result["discovery_inadequate"] is True
    assert result.get("warnings")
    assert "offline-test-key" not in str(result["warnings"])
    audio.assert_awaited_once_with(VIDEO_ID, skip_caption_cache=True)


@pytest.mark.asyncio
async def test_unexpected_audio_implementation_error_is_not_hidden(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "offline-test-key")
    _captions(monkeypatch)
    monkeypatch.setattr(
        transcribe,
        "transcribe_youtube_audio",
        AsyncMock(side_effect=RuntimeError("implementation bug")),
    )

    with pytest.raises(RuntimeError, match="implementation bug"):
        await transcribe.get_video_transcript(URL)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "recovered",
    [
        _result("thank you", source="groq_whisper"),
        _result("as damon simon", source="groq_whisper"),
        _result(RECOVERED, source="groq_whisper", segments=[]),
        _result(
            RECOVERED,
            source="groq_whisper",
            segments=[{"text": RECOVERED, "start": 9, "end": 2}],
        ),
        _result(
            RECOVERED,
            source="groq_whisper",
            segments=[{"text": RECOVERED, "start": 0, "end": float("nan")}],
        ),
    ],
    ids=[
        "fewer_words",
        "same_words",
        "untimed_text",
        "reversed_timing",
        "nonfinite_timing",
    ],
)
async def test_audio_without_additional_timed_speech_does_not_replace_captions(
    monkeypatch, recovered
):
    monkeypatch.setenv("GROQ_API_KEY", "offline-test-key")
    _captions(monkeypatch)
    monkeypatch.setattr(
        transcribe, "transcribe_youtube_audio", AsyncMock(return_value=recovered)
    )
    cache_write = Mock()
    monkeypatch.setattr(transcribe, "_write_caption_cache", cache_write)

    result = await transcribe.get_video_transcript(URL)

    assert result["source"] == "youtube_captions"
    assert result["transcript"] == SPARSE
    assert result["discovery_inadequate"] is True
    cache_write.assert_not_called()


@pytest.mark.asyncio
async def test_better_but_still_sparse_audio_keeps_discovery_warning(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "offline-test-key")
    _captions(monkeypatch)
    improved = "We cannot be fooled"
    monkeypatch.setattr(
        transcribe,
        "transcribe_youtube_audio",
        AsyncMock(return_value=_result(improved, source="groq_whisper")),
    )

    result = await transcribe.get_video_transcript(URL)

    assert result["source"] == "groq_whisper"
    assert result["transcript"] == improved
    assert result["discovery_inadequate"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "we can go out to vote",
        "independent parliamentary deliberation establishes accountability",
        "這是一個有足夠文字內容的完整字幕片段這些記錄不能因為缺少空格就被當作稀少的語音內容而重新辨識",
    ],
    ids=["six_short_words", "long_words", "unspaced_script"],
)
async def test_either_adequate_word_or_character_count_avoids_extra_audio(
    monkeypatch, text
):
    monkeypatch.setenv("GROQ_API_KEY", "offline-test-key")
    _captions(monkeypatch, text)
    audio = AsyncMock(side_effect=AssertionError("Caption is not discovery-inadequate"))
    monkeypatch.setattr(transcribe, "transcribe_youtube_audio", audio)

    result = await transcribe.get_video_transcript(URL)

    assert result["transcript"] == text
    assert result.get("discovery_inadequate") is not True
    audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_audio_entrypoint_preserves_default_caption_cache_behavior(monkeypatch):
    cached = _result(SPARSE)
    read = Mock(return_value=cached)
    monkeypatch.setattr(transcribe, "_read_caption_cache", read)

    result = await transcribe.transcribe_youtube_audio(VIDEO_ID)

    assert result == cached
    read.assert_called_once_with(VIDEO_ID)
    transcribe.download_short_media.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_audio_recovery_bypasses_caption_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("GROQ_API_KEY", "offline-test-key")
    read = Mock(
        side_effect=AssertionError("Independent audio must bypass caption cache")
    )
    monkeypatch.setattr(transcribe, "_read_caption_cache", read)
    media = tmp_path / "audio.m4a"
    media.write_bytes(b"offline mock audio")
    download = AsyncMock(
        return_value={"file_path": str(media), "duration": 12.0, "title": "Recording"}
    )
    monkeypatch.setattr(transcribe, "download_short_media", download)
    local = AsyncMock(return_value=_result(RECOVERED, source="groq_whisper"))
    monkeypatch.setattr(transcribe, "_transcribe_file", local)
    cache_write = Mock()
    monkeypatch.setattr(transcribe, "_write_caption_cache", cache_write)

    result = await transcribe.transcribe_youtube_audio(
        VIDEO_ID, skip_caption_cache=True
    )

    assert result["transcript"] == RECOVERED
    assert result["source"] == "groq_whisper"
    read.assert_not_called()
    download.assert_awaited_once()
    assert download.await_args.args == (URL,)
    assert download.await_args.kwargs["mode"] == "audio"
    local.assert_awaited_once()
    # The caller validates that recovery adds timed speech before updating cache.
    cache_write.assert_not_called()


@pytest.mark.asyncio
async def test_long_untimed_caption_tail_cannot_hide_sparse_timed_speech(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "offline-test-key")
    captions = _captions(
        monkeypatch,
        "as damon simon espuma que en el fútbol gay",
        segments=[{"text": SPARSE, "start": 4.0, "end": 13.0}],
    )
    captions.update(
        source="serpapi_captions",
        duration=None,
        duration_known=False,
        coverage="partial",
    )
    audio = AsyncMock(return_value=_result(RECOVERED, source="groq_whisper"))
    monkeypatch.setattr(transcribe, "transcribe_youtube_audio", audio)

    result = await transcribe.get_video_transcript(URL)

    audio.assert_awaited_once_with(VIDEO_ID, skip_caption_cache=True)
    assert result["source"] == "groq_whisper"
    assert result["transcript"] == RECOVERED


@pytest.mark.asyncio
async def test_untimed_caption_tail_is_retained_without_inventing_endpoint(monkeypatch):
    text = "as damon simon espuma que en el fútbol gay"
    timed = [{"text": SPARSE, "start": 4.0, "end": 13.0}]
    captions = _captions(monkeypatch, text, segments=timed)
    captions.update(
        source="serpapi_captions",
        duration=None,
        duration_known=False,
        coverage="partial",
    )
    audio = AsyncMock(side_effect=AssertionError("No audio provider is configured"))
    monkeypatch.setattr(transcribe, "transcribe_youtube_audio", audio)

    result = await transcribe.get_video_transcript(URL)

    assert result["discovery_inadequate"] is True
    assert result["transcript"] == text
    assert result["segments"] == timed
    assert result["duration"] is None
    assert result["duration_known"] is False
    audio.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        {"text": RECOVERED, "start": 13.0},
        {"text": RECOVERED, "start": 13.0, "end": 13.0},
        {"text": RECOVERED, "start": 13.0, "end": float("inf")},
    ],
    ids=["unknown_endpoint", "empty_interval", "nonfinite_endpoint"],
)
async def test_invalid_timed_tail_does_not_bypass_sparse_recovery(monkeypatch, invalid):
    monkeypatch.setenv("GROQ_API_KEY", "offline-test-key")
    _captions(
        monkeypatch,
        f"{SPARSE} {RECOVERED}",
        segments=[{"text": SPARSE, "start": 4.0, "end": 13.0}, invalid],
    )
    audio = AsyncMock(return_value=_result(RECOVERED, source="groq_whisper"))
    monkeypatch.setattr(transcribe, "transcribe_youtube_audio", audio)

    result = await transcribe.get_video_transcript(URL)

    assert result["transcript"] == RECOVERED
    audio.assert_awaited_once_with(VIDEO_ID, skip_caption_cache=True)
