"""Offline fixed-host caption locator tests, including real start-only shape."""

import importlib
import ssl

import httpx
import pytest

from backend.services.shared.errors import ErrorCode, QuantroError, Stage

module = importlib.import_module("backend.services.external_apis.youtube_transcript")
URL = "https://www.youtube.com/watch?v=eWynt87PaJ0"


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(module, "_environment", lambda: {
        "SEARCH_FALLBACK_PROVIDER": "serpapi", "SEARCH_FALLBACK_API_KEY": "private-test-key",
    })
    original = httpx.Client
    # No network occurs. Avoid opening an inherited SSL key-log pipe in this
    # sandbox while retaining required certificate and hostname verification.
    monkeypatch.setattr(module.ssl, "create_default_context", lambda: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    requests = []
    options = []

    def install(handler):
        def wrapped(request):
            requests.append(request)
            return handler(request)

        def client(**kwargs):
            options.append(kwargs)
            return original(transport=httpx.MockTransport(wrapped), **kwargs)

        monkeypatch.setattr(module.httpx, "Client", client)
        return requests, options

    return install


def assert_safe_failure(error):
    assert error.code == ErrorCode.SEARCH_FAILED
    assert error.stage == Stage.SEARCH_CANDIDATES
    assert error.retryable is True
    assert "private-test-key" not in str(error)
    assert "serpapi.com" not in str(error)
    assert error.__cause__ is None


def test_real_start_only_cues_are_not_given_invented_endings(provider):
    calls, options = provider(lambda request: httpx.Response(200, json={"transcript": [
        {"start_ms": 0, "snippet": "Good evening."},
        {"start_ms": 9_000, "snippet": "There is not a liberal America &amp; a conservative America."},
        {"start_ms": 823_000, "snippet": "We worship an awesome God in the blue states."},
    ]}))
    result = module.fetch_youtube_transcript("https://youtu.be/eWynt87PaJ0?t=823")
    assert [cue.model_dump() for cue in result] == [
        {"start_seconds": 0.0, "text": "Good evening."},
        {"start_seconds": 9.0, "text": "There is not a liberal America & a conservative America."},
        {"start_seconds": 823.0, "text": "We worship an awesome God in the blue states."},
    ]
    assert all(set(type(cue).model_fields) == {"start_seconds", "text"} for cue in result)
    assert len(calls) == 1 and calls[0].url.host == "serpapi.com"
    assert calls[0].url.path == "/search.json"
    assert calls[0].url.params["engine"] == "youtube_video_transcript"
    assert calls[0].url.params["v"] == "eWynt87PaJ0"
    assert calls[0].url.params["language_code"] == "en"
    assert options[0]["timeout"] == 12.0
    assert options[0]["trust_env"] is options[0]["follow_redirects"] is False
    assert isinstance(options[0]["verify"], ssl.SSLContext)
    assert options[0]["verify"].verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("settings", [
    {}, {"SEARCH_PROVIDER": "exa", "SEARCH_PROVIDER_API_KEY": "not-serpapi"},
    {"SEARCH_FALLBACK_PROVIDER": "brave", "SEARCH_FALLBACK_API_KEY": "not-serpapi"},
    {"SEARCH_FALLBACK_PROVIDER": "serpapi", "SEARCH_PROVIDER": "exa", "SEARCH_PROVIDER_API_KEY": "not-serpapi"},
    {"SEARCH_PROVIDER": "serpapi", "SEARCH_PROVIDER_API_KEY": " "},
])
def test_no_explicit_serpapi_key_pair_skips_without_http(monkeypatch, settings):
    monkeypatch.setattr(module, "_environment", lambda: settings)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: pytest.fail("No HTTP was authorized."))
    assert module.fetch_youtube_transcript(URL) is None


@pytest.mark.parametrize(("settings", "expected"), [
    ({"SEARCH_PROVIDER": "serpapi", "SEARCH_PROVIDER_API_KEY": "primary-key"}, "primary-key"),
    ({"SEARCH_PROVIDER": "serpapi", "SEARCH_PROVIDER_API_KEY": "primary-key",
      "SEARCH_FALLBACK_PROVIDER": "serpapi", "SEARCH_FALLBACK_API_KEY": "fallback-key"}, "fallback-key"),
])
def test_configured_primary_or_fallback_pair_selects_its_own_key(monkeypatch, provider, settings, expected):
    monkeypatch.setattr(module, "_environment", lambda: settings)
    calls, _ = provider(lambda request: httpx.Response(200, json={"transcript": [{"start_ms": 0, "snippet": "Words."}]}))
    module.fetch_youtube_transcript(URL)
    assert calls[0].url.params["api_key"] == expected


