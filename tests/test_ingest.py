import pytest
from fastapi.testclient import TestClient

import backend.services.resolution.run as ingest_module
from backend.main import app
from backend.routes.ingest import get_tiger_store
from backend.services.external_apis.retrieve_post import RetrievalError
from backend.services.resolution import media as media_module
from backend.services.shared.errors import QuantroError
from backend.services.shared.types import MediaFeatures, Post
from backend.services.storage.tiger import TigerConfigError, TigerStore


@pytest.mark.parametrize("frontend_origin,expected_origin", [
    ("http://localhost:3000/", "http://localhost:3000"),
    ("", "https://reports.example"),
])
def test_frontend_cors_can_remain_local_with_public_report_links(frontend_origin, expected_origin):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    result = subprocess.run(
        [sys.executable, "-c", """
import json
from fastapi.testclient import TestClient
from backend.main import app, FRONTEND_ORIGIN
client = TestClient(app)
response = client.options('/analyze/job_cors/run', headers={
    'Origin': FRONTEND_ORIGIN,
    'Access-Control-Request-Method': 'POST',
    'Access-Control-Request-Headers': 'Content-Type',
})
print(json.dumps({'origin': FRONTEND_ORIGIN, 'status': response.status_code,
                  'allowed': response.headers.get('access-control-allow-origin')}))
"""],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "FRONTEND_ORIGIN": frontend_origin, "REPORT_BASE_URL": "https://reports.example/"},
        capture_output=True, text=True, check=True, timeout=60,
    )
    assert json.loads(result.stdout) == {"origin": expected_origin, "status": 200, "allowed": expected_origin}


@pytest.fixture
def store(tmp_path):
    tiger = TigerStore(f"sqlite:///{tmp_path / 'tiger.db'}")
    tiger.ensure_schema()
    yield tiger
    tiger.close()


@pytest.fixture
def client(store):
    app.dependency_overrides[get_tiger_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


def _post():
    return {
        "post_id": "post_abc", "candidate_id": None, "post_url": "https://www.youtube.com/watch?v=x",
        "platform": "youtube", "title": "Clip", "caption": None,
        "author": {"id": None, "handle": "@someone", "display_name": None, "profile_url": None, "bio": None,
                   "verified": None, "followers_count": None, "following_count": None, "posts_count": None},
        "engagement": {"views_count": 10, "likes_count": None, "comments_count": None, "shares_count": None},
        "published_at": "2026-10-01T12:00:00Z",
        "media": {"media_id": "media_abc", "local_path": "/tmp/quantro-post-1/media.mp4", "mime_type": "video/mp4",
                  "playback_url": None, "duration_seconds": 10.0, "width": 1280, "height": 720, "fps": 30.0,
                  "has_audio": True},
        "retrieval": {"retrieved_at": "2026-10-10T18:00:00Z", "provider": "yt-dlp",
                      "metadata_source_urls": ["https://www.youtube.com/watch?v=x"],
                      "unavailable_fields": ["caption"], "warnings": []},
    }


def _features():
    return {
        "media_id": "media_abc", "duration_seconds": 10.0, "language": "en",
        "transcript": {"status": "complete", "processed_ranges": [{"start_seconds": 0.0, "end_seconds": 10.0}],
                       "sentences": [{"sentence_id": "media_abc_sentence_0001", "start_seconds": 0.5,
                                      "end_seconds": 4.0, "text": "Hello there.", "speaker_id": None}]},
        "frames": [{"frame_id": "media_abc_frame_0001", "timestamp_seconds": 5.0,
                    "image_path": "/tmp/quantro_frames_x/frame_0001.jpg", "image_url": None}],
        "visual_text": [], "warnings": ["Transcript timestamps come from the caption provider."],
    }


@pytest.fixture
def upstream(monkeypatch):
    calls = {}

    async def fake_resolve(url, **kwargs):
        calls["url"], calls["options"] = url, kwargs
        return Post.model_validate(_post()), MediaFeatures.model_validate(_features())

    def fake_persist(post):
        calls["media"] = post.media.model_dump(mode="json")

    monkeypatch.setattr(ingest_module, "resolve_media", fake_resolve)
    monkeypatch.setattr(ingest_module, "_persist_media", fake_persist)
    return calls


def test_happy_path_returns_ingest_result_and_persists(client, store, upstream):
    response = client.post("/ingest/job_1", json={"url": "https://www.youtube.com/watch?v=x"})

    assert response.status_code == 200
    body = response.json()
    assert {k: v for k, v in body.items() if k != "persisted_at"} == {
        "job_id": "job_1", "url": "https://www.youtube.com/watch?v=x", "post_id": "post_abc",
        "media_id": "media_abc", "transcript_status": "complete", "sentence_count": 1, "frames_count": 1,
    }
    assert body["persisted_at"].endswith("Z")
    assert upstream["media"] == _post()["media"]
    assert upstream["url"] == "https://www.youtube.com/watch?v=x"
    assert upstream["options"] == {}
    assert store.get_job("job_1")["post_json"] == _post()
    assert store.get_job("job_1")["features_json"] == _features()
    assert store.get_job("job_1")["status"] == "ingested"


def test_media_unavailable_returns_404_and_marks_job_failed(client, store, monkeypatch):
    async def fail(url):
        raise RetrievalError("MEDIA_UNAVAILABLE", "The submitted video could not be retrieved.")

    monkeypatch.setattr(ingest_module, "resolve_media", fail)

    response = client.post("/ingest/job_2", json={"url": "https://www.youtube.com/watch?v=gone"})

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "MEDIA_UNAVAILABLE", "stage": "retrieve_post",
                                         "message": "The submitted video could not be retrieved.", "retryable": False}}
    job = store.get_job("job_2")
    assert (job["status"], job["error_code"], job["error_stage"]) == ("failed", "MEDIA_UNAVAILABLE", "retrieve_post")
    assert job["post_json"] is None and job["features_json"] is None


