"""Independent offline checks of hybrid discovery boundaries."""

import importlib
import io
import json
from datetime import UTC, datetime, timedelta

import pytest
import test_run_analysis as pipeline_fixtures
from test_run_analysis import (
    CLIP_FEATURES,
    CLIP_URL,
    SHARED,
    _features,
    _post,
    _source_features,
)

from backend.scripts.import_source_archive import import_archive
from backend.services.external_apis.search_candidates import (
    HybridSearch,
    search_candidates,
    search_scope,
)
from backend.services.shared.errors import QuantroError
from backend.services.shared.types import MediaFeatures
from backend.services.storage.tiger import TigerStore

client = pipeline_fixtures.client
store = pipeline_fixtures.store
upstream = pipeline_fixtures.upstream


def row(url, query="bridge"):
    return ({"post_url": url, "title": "Source", "caption": None,
             "published_at": None, "author": {}}, [query])


class Rows:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def find_posts(self, queries, limit):
        self.calls.append((queries, limit))
        return self.rows[:limit]


def test_hybrid_canonical_dedupe_and_self_exclusion_before_limit():
    local = Rows([row("https://youtu.be/self"), row("https://youtu.be/source"),
                  row("https://www.youtube.com/watch?v=source"), row("https://youtu.be/other")])
    web = Rows([row("https://youtu.be/self"), row("https://youtu.be/web")])
    provider = HybridSearch(local, lambda: web, excluded_identities=frozenset({"youtube:self"}))
    result = search_candidates(["bridge"], 3, provider=provider)
    assert [c["url"] for c in result["candidates"]] == [
        "https://youtu.be/source", "https://youtu.be/other", "https://youtu.be/web"]
    assert local.calls[0][1] == web.calls[0][1] == 12
    assert any("unverified" in warning for warning in result["warnings"])


def test_hybrid_web_failure_is_search_failed_even_with_local_results():
    class Failed:
        def find_posts(self, queries, limit):
            raise ConnectionError("private provider diagnostics")
    provider = HybridSearch(Rows([row("https://youtu.be/local")]), Failed)
    with pytest.raises(QuantroError) as caught:
        search_candidates(["bridge"], 3, provider=provider)
    assert caught.value.code == "SEARCH_FAILED"
    assert "private provider diagnostics" not in str(caught.value)


def test_hybrid_ignores_unsafe_discovery_urls():
    provider = HybridSearch(Rows([row("http://127.0.0.1/private"),
                                 row("https://youtu.be/local")]),
                            lambda: Rows([row("https://youtu.be/web")]))
    result = search_candidates(["bridge"], 2, provider=provider)
    assert [c["url"] for c in result["candidates"]] == [
        "https://youtu.be/local", "https://youtu.be/web"]


def test_hybrid_selector_borrows_job_store_and_restores_context(tmp_path, monkeypatch):
    module = importlib.import_module("backend.services.external_apis.search_candidates")
    store = TigerStore(f"sqlite:///{tmp_path / 'selector.db'}")
    store.ensure_schema()
    web = Rows([row("https://youtu.be/web")])
    real_exa = module.ExaSearch

    class OfflineExa(real_exa):
        def __init__(self, key, **kwargs):
            self.warnings = []
            assert kwargs["excluded_identities"] == frozenset({"youtube:self"})

        def find_posts(self, queries, limit):
            return web.find_posts(queries, limit)

        def close(self):
            pass

    monkeypatch.setattr(module, "_environment", lambda: {"SEARCH_PROVIDER": "hybrid", "EXA_API_KEY": "test-only"})
    monkeypatch.setattr(module, "ExaSearch", OfflineExa)
    monkeypatch.setattr(TigerStore, "from_env", lambda: pytest.fail("Must borrow the job store"))
    try:
        with search_scope(store, {"youtube:self"}):
            result = search_candidates(["bridge"], 2)
            assert result["candidates"][0]["url"] == "https://youtu.be/web"
        assert module._search_context.get() == (None, frozenset())
        assert store.list_recent_jobs() == []
    finally:
        store.close()


def test_sqlite_import_search_exclusion_dedupe_and_untimed_evidence(tmp_path):
    store = TigerStore(f"sqlite:///{tmp_path / 'archive.db'}")
    store.ensure_schema()
    records = [
        {"version": 1, "source_url": url, "text": "The bridge will close next spring.",
         "metadata": {"title": "Speech"}, "provenance": {"rights_status": "test fixture"}}
        for url in ("https://youtu.be/self", "https://youtu.be/source",
                    "https://www.youtube.com/watch?v=source", "https://youtu.be/zother")
    ]
    try:
        counts = import_archive(io.BytesIO(b"".join(json.dumps(r).encode() + b"\n" for r in records)),
                                store, io.StringIO())
        assert counts["failed"] == 0
        results = store.find_transcript_posts(["bridge"], 2, excluded_identities=["youtube:self"])
        assert len(results) == 2
        assert {r[0]["post_url"] for r in results} == {"https://youtu.be/source", "https://youtu.be/zother"}
        with store._engine.connect() as connection:
            passages = connection.execute(store.source_passages.select()).mappings().all()
        assert passages
        assert all(p["start_seconds"] is None and p["end_seconds"] is None and
                   p["timing_basis"] == "unavailable" for p in passages)
        assert store.list_recent_jobs() == []
    finally:
        store.close()


