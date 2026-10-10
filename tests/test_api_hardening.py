"""HTTP-layer guarantees: one error envelope, bounded bodies, safe logging, honest job status."""

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

import backend.routes.analyze as analyze_route
import backend.routes.photon_webhook as photon_webhook
from backend.main import FRONTEND_ORIGIN, allowed_origins, app
from backend.routes.common import get_tiger_store
from backend.routes.request_guard import MAX_BODY_BYTES
from backend.services.messaging.delivery_tracker import DeliveryTracker
from backend.services.reporting import run_analysis as analysis_module
from backend.services.shared.errors import QuantroError
from backend.services.storage.tiger import TigerStore

client = TestClient(app, raise_server_exceptions=False)
ENVELOPE_KEYS = {"code", "stage", "message", "retryable"}
URL = "https://www.youtube.com/watch?v=x"


def _error(response) -> dict:
    assert response.headers["content-type"].startswith("application/json"), response.text[:100]
    body = response.json()
    assert set(body) == {"error"} and set(body["error"]) == ENVELOPE_KEYS, body
    return body["error"]


@pytest.fixture
def store(tmp_path):
    tiger = TigerStore(f"sqlite:///{tmp_path / 'jobs.db'}")
    tiger.ensure_schema()
    app.dependency_overrides[get_tiger_store] = lambda: tiger
    yield tiger
    app.dependency_overrides.clear()
    tiger.close()


@pytest.fixture
def no_store():
    app.dependency_overrides[get_tiger_store] = lambda: None
    yield
    app.dependency_overrides.clear()