@pytest.mark.parametrize("url,code", [
    ("not-a-url", "INVALID_URL"), ("ftp://nope", "INVALID_URL"),
    ("http://127.0.0.1/video.mp4", "INVALID_URL"),
    ("https://example.com/article", "UNSUPPORTED_PLATFORM"),
])
def test_bad_input_urls_return_400_before_storage_or_retrieval(client, store, upstream, url, code):
    response = client.post("/ingest/job_3", json={"url": url})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    assert store.get_job("job_3") is None
    assert upstream == {}


def test_transcription_failure_returns_502_and_marks_job_failed(client, store, monkeypatch, upstream):
    async def fail(url):
        raise QuantroError("TRANSCRIPTION_FAILED", "extract_media_features")

    monkeypatch.setattr(ingest_module, "resolve_media", fail)

    response = client.post("/ingest/job_4", json={"url": "https://www.youtube.com/watch?v=x"})

    assert response.status_code == 502
    assert response.json()["error"] == {"code": "TRANSCRIPTION_FAILED", "stage": "extract_media_features",
                                        "message": "The audio could not be transcribed.", "retryable": False}
    job = store.get_job("job_4")
    assert job["status"] == "failed"
    assert job["error_code"] == "TRANSCRIPTION_FAILED"
    assert job["error_stage"] == "extract_media_features"
    assert job["post_json"] is None and job["features_json"] is None


@pytest.mark.parametrize("code,retryable,status", [
    ("PROCESSING_FAILED", False, 500),
    ("MEDIA_UNAVAILABLE", False, 404),
    ("TRANSCRIPTION_FAILED", True, 502),
])
def test_extraction_preserves_typed_error(client, store, upstream, monkeypatch, code, retryable, status):
    original = QuantroError(code, "extract_media_features", retryable=retryable)

    async def fail(url):
        raise original

    monkeypatch.setattr(ingest_module, "resolve_media", fail)
    response = client.post("/ingest/job_typed_error", json={"url": "https://www.youtube.com/watch?v=x"})
    assert response.status_code == status
    assert response.json() == {"error": original.to_dict()}
    job = store.get_job("job_typed_error")
    assert (job["status"], job["error_code"], job["error_stage"]) == ("failed", code, "extract_media_features")


