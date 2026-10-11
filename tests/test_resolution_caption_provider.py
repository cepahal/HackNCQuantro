"""Offline hosted-caption acquisition and full-text provenance regressions."""

import asyncio
import json
import ssl
import subprocess
import wave
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from youtube_transcript_api import RequestBlocked, TranscriptsDisabled

from backend.services.resolution import captions_provider as provider
from backend.services.resolution import transcribe

VIDEO_ID = "3XPuGqqWLtk"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
PAYLOAD = {
    "transcript": [
        {"start_ms": 500, "snippet": "The first words."},
        {"start_ms": 2500, "snippet": "The next words."},
        {"start_ms": 7000, "snippet": "Retain every final word &amp; phrase."},
    ]
}


@pytest.fixture(autouse=True)
def isolated_providers(monkeypatch, tmp_path):
    monkeypatch.setattr(transcribe, "_CAPTION_CACHE_DIR", tmp_path / "captions")
    monkeypatch.setattr(transcribe, "_transcript_api_blocked_until", 0.0)
    monkeypatch.setattr(transcribe, "_ytdlp_captions_blocked_until", 0.0)
    monkeypatch.setattr(provider, "environment", dict)


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setattr(
        provider,
        "environment",
        lambda: {
            "SEARCH_FALLBACK_PROVIDER": "serpapi",
            "SEARCH_FALLBACK_API_KEY": "private-unit-test-key",
        },
    )
    monkeypatch.setattr(
        provider.ssl,
        "create_default_context",
        lambda: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
    )
    original = httpx.Client
    calls, options = [], []

    def install(handler):
        def respond(request):
            calls.append(request)
            return handler(request)

        def client(**kwargs):
            options.append(kwargs)
            return original(transport=httpx.MockTransport(respond), **kwargs)

        monkeypatch.setattr(provider.httpx, "Client", client)
        return calls, options

    return install


def test_start_only_cues_keep_all_text_but_only_bounded_cue_windows():
    result = provider._parse_transcript(PAYLOAD)

    assert result["source"] == "serpapi_captions"
    assert (
        result["transcript"]
        == "The first words. The next words. Retain every final word & phrase."
    )
    assert result["segments"] == [
        {"text": "The first words.", "start": 0.5, "end": 2.5},
        {"text": "The next words.", "start": 2.5, "end": 7.0},
    ]
    assert result["duration"] is None
    assert result["duration_known"] is False
    assert result["coverage"] == "partial"
    assert result["timing_basis"] == "caption_cue_windows"
    warnings = " ".join(result["warnings"]).lower()
    assert (
        "cue windows" in warnings
        and "not measured word or speech endpoints" in warnings
    )
    assert "final caption text was retained" in warnings and "omitted" in warnings


def test_duplicate_start_markers_use_next_strictly_greater_marker():
    result = provider._parse_transcript(
        {
            "transcript": [
                {"start_ms": 0, "snippet": "A"},
                {"start_ms": 0, "snippet": "B"},
                {"start_ms": 2000, "snippet": "C"},
                {"start_ms": 2000, "snippet": "D"},
                {"start_ms": 5000, "snippet": "E"},
            ]
        }
    )
    assert [
        (item["text"], item["start"], item["end"]) for item in result["segments"]
    ] == [
        ("A", 0.0, 2.0),
        ("B", 0.0, 2.0),
        ("C", 2.0, 5.0),
        ("D", 2.0, 5.0),
    ]
    assert result["transcript"] == "A B C D E"


def test_actual_provider_endpoints_are_preserved_without_claiming_recording_duration():
    result = provider._parse_transcript(
        {
            "transcript": [
                {"start_ms": 500, "end_ms": 1250, "snippet": "Speech."},
                {"start_ms": 2500, "end_ms": 3250, "snippet": "Final speech."},
            ]
        }
    )
    assert [segment["end"] for segment in result["segments"]] == [1.25, 3.25]
    assert result["timing_basis"] == "provider_caption_intervals"
    assert result["duration"] is None and result["duration_known"] is False
    assert result["coverage"] == "partial"