def test_real_hybrid_discovery_does_not_replace_independent_alignment(client, store, upstream, monkeypatch):
    analysis = importlib.import_module("backend.services.reporting.run_analysis")
    search = importlib.import_module("backend.services.external_apis.search_candidates")
    archive_record = {"version": 1, "source_url": "https://youtu.be/src1", "text": SHARED,
                      "metadata": {"title": "Local text hit"}, "provenance": {"rights_status": "test fixture"}}
    counts = import_archive(io.BytesIO((json.dumps(archive_record) + "\n").encode()), store, io.StringIO())
    assert counts["inserted"] == 1
    monkeypatch.setattr(search, "_environment", lambda: {"SEARCH_PROVIDER": "hybrid", "EXA_API_KEY": "test"})
    monkeypatch.setattr(search.ExaSearch, "find_posts", lambda self, queries, limit: [])
    monkeypatch.setattr(analysis, "search_candidates", search.search_candidates)
    extracted = []

    def independent_features(media, options):
        media_id = media.media_id if hasattr(media, "media_id") else media["media_id"]
        extracted.append(media_id)
        return _features(media_id, 120.0, [(1.0, 8.0, "A completely unrelated source explains ocean currents and sailing equipment.")])

    monkeypatch.setattr(analysis, "extract_media_features", independent_features)
    response = client.post("/analyze/job_hybrid_unverified/run", json={"url": CLIP_URL})
    assert response.status_code == 200
    assert response.json()["outcome"] == "no_verified_match"
    assert len(extracted) == 1
    assert upstream["compare_calls"] == []


def test_cached_source_skips_download_and_asr_but_reruns_alignment(client, store, upstream, monkeypatch):
    analysis = importlib.import_module("backend.services.reporting.run_analysis")
    post = _post("cached_source", "https://www.youtube.com/watch?v=src1", candidate_id="old_candidate")
    acquired = (datetime.now(UTC) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    post["retrieval"]["retrieved_at"] = acquired
    options = {**analysis.EXTRACTION_OPTIONS, "extract_frames": False,
               "extract_visual_text": False, "max_frames": 0}
    features = _source_features("cached_source")
    store.cache_source_features(post, features, analysis.extraction_signature(options), "b" * 64)
    monkeypatch.setattr(analysis, "retrieve_candidate_media", lambda *a, **k: pytest.fail("Cache hit must skip download"))
    monkeypatch.setattr(analysis, "extract_media_features", lambda *a, **k: pytest.fail("Cache hit must skip ASR"))
    real_align = analysis.verify_and_align
    calls = []

    def observed_align(clip, source):
        calls.append(source)
        return real_align(clip, source)

    monkeypatch.setattr(analysis, "verify_and_align", observed_align)
    response = client.post("/analyze/job_cached_source/run", json={"url": CLIP_URL})
    assert response.status_code == 200
    assert response.json()["outcome"] == "matched"
    assert len(calls) == 1
    report = client.get("/reports/report_job_cached_source").json()
    assert report["source"]["post"]["candidate_id"] == "cand_1"
    assert report["source"]["post"]["retrieval"]["retrieved_at"] == acquired
    assert any(acquired in text and "not re-downloaded" in text for text in report["limitations"])
    cached = store.get_cached_source(post["post_url"], analysis.extraction_signature(options))
    assert cached["post_json"]["candidate_id"] == "old_candidate"
    assert cached["features_json"] == features


def test_cached_resolved_self_match_is_rejected_before_alignment(store, monkeypatch):
    analysis = importlib.import_module("backend.services.reporting.run_analysis")
    post = _post("self_snapshot", "https://www.youtube.com/watch?v=src1")
    post["retrieval"]["retrieved_at"] = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    post["retrieval"]["metadata_source_urls"] = [CLIP_URL]
    options = {**analysis.EXTRACTION_OPTIONS, "extract_frames": False,
               "extract_visual_text": False, "max_frames": 0}
    store.cache_source_features(post, _source_features("self_snapshot"),
                                analysis.extraction_signature(options), "c" * 64)
    monkeypatch.setattr(analysis, "retrieve_candidate_media", lambda *a, **k: pytest.fail("Should use cache"))
    monkeypatch.setattr(analysis, "verify_and_align", lambda *a, **k: pytest.fail("Self match must be rejected first"))
    with pytest.raises(QuantroError, match="cannot be its own source"):
        analysis._assess_candidate({"url": post["post_url"], "candidate_id": "new"},
                                   MediaFeatures.model_validate(CLIP_FEATURES), {"youtube:clip"}, store=store)
