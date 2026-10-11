"""A miss can widen hybrid discovery once without repeating source downloads."""

import importlib

import test_run_analysis as fixtures

from backend.services.shared.errors import QuantroError

client = fixtures.client
store = fixtures.store
upstream = fixtures.upstream


def setup_expansion(monkeypatch):
    module = importlib.import_module("backend.services.reporting.run_analysis")
    monkeypatch.setenv("SEARCH_PROVIDER", "hybrid")
    monkeypatch.setenv("EXA_API_KEY", "offline-test-not-a-credential")
    monkeypatch.setattr(module, "build_search_queries", lambda *args: {
        "queries": ['"' + fixtures.SHARED + '"'], "warnings": [],
    })
    return module


def test_initial_miss_widens_and_verifies_second_pass(client, store, upstream, monkeypatch):
    module = setup_expansion(monkeypatch)
    calls = []

    def search(queries, limit):
        calls.append((queries.queries, limit))
        return {"candidates": [] if len(calls) == 1 else [fixtures._candidate(1)], "warnings": []}

    monkeypatch.setattr(module, "search_candidates", search)
    response = client.post("/analyze/job_expanded/run", json={"url": fixtures.CLIP_URL})
    assert response.status_code == 200
    assert response.json()["outcome"] == "matched"
    assert calls == [(['"' + fixtures.SHARED + '"'], 5), ([fixtures.SHARED], 2)]
    assert any("bounded second search" in warning for warning in response.json()["warnings"])


def test_successful_first_pass_does_not_spend_second_search(client, store, upstream, monkeypatch):
    module = setup_expansion(monkeypatch)
    calls = []

    def search(queries, limit):
        calls.append(limit)
        return {"candidates": [fixtures._candidate(1)], "warnings": []}

    monkeypatch.setattr(module, "search_candidates", search)
    assert client.post("/analyze/job_first/run", json={"url": fixtures.CLIP_URL}).json()["outcome"] == "matched"
    assert calls == [5]


def test_second_pass_provider_failure_is_error_not_no_match(client, store, upstream, monkeypatch):
    module = setup_expansion(monkeypatch)
    calls = []

    def search(queries, limit):
        calls.append(limit)
        if len(calls) == 2:
            raise QuantroError("SEARCH_FAILED", "search_candidates", retryable=True)
        return {"candidates": [], "warnings": []}

    monkeypatch.setattr(module, "search_candidates", search)
    response = client.post("/analyze/job_expansion_failure/run", json={"url": fixtures.CLIP_URL})
    assert response.status_code >= 400
    assert response.json()["error"]["code"] == "SEARCH_FAILED"
    assert calls == [5, 2]
    assert store.get_job("job_expansion_failure")["status"] == "failed"
    assert store.get_job_reports("job_expansion_failure") == []


def test_repeated_candidate_in_expanded_response_is_not_retrieved_twice(client, store, upstream, monkeypatch):
    module = setup_expansion(monkeypatch)
    upstream["failing_candidates"] = {"cand_1"}
    search_calls = []
    retrievals = []
    old_retrieve = module.retrieve_candidate_media

    def search(queries, limit):
        search_calls.append(limit)
        candidates = [fixtures._candidate(1)]
        if len(search_calls) == 2:
            candidates.append(fixtures._candidate(2))
        return {"candidates": candidates, "warnings": []}

    def retrieve(candidate):
        retrievals.append(candidate["candidate_id"])
        return old_retrieve(candidate)

    monkeypatch.setattr(module, "search_candidates", search)
    monkeypatch.setattr(module, "retrieve_candidate_media", retrieve)
    response = client.post("/analyze/job_no_repeat/run", json={"url": fixtures.CLIP_URL})
    assert response.status_code == 200
    assert response.json()["outcome"] == "matched"
    assert retrievals == ["cand_1", "cand_2"]


def test_resolved_recording_alias_is_excluded_on_wider_search(client, store, upstream, monkeypatch):
    module = setup_expansion(monkeypatch)
    calls, retrievals = [], []
    original_retrieve = module.retrieve_candidate_media
    original_extract = module.extract_media_features

    def search(queries, limit):
        calls.append(limit)
        return {"candidates": [fixtures._candidate(1)] if len(calls) == 1 else
                [fixtures._candidate(2), fixtures._candidate(3)], "warnings": []}

    def retrieve(candidate):
        retrievals.append(candidate["candidate_id"])
        post = original_retrieve(candidate)
        if candidate["candidate_id"] == "cand_1":
            post["retrieval"]["metadata_source_urls"].append(fixtures._candidate(2)["url"])
        return post

    def extract(media, options):
        if media.media_id == "src_media_1":
            return fixtures._features("src_media_1", 120.0, [(0.0, 5.0, "An unrelated opening about coastal erosion and rainfall.")])
        return original_extract(media, options)

    monkeypatch.setattr(module, "search_candidates", search)
    monkeypatch.setattr(module, "retrieve_candidate_media", retrieve)
    monkeypatch.setattr(module, "extract_media_features", extract)
    response = client.post("/analyze/job_redirect_dedupe/run", json={"url": fixtures.CLIP_URL})
    assert response.status_code == 200
    assert response.json()["outcome"] == "matched"
    assert retrievals == ["cand_1", "cand_3"]
