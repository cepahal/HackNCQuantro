"""Real resolver + SQLite + durable Photon claims, with offline provider stages."""

import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.routes import photon_webhook
from backend.routes.common import get_tiger_store
from backend.services.messaging.delivery_tracker import TigerDeliveryTracker
from backend.services.messaging.inbound import ACK_REPLY
from backend.services.messaging.photon import PhotonProvider
from backend.services.reporting.build_report import build_report
from backend.services.resolution import run
from backend.services.shared.errors import ErrorCode, QuantroError, Stage
from backend.services.shared.types import (
    Author,
    Engagement,
    MediaAsset,
    MediaFeatures,
    Post,
    Retrieval,
)
from backend.services.storage.tiger import TigerStore

URL = "https://www.youtube.com/watch?v=3XPuGqqWLtk"
KEY = "offline-retry-test-key"


def report(job_id):
    post = Post(
        post_id="post_retry",
        post_url=URL,
        platform="youtube",
        author=Author(),
        engagement=Engagement(),
        media=MediaAsset(
            media_id="media_retry",
            local_path="",
            mime_type="video/mp4",
            duration_seconds=10.0,
        ),
        retrieval=Retrieval(provider="offline", retrieved_at="2026-10-11T00:00:00Z"),
    )
    features = MediaFeatures(
        media_id="media_retry",
        duration_seconds=10.0,
        transcript={"status": "unavailable", "processed_ranges": [], "sentences": []},
    )
    return build_report(job_id, post, features, None, None, None, None, None)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("PHOTON_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("TIGER_DATABASE_URL", f"sqlite:///{tmp_path / 'retry.db'}")
    monkeypatch.setattr(run, "validate_search_settings", lambda _: None)
    store = TigerStore(f"sqlite:///{tmp_path / 'retry.db'}")
    store.ensure_schema()
    inbound = TigerDeliveryTracker(store, "photon_inbound")
    summary = TigerDeliveryTracker(store, "photon_summary")
    provider = PhotonProvider(
        api_key=KEY, base_url="https://photon.invalid", sender_id="test_sender"
    )
    sent = []

    def send(conversation_id, body):
        sent.append(body)
        return {"message_id": f"msg_{len(sent)}", "status": "accepted"}

    monkeypatch.setattr(provider, "send_message", send)
    # Crucially, do not mock photon_webhook.run_analysis or resolver reservation/save/delivery.
    monkeypatch.setattr(photon_webhook, "run_analysis", run.run_analysis)
    app.dependency_overrides[get_tiger_store] = lambda: store
    app.dependency_overrides[photon_webhook.get_photon_provider] = lambda: provider
    app.dependency_overrides[photon_webhook.get_inbound_tracker] = lambda: inbound
    app.dependency_overrides[photon_webhook.get_summary_tracker] = lambda: summary
    yield store, inbound, summary, sent, provider
    app.dependency_overrides.clear()
    store.close()


def submit(event):
    raw = json.dumps(
        {
            "conversation_id": "conv_retry",
            "sender_handle": "+15550000000",
            "text": URL,
            "message_id": event,
        }
    ).encode()
    signature = hmac.new(KEY.encode(), raw, hashlib.sha256).hexdigest()
    # No context manager: production lifespan opens configured stores. All route
    # dependencies here must remain the isolated SQLite/test providers above.
    client = TestClient(app)
    return client.post(
        "/webhooks/photon",
        content=raw,
        headers={"Content-Type": "application/json", "X-Photon-Signature": signature},
    )


def test_real_resolver_retries_twice_then_saves_once_and_preserves_old_jobs(
    wired, monkeypatch
):
    store, _, summary, sent, _ = wired
    attempts = []

    async def build(job_id, *args, **kwargs):
        attempts.append(job_id)
        if len(attempts) < 3:
            raise QuantroError(
                ErrorCode.SEARCH_FAILED, Stage.SEARCH_CANDIDATES, retryable=True
            )
        return report(job_id)

    monkeypatch.setattr(run, "_build", AsyncMock(side_effect=build))
    base = photon_webhook.job_id_for("conv_retry", URL)
    for index, job_id in enumerate((base, base + "_retry_1", base + "_retry_2")):
        response = submit(f"event_{index}")
        assert response.json() == {"status": "accepted", "job_id": job_id}
        assert submit(f"event_{index}").json() == {
            "status": "duplicate",
            "job_id": job_id,
        }
    assert attempts == [base, base + "_retry_1", base + "_retry_2"]
    assert (
        store.get_job(base)["status"]
        == store.get_job(base + "_retry_1")["status"]
        == "failed"
    )
    successful = base + "_retry_2"
    assert store.get_job(successful)["status"] == "completed"
    assert len(store.get_job_reports(successful)) == 1
    assert summary.has_record("conv_retry", successful)
    assert sum("View the evidence:" in body for body in sent) == 1
    assert submit("new_event_after_success").json()["status"] == "duplicate"
    assert len(attempts) == 3


@pytest.mark.parametrize(
    "guard",
    [
        "pending_summary",
        "accepted_summary",
        "saved_report",
        "save_boundary",
        "nonretryable",
        "running",
        "completed",
    ],
)
def test_failed_attempt_never_advances_when_delivery_or_persistence_is_uncertain(
    wired, monkeypatch, guard
):
    store, _, summary, _, _ = wired
    base = photon_webhook.job_id_for("conv_retry", URL)
    store.try_create_job(base, URL)
    store.set_job_status(
        base,
        "failed",
        "SEARCH_FAILED",
        "save_report_and_complete_job"
        if guard == "save_boundary"
        else "search_candidates",
        error_retryable=guard != "nonretryable",
    )
    if guard in ("running", "completed"):
        store.set_job_status(base, guard)
    if guard in ("pending_summary", "accepted_summary"):
        summary.check_and_record("conv_retry", base)
        if guard == "accepted_summary":
            summary.record_message_id("conv_retry", base, "accepted_receipt")
    if guard == "saved_report":
        store.save_report("report_" + base, base, report(base).model_dump(mode="json"))
    build = AsyncMock()
    monkeypatch.setattr(run, "_build", build)
    assert submit("guarded_event").json()["status"] == "duplicate"
    assert store.get_job(base + "_retry_1") is None
    build.assert_not_awaited()


def test_real_resolver_uncertain_summary_keeps_retry_reserved(wired, monkeypatch):
    store, _, summary, sent, provider = wired
    base = photon_webhook.job_id_for("conv_retry", URL)
    store.try_create_job(base, URL)
    store.set_job_status(
        base, "failed", "SEARCH_FAILED", "search_candidates", error_retryable=True
    )

    async def build(job_id, *args, **kwargs):
        return report(job_id)

    monkeypatch.setattr(run, "_build", AsyncMock(side_effect=build))

    def uncertain_send(conversation_id, body):
        sent.append(body)
        if body != ACK_REPLY:
            raise QuantroError(
                ErrorCode.MESSAGE_SEND_FAILED, Stage.SEND_SUMMARY, retryable=True
            )
        return {"message_id": "ack_only", "status": "accepted"}

    monkeypatch.setattr(provider, "send_message", uncertain_send)
    attempt = base + "_retry_1"
    assert submit("uncertain_a").json()["job_id"] == attempt
    assert store.get_job(attempt)["status"] == "completed"
    assert summary.has_record("conv_retry", attempt)
    assert summary.get_message_id("conv_retry", attempt) is None
    assert submit("uncertain_b").json()["status"] == "duplicate"
    assert submit("uncertain_a").json() == {"status": "duplicate", "job_id": attempt}
    assert run._build.await_count == 1
    assert len(sent) == 2


def test_distinct_event_cannot_duplicate_inflight_retry(wired, monkeypatch):
    store, _, _, sent, _ = wired
    base = photon_webhook.job_id_for("conv_retry", URL)
    store.try_create_job(base, URL)
    store.set_job_status(
        base, "failed", "SEARCH_FAILED", "search_candidates", error_retryable=True
    )
    started, release = Event(), Event()

    async def build(job_id, *args, **kwargs):
        started.set()
        assert release.wait(10)
        return report(job_id)

    build_mock = AsyncMock(side_effect=build)
    monkeypatch.setattr(run, "_build", build_mock)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(submit, "retry_a")
        try:
            assert started.wait(10)
            assert submit("retry_b").json()["status"] == "duplicate"
        finally:
            release.set()
        assert first.result(timeout=10).json()["job_id"] == base + "_retry_1"
    assert build_mock.await_count == 1
    assert sent.count(ACK_REPLY) == 1
