import json

import httpx
import pytest

from benchmarks.run import canonical, fuse, search, search_one, target_rank, word_error


def test_word_error_counts_edits_and_ignores_punctuation():
    assert word_error('Hello, WORLD!', 'hello world')['wer'] == 0
    assert word_error('one two three', 'one four')['edit_distance'] == 2
    assert word_error('', 'hallucination')['wer'] is None


def test_url_identity_preserves_video_identifiers():
    assert canonical('https://example.org/watch?v=1&utm_source=test#x') == 'https://example.org/watch?v=1'
    assert canonical('https://example.org/watch?v=1') != canonical('https://example.org/watch?v=2')
    with pytest.raises(ValueError):
        canonical('file:///private/file')


def test_fusion_deduplicates_without_domain_level_false_hits():
    a = {'url': 'https://example.org/watch?v=1'}
    b = {'url': 'https://example.org/watch?v=2'}
    c = {'url': 'https://elsewhere.org/video'}
    assert fuse([[a, b], [a, c]]) == [a, b, c]
    assert target_rank([b], [a['url']]) is None
    assert target_rank([b, a], [a['url']]) == 2


@pytest.mark.parametrize('provider,payload', [
    ('exa', {'results': [{'url': 'https://example.org/a', 'title': 'Title'}], 'costDollars': {'total': .007}}),
    ('serpapi', {'organic_results': [{'link': 'https://example.org/a', 'title': 'Title'}]})])
def test_search_adapters_use_provider_contract(provider, payload):
    def handler(request):
        if provider == 'exa':
            assert request.headers['x-api-key'] == 'fake-key'
            assert json.loads(request.content)['numResults'] == 5
        else:
            assert request.url.params['api_key'] == 'fake-key'
            assert request.url.params['engine'] == 'google'
        return httpx.Response(200, json=payload)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows, cost = search_one(provider, 'spoken phrase', 'fake-key', client)
    assert rows[0]['url'] == 'https://example.org/a'
    assert rows[0]['query_rank'] == 1


def test_provider_error_is_not_an_empty_success():
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'error': 'fake'}))) as client:
        with pytest.raises(ValueError):
            search_one('serpapi', 'query', 'fake-key', client)


def test_missing_key_is_recorded_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr('benchmarks.run.load_settings', lambda: None)
    monkeypatch.delenv('EXA_API_KEY', raising=False)
    monkeypatch.setattr('benchmarks.run.search_one', lambda *a: pytest.fail('Unexpected network request'))
    (tmp_path / 'asr_a_clean_whisper.json').write_text(json.dumps({
        'status': 'ok', 'case': 'a_clean', 'queries': ['phrase']}))
    search({'sources': [{'id': 'a', 'group': 'a', 'split': 'dev', 'accepted_urls': []}]}, tmp_path, 'whisper', ['exa'])
    result = json.loads((tmp_path / 'search_a_clean_whisper_exa.json').read_text())
    assert result['status'] == 'missing_key'
    assert result['requests_attempted'] == 0
    assert 'exact_target_top5' not in result