def test_oversized_body_is_rejected_by_content_length(no_store, monkeypatch):
    monkeypatch.setattr(analyze_route, "run_analysis", lambda *a, **k: pytest.fail("must not run"))
    deep = "[" * 100_000 + "]" * 100_000
    response = client.post("/analyze/job_1/run", content=deep, headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    _error(response)


def test_streamed_body_is_cut_off_at_the_limit(no_store, monkeypatch):
    monkeypatch.setattr(analyze_route, "run_analysis", lambda *a, **k: pytest.fail("must not run"))

    def chunks():
        for _ in range(MAX_BODY_BYTES // 1024 + 2):
            yield b" " * 1024

    response = client.post("/analyze/job_1/run", content=chunks(), headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    _error(response)


def test_deeply_nested_body_within_the_limit_is_a_client_error_envelope(no_store):
    nested = "[" * 20_000 + "]" * 20_000
    response = client.post("/analyze/job_1/run", content=nested, headers={"Content-Type": "application/json"})
    assert response.status_code in (400, 422)
    _error(response)


def test_validation_errors_are_enveloped_without_echoing_input(no_store, monkeypatch):
    monkeypatch.setattr(analyze_route, "run_analysis", lambda *a, **k: pytest.fail("must not run"))
    missing = client.post("/analyze/job_1/run", json={})
    assert missing.status_code == 422 and _error(missing)["code"] == "INVALID_URL"
    extra = client.post("/analyze/job_1/run", json={"url": URL, "<secret-field>": "hunter2"})
    assert extra.status_code == 422 and _error(extra)["code"] == "PROCESSING_FAILED"
    assert "hunter2" not in extra.text and "secret-field" not in extra.text
    bad_id = client.post("/analyze/bad.id/run", json={"url": URL})
    assert bad_id.status_code == 422
    _error(bad_id)


def test_unexpected_route_exception_is_a_logged_500_envelope(no_store, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("db password=hunter2")

    monkeypatch.setattr(analyze_route, "run_analysis", boom)
    with caplog.at_level(logging.ERROR):
        response = client.post("/analyze/job_1/run", json={"url": URL})
    assert response.status_code == 500
    assert _error(response)["code"] == "PROCESSING_FAILED"
    assert "hunter2" not in response.text
    assert "RuntimeError" in caplog.text


def test_not_found_and_unknown_routes_use_the_envelope(store):
    assert client.get("/reports/report_missing").status_code == 404
    _error(client.get("/reports/report_missing"))
    _error(client.get("/analyze/job_missing/status"))
    unknown = client.get("/nope")
    assert unknown.status_code == 404
    _error(unknown)
    wrong_method = client.delete("/analyze/job_1/status")
    assert wrong_method.status_code == 405 and "allow" in wrong_method.headers
    _error(wrong_method)


def test_completed_job_with_other_url_is_409(store):
    store.upsert_job("job_done", "https://www.youtube.com/watch?v=original", "completed")
    store.save_report("report_job_done", {"job_id": "job_done"}, "job_done")
    response = client.post("/analyze/job_done/run", json={"url": "https://www.youtube.com/watch?v=other"})
    assert response.status_code == 409
    assert _error(response)["code"] == "PROCESSING_FAILED"


def test_invalid_search_provider_fails_before_ingest(store, monkeypatch, caplog):
    monkeypatch.setenv("SEARCH_PROVIDER", "serpapi")
    monkeypatch.setattr(analysis_module, "ingest_post_and_features", lambda *a, **k: pytest.fail("ingest ran"))
    with caplog.at_level(logging.ERROR):
        response = client.post("/analyze/job_cfg/run", json={"url": URL})
    error = _error(response)
    assert (error["code"], error["stage"]) == ("SEARCH_FAILED", "search_candidates")
    assert "SEARCH_PROVIDER" not in error["message"] and "SEARCH_PROVIDER" in caplog.text


def test_route_failures_are_logged_with_code_stage_and_cause(store, monkeypatch, caplog):
    def fail(*args, **kwargs):
        try:
            raise TimeoutError("https://token@private.example")
        except TimeoutError as exc:
            raise QuantroError("SEARCH_FAILED", "search_candidates", retryable=True) from exc

    monkeypatch.setattr(analyze_route, "run_analysis", fail)
    with caplog.at_level(logging.WARNING):
        client.post("/analyze/job_log/run", json={"url": URL})
    assert "code=SEARCH_FAILED stage=search_candidates" in caplog.text
    assert "cause=TimeoutError" in caplog.text
    assert "private.example" not in caplog.text


def test_run_analysis_keeps_the_underlying_cause(store, monkeypatch):
    def broken(*args, **kwargs):
        raise KeyError("boom")

    monkeypatch.setattr(analysis_module, "ingest_post_and_features", broken)
    with pytest.raises(QuantroError) as caught:
        analysis_module.run_analysis("job_cause", URL, store=store)
    assert isinstance(caught.value.__cause__, KeyError)


@pytest.mark.parametrize("code,retryable", [("STORAGE_FAILED", True), ("SEARCH_FAILED", True), ("MEDIA_UNAVAILABLE", False)])
def test_failed_status_derives_retryable_from_code(store, code, retryable):
    store.upsert_job("job_failed", URL, "failed", code, "retrieve_post")
    error = client.get("/analyze/job_failed/status").json()["error"]
    assert (error["code"], error["retryable"]) == (code, retryable)
    assert error["message"]


def test_orphaned_running_job_is_reported_as_a_retryable_failure(store, monkeypatch):
    store.upsert_job("job_running", URL, "running")
    assert client.get("/analyze/job_running/status").json()["status"] == "running"
    later = datetime.now(UTC) + analyze_route.STALE_JOB_AFTER + timedelta(minutes=1)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return later

    monkeypatch.setattr(analyze_route, "datetime", Clock)
    body = client.get("/analyze/job_running/status").json()
    assert body["status"] == "failed" and body["report_id"] is None
    assert body["error"]["retryable"] is True and body["error"]["code"] == "PROCESSING_FAILED"
    assert store.get_job("job_running")["status"] == "running"


def test_cors_allows_the_loopback_twin_and_exposes_range_headers():
    assert allowed_origins("http://localhost:3000") == ["http://localhost:3000", "http://127.0.0.1:3000"]
    assert allowed_origins("http://127.0.0.1:3000") == ["http://127.0.0.1:3000", "http://localhost:3000"]
    assert allowed_origins("https://quantro.vercel.app") == ["https://quantro.vercel.app"]
    cors = next(middleware for middleware in app.user_middleware if middleware.cls is CORSMiddleware)
    assert cors.kwargs["allow_origins"] == allowed_origins(FRONTEND_ORIGIN)
    assert "*" not in cors.kwargs["allow_origins"]
    assert "HEAD" in cors.kwargs["allow_methods"]
    assert {"Content-Range", "Accept-Ranges", "Content-Length", "ETag"} <= set(cors.kwargs["expose_headers"])


class TrustingProvider:
    sender_id = "me"

    def verify_webhook_signature(self, headers, raw_body):
        return True

    def send_message(self, conversation_id, body):
        return {"message_id": "m", "status": "accepted"}


@pytest.fixture
def webhook(monkeypatch):
    runs = []
    monkeypatch.setattr(photon_webhook, "run_analysis", lambda *a, **k: runs.append(a))
    app.dependency_overrides[get_tiger_store] = lambda: None
    app.dependency_overrides[photon_webhook.get_photon_provider] = lambda: TrustingProvider()
    app.dependency_overrides[photon_webhook.get_inbound_tracker] = lambda: DeliveryTracker()
    app.dependency_overrides[photon_webhook.get_summary_tracker] = lambda: DeliveryTracker()
    yield runs
    app.dependency_overrides.clear()


def test_webhook_deeply_nested_signed_payload_is_400(webhook):
    response = client.post("/webhooks/photon", content=("[" * 30_000 + "]" * 30_000).encode())
    assert response.status_code == 400
    _error(response)


@pytest.mark.parametrize("stamp", [
    (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
    int((datetime.now(UTC) - timedelta(hours=2)).timestamp()),
    int((datetime.now(UTC) + timedelta(hours=2)).timestamp() * 1000),
])
def test_webhook_rejects_a_stale_signed_timestamp(webhook, stamp):
    payload = {"conversation_id": "conv_1", "text": URL, "timestamp": stamp}
    response = client.post("/webhooks/photon", json=payload)
    assert response.status_code == 401
    _error(response)
    assert webhook == []


@pytest.mark.parametrize("stamp", [datetime.now(UTC).isoformat(), int(datetime.now(UTC).timestamp()), 7_000_000, "not a time"])
def test_webhook_accepts_fresh_or_unrecognized_timestamps(webhook, stamp):
    payload = {"conversation_id": "conv_1", "data": {"text": URL, "sent_at": stamp}}
    assert client.post("/webhooks/photon", json=payload).json()["status"] == "accepted"


def test_webhook_signature_and_size_failures_use_the_envelope(monkeypatch):
    app.dependency_overrides[get_tiger_store] = lambda: None
    try:
        raw = json.dumps({"conversation_id": "c", "text": URL}).encode()
        wrong = hmac.new(b"wrong", raw, hashlib.sha256).hexdigest()
        response = client.post("/webhooks/photon", content=raw, headers={"X-Photon-Signature": wrong})
        assert response.status_code == 401
        _error(response)
        too_large = client.post("/webhooks/photon", content=b"x" * (MAX_BODY_BYTES + 1))
        assert too_large.status_code == 413
        _error(too_large)
    finally:
        app.dependency_overrides.clear()