def test_independent_duration_can_bound_last_cue_as_an_explicit_window():
    result = provider._parse_transcript(PAYLOAD, known_duration=12.0)
    assert result["segments"][-1] == {
        "text": "Retain every final word & phrase.",
        "start": 7.0,
        "end": 12.0,
    }
    assert result["duration"] == 12.0 and result["duration_known"] is True
    assert result["coverage"] == "partial"
    assert "known recording end" in " ".join(result["warnings"])


@pytest.mark.parametrize("duration", [2.0, 7.0])
def test_contradictory_search_duration_keeps_caption_text_and_real_windows(duration):
    result = provider._parse_transcript(PAYLOAD, known_duration=duration)
    unknown = provider._parse_transcript(PAYLOAD)
    assert result["transcript"] == unknown["transcript"]
    assert result["segments"] == unknown["segments"]
    assert result["duration"] is None and result["duration_known"] is False
    assert result["duration_hint_rejected"] is True
    assert "duration metadata contradicts" in " ".join(result["warnings"])


def test_real_endpoint_past_duration_hint_is_preserved():
    result = provider._parse_transcript(
        {"transcript": [{"start_ms": 500, "end_ms": 3250, "snippet": "Speech."}]},
        known_duration=2.0,
    )
    assert result["segments"] == [{"text": "Speech.", "start": 0.5, "end": 3.25}]
    assert result["duration"] is None and result["duration_hint_rejected"] is True


@pytest.mark.asyncio
async def test_rejected_duration_survives_cache_and_metadata_merge(monkeypatch):
    result = provider._parse_transcript(PAYLOAD, known_duration=2.0)
    transcribe._write_caption_cache(
        transcribe._CAPTION_CACHE_DIR / f"{VIDEO_ID}.json", result
    )
    assert transcribe._read_caption_cache(VIDEO_ID) == result
    actual = await transcribe.get_video_transcript(
        URL, known_info={"duration": 2.0, "title": "Real recording"}
    )
    assert actual["transcript"] == result["transcript"]
    assert actual["segments"] == result["segments"]
    assert actual["duration"] is None and actual["duration_known"] is False
    assert actual["duration_hint_rejected"] is True


def test_one_unbounded_cue_is_text_only_without_fabricated_timing():
    result = provider._parse_transcript(
        {"transcript": [{"start_ms": 0, "snippet": "Only speech."}]}
    )
    assert result["transcript"] == "Only speech."
    assert result["segments"] == []
    assert result["duration"] is None


@pytest.mark.parametrize(
    "value", [True, "1000", None, -1, float("inf"), float("nan"), 86_400_001, 10**400]
)
def test_invalid_starts_cannot_become_timing(value):
    with pytest.raises((TypeError, ValueError)):
        provider._parse_transcript(
            {"transcript": [{"start_ms": value, "snippet": "Speech."}]}
        )


@pytest.mark.parametrize("value", [True, "2000", -1, 500, float("inf"), 86_400_001])
def test_invalid_explicit_endpoints_are_rejected(value):
    with pytest.raises((TypeError, ValueError)):
        provider._parse_transcript(
            {"transcript": [{"start_ms": 500, "end_ms": value, "snippet": "Speech."}]}
        )


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"error": "private diagnostic"},
        {"transcript": []},
        {"transcript": [None]},
        {"transcript": [{"start_ms": 0, "snippet": " "}]},
        {
            "transcript": [
                {"start_ms": 1000, "snippet": "A"},
                {"start_ms": 500, "snippet": "B"},
            ]
        },
    ],
)
def test_invalid_cue_payloads_are_rejected(payload):
    with pytest.raises((TypeError, ValueError)):
        provider._parse_transcript(payload)


def test_cue_count_and_text_limits_reject_without_truncating(monkeypatch):
    monkeypatch.setattr(provider, "MAX_CUES", 1)
    with pytest.raises(ValueError):
        provider._parse_transcript(PAYLOAD)
    monkeypatch.setattr(provider, "MAX_CUES", 10)
    monkeypatch.setattr(provider, "MAX_TEXT_CHARACTERS", 5)
    with pytest.raises(ValueError):
        provider._parse_transcript(PAYLOAD)


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"SEARCH_PROVIDER": "exa", "SEARCH_PROVIDER_API_KEY": "not-serpapi"},
        {
            "SEARCH_FALLBACK_PROVIDER": "serpapi",
            "SEARCH_PROVIDER": "exa",
            "SEARCH_PROVIDER_API_KEY": "not-serpapi",
        },
        {"SEARCH_PROVIDER": "serpapi", "SEARCH_PROVIDER_API_KEY": " "},
    ],
)
def test_unconfigured_provider_never_calls_http(monkeypatch, settings):
    monkeypatch.setattr(provider, "environment", lambda: settings)
    monkeypatch.setattr(
        provider.httpx,
        "Client",
        lambda **kwargs: pytest.fail("No provider call is configured."),
    )
    assert provider.fetch_caption_transcript(VIDEO_ID) is None


