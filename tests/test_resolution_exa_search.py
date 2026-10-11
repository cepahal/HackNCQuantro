"""Exa source discovery is bounded, URL-guarded and independent of Groq."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from backend.services.resolution import candidates, exa_search, groq_search
from backend.services.resolution.diagnostics import DiagnosticCapture

A = "https://www.youtube.com/watch?v=e50Tt9qJRQk"
B = "https://www.youtube.com/watch?v=hA5ezR0Kh80"
TRANSCRIPT = (
    "This distinctive parliamentary discussion establishes democratic accountability today "
    "while another independent observation supplies substantially different historical evidence."
)


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "offline-exa-secret")
    monkeypatch.setenv("GROQ_API_KEY", "offline-groq-secret")
    search = Mock(return_value=[({"post_url": A, "title": "Full event"}, ["query"])])
    client = SimpleNamespace(find_posts=search, close=Mock())
    factory = Mock(return_value=client)
    monkeypatch.setattr(exa_search, "ExaSearch", factory)
    monkeypatch.setattr(
        groq_search, "AsyncGroq", Mock(side_effect=AssertionError("No Groq web search"))
    )
    return SimpleNamespace(search=search, client=client, factory=factory)


def run(**kwargs):
    return asyncio.run(
        exa_search.search_source_links(
            title=kwargs.pop("title", "Political interview excerpt"),
            transcript=kwargs.pop("transcript", TRANSCRIPT),
            **kwargs,
        )
    )


def test_existing_exa_adapter_receives_only_bounded_observed_queries(provider):
    assert run() == [{"url": A, "title": "Full event"}]
    provider.factory.assert_called_once_with("offline-exa-secret")
    queries, limit = provider.search.call_args.args
    assert queries[0] == "political interview excerpt"
    normalized = candidates.normalize_text(TRANSCRIPT)
    assert len(queries) == 3
    for query in queries[1:]:
        words = query.strip('"')
        assert len(words.split()) == 8
        assert words in normalized
    assert limit == 12
    provider.client.close.assert_called_once()
    groq_search.AsyncGroq.assert_not_called()


def test_canonical_links_are_deduplicated_and_non_recordings_are_rejected(provider):
    provider.search.return_value = [
        ({"post_url": A, "title": "First"}, []),
        ({"post_url": "https://youtu.be/e50Tt9qJRQk", "title": "Duplicate"}, []),
        ({"post_url": "https://www.youtube.com/shorts/hA5ezR0Kh80"}, []),
        ({"post_url": "https://secret@www.youtube.com/watch?v=hA5ezR0Kh80"}, []),
        ({"post_url": "http://127.0.0.1/video"}, []),
        ({"post_url": "https://www.youtube.com:444/watch?v=hA5ezR0Kh80"}, []),
        ({"post_url": "https://example.com/video"}, []),
        (
            {
                "post_url": "https://www.youtube-nocookie.com/embed/hA5ezR0Kh80",
                "title": "Second",
            },
            [],
        ),
    ]
    assert run() == [{"url": A, "title": "First"}, {"url": B, "title": "Second"}]


@pytest.mark.parametrize("limit", [0, -1, True, "12"])
def test_invalid_limits_do_not_construct_provider(provider, limit):
    assert run(limit=limit) == []
    provider.factory.assert_not_called()


def test_missing_exa_key_cannot_fall_back_to_groq(provider, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    assert run() == []
    provider.factory.assert_not_called()
    groq_search.AsyncGroq.assert_not_called()


def test_empty_input_skips_provider(provider):
    assert run(title="", transcript="") == []
    provider.factory.assert_not_called()


def test_title_only_remains_an_unverified_discovery_clue(provider):
    assert run(title="A named event", transcript="")
    assert provider.search.call_args.args[0] == ["a named event"]


@pytest.mark.parametrize(
    "malformed", [None, {}, [None, (None, [])], [({"post_url": None}, [])]]
)
def test_malformed_results_are_optional_empty_leads(provider, malformed):
    provider.search.return_value = malformed
    assert run() == []
    provider.client.close.assert_called_once()


@pytest.mark.parametrize(
    "error,code",
    [
        (TimeoutError("offline-exa-secret provider response"), "timeout"),
        (RuntimeError("offline-exa-secret provider response"), "provider_error"),
        (
            httpx.HTTPStatusError(
                "offline-exa-secret",
                request=httpx.Request("POST", "https://api.exa.ai/search"),
                response=httpx.Response(429),
            ),
            "429",
        ),
    ],
)
def test_failed_discovery_closes_client_and_redacts_provider_details(
    provider, error, code
):
    provider.search.side_effect = error
    capture = DiagnosticCapture(
        "job_exa", SimpleNamespace(save_diagnostics=lambda *args: None)
    )
    with capture.bind():
        assert run() == []
    provider.client.close.assert_called_once()
    assert [event["status"] for event in capture.events] == ["started", "failed"]
    assert capture.events[-1]["data"]["code"] == code
    assert "offline-exa-secret" not in json.dumps(capture.events)


def test_input_output_and_provider_result_budgets_are_bounded(provider):
    provider.search.return_value = [({"post_url": A, "title": "T" * 800}, [])]
    assert (
        run(title="Alpha " + "A" * 900, transcript="", limit=1000)[0]["title"]
        == "T" * 500
    )
    queries, limit = provider.search.call_args.args
    assert len(queries[0]) == 500
    assert limit == 12


def test_outer_deadline_is_fifty_seconds(provider, monkeypatch):
    context = AsyncMock()
    context.__aexit__.return_value = False
    timeout = Mock(return_value=context)
    monkeypatch.setattr(exa_search.asyncio, "timeout", timeout)
    run()
    timeout.assert_called_once_with(50)


def test_limit_caps_returned_canonical_links(provider):
    provider.search.return_value = [({"post_url": A}, []), ({"post_url": B}, [])]
    assert run(limit=1) == [{"url": A, "title": ""}]
    assert provider.search.call_args.args[1] == 1