@pytest.mark.parametrize("failure,retryable", [(ValueError("private provider detail"), False), (TimeoutError(), True)])
def test_untyped_extraction_error_uses_safe_message_and_correct_retryability(client, upstream, monkeypatch, failure, retryable):
    async def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(media_module, "get_video_transcript", fail)
    monkeypatch.setattr(ingest_module, "resolve_media", media_module.resolve_media)
    response = client.post("/ingest/job_untyped_error", json={"url": "https://www.youtube.com/watch?v=x"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "TRANSCRIPTION_FAILED"
    assert response.json()["error"]["retryable"] is retryable
    assert "private" not in response.text


def test_tiger_round_trip_matches_exact_contract_json(client, store, upstream):
    client.post("/ingest/job_5", json={"url": "https://www.youtube.com/watch?v=x"})

    response = client.get("/ingest/job_5")

    assert response.status_code == 200
    envelope = response.json()
    assert set(envelope) == {"job", "post", "features"}
    job = envelope["job"]
    stored = store.get_job("job_5")
    assert stored["post_json"] == _post()
    assert stored["features_json"] == _features()
    assert envelope["post"] == _post()
    assert envelope["features"] == _features()
    assert job["created_at"].endswith("Z") and job["updated_at"].endswith("Z")


def test_get_unknown_job_returns_404(client):
    assert client.get("/ingest/missing").status_code == 404


def test_invalid_job_id_is_rejected(client):
    assert client.post("/ingest/bad%20id", json={"url": "https://x.com/a"}).status_code == 422


def test_missing_tiger_database_url_fails_fast_at_startup(monkeypatch):
    monkeypatch.delenv("TIGER_DATABASE_URL", raising=False)
    monkeypatch.delenv("TIGERDATA_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(TigerConfigError, match="TIGER_DATABASE_URL is not set"), TestClient(app):
        pass


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user:pw@db.example.com:5432/tsdb",
        "postgres://user:pw@db.example.com:5432/tsdb?sslmode=disable",
        "postgres://user:pw@db.example.com/tsdb?sslmode=requirepostgres://user:pw@db.example.com/tsdb",
    ],
)
def test_postgres_urls_without_sslmode_require_are_rejected(url):
    with pytest.raises(TigerConfigError) as caught:
        TigerStore(url)

    assert "pw" not in str(caught.value)


def test_postgres_url_with_sslmode_require_is_accepted():
    store = TigerStore("postgres://user:pw@db.example.com:5432/tsdb?sslmode=require")
    assert store._engine.url.drivername == "postgresql+psycopg"
    store.close()


def test_missing_primary_database_url_cannot_fall_back_at_startup(monkeypatch):
    monkeypatch.delenv("TIGER_DATABASE_URL", raising=False)
    monkeypatch.setenv("TIGERDATA_DSN", "postgresql://user:password@localhost/db")
    with pytest.raises(TigerConfigError, match="TIGER_DATABASE_URL is not set"), TestClient(app):
        pass


@pytest.mark.parametrize("url", ["postgresql://user:password@localhost/db", "sqlite:///:memory:"])
def test_production_requires_postgres_tls(monkeypatch, url):
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(TigerConfigError):
        TigerStore(url)


def test_report_save_prompt3_order_is_idempotent(store):
    store.upsert_job("job_report", "https://youtube.com/watch?v=x", "ingested")
    assert store.save_report("report_prompt3", "job_report", {"value": 1})
    assert not store.save_report("report_prompt3", "job_report", {"value": 2})
    assert store.get_report("report_prompt3") == {"value": 1}


@pytest.mark.parametrize("status", ["running", "ingested", "completed", "failed"])
def test_public_ingest_never_overwrites_an_existing_job(client, store, upstream, status):
    original_url = "https://www.youtube.com/watch?v=original"
    store.upsert_job("job_owned", original_url, status, "SEARCH_FAILED" if status == "failed" else None)
    store.save_post_and_features("job_owned", _post(), _features())
    before = store.get_job("job_owned")
    response = client.post("/ingest/job_owned", json={"url": "https://www.youtube.com/watch?v=changed"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROCESSING_FAILED"
    assert response.json()["error"]["retryable"] is False
    assert store.get_job("job_owned") == before
    assert upstream == {}


def test_public_ingest_duplicate_keeps_the_original_features(client, store, upstream):
    body = {"url": "https://www.youtube.com/watch?v=x"}
    assert client.post("/ingest/job_once", json=body).status_code == 200
    before = store.get_job("job_once")
    upstream.clear()
    assert client.post("/ingest/job_once", json=body).status_code == 409
    assert store.get_job("job_once") == before
    assert upstream == {}


def test_ingest_job_reservation_is_atomic_under_concurrent_claims(store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    barrier = Barrier(2)
    def claim(index):
        barrier.wait(timeout=5)
        return store.try_create_job("job_race", f"https://example.com/{index}.mp4")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    assert sorted(results) == [False, True]
    assert store.get_job("job_race")["status"] == "running"
    assert store.get_job("job_race")["post_json"] is None


def test_job_error_columns_migrate_existing_schema(tmp_path):
    from sqlalchemy import create_engine, text
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE quantro_jobs (id TEXT PRIMARY KEY, url TEXT NOT NULL, status TEXT NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, error_code TEXT, error_stage TEXT)"))
        connection.execute(text("INSERT INTO quantro_jobs VALUES ('prior','https://example.com/v.mp4','failed','2026-10-10','2026-10-10','SEARCH_FAILED','search_candidates')"))
    engine.dispose()
    tiger = TigerStore(url)
    try:
        tiger.ensure_schema()
        tiger.ensure_schema()
        assert tiger.get_job("prior")["error_retryable"] is False
        tiger.set_job_status("prior", "failed", "SEARCH_FAILED", "search_candidates", "Try again later.", True)
        row = tiger.get_job("prior")
        assert row["error_message"] == "Try again later."
        assert row["error_retryable"] is True
        assert row["url"] == "https://example.com/v.mp4"
    finally:
        tiger.close()


def test_ingest_extra_fields_are_rejected_without_rows(client, store):
    response = client.post("/ingest/job_extra", json={"url": "https://example.com/v.mp4", "extra": "private"})
    assert response.status_code == 422
    assert "private" not in response.text
    assert store.get_job("job_extra") is None


@pytest.mark.parametrize("method,stage", [
    ("try_create_job", "retrieve_post"),
    ("save_post_and_features", "extract_media_features"),
])
def test_ingest_storage_failures_are_safe(client, store, upstream, monkeypatch, method, stage):
    def fail(*args, **kwargs):
        raise RuntimeError("postgresql://private-password@host/db")
    monkeypatch.setattr(store, method, fail)
    response = client.post("/ingest/job_storage_failure", json={"url": "https://example.com/video.mp4"})
    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "STORAGE_FAILED" and error["stage"] == stage
    assert error["retryable"] is True
    assert "private-password" not in response.text


def test_real_error_type_is_logged_without_private_details(client, upstream, monkeypatch, caplog):
    async def fail(*args, **kwargs):
        raise ValueError("secret private evidence")
    monkeypatch.setattr(media_module, "get_video_transcript", fail)
    monkeypatch.setattr(ingest_module, "resolve_media", media_module.resolve_media)
    response = client.post("/ingest/job_logged", json={"url": "https://www.youtube.com/watch?v=x"})
    assert response.status_code == 502
    assert "stage=extract_media_features type=ValueError" in caplog.text
    assert "secret private evidence" not in caplog.text