def test_fixed_host_bounded_client_and_configured_key_pair(hosted, monkeypatch):
    monkeypatch.setattr(
        provider,
        "environment",
        lambda: {
            "SEARCH_PROVIDER": "serpapi",
            "SEARCH_PROVIDER_API_KEY": "primary-unit-key",
            "SEARCH_FALLBACK_PROVIDER": "serpapi",
            "SEARCH_FALLBACK_API_KEY": "fallback-unit-key",
        },
    )
    calls, options = hosted(lambda request: httpx.Response(200, json=PAYLOAD))
    result = provider.fetch_caption_transcript(VIDEO_ID)
    assert result["source"] == "serpapi_captions"
    assert len(calls) == 1
    request = calls[0]
    assert (
        request.url.scheme == "https"
        and request.url.host == "serpapi.com"
        and request.url.path == "/search.json"
    )
    assert dict(request.url.params) == {
        "engine": "youtube_video_transcript",
        "v": VIDEO_ID,
        "language_code": "en",
        "api_key": "fallback-unit-key",
    }
    assert options[0]["trust_env"] is options[0]["follow_redirects"] is False
    assert options[0]["timeout"] == 30.0
    assert options[0]["verify"].verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize(
    "video_id", ["../secrets", "bad", "https://youtube.com/watch?v=" + VIDEO_ID, None]
)
def test_invalid_ids_never_reach_http(monkeypatch, video_id):
    monkeypatch.setattr(
        provider.httpx,
        "Client",
        lambda **kwargs: pytest.fail("Invalid ID reached HTTP."),
    )
    with pytest.raises(ValueError):
        provider.fetch_caption_transcript(video_id)


@pytest.mark.parametrize(
    "kind",
    [
        "redirect",
        "rate-limit",
        "network",
        "declared-large",
        "streamed-large",
        "not-json",
        "invalid-json",
    ],
)
def test_provider_failures_are_safe_and_single_attempt(hosted, monkeypatch, kind):
    monkeypatch.setattr(provider, "MAX_RESPONSE_BYTES", 32)

    def respond(request):
        if kind == "network":
            raise httpx.ReadTimeout(
                "private-unit-test-key signed-provider-url", request=request
            )
        if kind == "redirect":
            return httpx.Response(302, headers={"location": "https://evil.test/"})
        if kind == "rate-limit":
            return httpx.Response(429, json={"error": "private-unit-test-key"})
        if kind == "declared-large":
            return httpx.Response(
                200,
                headers={"content-type": "application/json", "content-length": "1000"},
                content=b"{}",
            )
        if kind == "streamed-large":
            return httpx.Response(
                200, headers={"content-type": "application/json"}, content=b"x" * 33
            )
        if kind == "not-json":
            return httpx.Response(
                200, headers={"content-type": "text/html"}, content=b"{}"
            )
        return httpx.Response(
            200, headers={"content-type": "application/json"}, content=b"{broken"
        )

    calls, _ = hosted(respond)
    with pytest.raises(provider.CaptionProviderError) as caught:
        provider.fetch_caption_transcript(VIDEO_ID)
    assert str(caught.value) == "Hosted captions were unavailable."
    assert caught.value.__cause__ is None
    assert len(calls) == 1
    assert "private-unit-test-key" not in str(caught.value)


