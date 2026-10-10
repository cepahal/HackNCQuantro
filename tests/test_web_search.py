"""Fixed-host web search contracts using deterministic HTTP response fixtures."""

import importlib
import os
from threading import Barrier, Event, Lock

import httpx
import pytest

from backend.services.shared.errors import QuantroError

module = importlib.import_module("backend.services.external_apis.search_candidates")


@pytest.fixture(autouse=True)
def isolated_search_settings(monkeypatch):
    # Never read the developer's private provider credentials in offline tests.
    monkeypatch.setattr(module, "_environment", lambda: dict(os.environ))


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
        ("", "key", "SEARCH_PROVIDER is not configured"),
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


def test_parallel_queries_keep_query_order_provenance_and_partial_failures(monkeypatch, web):
    monkeypatch.setenv("SEARCH_PROVIDER", "serpapi")
    entered = Barrier(3, timeout=5)
    third_done, second_done = Event(), Event()
    lock = Lock()
    completed = []
    active = maximum = 0
    shared = "https://www.youtube.com/watch?v=shared"

    def response(request):
        nonlocal active, maximum
        query = request.url.params["q"]
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            if query == "failed":
                return httpx.Response(503, json={"error": "test-private-key"})
            # All three requests must overlap. Release them in reverse order.
            entered.wait()
            if query == "first":
                assert second_done.wait(5)
            elif query == "second":
                assert third_done.wait(5)
            with lock:
                completed.append(query)
            if query == "third":
                third_done.set()
            elif query == "second":
                second_done.set()
            return httpx.Response(200, json={"organic_results": [
                {"link": shared, "title": query},
                {"link": f"https://www.youtube.com/watch?v={query}"},
            ]})
        finally:
            with lock:
                active -= 1

    web(response)
    result = module.search_candidates(
        {"queries": ["first", "second", "third", "failed"], "warnings": ["inherited"]}, 9
    )
    assert maximum == 3
    assert completed == ["third", "second", "first"]
    assert [candidate["url"] for candidate in result["candidates"]] == [
        shared,
        "https://www.youtube.com/watch?v=first",
        "https://www.youtube.com/watch?v=second",
        "https://www.youtube.com/watch?v=third",
    ]
    assert result["candidates"][0]["title"] == "first"
    assert result["candidates"][0]["discovered_by_queries"] == ["first", "second", "third"]
    assert result["warnings"] == ["inherited", "1 search queries failed; results may be incomplete."]


@pytest.mark.parametrize("second_status,retryable", [(401, False), (503, True)])
def test_parallel_total_failure_preserves_retryability(monkeypatch, web, second_status, retryable):
    monkeypatch.setenv("SEARCH_PROVIDER", "serpapi")
    entered = Barrier(2, timeout=5)

    def response(request):
        entered.wait()
        return httpx.Response(
            401 if request.url.params["q"] == "first" else second_status,
            json={"error": "test-private-key"},
        )

    web(response)
    with pytest.raises(QuantroError) as caught:
        module.search_candidates(["first", "second"], 5)
    assert caught.value.code == "SEARCH_FAILED"
    assert caught.value.stage == "search_candidates"
    assert caught.value.retryable is retryable
    assert "test-private-key" not in str(caught.value)
