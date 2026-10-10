"""Public errors and bounded JSON processing do not expose caller/provider data."""

import asyncio
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from backend import main
from backend.routes import analyze
from backend.routes.common import get_tiger_store, quantro_error_response
from backend.routes.request_limits import (
    JSON_BODY_LIMIT,
    MAX_JSON_DEPTH,
    WEBHOOK_BODY_LIMIT,
    RequestLimitsMiddleware,
    _too_deep,
)
from backend.services.shared.errors import QuantroError, normalize_exception

PRIVATE = "PRIVATE_CALLER_OR_PROVIDER_DETAILS"


@pytest.fixture
def client(monkeypatch):
    main.app.dependency_overrides[get_tiger_store] = lambda: None
    monkeypatch.setattr(analyze, "run_analysis", Mock(side_effect=RuntimeError(PRIVATE)))
    yield TestClient(main.app, raise_server_exceptions=False)
    main.app.dependency_overrides.clear()


def _assert_envelope(response, status):
    assert response.status_code == status
    data = response.json()
    assert set(data) == {"error"}
    assert set(data["error"]) == {"code", "stage", "message", "retryable"}
    assert PRIVATE not in response.text
    return data["error"]


def test_validation_never_echoes_invalid_input(client):
    response = client.post("/analyze/job_test/run", json={"url": {"private": PRIVATE}})
    _assert_envelope(response, 422)


def test_unexpected_route_error_is_enveloped_and_logged_safely(client, caplog):
    response = client.post("/analyze/job_test/run", json={"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw"})
    error = _assert_envelope(response, 500)
    assert error["code"] == "PROCESSING_FAILED"
    assert "exception_type=RuntimeError" in caplog.text
    assert PRIVATE not in caplog.text


def test_deep_json_rejected_before_fastapi_formats_errors(client):
    body = '{"url":' + '[' * (MAX_JSON_DEPTH + 1) + '"' + PRIVATE + '"' + ']' * (MAX_JSON_DEPTH + 1) + '}'
    _assert_envelope(client.post("/analyze/job_test/run", content=body, headers={"Content-Type": "application/json"}), 400)
    analyze.run_analysis.assert_not_called()


def test_oversized_json_rejected_before_pipeline(client):
    response = client.post("/ingest/job_test", content=b"x" * (JSON_BODY_LIMIT + 1))
    _assert_envelope(response, 413)


def test_unknown_route_and_wrong_method_are_enveloped(client):
    _assert_envelope(client.get("/not-a-route"), 404)
    _assert_envelope(client.delete("/analyze/job_test/run"), 405)


def test_webhook_body_is_capped_before_authentication_or_provider_use(client):
    _assert_envelope(client.post("/webhooks/photon", content=b"x" * (WEBHOOK_BODY_LIMIT + 1)), 413)


def test_braces_and_escaped_quotes_inside_strings_do_not_count_as_depth():
    assert not _too_deep(json.dumps({"caption": '[{"' * 1000}).encode())
    assert _too_deep(b"[" * (MAX_JSON_DEPTH + 1))


def test_key_config_names_are_private_but_safe_identity_reason_survives():
    error = QuantroError("SEARCH_FAILED", "search_candidates", "EXA_API_KEY is required when SEARCH_PROVIDER=exa.")
    response = quantro_error_response(error)
    assert "EXA_API_KEY" not in response.body.decode()
    assert error.message.startswith("EXA_API_KEY")
    safe = QuantroError("PROCESSING_FAILED", "run_analysis", "The submitted post cannot be used as its own source.")
    assert json.loads(quantro_error_response(safe).body)["error"]["message"] == safe.message


def test_only_explicit_conflict_status_overrides_contract_http_code():
    conflict = QuantroError("PROCESSING_FAILED", "retrieve_post")
    conflict.status_code = 409
    assert quantro_error_response(conflict).status_code == 409
    media = QuantroError("MEDIA_UNAVAILABLE", "retrieve_post")
    media.status_code = 502
    assert quantro_error_response(media).status_code == 404


def test_normalized_failure_logs_original_type_without_message(caplog):
    error = normalize_exception("run_analysis", ValueError(PRIVATE))
    quantro_error_response(error)
    assert "exception_type=ValueError" in caplog.text
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("declared", [False, True])
def test_body_limit_checks_real_chunks_or_rejects_length_before_read(declared):
    called = Mock()
    chunks = [b"x" * (JSON_BODY_LIMIT // 2), b"y" * (JSON_BODY_LIMIT // 2 + 1), b"unread"]
    received = []
    sent = []

    async def app(scope, receive, send):
        called()

    async def receive():
        received.append(1)
        return {"type": "http.request", "body": chunks.pop(0), "more_body": True}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/analyze/job_test/run",
             "headers": [(b"content-length", str(JSON_BODY_LIMIT + 1).encode())] if declared else []}
    asyncio.run(RequestLimitsMiddleware(app)(scope, receive, send))
    called.assert_not_called()
    assert len(received) == (0 if declared else 2)
    assert sent[0]["status"] == 413
    assert set(json.loads(sent[1]["body"])["error"]) == {"code", "stage", "message", "retryable"}


@pytest.mark.parametrize("headers", [
    [(b"content-length", b"-1")],
    [(b"content-length", b"not-a-number")],
    [(b"content-length", b"1"), (b"content-length", b"2")],
])
def test_invalid_content_lengths_are_rejected_without_reading(headers):
    sent = []

    async def app(scope, receive, send):
        pytest.fail("Malformed headers reached the application")

    async def receive():
        pytest.fail("Malformed headers read the body")

    async def send(event):
        sent.append(event)

    asyncio.run(RequestLimitsMiddleware(app)(
        {"type": "http", "method": "POST", "path": "/ingest/job", "headers": headers}, receive, send))
    assert sent[0]["status"] == 400


def test_valid_chunked_body_reaches_application_unchanged():
    parts = [b'{"url":', b'"https://example.org/example.mp4"}']
    observed = []

    async def app(scope, receive, send):
        observed.append((await receive())["body"])

    async def receive():
        body = parts.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(parts)}

    async def send(event):
        pytest.fail("Valid body was rejected")

    asyncio.run(RequestLimitsMiddleware(app)(
        {"type": "http", "method": "POST", "path": "/ingest/job", "headers": []}, receive, send))
    assert observed == [b'{"url":"https://example.org/example.mp4"}']
