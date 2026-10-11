import httpx
import pytest

from backend.services.external_apis.exa_search import ExaSearch
from backend.services.external_apis.retrieve_post import _validate_url
from backend.services.external_apis.search_candidates import search_candidates
from backend.services.shared.errors import QuantroError


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_exa_rank_fusion_and_provenance_with_webpages():
    def handler(request):
        import json
        q = json.loads(request.content)['query']
        assert request.headers['x-api-key'] == 'test-key'
        rows = [{'url': 'https://nasa.gov/speech', 'title': 'Speech', 'publishedDate': '2026-10-10T10:00:00Z'}]
        rows += [{'url': 'https://example.org/' + q}]
        return httpx.Response(200, json={'results': rows})
    provider = ExaSearch('test-key', client=client(handler))
    result = search_candidates(['first', 'second'], 5, provider=provider)
    assert [r['url'] for r in result['candidates']] == ['https://nasa.gov/speech', 'https://example.org/first', 'https://example.org/second']
    assert result['candidates'][0]['discovered_by_queries'] == ['first', 'second']
    assert result['candidates'][0]['published_at'] == '2026-10-10T10:00:00Z'
    provider.close()


def test_production_selector_uses_exa_without_tiger(monkeypatch):
    import importlib
    module = importlib.import_module('backend.services.external_apis.search_candidates')
    monkeypatch.setattr(module, '_environment', lambda: {'SEARCH_PROVIDER': 'exa', 'EXA_API_KEY': 'test-key'})
    monkeypatch.setattr(module.TigerDataPostStore, 'from_env', lambda: pytest.fail('Must not use corpus'))
    monkeypatch.setattr(ExaSearch, 'find_posts', lambda self,q,n: [])
    result = search_candidates(['words'], 5)
    assert result['candidates'] == []


@pytest.mark.parametrize('status,retryable', [(401,False),(402,False),(429,True),(503,True)])
def test_errors_are_redacted_and_not_no_match(status, retryable):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={'error': 'private-test-key'})
    p=ExaSearch('private-test-key', client=client(handler))
    with pytest.raises(QuantroError) as caught:
        search_candidates(['q1','q2'], 5, provider=p)
    assert caught.value.retryable is retryable
    assert 'private-test-key' not in str(caught.value)
    assert len(calls)==3
    p.close()


def test_partial_failure_warning_and_bounded_queries():
    calls=[]
    def handler(request):
        calls.append(request)
        if len(calls)==2: return httpx.Response(503)
        return httpx.Response(200,json={'results': []})
    p=ExaSearch('test',client=client(handler))
    with pytest.raises(QuantroError):
        p.find_posts(['a','b','c','d','e'],5)
    assert len(calls)==4
    assert any('first three' in w for w in p.warnings)
    p.close()


@pytest.mark.parametrize('url',['http://127.0.0.1/a','http://localhost/a','file:///tmp/a','https://user:pass@example.com/a'])
def test_candidate_pages_keep_network_boundary(url):
    with pytest.raises(QuantroError): _validate_url(url,allow_webpage=True)


def test_webpage_mode_is_explicit_for_submitted_posts():
    assert _validate_url('https://nasa.gov/speech',allow_webpage=True)==('https://nasa.gov/speech','web')
    with pytest.raises(QuantroError): _validate_url('https://nasa.gov/speech')


def test_candidate_retrieval_enables_guarded_page_extraction(monkeypatch):
    import importlib
    module=importlib.import_module('backend.services.external_apis.retrieve_candidate_media')
    seen={}
    def retrieve(url,**kwargs):
        seen.update(kwargs)
        raise module.RetrievalError('MEDIA_UNAVAILABLE','No accessible video')
    monkeypatch.setattr(module,'_download_and_probe',retrieve)
    with pytest.raises(QuantroError) as error:
        module.retrieve_candidate_media({'candidate_id':'c','url':'https://nasa.gov/speech',
            'title':None,'publisher':None,'published_at':None,'snippet':None,'discovered_by_queries':['q']})
    assert seen['allow_webpage'] is True
    assert error.value.stage.value=='retrieve_candidate_media'


def test_recording_links_survive_candidate_limit_without_claiming_verification():
    def handler(request):
        return httpx.Response(200, json={"results": [
            {"url": "https://example.org/article", "title": "Related article"},
            {"url": "https://www.youtube.com/watch?v=source", "title": "Recording"},
            {"url": "https://example.org/full.mp4", "title": "Full recording"},
        ]})
    provider = ExaSearch("test", client=client(handler))
    result = search_candidates(["speech"], 3, provider=provider)
    assert [item["url"] for item in result["candidates"]] == [
        "https://www.youtube.com/watch?v=source", "https://example.org/full.mp4", "https://example.org/article"]
    assert all(item["discovered_by_queries"] == ["speech"] for item in result["candidates"])
    assert any("not verified" in warning for warning in result["warnings"])
    provider.close()


def test_direct_images_rank_after_direct_recordings_and_pages():
    def handler(request):
        return httpx.Response(200, json={"results": [
            {"url": "https://example.org/still.jpg"},
            {"url": "https://example.org/article"},
            {"url": "https://example.org/clip.mp4"},
        ]})
    provider = ExaSearch("test", client=client(handler))
    result = search_candidates(["speech"], 3, provider=provider)
    assert [item["url"] for item in result["candidates"]] == [
        "https://example.org/clip.mp4", "https://example.org/still.jpg", "https://example.org/article"]
    provider.close()


