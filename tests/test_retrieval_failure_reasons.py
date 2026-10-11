"""Safe retrieval diagnostics and failed-job persistence without network calls."""

import logging
import subprocess

import pytest
from fastapi.testclient import TestClient

import backend.services.pipeline.run_ingest as ingest_module
from backend.main import app
from backend.routes.common import get_tiger_store
from backend.services.external_apis import retrieve_post as retrieval
from backend.services.shared.errors import ErrorCode, Stage
from backend.services.storage.tiger import TigerStore


@pytest.mark.parametrize(
    ("stderr", "markers", "retryable"),
    [
        ("ERROR: Sign in to confirm you're not a bot", ("platform", "blocked"), False),
        ("ERROR: Sign in to confirm you’re not a bot", ("platform", "blocked"), False),
        ("ERROR: This video is private", ("private", "restricted", "login"), False),
        ("ERROR: Login required", ("private", "restricted", "login"), False),
        ("ERROR: Sign in to confirm your age", ("age", "restricted", "login"), False),
        ("ERROR: This video is age-restricted", ("age", "restricted", "login"), False),
        ("ERROR: Video unavailable. This video has been removed by the uploader.", ("unavailable", "removed"), False),
        ("ERROR: HTTP Error 429: Too Many Requests", ("rate",), True),
        ("ERROR: HTTP Error 403: Forbidden", ("blocked",), False),
        ("ERROR: The read operation timed out", ("temporar", "network", "timed"), True),
        ("ERROR: Connection reset by peer", ("temporar", "network", "timed"), True),
        ("ERROR: Temporary failure in name resolution", ("temporar", "network", "timed"), True),
        ("ERROR: Network is unreachable", ("temporar", "network", "timed"), True),
        ("ERROR: Requested format is not available", ("format",), False),
    ],
)
def test_failure_classifier_preserves_public_contract(stderr, markers, retryable):
    error = retrieval._download_failure(stderr)

    assert isinstance(error, retrieval.RetrievalError)
    assert error.code == ErrorCode.MEDIA_UNAVAILABLE
    assert error.stage == Stage.RETRIEVE_POST
    body = error.public_dict()
    assert set(body) == {"code", "stage", "message", "retryable"}
    assert body["code"] == "MEDIA_UNAVAILABLE"
    assert body["stage"] == "retrieve_post"
    assert body["retryable"] is retryable
    assert any(marker in body["message"].lower() for marker in markers)
    assert "ERROR:" not in body["message"]


@pytest.mark.parametrize("stderr", ["", "unrecognized provider diagnostic"])
def test_unknown_failure_remains_safe_nonretryable_media_error(stderr):
    error = retrieval._download_failure(stderr)

    assert error.code == ErrorCode.MEDIA_UNAVAILABLE
    assert error.stage == Stage.RETRIEVE_POST
    assert error.retryable is False
    assert error.message
    assert "unrecognized provider diagnostic" not in error.message


@pytest.mark.parametrize("diagnostic", [
    "Sign in to confirm you're not a bot",
    "HTTP Error 429: Too Many Requests",
    "HTTP Error 403: Forbidden",
    "Connection reset by peer",
    "unrecognized provider diagnostic",
])
def test_failure_classifier_never_echoes_provider_secrets(diagnostic, caplog):
    signed_url = "https://media.example/clip.mp4?api_key=sample-test-key&signature=sample-signed-value"
    stderr = f"ERROR: {diagnostic}\nProvider response: {signed_url}\nAuthorization: Bearer sample-auth-token"

    with caplog.at_level(logging.DEBUG):
        error = retrieval._download_failure(stderr)

    output = str(error.public_dict()) + str(error) + caplog.text
    for forbidden in (signed_url, "sample-test-key", "sample-signed-value", "sample-auth-token", "Authorization:"):
        assert forbidden not in output


def test_real_retrieval_failure_persists_safe_reason_and_never_extracts(
    monkeypatch, tmp_path, caplog
):
    monkeypatch.setenv("APP_ENV", "test")
    store = TigerStore(f"sqlite:///{tmp_path / 'retrieval-failure.db'}")
    store.ensure_schema()
    calls = []
    signed_url = "https://media.example/clip.mp4?signature=sample-private-signature"

    def failed_worker(command, **kwargs):
        calls.append(command)
        assert kwargs["capture_output"] is True
        return subprocess.CompletedProcess(
            command, 1, "",
            f"ERROR: Sign in to confirm you're not a bot\n{signed_url}",
        )

    monkeypatch.setattr(retrieval.subprocess, "run", failed_worker)

    def retrieve_from_worker(url):
        return retrieval.retrieve_post(url, output_dir=tmp_path / "media")

    def unexpected_extract(*args, **kwargs):
        pytest.fail("Feature extraction must not run after failed submitted retrieval.")

    monkeypatch.setattr(ingest_module, "retrieve_post", retrieve_from_worker)
    monkeypatch.setattr(ingest_module, "extract_media_features", unexpected_extract)
    prior_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_tiger_store] = lambda: store
    try:
        with caplog.at_level(logging.WARNING):
            response = TestClient(app).post(
                "/ingest/job_blocked_download",
                json={"url": "https://www.youtube.com/watch?v=abcdefghijk"},
            )

        assert response.status_code == 404
        assert set(response.json()) == {"error"}
        error = response.json()["error"]
        assert set(error) == {"code", "stage", "message", "retryable"}
        assert error["code"] == "MEDIA_UNAVAILABLE"
        assert error["stage"] == "retrieve_post"
        assert error["retryable"] is False
        assert any(marker in error["message"].lower() for marker in ("platform", "blocked"))
        job = store.get_job("job_blocked_download")
        assert job is not None
        assert job["status"] == "failed"
        assert job["error_code"] == error["code"]
        assert job["error_stage"] == error["stage"]
        assert job["error_message"] == error["message"]
        assert job["error_retryable"] is False
        assert job["post_json"] is None
        assert job["features_json"] is None
        assert len(calls) == 1
        assert signed_url not in response.text + caplog.text + str(job)
        assert "sample-private-signature" not in response.text + caplog.text + str(job)
        assert not list((tmp_path / "media").iterdir())
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(prior_overrides)
        store.close()