@pytest.mark.parametrize("url", ["not-a-url", "https://youtube.com.evil.test/watch?v=eWynt87PaJ0",
                                  "https://token@youtube.com/watch?v=eWynt87PaJ0",
                                  "https://www.youtube.com/watch?v=short", "http://127.0.0.1/video"])
def test_invalid_or_unsupported_url_never_reaches_provider(monkeypatch, provider, url):
    provider(lambda request: pytest.fail("Unsafe URL reached provider."))
    with pytest.raises(QuantroError) as caught:
        module.fetch_youtube_transcript(url)
    assert caught.value.code in {ErrorCode.INVALID_URL, ErrorCode.UNSUPPORTED_PLATFORM}


@pytest.mark.parametrize("start", [True, False, "9000", None, -1, float("inf"), float("nan"), 86_400_001, 10 ** 400])
def test_invalid_millisecond_start_never_becomes_timing(start):
    with pytest.raises(ValueError):
        module._parse_cues({"transcript": [{"start_ms": start, "snippet": "Words."}]})


@pytest.mark.parametrize("payload", [[], {}, {"error": "PRIVATE_DIAGNOSTIC private-test-key"},
    {"transcript": []}, {"transcript": "words"}, {"transcript": [None]},
    {"transcript": [{"start_ms": 0, "snippet": " "}]},
    {"transcript": [{"start_ms": 0, "snippet": None}]},
    {"transcript": [{"start_ms": 2, "snippet": "A"}, {"start_ms": 1, "snippet": "B"}]},
])
def test_invalid_configured_response_raises_safe_failure(provider, payload):
    provider(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(QuantroError) as caught:
        module.fetch_youtube_transcript(URL)
    assert_safe_failure(caught.value)


@pytest.mark.parametrize("status", [301, 400, 401, 403, 429, 500, 503])
def test_http_failures_are_safe_retryable_and_never_follow_redirects(provider, status):
    calls, _ = provider(lambda request: httpx.Response(status, headers={"location": "https://evil.test"},
                                                    json={"error": "private-test-key"}))
    with pytest.raises(QuantroError) as caught:
        module.fetch_youtube_transcript(URL)
    assert_safe_failure(caught.value)
    assert len(calls) == 1


def test_network_failure_is_safe_without_retry(provider):
    def fail(request):
        raise httpx.ReadTimeout("PRIVATE_DIAGNOSTIC private-test-key", request=request)
    calls, _ = provider(fail)
    with pytest.raises(QuantroError) as caught:
        module.fetch_youtube_transcript(URL)
    assert_safe_failure(caught.value)
    assert len(calls) == 1


def test_optional_end_is_ignored_and_html_is_not_rewritten_as_words(provider):
    provider(lambda request: httpx.Response(200, json={"transcript": [
        {"start_ms": 500.5, "end_ms": None, "snippet": "  No &lt;b&gt;new&lt;/b&gt; words.  "},
    ]}))
    cue = module.fetch_youtube_transcript(URL)[0]
    assert cue.start_seconds == 0.5005
    assert cue.text == "  No <b>new</b> words.  "


def test_cue_and_text_limits_reject_instead_of_silently_truncating():
    with pytest.raises(ValueError):
        module._parse_cues({"transcript": [{"start_ms": 0, "snippet": "A"}] * (module.MAX_CUES + 1)})
    with pytest.raises(ValueError):
        module._parse_cues({"transcript": [{"start_ms": 0, "snippet": "A" * (module.MAX_TEXT_CHARACTERS + 1)}]})


@pytest.mark.parametrize("kind", ["declared", "streamed", "non-json", "invalid-json"])
def test_response_limits_and_content_type_are_enforced(provider, kind):
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(module.MAX_RESPONSE_BYTES // 16_384 + 1):
                yield b"x" * 16_384
    def response(request):
        if kind == "declared":
            return httpx.Response(200, headers={"content-type": "application/json", "content-length": str(module.MAX_RESPONSE_BYTES + 1)}, content=b"{}")
        if kind == "streamed":
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=Chunks())
        if kind == "non-json":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"{}")
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{not json}")
    calls, _ = provider(response)
    with pytest.raises(QuantroError) as caught:
        module.fetch_youtube_transcript(URL)
    assert_safe_failure(caught.value)
    assert len(calls) == 1
