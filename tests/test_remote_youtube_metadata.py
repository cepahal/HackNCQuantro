"""Public oEmbed metadata is bounded and optional; all HTTP calls are mocked."""

import json
import ssl

import httpx
import pytest

import backend.services.reporting.remote_youtube as remote

URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
PROFILE = "https://www.youtube.com/@official_uploader"


@pytest.fixture
def transport(monkeypatch):
    real_client = httpx.Client
    state = {"calls": [], "options": [], "clients": [], "handler": None}

    def handle(request):
        state["calls"].append(request)
        return state["handler"](request)

    def factory(**kwargs):
        state["options"].append(kwargs)
        client = real_client(transport=httpx.MockTransport(handle), **kwargs)
        state["clients"].append(client)
        return client

    monkeypatch.setattr(remote.httpx, "Client", factory)
    return state


def respond(state, payload, *, status=200, headers=None):
    state["handler"] = lambda request: httpx.Response(
        status, json=payload, headers=headers
    )


def test_fixed_host_canonical_query_verified_tls_and_whitelisted_fields(transport):
    respond(
        transport,
        {
            "title": "  Official video title  ",
            "author_name": " Uploader account ",
            "author_url": PROFILE,
            "html": "<iframe src='https://evil.invalid'></iframe>",
            "thumbnail_url": "https://evil.invalid/secret",
            "caption": "Must never become the submitted caption",
            "speaker": "Not independently identified",
        },
    )

    result = remote._youtube_metadata("https://youtu.be/jNQXAC9IVRw?si=tracking")

    assert result == {
        "title": "Official video title",
        "author_name": "Uploader account",
        "author_url": PROFILE,
    }
    assert len(transport["calls"]) == 1
    request = transport["calls"][0]
    assert request.method == "GET"
    assert request.url.scheme == "https"
    assert request.url.host == "www.youtube.com"
    assert request.url.path == "/oembed"
    assert dict(request.url.params) == {"url": URL, "format": "json"}
    options = transport["options"][0]
    assert options["follow_redirects"] is False
    assert options["trust_env"] is False
    assert options["timeout"] == 5
    context = options["verify"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert transport["clients"][0].is_closed


@pytest.mark.parametrize("status", [301, 302, 307, 400, 403, 404, 429, 500, 503])
def test_failure_or_redirect_is_optional_and_never_followed(status, transport):
    respond(
        transport,
        {"title": "Not accepted"},
        status=status,
        headers={"location": "https://127.0.0.1/private"},
    )

    assert remote._youtube_metadata(URL) == {}
    assert len(transport["calls"]) == 1
    assert transport["clients"][0].is_closed


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError])
def test_network_error_does_not_block_transcript(error, transport):
    def handler(request):
        raise error("Private diagnostics must not escape", request=request)

    transport["handler"] = handler
    assert remote._youtube_metadata(URL) == {}
    assert len(transport["calls"]) == 1
    assert transport["clients"][0].is_closed


def test_tls_context_initialization_failure_is_optional(monkeypatch, transport):
    def fail_context():
        raise OSError("Local verified TLS context could not be initialized")

    monkeypatch.setattr(remote.ssl, "create_default_context", fail_context)

    assert remote._youtube_metadata(URL) == {}
    assert transport["calls"] == []


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b"not-json", "application/json"),
        (b"<html>challenge</html>", "text/html"),
        (b'{"title":"HTML pretending metadata"}', "text/html"),
        (b'{"title":"missing content type"}', ""),
        (b"[]", "application/json"),
        (b"null", "application/json"),
        (b'"string"', "application/json"),
    ],
)
def test_invalid_json_or_non_json_response_is_optional(body, content_type, transport):
    transport["handler"] = lambda request: httpx.Response(
        200, content=body, headers={"content-type": content_type}
    )

    assert remote._youtube_metadata(URL) == {}
    assert transport["clients"][0].is_closed


@pytest.mark.parametrize("extra", [0, 1])
def test_response_byte_budget_boundary(extra, transport):
    body = json.dumps({"title": "Bounded title"}).encode()
    body += b" " * (32768 + extra - len(body))
    transport["handler"] = lambda request: httpx.Response(
        200, content=body, headers={"content-type": "application/json; charset=utf-8"}
    )

    assert remote._youtube_metadata(URL) == ({} if extra else {"title": "Bounded title"})


def test_oversized_stream_stops_reading_and_closes(transport):
    class ChunkStream(httpx.SyncByteStream):
        def __init__(self):
            self.read = 0
            self.closed = False

        def __iter__(self):
            for chunk in (b" " * 16384, b" " * 16384, b"x", b"never read"):
                self.read += 1
                yield chunk

        def close(self):
            self.closed = True

    stream = ChunkStream()
    transport["handler"] = lambda request: httpx.Response(
        200, stream=stream, headers={"content-type": "application/json"}
    )

    assert remote._youtube_metadata(URL) == {}
    assert stream.read == 3
    assert stream.closed
    assert transport["clients"][0].is_closed


@pytest.mark.parametrize(
    "profile",
    [
        "http://www.youtube.com/@account",
        "https://www.youtube.com.evil.invalid/@account",
        "https://127.0.0.1/@account",
        "https://www.youtube.com@evil.invalid/@account",
        "https://user:password@www.youtube.com/@account",
        "https://www.youtube.com:444/@account",
        "https://www.youtube.com:notaport/@account",
        "https://youtu.be/jNQXAC9IVRw",
        "javascript:alert(1)",
        "file:///tmp/profile",
        "//www.youtube.com/@account",
        "https://www.youtube.com/@account space",
    ],
)
def test_untrusted_profile_never_becomes_public_metadata(profile, transport):
    respond(transport, {"title": "A valid title", "author_url": profile})

    result = remote._youtube_metadata(URL)

    assert "author_url" not in result
    assert len(transport["calls"]) == 1


@pytest.mark.parametrize("profile", [PROFILE, "https://youtube.com:443/channel/abc"])
def test_youtube_public_profile_is_retained(profile, transport):
    respond(transport, {"author_url": profile})
    assert remote._youtube_metadata(URL) == {"author_url": profile}


@pytest.mark.parametrize("value", [None, False, 42, [], {}, "", "  ", "x" * 501])
def test_invalid_or_oversized_text_fields_are_omitted(value, transport):
    respond(transport, {"title": value, "author_name": value})
    assert remote._youtube_metadata(URL) == {}


def test_text_field_size_boundary_after_trimming(transport):
    respond(transport, {"title": " " + "x" * 500 + " ", "author_name": " A "})
    assert remote._youtube_metadata(URL) == {"title": "x" * 500, "author_name": "A"}


@pytest.mark.parametrize(
    "url",
    [
        "not-a-url",
        "https://127.0.0.1/watch?v=jNQXAC9IVRw",
        "https://www.youtube.com.evil.invalid/watch?v=jNQXAC9IVRw",
        "https://user:password@www.youtube.com/watch?v=jNQXAC9IVRw",
    ],
)
def test_invalid_input_does_not_construct_http_client(url, transport):
    assert remote._youtube_metadata(url) == {}
    assert transport["options"] == transport["calls"] == []
