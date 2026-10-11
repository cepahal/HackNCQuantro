import copy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from backend.services.storage.tiger import TigerStore

SIG = 'a' * 64
HASH = 'b' * 64


@pytest.fixture
def store(tmp_path):
    instance = TigerStore(f"sqlite:///{tmp_path / 'cache.db'}")
    instance.ensure_schema()
    yield instance
    instance.close()


def sample(age=0):
    timestamp = (datetime.now(UTC) - timedelta(seconds=age)).isoformat().replace('+00:00', 'Z')
    return ({'post_id': 'p1', 'post_url': 'https://youtu.be/example', 'platform': 'youtube',
             'author': {'followers_count': None}, 'engagement': {},
             'media': {'media_id': 'm1', 'local_path': '/private/cache.mp4', 'mime_type': 'video/mp4'},
             'retrieval': {'retrieved_at': timestamp, 'provider': 'fixture'}},
            {'media_id': 'm1', 'duration_seconds': 10.0,
             'transcript': {'status': 'complete', 'sentences': [
                 {'sentence_id': 's1', 'start_seconds': 1.0, 'end_seconds': 4.0,
                  'text': 'public education funding'}]}})


def count(store):
    with store._engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(store.source_features)).scalar_one()


def test_roundtrip_alias_nulls_and_transactional_passages(store):
    post, features = sample()
    expected = copy.deepcopy((post, features))
    store.cache_source_features(post, features, SIG, HASH)
    cached = store.get_cached_source('https://www.youtube.com/watch?v=example', SIG)
    assert cached['post_json'] == post and cached['features_json'] == features
    assert cached['retrieved_at'] == post['retrieval']['retrieved_at']
    assert cached['post_json']['author']['followers_count'] is None
    assert (post, features) == expected and cached['media_sha256'] == HASH
    assert store.find_transcript_posts(['education funding'], 1)


def test_content_and_model_keys_are_distinct_idempotent_and_immutable(store):
    post, features = sample()
    store.cache_source_features(post, features, SIG, HASH)
    changed = copy.deepcopy(post)
    changed['title'] = 'Should not replace immutable prior snapshot'
    store.cache_source_features(changed, features, SIG, HASH)
    assert count(store) == 1
    assert 'title' not in store.get_cached_source(post['post_url'], SIG)['post_json']
    store.cache_source_features(post, features, 'c' * 64, HASH)
    store.cache_source_features(post, features, SIG, 'd' * 64)
    assert count(store) == 3
    assert store.get_cached_source(post['post_url'], 'e' * 64) is None
    assert store.get_cached_source('https://youtu.be/unknown', SIG) is None


def test_ttl_uses_original_retrieval_not_reinsert_time(store):
    post, features = sample(age=3700)
    store.cache_source_features(post, features, SIG, HASH)
    assert store.get_cached_source(post['post_url'], SIG) is None
    assert store.get_cached_source(post['post_url'], SIG, 4000) is not None
    store.cache_source_features(post, features, SIG, HASH)
    assert store.get_cached_source(post['post_url'], SIG) is None


def test_invalid_ids_hashes_and_timing_roll_back_cache(store):
    post, features = sample()
    mismatched = copy.deepcopy(features)
    mismatched['media_id'] = 'other'
    with pytest.raises(ValueError, match='media_id'):
        store.cache_source_features(post, mismatched, SIG, HASH)
    for signature, digest in [('not-a-hash', HASH), (SIG, 'no')]:
        with pytest.raises(ValueError, match='hexadecimal'):
            store.cache_source_features(post, features, signature, digest)
    bad = copy.deepcopy(features)
    bad['transcript']['sentences'][0]['end_seconds'] = 11.0
    with pytest.raises(ValueError, match='observed media'):
        store.cache_source_features(post, bad, SIG, HASH)
    assert count(store) == 0
    with pytest.raises(ValueError, match='TTL'):
        store.get_cached_source(post['post_url'], SIG, 0)