def test_hosted_fallback_follows_both_original_methods_then_caches_full_text(
    monkeypatch,
):
    calls = []

    def native(operation, payload):
        calls.append(operation)
        raise RequestBlocked(payload["video_id"])

    def hosted(video_id, **kwargs):
        calls.append("serpapi")
        return provider._parse_transcript(PAYLOAD)

    monkeypatch.setattr(transcribe, "_guarded_call", native)
    monkeypatch.setattr(transcribe, "fetch_caption_transcript", hosted)
    first = transcribe._fetch_youtube_captions_sync(VIDEO_ID)
    second = transcribe._fetch_youtube_captions_sync(VIDEO_ID)
    assert calls == ["captions_api", "captions_ytdlp", "serpapi"]
    assert second == first
    assert second["transcript"].endswith("Retain every final word & phrase.")
    assert second["duration"] is None and second["duration_known"] is False
    assert (
        second["timing_basis"] == "caption_cue_windows"
        and second["coverage"] == "partial"
    )
    assert second["warnings"] == first["warnings"]


def test_native_caption_success_never_calls_hosted_provider(monkeypatch):
    native = {
        "title": "Speech",
        "transcript": "Actual caption.",
        "segments": [{"text": "Actual caption.", "start": 0.0, "end": 2.0}],
        "duration": 2.0,
        "source": "youtube_captions",
    }
    monkeypatch.setattr(
        transcribe, "_fetch_youtube_captions_uncached", lambda video_id: native
    )
    hosted = Mock(side_effect=AssertionError("native succeeded"))
    monkeypatch.setattr(transcribe, "fetch_caption_transcript", hosted)
    assert transcribe._fetch_youtube_captions_sync(VIDEO_ID) == native
    hosted.assert_not_called()


def test_native_negative_cache_does_not_disable_hosted_captions(monkeypatch):
    transcribe._write_caption_cache(
        transcribe._CAPTION_CACHE_DIR / f"{VIDEO_ID}.json",
        {"no_captions": "TranscriptsDisabled"},
    )
    monkeypatch.setattr(
        transcribe,
        "_fetch_youtube_captions_uncached",
        lambda _: pytest.fail("Native negative cache should be used."),
    )
    result = provider._parse_transcript(PAYLOAD)
    hosted = Mock(return_value=result)
    monkeypatch.setattr(transcribe, "fetch_caption_transcript", hosted)
    assert transcribe._fetch_youtube_captions_sync(VIDEO_ID) == result
    assert transcribe._read_caption_cache(VIDEO_ID) == result
    hosted.assert_called_once()


def test_native_ip_backoff_does_not_disable_hosted_captions(monkeypatch):
    monkeypatch.setattr(transcribe, "_transcript_api_blocked_until", float("inf"))
    monkeypatch.setattr(transcribe, "_ytdlp_captions_blocked_until", float("inf"))
    monkeypatch.setattr(
        transcribe,
        "_guarded_call",
        lambda *_: pytest.fail("Native providers are in backoff."),
    )
    hosted = Mock(return_value=provider._parse_transcript(PAYLOAD))
    monkeypatch.setattr(transcribe, "fetch_caption_transcript", hosted)
    assert (
        transcribe._fetch_youtube_captions_sync(VIDEO_ID)["source"]
        == "serpapi_captions"
    )
    hosted.assert_called_once()


@pytest.mark.asyncio
async def test_submitted_hosted_captions_return_before_whisper_and_keep_unknown_duration(
    monkeypatch,
):
    monkeypatch.setattr(
        transcribe,
        "_fetch_youtube_captions_uncached",
        Mock(side_effect=RequestBlocked(VIDEO_ID)),
    )
    monkeypatch.setattr(
        transcribe,
        "fetch_caption_transcript",
        Mock(return_value=provider._parse_transcript(PAYLOAD)),
    )
    whisper = AsyncMock(
        side_effect=AssertionError(
            "Usable hosted captions should avoid audio download."
        )
    )
    monkeypatch.setattr(transcribe, "transcribe_youtube_audio", whisper)
    result = await transcribe.get_video_transcript(URL)
    assert result["duration"] is None and result["duration_known"] is False
    assert result["transcript"].endswith("Retain every final word & phrase.")
    whisper.assert_not_called()


