import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from backend.services.storage.tiger import TigerStore


@pytest.fixture
def store(tmp_path):
    result=TigerStore(f"sqlite:///{tmp_path/'transcripts.db'}")
    result.ensure_schema()
    yield result
    result.close()


def document(store,url,text,**extra):
    return store.upsert_transcript_document(source_url=url,text=text,metadata=extra,
        provenance={'sha256':hashlib.sha256(text.encode()).hexdigest(),
                    'rights_status':'test fixture','transcript_source_url':url,
                    'acquired_at':'2026-10-10T12:00:00Z'})


def features():
    post={'post_id':'p1','post_url':'https://youtu.be/timed','platform':'youtube',
          'author':{},'engagement':{},'media':{'media_id':'m1','local_path':'/private/media.mp4','mime_type':'video/mp4'},
          'retrieval':{'retrieved_at':'2026-10-10T12:00:00Z','provider':'fixture'}}
    data={'media_id':'m1','duration_seconds':12.0,'transcript':{'status':'partial','sentences':[
        {'sentence_id':'s1','start_seconds':2.0,'end_seconds':5.0,'text':'climate policy clean energy'},
        {'sentence_id':'s2','start_seconds':7.0,'end_seconds':11.0,'text':'public service funding'}]}}
    return post,data


def test_document_immutable_chunks_null_timing_and_idempotence(store):
    text='budget education '+('long archive phrase '*250)
    assert document(store,'https://example.com/speech',text,title='Budget')
    assert not document(store,'https://example.com/speech',text,title='Budget')
    with store._engine.connect() as connection:
        rows=connection.execute(select(store.source_passages)).mappings().all()
    assert rows[0]['passage_text']==text[:2000]
    assert rows[1]['passage_text'].startswith(text[1750:1800])
    assert all(r['passage_text'] in text for r in rows)
    assert rows[-1]['passage_text'].endswith(text[-100:])
    assert all(r['start_seconds'] is None and r['end_seconds'] is None for r in rows)
    assert all(r['timing_basis']=='unavailable' and len(r['passage_text'])<=2000 for r in rows)
    assert rows[0]['provenance']['rights_status']=='test fixture'


def test_autoindex_preserves_post_features_and_local_paths_stay_internal(store):
    post,data=features();original=copy.deepcopy((post,data))
    store.upsert_job('job1',post['post_url'],'running')
    store.save_post_and_features('job1',post,data)
    job=store.get_job('job1')
    assert job['post_json']==post and job['features_json']==data and (post,data)==original
    rows=store.find_transcript_posts(['climate policy'],5)
    assert len(rows)==1 and rows[0][1]==['climate policy']
    with store._engine.connect() as connection:
        passages=connection.execute(select(store.source_passages)).mappings().all()
    assert len(passages)==1 and passages[0]['start_seconds']==2.0 and passages[0]['end_seconds']==11.0
    assert passages[0]['provenance']['index_revision']=='timed-v2'
    assert store.find_transcript_posts(['"energy public service"'],1)
    assert '/private/' not in json.dumps([dict(x) for x in passages],default=str)
    assert 'local_path' not in json.dumps(rows)


def test_missing_transcript_legacy_fixture_still_saves(store):
    store.upsert_job('legacy','https://example.com','running')
    store.save_post_and_features('legacy',{'post_id':'old'},{'media_id':'m'})
    assert store.get_job('legacy')['features_json']=={'media_id':'m'}
    assert store.find_transcript_posts(['old'],1)==[]


def test_legacy_complete_fixture_and_boundary_phrase(store):
    store.upsert_job('legacy_complete','https://example.com','running')
    store.save_post_and_features('legacy_complete',{'post_id':'old'},
                                {'transcript':{'status':'complete','sentences':[]}})
    assert store.get_job('legacy_complete')['post_json']=={'post_id':'old'}
    text=('x '*990)+'critical boundary phrase '+('y '*1000)
    document(store,'https://example.com/boundary',text)
    assert len(store.find_transcript_posts(['critical boundary phrase'],1))==1
    assert store.find_transcript_posts(['critical impossible'],1)==[]


