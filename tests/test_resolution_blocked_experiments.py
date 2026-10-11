"""Offline blocked-acquisition matrix, not live provider availability or accuracy."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from youtube_transcript_api import RequestBlocked

from backend.services.reporting import remote_youtube
from backend.services.resolution import candidates, captions_provider, media, transcribe
from backend.services.resolution.alignment import build_alignment
from backend.services.resolution.download import MediaDownloadError
from backend.services.shared.errors import QuantroError
from backend.services.shared.types import MediaFeatures

VIDEO_ID = "3XPuGqqWLtk"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
TEXT = "The observatory expedition discovered unusual mineral formations beyond the northern ridge during our winter survey."
MODES = (
    "hosted_timed",
    "hosted_untimed",
    "hosted_empty",
    "hosted_failure",
    "cached_timed",
    "cached_no_credentials",
    "no_credentials",
)


@pytest.fixture
def blocked(monkeypatch, tmp_path):
    for name in (
        "GROQ_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "YOUTUBE_API_KEY",
        "SEARCH_PROVIDER_API_KEY",
        "SEARCH_FALLBACK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RESOLUTION_DOWNLOAD_VIDEO", "true")
    monkeypatch.setattr(transcribe, "_CAPTION_CACHE_DIR", tmp_path / "captions")
    monkeypatch.setattr(transcribe, "_transcript_api_blocked_until", 0.0)
    monkeypatch.setattr(transcribe, "_ytdlp_captions_blocked_until", 0.0)
    calls = {"native": [], "hosted": [], "media": [], "audio": []}

    def native(operation, payload):
        calls["native"].append(operation)
        raise RequestBlocked(payload["video_id"])

    async def unavailable_media(url, **kwargs):
        calls["media"].append(url)
        raise MediaDownloadError(
            "auth_required", "Offline simulated bot block", url, "youtube"
        )

    async def unavailable_audio(url, **kwargs):
        calls["audio"].append(url)
        raise MediaDownloadError(
            "auth_required", "Offline simulated bot block", url, "youtube"
        )

    monkeypatch.setattr(transcribe, "_guarded_call", native)
    monkeypatch.setattr(media, "download_short_media", unavailable_media)
    monkeypatch.setattr(transcribe, "download_short_media", unavailable_audio)
    monkeypatch.setattr(
        media,
        "fetch_youtube_oembed",
        AsyncMock(return_value={"title": "Offline fixture"}),
    )
    monkeypatch.setattr(
        candidates, "fetch_media_info", AsyncMock(return_value={"duration": 300.0})
    )
    monkeypatch.setattr(
        remote_youtube,
        "build_remote_youtube_report",
        lambda *a, **k: pytest.fail("No Gemini fallback permitted"),
    )
    monkeypatch.setattr(
        candidates, "AsyncGroq", lambda *a, **k: pytest.fail("No Groq call permitted")
    )
    original_client = httpx.Client

    def configure(mode):
        settings = (
            {}
            if mode in ("no_credentials", "cached_no_credentials")
            else {
                "SEARCH_FALLBACK_PROVIDER": "serpapi",
                "SEARCH_FALLBACK_API_KEY": "offline-placeholder",
            }
        )
        monkeypatch.setattr(captions_provider, "environment", lambda: settings)

        def response(request):
            calls["hosted"].append(request.url.host)
            if mode in ("no_credentials", "cached_timed", "cached_no_credentials"):
                pytest.fail(
                    "Cache hit or missing credentials must not request hosted captions"
                )
            if mode == "hosted_failure":
                return httpx.Response(503, json={"error": "offline unavailable"})
            if mode == "hosted_empty":
                return httpx.Response(200, json={"transcript": []})
            row = {"start_ms": 100000, "snippet": TEXT}
            if mode == "hosted_timed":
                row["end_ms"] = 110000
            return httpx.Response(200, json={"transcript": [row]})

        monkeypatch.setattr(
            captions_provider.httpx,
            "Client",
            lambda **kwargs: original_client(
                transport=httpx.MockTransport(response), **kwargs
            ),
        )
        if mode in ("cached_timed", "cached_no_credentials"):
            transcribe._write_caption_cache(
                transcribe._CAPTION_CACHE_DIR / f"{VIDEO_ID}.json",
                {
                    "transcript": TEXT,
                    "segments": [{"text": TEXT, "start": 100.0, "end": 110.0}],
                    "duration": None,
                    "duration_known": False,
                    "source": "serpapi_captions",
                    "coverage": "partial",
                },
            )
        return calls

    return configure


@pytest.mark.parametrize("mode", MODES)
def test_submitted_url_blocked_acquisition_capability_matrix(blocked, mode):
    calls = blocked(mode)
    if mode in ("hosted_empty", "hosted_failure", "no_credentials"):
        with pytest.raises(QuantroError) as error:
            asyncio.run(media.resolve_media(URL))
        assert error.value.code.value == "TRANSCRIPTION_FAILED"
    else:
        post, features = asyncio.run(media.resolve_media(URL))
        assert post.media.local_path == ""
        assert post.media.playback_url == URL
        assert post.media.duration_seconds is None
        assert features.full_transcript_text == TEXT
        assert features.transcript.status.value == "partial"
        if mode == "hosted_untimed":
            assert features.transcript.sentences == []
            assert features.transcript.processed_ranges == []
            assert features.duration_seconds == 0.0
        else:
            assert [
                (s.start_seconds, s.end_seconds) for s in features.transcript.sentences
            ] == [(100.0, 110.0)]
        assert any(
            "not downloaded" in w or "blocked" in w.lower() for w in features.warnings
        )
    assert calls["media"] == [URL]
    assert calls["audio"] == []  # Missing Groq key must not initiate audio acquisition.
    if mode in ("cached_timed", "cached_no_credentials", "no_credentials"):
        assert calls["hosted"] == []
    if mode in ("cached_timed", "cached_no_credentials"):
        assert calls["native"] == []


@pytest.mark.parametrize("mode", MODES)
def test_discovery_to_resolver_blocked_candidate_matrix(blocked, monkeypatch, mode):
    calls = blocked(mode)
    # Query construction, rank merge, caption acquisition, scoring and media
    # conversion stay real; only external search results are supplied offline.
    monkeypatch.setattr(
        candidates,
        "_search",
        AsyncMock(
            return_value=[candidates.Candidate(VIDEO_ID, "Offline full source", None)]
        ),
    )
    result = asyncio.run(candidates.find_source_video(TEXT, "Offline fixture"))
    if mode in ("hosted_empty", "hosted_failure", "no_credentials"):
        assert result["status"] == "not_found"
        assert result["candidates"] == []
        assert len(result["leads"]) == 1
    else:
        # success here is a lexical lead only, never proof of a downloaded source.
        assert result["status"] == "success"
        candidate = result["candidates"][0]
        post, features = asyncio.run(
            media.resolve_media(
                candidate["video_url"],
                candidate_id="candidate_1",
                transcript_result=candidate["transcript_result"],
                metadata=candidate,
            )
        )
        assert post.media.local_path == ""
        assert post.media.playback_url == URL
        clip = MediaFeatures(
            media_id="independent_clip",
            duration_seconds=10.0,
            transcript={
                "status": "complete",
                "processed_ranges": [{"start_seconds": 0.0, "end_seconds": 10.0}],
                "sentences": [
                    {
                        "sentence_id": "clip_s1",
                        "start_seconds": 0.0,
                        "end_seconds": 10.0,
                        "text": TEXT,
                    }
                ],
            },
        )
        alignment = build_alignment(clip, features, candidate["match_score"])
        if mode == "hosted_untimed":
            assert features.transcript.sentences == []
            assert features.duration_seconds == 0.0
            assert alignment.status != "verified"
            assert alignment.matches == []
        else:
            assert features.transcript.sentences[0].start_seconds == 100.0
            assert features.transcript.sentences[0].end_seconds == 110.0
            assert alignment.status == "verified"
            assert alignment.matches[0].source_start_seconds == 100.0
            assert alignment.matches[0].source_end_seconds == 110.0
    assert calls["media"] == []
    assert calls["audio"] == []
    if mode in ("cached_timed", "cached_no_credentials", "no_credentials"):
        assert calls["hosted"] == []