@pytest.mark.asyncio
async def test_text_only_hosted_caption_remains_usable_and_cacheable(monkeypatch):
    # Short final cues remain usable without an independent speech provider.
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    result = provider._parse_transcript(
        {"transcript": [{"start_ms": 0, "snippet": "Retain this final cue."}]}
    )
    monkeypatch.setattr(
        transcribe,
        "_fetch_youtube_captions_uncached",
        Mock(side_effect=RequestBlocked(VIDEO_ID)),
    )
    hosted = Mock(return_value=result)
    monkeypatch.setattr(transcribe, "fetch_caption_transcript", hosted)
    monkeypatch.setattr(
        transcribe,
        "transcribe_youtube_audio",
        AsyncMock(side_effect=AssertionError("Hosted text is available.")),
    )
    assert (await transcribe.get_video_transcript(URL))["segments"] == []
    assert (await transcribe.get_video_transcript(URL))[
        "transcript"
    ] == "Retain this final cue."
    assert transcribe._read_caption_cache(VIDEO_ID)["duration"] is None
    hosted.assert_called_once()


@pytest.mark.asyncio
async def test_hosted_failure_retains_original_whisper_workflow(monkeypatch):
    monkeypatch.setattr(
        transcribe,
        "_fetch_youtube_captions_uncached",
        Mock(side_effect=TranscriptsDisabled(VIDEO_ID)),
    )
    monkeypatch.setattr(
        transcribe,
        "fetch_caption_transcript",
        Mock(
            side_effect=provider.CaptionProviderError(
                "Hosted captions were unavailable."
            )
        ),
    )
    result = {
        "title": "Speech",
        "transcript": "Whisper words.",
        "segments": [{"text": "Whisper words.", "start": 0.0, "end": 2.0}],
        "duration": 2.0,
        "source": "groq_whisper",
    }
    whisper = AsyncMock(return_value=result)
    monkeypatch.setattr(transcribe, "transcribe_youtube_audio", whisper)
    assert await transcribe.get_video_transcript(URL) == result
    whisper.assert_awaited_once_with(VIDEO_ID)


def test_cache_read_never_fetches_and_preserves_exact_full_text(monkeypatch):
    result = provider._parse_transcript(PAYLOAD)
    result["transcript"] += " [Final source wording]"
    transcribe._write_caption_cache(
        transcribe._CAPTION_CACHE_DIR / f"{VIDEO_ID}.json", result
    )
    monkeypatch.setattr(
        transcribe,
        "fetch_caption_transcript",
        lambda *_: pytest.fail("Cache helper must never fetch."),
    )
    assert (
        transcribe._read_caption_cache(VIDEO_ID)["transcript"] == result["transcript"]
    )
    assert transcribe._read_caption_cache("abcdefghijk") is None
    assert (
        json.loads((transcribe._CAPTION_CACHE_DIR / f"{VIDEO_ID}.json").read_text())[
            "duration"
        ]
        is None
    )


@pytest.mark.asyncio
async def test_real_ffmpeg_conversion_precedes_mocked_whisper(monkeypatch, tmp_path):
    ffmpeg, ffprobe = transcribe._find_tool("ffmpeg"), transcribe._find_tool("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip(
            "FFmpeg and ffprobe are required for the local media conversion smoke."
        )
    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x00\x00" * 16_000)
    recording = tmp_path / "recording.mp4"
    await asyncio.to_thread(
        subprocess.run,
        [ffmpeg, "-y", "-v", "error", "-i", str(source), "-c:a", "aac", str(recording)],
        capture_output=True,
        check=True,
        timeout=10,
    )
    calls = []

    async def whisper(path, url, platform):
        calls.append((path.suffix, url, platform))
        probed = await asyncio.to_thread(
            subprocess.run,
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "stream=channels,sample_rate",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            check=True,
            timeout=10,
        )
        stream = json.loads(probed.stdout)["streams"][0]
        assert stream["channels"] == 1 and stream["sample_rate"] == "16000"
        assert path.stat().st_size > 0
        return {
            "transcript": "Mocked speech.",
            "segments": [{"text": "Mocked speech.", "start": 0.0, "end": 0.75}],
            "duration": 1.0,
        }

    monkeypatch.setattr(transcribe, "_transcribe_with_groq", whisper)
    result = await transcribe._transcribe_file(recording, URL, "youtube", tmp_path)
    assert calls == [(".mp3", URL, "youtube")]
    assert result["source"] == "groq_whisper"
    assert result["segments"] == [{"text": "Mocked speech.", "start": 0.0, "end": 0.75}]
    assert 0.9 <= result["duration"] <= 1.2