def test_missing_exa_key_is_logged_not_named_to_clients(monkeypatch, caplog):
    import importlib
    import logging
    module = importlib.import_module('backend.services.external_apis.search_candidates')
    monkeypatch.setattr(module, '_environment', lambda: {'SEARCH_PROVIDER': 'exa', 'EXA_API_KEY': ' '})
    with caplog.at_level(logging.ERROR), pytest.raises(QuantroError) as caught:
        search_candidates(['words'], 5)
    assert caught.value.code.value == 'SEARCH_FAILED'
    assert 'EXA_API_KEY' not in caught.value.message and 'EXA_API_KEY' in caplog.text
    with pytest.raises(QuantroError) as direct:
        ExaSearch('')
    assert 'EXA_API_KEY' not in direct.value.message


def test_video_host_search_adds_recordings_within_four_call_budget():
    import json
    requests = []
    def handler(request):
        body = json.loads(request.content); requests.append(body)
        url = "https://www.youtube.com/watch?v=full" if body.get("includeDomains") else "https://example.org/article"
        return httpx.Response(200, json={"results": [{"url": url}]})
    provider = ExaSearch("test", client=client(handler))
    result = search_candidates(["spoken phrase", "visible title", "post title", "extra speech"], 5, provider=provider)
    assert len(requests) == 4
    assert [body["includeDomains"] for body in requests if "includeDomains" in body] == [["youtube.com", "vimeo.com", "archive.org"]]
    assert {body["query"] for body in requests} == {"spoken phrase", "visible title", "post title"}
    assert result["candidates"][0]["url"] == "https://www.youtube.com/watch?v=full"
    assert result["candidates"][0]["discovered_by_queries"] == ["spoken phrase"]
    provider.close()


@pytest.mark.parametrize("status,retryable", [(402, False), (429, True), (503, True), (403, False)])
def test_empty_success_plus_failure_is_not_no_match(status, retryable):
    import json
    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(status if body.get("includeDomains") else 200, json={"results": []})
    provider = ExaSearch("test", client=client(handler))
    with pytest.raises(QuantroError) as error:
        provider.find_posts(["clue"], 5)
    assert error.value.code.value == "SEARCH_FAILED"
    assert error.value.retryable is retryable
    provider.close()


def test_successful_empty_is_valid_and_partial_hits_keep_warning():
    import json
    def handler(request):
        body = json.loads(request.content)
        if body.get("includeDomains"):
            return httpx.Response(503)
        return httpx.Response(200, json={"results": [{"url": "https://example.org/source"}]})
    provider = ExaSearch("test", client=client(handler))
    assert len(provider.find_posts(["clue"], 5)) == 1
    assert any("failed" in warning for warning in provider.warnings)
    provider.close()
    provider = ExaSearch("test", client=client(lambda request: httpx.Response(200, json={"results": []})))
    assert provider.find_posts(["clue"], 5) == []
    provider.close()


def test_canonical_exclusion_and_dedup_precede_final_limit():
    from backend.services.shared.post_identity import post_identity
    rows = [{"url": "https://www.youtube.com/watch?v=self"},
            {"url": "https://youtu.be/self"},
            {"url": "https://youtu.be/source"},
            {"url": "https://www.youtube.com/watch?v=source"},
            {"url": "https://example.org/other.mp4"}]
    provider = ExaSearch("test", excluded_identities={post_identity(rows[0]["url"])},
                         client=client(lambda request: httpx.Response(200, json={"results": rows})))
    found = provider.find_posts(["clue"], 2)
    assert len(found) == 2
    assert {post_identity(post["post_url"]) for post, queries in found} == {
        post_identity("https://youtu.be/source"), post_identity("https://example.org/other.mp4")}
    provider.close()


def test_concurrent_requests_merge_deterministically_and_close():
    import json
    import threading
    barrier = threading.Barrier(3)
    active = 0
    peak = 0
    lock = threading.Lock()
    def handler(request):
        nonlocal active, peak
        body = json.loads(request.content)
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=3)
        with lock:
            active -= 1
        return httpx.Response(200, json={"results": [{"url": "https://example.org/" + body["query"]}]})
    transport_client = client(handler)
    provider = ExaSearch("test", client=transport_client)
    for _ in range(2):
        assert [post["post_url"] for post, queries in provider.find_posts(["first", "second"], 5)] == [
            "https://example.org/first", "https://example.org/second"]
    assert peak == 3
    provider.close()
    assert transport_client.is_closed


def test_transport_failure_is_retryable_and_client_cleanup_is_explicit():
    def handler(request):
        raise httpx.ReadTimeout("private-provider-detail", request=request)
    transport_client = client(handler)
    provider = ExaSearch("test", client=transport_client)
    try:
        with pytest.raises(QuantroError) as error:
            provider.find_posts(["clue"], 5)
        assert error.value.retryable is True
        assert "private-provider-detail" not in str(error.value)
    finally:
        provider.close()
    assert transport_client.is_closed
