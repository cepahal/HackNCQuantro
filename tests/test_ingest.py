import pytest
from fastapi.testclient import TestClient

import backend.services.pipeline.run_ingest as ingest_module
from backend.main import app
from backend.routes.ingest import get_tiger_store
from backend.services.external_apis.retrieve_post import RetrievalError
from backend.services.reporting.errors import QuantroError
from backend.services.storage.tiger import TigerConfigError, TigerStore


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
        "visual_text": [], "warnings": ["Gemini transcript timestamps are estimates."],
    }


@pytest.fixture
def upstream(monkeypatch):
    calls = {}

    def fake_retrieve(url):
        calls["url"] = url
        return _post()

    def fake_extract(media, options):
        calls["media"], calls["options"] = media, options
        return _features()

    monkeypatch.setattr(ingest_module, "retrieve_post", fake_retrieve)
    monkeypatch.setattr(ingest_module, "extract_media_features", fake_extract)
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
    assert upstream["options"] == ingest_module.EXTRACTION_OPTIONS
    assert store.get_job("job_1")["status"] == "ingested"


def test_media_unavailable_returns_404_and_marks_job_failed(client, store, monkeypatch):
    def fail(url):
        raise RetrievalError("MEDIA_UNAVAILABLE", "The submitted video could not be retrieved.")

    monkeypatch.setattr(ingest_module, "retrieve_post", fail)

    response = client.post("/ingest/job_2", json={"url": "https://www.youtube.com/watch?v=gone"})

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "MEDIA_UNAVAILABLE", "stage": "retrieve_post",
                                         "message": "The media could not be retrieved.", "retryable": False}}
    job = store.get_job("job_2")
    assert (job["status"], job["error_code"], job["error_stage"]) == ("failed", "MEDIA_UNAVAILABLE", "retrieve_post")
    assert job["post_json"] is None and job["features_json"] is None


@pytest.mark.parametrize("code", ["INVALID_URL", "UNSUPPORTED_PLATFORM"])
def test_bad_input_urls_return_400(client, store, monkeypatch, code):
    def fail(url):
        raise RetrievalError(code, "Provide a valid public post URL.", status_code=400)

    monkeypatch.setattr(ingest_module, "retrieve_post", fail)

    response = client.post("/ingest/job_3", json={"url": "ftp://nope"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    assert store.get_job("job_3")["status"] == "failed"


def test_transcription_failure_returns_502_and_marks_job_failed(client, store, monkeypatch, upstream):
    def fail(media, options):
        raise QuantroError("TRANSCRIPTION_FAILED", "extract_media_features")

    monkeypatch.setattr(ingest_module, "extract_media_features", fail)

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

    def fail(media, options):
        raise original

    monkeypatch.setattr(ingest_module, "extract_media_features", fail)
    response = client.post("/ingest/job_typed_error", json={"url": "https://www.youtube.com/watch?v=x"})
    assert response.status_code == status
    assert response.json() == {"error": original.to_dict()}
    job = store.get_job("job_typed_error")
    assert (job["status"], job["error_code"], job["error_stage"]) == ("failed", code, "extract_media_features")


@pytest.mark.parametrize("failure,retryable", [(ValueError("private provider detail"), False), (TimeoutError(), True)])
def test_untyped_extraction_error_uses_safe_message_and_correct_retryability(client, upstream, monkeypatch, failure, retryable):
    def fail(media, options):
        raise failure

    monkeypatch.setattr(ingest_module, "extract_media_features", fail)
    response = client.post("/ingest/job_untyped_error", json={"url": "https://www.youtube.com/watch?v=x"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "TRANSCRIPTION_FAILED"
    assert response.json()["error"]["retryable"] is retryable
    assert "private" not in response.text


def test_tiger_round_trip_keeps_internal_paths_private(client, store, upstream):
    client.post("/ingest/job_5", json={"url": "https://www.youtube.com/watch?v=x"})

    response = client.get("/ingest/job_5")

    assert response.status_code == 200
    job = response.json()
    stored = store.get_job("job_5")
    assert stored["post_json"] == _post()
    assert stored["features_json"] == _features()
    assert "local_path" not in job["post_json"]["media"]
    assert "image_path" not in job["features_json"]["frames"][0]
    assert "/tmp/" not in response.text
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