def test_bulk_batches_partial_replay_and_publisher_provenance(store):
    inserts=[]
    def before_execute(conn,cursor,statement,parameters,context,executemany):
        if statement.startswith('INSERT INTO quantro_source_passages'):
            inserts.append(statement)
    event.listen(store._engine,'before_cursor_execute',before_execute)
    payload={"source_url": 'https://example.com/bulk',"metadata": {'publisher':'Public Archive'},
                 "provenance": {'sha256':'a'*64,'event_date':'2010-01-01','publication_date':None},
                 "passages": [(f'passage {i}',None,None) for i in range(121)],"timing_basis": 'unavailable'}
    with store._engine.begin() as connection:
        assert store._write_passages(connection,**payload)
    assert len(inserts)==3
    with store._engine.begin() as connection:
        assert not store._write_passages(connection,**payload)
        assert connection.execute(select(func.count()).select_from(store.source_passages)).scalar_one()==121
        row=connection.execute(select(store.source_passages)).mappings().first()
    assert len(inserts)==6
    assert row['post_metadata']['author']['display_name']=='Public Archive'
    assert row['provenance']['event_date']=='2010-01-01' and row['provenance']['publication_date'] is None
    with pytest.raises(TypeError,match='publisher'):
        document(store,'https://example.com/bad','speech',publisher=123)


def test_dedupe_and_self_exclusion_before_limit(store):
    document(store,'https://youtu.be/self','public budget '*200)
    document(store,'https://www.youtube.com/watch?v=self','public budget different wording')
    document(store,'https://youtu.be/other','public budget alternative')
    rows=store.find_transcript_posts(['public','budget'],1,excluded_identities=('youtube:self',))
    assert len(rows)==1 and rows[0][0]['post_url']=='https://youtu.be/other'
    assert rows[0][1]==['public','budget']


def test_bad_hash_and_fabricated_clock_rejected(store):
    with pytest.raises(ValueError,match='SHA256'):
        store.upsert_transcript_document(source_url='https://example.com',text='speech',metadata={},provenance={'sha256':'wrong'})
    with pytest.raises(ValueError,match='timing_basis'):
        store.upsert_transcript_document(source_url='https://example.com',text='speech',metadata={},provenance={},timing_basis='estimated')
    post,data=features();data['transcript']['sentences'][0]['end_seconds']=13.0
    with pytest.raises(ValueError,match='observed media duration'):store.upsert_transcript_source(post,data)


def test_autoindex_failure_rolls_back_post_write(store):
    post,data=features();store.upsert_job('rollback',post['post_url'],'running')
    data['transcript']['sentences'][0]['end_seconds']=float('inf')
    with pytest.raises(ValueError):store.save_post_and_features('rollback',post,data)
    assert store.get_job('rollback')['post_json'] is None


def test_query_binding_and_postgres_gin_compilation(store):
    document(store,'https://example.com','public budget')
    assert store.find_transcript_posts(["x'; DROP TABLE quantro_posts; --"],1)==[]
    original=store._engine
    try:
        store._engine=SimpleNamespace(dialect=postgresql.dialect())
        statement,params=store._transcript_search_statement(['quoted query'],5,('youtube:self',))
        compiled=str(statement.compile(dialect=postgresql.dialect()))
        assert 'quoted query' not in compiled and params['queries']==['quoted query']
        assert 'source_identity != ALL' in compiled and 'GROUP BY source_identity' in compiled
        index=next(i for i in store.source_passages.indexes if i.name.endswith('passage_fts_idx'))
        ddl=str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        assert 'USING gin' in ddl and "to_tsvector('simple', passage_text)" in ddl
    finally:store._engine=original


def test_publisher_link_requires_explicit_provenance(store):
    text='recorded transcript';digest=hashlib.sha256(text.encode()).hexdigest()
    store.upsert_transcript_document(source_url='https://example.com/transcript',text=text,
        metadata={'recording_url':'https://youtu.be/recording'},provenance={'sha256':digest,'recording_link_basis':'publisher_link'})
    assert store.find_transcript_posts(['recorded'],1)[0][0]['post_url']=='https://youtu.be/recording'
