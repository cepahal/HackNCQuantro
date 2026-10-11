"""Hybrid discovery preserves v2 shapes and distinguishes errors from no match."""

import copy
import importlib

import pytest

from backend.services.shared.config import validate_search_settings
from backend.services.shared.errors import QuantroError
from backend.services.shared.post_identity import post_identity

search = importlib.import_module("backend.services.external_apis.search_candidates")
QUERY = "the bridge remains closed until spring"


def metadata(url, text=QUERY):
    return {
        "post_url": url,
        "title": None,
        "caption": text,
        "published_at": None,
        "author": {"display_name": None},
    }


class Provider:
    def __init__(self, rows=(), error=None):
        self.rows = list(rows)
        self.error = error
        self.calls = []
        self.closed = False
        self.warnings = []

    def find_posts(self, queries, limit):
        self.calls.append((queries, limit))
        if self.error:
            raise self.error
        return self.rows[:limit]

    def close(self):
        self.closed = True


def test_local_miss_falls_back_and_enforces_exact_candidate_shape():
    local = Provider()
    web = Provider([(metadata("https://youtu.be/source"), [QUERY])])
    result = search.search_candidates(
        [QUERY], 5, provider=search.HybridSearch(local, lambda: web)
    )
    assert local.calls and web.calls
    assert set(result) == {"candidates", "warnings"}
    candidate = result["candidates"][0]
    assert set(candidate) == {
        "candidate_id",
        "url",
        "title",
        "publisher",
        "published_at",
        "snippet",
        "discovered_by_queries",
    }
    assert candidate["title"] is None and candidate["publisher"] is None
    assert candidate["discovered_by_queries"] == [QUERY]


def test_canonical_deduplication_and_self_exclusion_precede_limit():
    local = Provider(
        [
            (metadata("https://youtu.be/submitted"), [QUERY]),
            (metadata("https://youtu.be/source"), [QUERY]),
            (metadata("https://youtube.com/watch?v=source&tracking=1"), [QUERY]),
        ]
    )
    web = Provider(
        [
            (metadata("https://youtube.com/shorts/submitted"), [QUERY]),
            (metadata("https://youtube.com/watch?v=other"), [QUERY]),
        ]
    )
    original = copy.deepcopy((local.rows, web.rows))
    result = search.search_candidates(
        [QUERY],
        2,
        provider=search.HybridSearch(
            local, lambda: web, excluded_identities=frozenset({"youtube:submitted"})
        ),
    )
    assert [post_identity(c["url"]) for c in result["candidates"]] == [
        "youtube:source",
        "youtube:other",
    ]
    assert (local.rows, web.rows) == original


def test_full_local_budget_still_reserves_a_web_candidate():
    local = Provider(
        [(metadata(f"https://youtu.be/local{i}"), [QUERY]) for i in range(5)]
    )
    web = Provider([(metadata("https://youtu.be/websource"), [QUERY])])
    result = search.search_candidates(
        [QUERY], 5, provider=search.HybridSearch(local, lambda: web)
    )
    assert len(result["candidates"]) == 5
    assert result["candidates"][-1]["url"] == "https://youtu.be/websource"


def test_limit_one_local_hit_skips_lazy_web_factory():
    local = Provider([(metadata("https://youtu.be/source"), [QUERY])])

    def unexpected():
        pytest.fail("A web client should not be created without a free candidate slot")

    result = search.search_candidates(
        [QUERY], 1, provider=search.HybridSearch(local, unexpected)
    )
    assert len(result["candidates"]) == 1
    assert any("no web discovery slot" in warning for warning in result["warnings"])


@pytest.mark.parametrize("fail_local", [True, False])
def test_provider_failure_is_search_failed_even_with_local_candidates(fail_local):
    failure = QuantroError("SEARCH_FAILED", "search_candidates", retryable=True)
    local = Provider(
        [(metadata("https://youtu.be/local"), [QUERY])], failure if fail_local else None
    )
    web = Provider(error=None if fail_local else failure)
    with pytest.raises(QuantroError) as caught:
        search.search_candidates(
            [QUERY], 5, provider=search.HybridSearch(local, lambda: web)
        )
    assert caught.value.code.value == "SEARCH_FAILED"
    assert caught.value.retryable is True


def test_successful_empty_providers_remain_empty_success():
    result = search.search_candidates(
        [QUERY], 5, provider=search.HybridSearch(Provider(), Provider)
    )
    assert result["candidates"] == []


def test_scoped_store_is_borrowed_and_restored(monkeypatch):
    class Store:
        def find_transcript_posts(self, queries, limit, *, excluded_identities):
            assert set(excluded_identities) == {"youtube:submitted"}
            return [(metadata("https://youtu.be/local"), queries)]

        def close(self):
            pytest.fail("The job owns this store")

    created = []

    class Exa(Provider):
        def __init__(self, key, **kwargs):
            super().__init__()
            created.append(self)

    monkeypatch.setattr(
        search,
        "_environment",
        lambda: {"SEARCH_PROVIDER": "hybrid", "EXA_API_KEY": "test"},
    )
    monkeypatch.setattr(search, "ExaSearch", Exa)
    with search.search_scope(Store(), {"youtube:submitted"}):
        result = search.search_candidates([QUERY], 5)
    assert result["candidates"][0]["url"] == "https://youtu.be/local"
    assert search._search_context.get() == (None, frozenset())
    assert created[0].closed


def test_hybrid_configuration_requires_exa_key():
    with pytest.raises(RuntimeError, match="credential"):
        validate_search_settings({"SEARCH_PROVIDER": "hybrid"})
    validate_search_settings({"SEARCH_PROVIDER": "hybrid", "EXA_API_KEY": "test"})


def test_private_candidate_cannot_escape_hybrid_boundary():
    local = Provider(
        [
            (metadata("http://127.0.0.1/private"), [QUERY]),
            (metadata("https://youtu.be/valid"), [QUERY]),
        ]
    )
    result = search.search_candidates(
        [QUERY], 5, provider=search.HybridSearch(local, Provider)
    )
    assert [c["url"] for c in result["candidates"]] == ["https://youtu.be/valid"]
    assert any("discarded" in warning for warning in result["warnings"])
