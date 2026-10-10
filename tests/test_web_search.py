"""Fixed-host web search contracts using deterministic HTTP response fixtures."""

import importlib

import httpx
import pytest

from backend.services.shared.errors import QuantroError

module = importlib.import_module("backend.services.external_apis.search_candidates")


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDER_API_KEY", "test-private-key")
    monkeypatch.delenv("SEARCH_GOOGLE_CSE_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CSE_ID", raising=False)
    original = httpx.Client
    calls = []

    def install(handler):
        def wrapped(request):
            calls.append(request)
            return handler(request)

        monkeypatch.setattr(
            module.httpx,
            "Client",
            lambda **kwargs: original(transport=httpx.MockTransport(wrapped), **kwargs),
        )
        return calls

    return install


@pytest.mark.parametrize(
    "provider,host,body",
    [
        (
            "serpapi",
            "serpapi.com",
            {
                "organic_results": [
                    {
                        "link": "https://www.youtube.com/watch?v=real",
                        "title": "Title",
                        "snippet": "Words",
                    }
                ]
            },
        ),
        (
            "brave",
            "api.search.brave.com",
            {
                "web": {
                    "results": [
                        {
                            "url": "https://www.youtube.com/watch?v=real",
                            "title": "Title",
                            "description": "Words",
                        }
                    ]
                }
            },
        ),
        (
            "google_cse",
            "www.googleapis.com",
            {
                "items": [
                    {
                        "link": "https://www.youtube.com/watch?v=real",
                        "title": "Title",
                        "snippet": "Words",
                    }
                ]
            },
        ),
    ],
)
def test_provider_mapping_dedup_provenance(monkeypatch, web, provider, host, body):
    monkeypatch.setenv("SEARCH_PROVIDER", provider)
    monkeypatch.setenv("SEARCH_GOOGLE_CSE_ID", "test-engine")
    calls = web(lambda request: httpx.Response(200, json=body))
    result = module.search_candidates(
        {"queries": ["first", "second"], "warnings": ["inherited"]}, 4
    )
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["discovered_by_queries"] == ["first", "second"]
    assert candidate["publisher"] == "www.youtube.com"
    assert candidate["published_at"] is None
    assert candidate["title"] == "Title" and candidate["snippet"] == "Words"
    assert result["warnings"] == ["inherited"]
    assert len(calls) == 2 and all(r.url.host == host for r in calls)
    if provider == "brave":
        assert calls[0].headers["X-Subscription-Token"] == "test-private-key"
        assert "test-private-key" not in str(calls[0].url)


@pytest.mark.parametrize(
    "provider,key,message",
    [
        ("", "key", "SEARCH_PROVIDER not configured"),
        ("brave", "", "SEARCH_PROVIDER not configured"),
        ("unknown", "key", "unsupported"),
        ("bing", "key", "retired"),
        ("google_cse", "key", "SEARCH_GOOGLE_CSE_ID not configured"),
    ],
)
def test_missing_configuration_is_explicit(monkeypatch, provider, key, message):
    monkeypatch.setenv("SEARCH_PROVIDER", provider)
    monkeypatch.setenv("SEARCH_PROVIDER_API_KEY", key)
    monkeypatch.delenv("SEARCH_GOOGLE_CSE_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CSE_ID", raising=False)
    with pytest.raises(QuantroError, match=message) as caught:
        module.search_candidates(["search"], 5)
    assert not caught.value.retryable


@pytest.mark.parametrize("status,retryable", [(401, False), (429, True), (503, True)])
def test_total_failure_redacted(monkeypatch, web, status, retryable):
    monkeypatch.setenv("SEARCH_PROVIDER", "serpapi")
    web(lambda request: httpx.Response(status, json={"error": "test-private-key"}))
    with pytest.raises(QuantroError) as caught:
        module.search_candidates(["search"], 5)
    assert caught.value.code == "SEARCH_FAILED"
    assert caught.value.retryable is retryable
    assert "test-private-key" not in str(caught.value)


def test_partial_failure_and_empty_search(monkeypatch, web):
    monkeypatch.setenv("SEARCH_PROVIDER", "brave")
    web(
        lambda request: httpx.Response(
            503 if request.url.params["q"] == "bad" else 200,
            json={"web": {"results": []}},
        )
    )
    result = module.search_candidates(["bad", "good"], 5)
    assert result["candidates"] == []
    assert "incomplete" in result["warnings"][0]


def test_unsupported_and_private_results_are_skipped(monkeypatch, web):
    monkeypatch.setenv("SEARCH_PROVIDER", "serpapi")
    web(
        lambda request: httpx.Response(
            200,
            json={
                "organic_results": [
                    {"link": "http://127.0.0.1/private.mp4"},
                    {"link": "https://unsupported.example/article"},
                    {"link": "https://youtube.com/watch?v=real"},
                    {"link": "https://youtube.com/watch?v=other"},
                ]
            },
        )
    )
    result = module.search_candidates(["query"], 1)
    # Only the provider's top N is considered; unsupported results are not invented replacements.
    assert result["candidates"] == []


def test_public_article_is_discovered_without_claiming_download_support(
    monkeypatch, web
):
    monkeypatch.setenv("SEARCH_PROVIDER", "serpapi")
    web(
        lambda request: httpx.Response(
            200,
            json={
                "organic_results": [
                    {"link": "https://news.example.com/article", "title": "Article"},
                ]
            },
        )
    )
    result = module.search_candidates(["query"], 2)
    assert result["candidates"][0]["url"] == "https://news.example.com/article"


def test_supported_media_ranks_before_articles_before_limit(monkeypatch, web):
    monkeypatch.setenv("SEARCH_PROVIDER", "serpapi")
    queries = ["q1", "q2", "q3", "q4", "q5", "q6"]

    def response(request):
        query = request.url.params["q"]
        url = (
            "https://www.youtube.com/watch?v=actual-result"
            if query == "q6"
            else "https://news.example.com/" + query
        )
        return httpx.Response(200, json={"organic_results": [{"link": url}]})

    calls = web(response)
    result = module.search_candidates(queries, 3)
    assert len(calls) == 6
    assert len(result["candidates"]) == 3
    assert (
        result["candidates"][0]["url"]
        == "https://www.youtube.com/watch?v=actual-result"
    )
    assert result["candidates"][0]["discovered_by_queries"] == ["q6"]
    assert [item["url"] for item in result["candidates"][1:]] == [
        "https://news.example.com/q1",
        "https://news.example.com/q2",
    ]
