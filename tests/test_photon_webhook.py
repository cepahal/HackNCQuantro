import hashlib
import hmac
import json
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock, patch
from urllib.error import URLError

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.routes import photon_webhook
from backend.routes.common import get_tiger_store
from backend.services.messaging import photon
from backend.services.messaging.delivery_tracker import (
    DeliveryTracker,
    TigerDeliveryTracker,
)
from backend.services.messaging.inbound import (
    ACK_REPLY,
    NO_URL_REPLY,
    canned_reply,
    extract_url,
    parse_inbound,
)
from backend.services.messaging.photon import PhotonProvider
from backend.services.reporting.send_summary import send_summary
from backend.services.shared.errors import ErrorCode, QuantroError, Stage
from backend.services.storage.tiger import TigerStore

KEY = "test-only-photon-key"
POST_URL = "https://x.com/user/status/1"
client = TestClient(app)


class RecordingProvider(PhotonProvider):
    def __init__(self):
        super().__init__(
            api_key=KEY, base_url="https://photon.invalid", sender_id="sender_test"
        )
        self.sent = []

    def send_message(self, conversation_id, body):
        self.sent.append((conversation_id, body))
        return {"message_id": f"msg_{len(self.sent)}", "status": "accepted"}


class Response:
    def __init__(self, body, status=200):
        self.body = json.dumps(body).encode()
        self.status = status

    def read(self, limit):
        return self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.delenv("PHOTON_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    provider = RecordingProvider()
    runs = []
    monkeypatch.setattr(
        photon_webhook,
        "run_analysis",
        lambda *args, **kwargs: runs.append((args, kwargs)),
    )
    inbound_tracker, summary_tracker = DeliveryTracker(), DeliveryTracker()
    app.dependency_overrides[get_tiger_store] = lambda: None
    app.dependency_overrides[photon_webhook.get_photon_provider] = lambda: provider
    app.dependency_overrides[photon_webhook.get_inbound_tracker] = lambda: (
        inbound_tracker
    )
    app.dependency_overrides[photon_webhook.get_summary_tracker] = lambda: (
        summary_tracker
    )
    yield provider, runs, summary_tracker
    app.dependency_overrides.clear()


def post(payload, key=KEY):
    raw = json.dumps(payload).encode()
    signature = hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/photon",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Photon-Signature": f"sha256={signature}",
        },
    )


def message(text, conversation_id="conv_1"):
    return {
        "conversation_id": conversation_id,
        "sender_handle": "+15550000000",
        "text": text,
    }


def test_valid_signature_with_url_schedules_analysis_and_acks(harness):
    provider, runs, summary_tracker = harness

    response = post(message(f"is this real? {POST_URL}."))

    job_id = "job_" + hashlib.sha1(("conv_1" + POST_URL).encode()).hexdigest()[:12]
    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "job_id": job_id}
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id)
    assert runs == [
        (
            (job_id, POST_URL, "conv_1"),
            {"store": None, "provider": provider, "tracker": summary_tracker},
        )
    ]
    assert provider.sent == [("conv_1", ACK_REPLY)]


@pytest.mark.parametrize("key", ["wrong-key", None])
def test_invalid_or_missing_signature_is_rejected_without_processing(harness, key):
    provider, runs, _ = harness
    if key is None:
        response = client.post("/webhooks/photon", json=message(POST_URL))
    else:
        response = post(message(POST_URL), key=key)

    assert response.status_code == 401
    assert provider.sent == []
    assert runs == []


def test_message_without_url_gets_canned_reply(harness):
    provider, runs, _ = harness

    response = post(message("hello there"))

    assert response.status_code == 200
    assert provider.sent == [("conv_1", NO_URL_REPLY)]
    assert NO_URL_REPLY == "Send me a post URL to check."
    assert runs == []


def test_duplicate_webhook_runs_one_job_and_sends_one_ack(harness):
    provider, runs, _ = harness

    first = post(message(POST_URL))
    second = post(message(POST_URL))

    assert first.status_code == second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert len(runs) == 1
    assert provider.sent == [("conv_1", ACK_REPLY)]


def test_outbound_echo_is_ignored(harness):
    provider, runs, _ = harness

    response = post({**message("Send me a post URL to check."), "is_from_me": True})

    assert response.status_code == 200
    assert provider.sent == []
    assert runs == []


def test_analysis_failure_replies_with_safe_message(harness, monkeypatch):
    provider, _, _ = harness

    def fail(*_args, **_kwargs):
        raise QuantroError(
            ErrorCode.SEARCH_FAILED, Stage.SEARCH_CANDIDATES, retryable=True
        )

    monkeypatch.setattr(photon_webhook, "run_analysis", fail)

    assert post(message(POST_URL)).status_code == 200
    assert provider.sent == [
        ("conv_1", ACK_REPLY),
        ("conv_1", "Sorry, I couldn't check that post. The source search failed."),
    ]


def test_send_summary_twice_for_same_job_makes_one_photon_call():
    provider = PhotonProvider(
        api_key=KEY, base_url="https://photon.invalid", sender_id="sender_test"
    )
    tracker = DeliveryTracker()
    with patch.object(photon, "build_opener") as build_opener:
        opener = Mock()
        build_opener.return_value = opener
        opener.open.return_value = Response(
            {"message_id": "photon_msg_1", "status": "queued"}
        )

        results = [
            send_summary(
                "conv_1",
                "No verified source was found.",
                "https://app.example/reports/report_job_1",
                job_id="job_1",
                provider=provider,
                tracker=tracker,
            )
            for _ in range(2)
        ]

    assert opener.open.call_count == 1
    assert [result.message_id for result in results] == ["photon_msg_1", "photon_msg_1"]
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://photon.invalid/messages"
    assert request.get_header("Authorization") == f"Bearer {KEY}"
    assert json.loads(request.data) == {
        "sender_id": "sender_test",
        "conversation_id": "conv_1",
        "body": "No verified source was found. View the evidence: https://app.example/reports/report_job_1",
    }


def test_send_message_failure_raises_retryable_send_failed():
    provider = PhotonProvider(
        api_key=KEY, base_url="https://photon.invalid", sender_id="sender_test"
    )
    with patch.object(photon, "build_opener") as build_opener:
        build_opener.return_value.open.side_effect = URLError("down")
        with pytest.raises(QuantroError) as caught:
            provider.send_message("conv_1", "hi")

    assert caught.value.code is ErrorCode.MESSAGE_SEND_FAILED
    assert caught.value.stage is Stage.SEND_SUMMARY
    assert caught.value.retryable is True


def test_signature_verification_fails_closed_without_key():
    raw = b'{"conversation_id":"conv_1"}'
    signature = hmac.new(b"", raw, hashlib.sha256).hexdigest()
    provider = PhotonProvider(api_key="", base_url="", sender_id="")

    assert (
        provider.verify_webhook_signature({"X-Photon-Signature": signature}, raw)
        is False
    )


def test_parse_inbound_and_extract_url():
    parsed = parse_inbound(
        {
            "data": {
                "chatGuid": "conv_9",
                "from": "+1555",
                "text": "see (https://a.example/p?q=1)",
                "attachments": [
                    {"url": "https://cdn.example/1.jpg"},
                    {"url": "file:///etc"},
                ],
            }
        }
    )

    assert parsed == {
        "conversation_id": "conv_9",
        "sender_handle": "+1555",
        "text": "see (https://a.example/p?q=1)",
        "media_urls": ["https://cdn.example/1.jpg"],
    }
    assert extract_url(parsed["text"]) == "https://a.example/p?q=1"
    assert extract_url("no link here") is None
    assert canned_reply({"text": "hi", "media_urls": []}) == NO_URL_REPLY
    with pytest.raises(ValueError):
        parse_inbound({"text": POST_URL})


def test_failed_analysis_can_be_retried_with_same_url(harness, monkeypatch):
    attempts = []

    def fail_once(*args, **kwargs):
        attempts.append(args)
        if len(attempts) == 1:
            raise QuantroError(
                ErrorCode.SEARCH_FAILED, Stage.SEARCH_CANDIDATES, retryable=True
            )

    monkeypatch.setattr(photon_webhook, "run_analysis", fail_once)
    assert post(message(POST_URL)).json()["status"] == "accepted"
    # Replaying the same event is always suppressed; a new user event may retry a proven early failure.
    assert post(message(POST_URL)).json()["status"] == "duplicate"
    assert (
        post({**message(POST_URL), "message_id": "new-user-event"}).json()["status"]
        == "accepted"
    )
    assert (
        post({**message(POST_URL), "message_id": "new-user-event"}).json()["status"]
        == "duplicate"
    )
    assert len(attempts) == 2


def test_unconfirmed_delivery_never_releases_inbound_dedupe(harness, monkeypatch):
    monkeypatch.setattr(
        photon_webhook, "run_analysis", lambda *a, **kw: Mock(delivery_status="failed")
    )
    assert post(message(POST_URL)).json()["status"] == "accepted"
    assert post(message(POST_URL)).json()["status"] == "duplicate"


def test_lost_http_response_after_provider_acceptance_does_not_resend(
    harness, monkeypatch
):
    provider, _, summary_tracker = harness
    attempts = []

    def accepted_but_response_lost(conversation_id, body):
        provider.sent.append((conversation_id, body))
        if body == ACK_REPLY:
            return {"message_id": "ack_1", "status": "accepted"}
        attempts.append(body)
        raise QuantroError(
            ErrorCode.MESSAGE_SEND_FAILED, Stage.SEND_SUMMARY, retryable=True
        )

    monkeypatch.setattr(provider, "send_message", accepted_but_response_lost)

    def analyze(job_id, _url, conversation_id, **_kwargs):
        with pytest.raises(QuantroError) as caught:
            send_summary(
                conversation_id,
                "Saved report",
                f"https://app.example/reports/report_{job_id}",
                job_id=job_id,
                provider=provider,
                tracker=summary_tracker,
            )
        assert caught.value.retryable is False
        return Mock(delivery_status="failed")

    monkeypatch.setattr(photon_webhook, "run_analysis", analyze)
    assert post(message(POST_URL)).json()["status"] == "accepted"
    assert post(message(POST_URL)).json()["status"] == "duplicate"
    assert post(message(POST_URL)).json()["status"] == "duplicate"
    assert len(attempts) == 1
    assert sum(body == ACK_REPLY for _, body in provider.sent) == 1


@pytest.mark.parametrize(
    "stage",
    [Stage.SEND_SUMMARY, Stage.SAVE_REPORT_AND_COMPLETE_JOB, Stage.RUN_ANALYSIS],
)
def test_unknown_or_delivery_boundary_failure_keeps_inbound_reservation(
    harness, monkeypatch, stage
):
    attempts = []

    def fail(*args, **_kwargs):
        attempts.append(args)
        raise QuantroError(
            ErrorCode.MESSAGE_SEND_FAILED
            if stage == Stage.SEND_SUMMARY
            else ErrorCode.STORAGE_FAILED,
            stage,
            retryable=True,
        )

    monkeypatch.setattr(photon_webhook, "run_analysis", fail)
    assert post(message(POST_URL)).json()["status"] == "accepted"
    assert post(message(POST_URL)).json()["status"] == "duplicate"
    assert len(attempts) == 1


@pytest.mark.parametrize("accepted", [False, True])
def test_existing_pending_or_accepted_summary_prohibits_early_failure_replay(
    harness, monkeypatch, accepted
):
    _, _, summary_tracker = harness
    job_id = photon_webhook.job_id_for("conv_1", POST_URL)
    assert summary_tracker.check_and_record("conv_1", job_id)
    if accepted:
        summary_tracker.record_message_id("conv_1", job_id, "previous_accepted_message")

    def fail(*_args, **_kwargs):
        raise QuantroError(
            ErrorCode.SEARCH_FAILED, Stage.SEARCH_CANDIDATES, retryable=True
        )

    monkeypatch.setattr(photon_webhook, "run_analysis", fail)
    assert post(message(POST_URL)).json()["status"] == "accepted"
    assert post(message(POST_URL)).json()["status"] == "duplicate"
    assert summary_tracker.has_record("conv_1", job_id)
    assert summary_tracker.get_message_id("conv_1", job_id) == (
        "previous_accepted_message" if accepted else None
    )


def test_nonretryable_early_failure_keeps_inbound_reservation(harness, monkeypatch):
    def fail(*_args, **_kwargs):
        raise QuantroError(ErrorCode.INVALID_URL, Stage.RETRIEVE_POST, retryable=False)

    monkeypatch.setattr(photon_webhook, "run_analysis", fail)
    assert post(message(POST_URL)).json()["status"] == "accepted"
    assert post(message(POST_URL)).json()["status"] == "duplicate"


def test_private_inbound_payload_is_not_logged(harness, caplog):
    with caplog.at_level("INFO", logger="backend.routes.photon_webhook"):
        assert post(message(f"PRIVATE_SECRET {POST_URL}")).status_code == 200
    assert "PRIVATE_SECRET" not in caplog.text
    assert POST_URL not in caplog.text


@pytest.mark.parametrize(
    "kind,status", [("signature", 401), ("json", 400), ("oversize", 413)]
)
def test_webhook_rejections_use_safe_canonical_error_envelope(harness, kind, status):
    provider, runs, _ = harness
    body = (
        b"PRIVATE_BODY"
        if kind == "signature"
        else b"{"
        if kind == "json"
        else b"x" * (64 * 1024 + 1)
    )
    headers = {"Content-Type": "application/json"}
    if kind != "signature":
        headers["X-Photon-Signature"] = hmac.new(
            KEY.encode(), body, hashlib.sha256
        ).hexdigest()
    response = client.post("/webhooks/photon", content=body, headers=headers)
    assert response.status_code == status
    error = response.json()["error"]
    assert set(error) == {"code", "stage", "message", "retryable"}
    assert error["stage"] == "run_analysis" and error["retryable"] is False
    assert "PRIVATE_BODY" not in response.text and not runs and not provider.sent


def signed_v0(body, timestamp="1700000000", secret="test-only-signing-secret"):
    signature = hmac.new(
        secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return {"X-Photon-Signature": "v0=" + signature, "X-Photon-Timestamp": timestamp}


def test_official_v0_signature_uses_dedicated_secret_and_exact_raw_bytes(monkeypatch):
    monkeypatch.setattr(photon.time, "time", lambda: 1700000000)
    provider = PhotonProvider(api_key="", webhook_secret="test-only-signing-secret")
    raw = '{"event":"new-message","data":{"text":"héllo"}}'.encode()
    headers = signed_v0(raw)
    assert provider.verify_webhook_signature(headers, raw)
    assert not provider.verify_webhook_signature(headers, raw + b" ")
    assert not provider.verify_webhook_signature(signed_v0(raw, secret=KEY), raw)


@pytest.mark.parametrize(
    "timestamp",
    ["1699999699", "1700000301", "", "not-a-time", "1.7e9", "-1", "9" * 100],
)
def test_official_signature_rejects_stale_future_or_malformed_timestamp(
    monkeypatch, timestamp
):
    monkeypatch.setattr(photon.time, "time", lambda: 1700000000)
    provider = PhotonProvider(webhook_secret="test-only-signing-secret")
    raw = b"{}"
    assert not provider.verify_webhook_signature(signed_v0(raw, timestamp), raw)


@pytest.mark.parametrize(
    "signature", ["", "v1=" + "a" * 64, "v0=é", "v0=" + "a" * 65, "sha256=" + "a" * 64]
)
def test_official_signature_rejects_bad_version_format_and_missing_timestamp(
    monkeypatch, signature
):
    monkeypatch.setattr(photon.time, "time", lambda: 1700000000)
    provider = PhotonProvider(webhook_secret="test-only-signing-secret")
    assert not provider.verify_webhook_signature(
        {"X-Photon-Signature": signature, "X-Photon-Timestamp": "1700000000"}, b"{}"
    )
    assert not provider.verify_webhook_signature(
        signed_v0(b"{}").copy() | {"X-Photon-Timestamp": ""}, b"{}"
    )
    assert not provider.verify_webhook_signature(
        {"X-Photon-Signature": signed_v0(b"{}")["X-Photon-Signature"]}, b"{}"
    )


@pytest.mark.parametrize("variable", ["APP_ENV", "ENVIRONMENT"])
def test_production_rejects_legacy_signature_without_dedicated_secret(
    monkeypatch, variable
):
    monkeypatch.setenv(variable, " Production ")
    provider = PhotonProvider(api_key=KEY, webhook_secret="")
    raw = b"{}"
    legacy = hmac.new(KEY.encode(), raw, hashlib.sha256).hexdigest()
    assert not provider.verify_webhook_signature(
        {"X-Photon-Signature": "sha256=" + legacy}, raw
    )


def test_production_accepts_fresh_dedicated_secret_signature_and_dedupes(
    harness, monkeypatch
):
    provider, runs, _ = harness
    provider.webhook_secret = "test-only-signing-secret"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(photon.time, "time", lambda: 1700000000)
    raw = json.dumps({"event": "new-message", "data": message(POST_URL)}).encode()
    first = client.post("/webhooks/photon", content=raw, headers=signed_v0(raw))
    second = client.post("/webhooks/photon", content=raw, headers=signed_v0(raw))
    assert first.status_code == 200 and first.json()["status"] == "accepted"
    assert second.status_code == 200 and second.json()["status"] == "duplicate"
    assert len(runs) == 1 and provider.sent == [("conv_1", ACK_REPLY)]


def test_official_non_message_event_is_ignored_without_reply_or_analysis(
    harness, monkeypatch
):
    provider, runs, _ = harness
    provider.webhook_secret = "test-only-signing-secret"
    monkeypatch.setattr(photon.time, "time", lambda: 1700000000)
    raw = json.dumps({"event": "typing-indicator", "data": message(POST_URL)}).encode()
    response = client.post("/webhooks/photon", content=raw, headers=signed_v0(raw))
    assert response.status_code == 200 and response.json() == {"status": "ignored"}
    assert not runs and not provider.sent


@pytest.mark.parametrize("timestamp", ["1699999700", "1700000300"])
def test_official_signature_accepts_exact_clock_skew_boundary(monkeypatch, timestamp):
    monkeypatch.setattr(photon.time, "time", lambda: 1700000000)
    provider = PhotonProvider(api_key="", webhook_secret="test-only-signing-secret")
    assert provider.verify_webhook_signature(signed_v0(b"{}", timestamp), b"{}")


def test_webhook_signing_secret_cannot_reuse_the_send_api_key(monkeypatch):
    monkeypatch.setattr(photon.time, "time", lambda: 1700000000)
    provider = PhotonProvider(api_key=KEY, webhook_secret=KEY)
    assert not provider.verify_webhook_signature(signed_v0(b"{}", secret=KEY), b"{}")


@pytest.fixture
def durable_store(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    store = TigerStore(f"sqlite:///{tmp_path / 'delivery.db'}")
    store.ensure_schema()
    yield store
    store.close()


def test_tiger_tracker_claims_survive_new_instances_and_are_namespaced(durable_store):
    first = TigerDeliveryTracker(durable_store, "photon_inbound")
    second = TigerDeliveryTracker(durable_store, "photon_inbound")
    summary = TigerDeliveryTracker(durable_store, "photon_summary")
    assert first.check_and_record("private-conversation", "event_1")
    assert not second.check_and_record("private-conversation", "event_1")
    assert second.has_record("private-conversation", "event_1")
    assert summary.check_and_record("private-conversation", "event_1")
    with durable_store._engine.connect() as connection:
        rows = connection.execute(durable_store.deliveries.select()).mappings().all()
    assert len(rows) == 2
    assert all(
        len(row["id"]) == 64 and "private-conversation" not in str(row) for row in rows
    )


def test_tiger_summary_unknown_acceptance_stays_reserved_across_restart(durable_store):
    calls = []
    provider = Mock()
    provider.send.side_effect = lambda *args: (
        calls.append(args)
        or (_ for _ in ()).throw(URLError("lost response after acceptance"))
    )
    for _ in range(2):
        tracker = TigerDeliveryTracker(durable_store, "photon_summary")
        with pytest.raises(QuantroError) as caught:
            send_summary(
                "conv_1",
                "Summary",
                "https://app.example/reports/report_job_1",
                job_id="job_1",
                provider=provider,
                tracker=tracker,
            )
        assert not caught.value.retryable
    assert len(calls) == 1
    assert (
        tracker.has_record("conv_1", "job_1")
        and tracker.get_message_id("conv_1", "job_1") is None
    )


def test_tiger_summary_acceptance_is_preserved_and_cannot_be_released(durable_store):
    tracker = TigerDeliveryTracker(durable_store, "photon_summary")
    assert tracker.check_and_record("conv_1", "job_1")
    tracker.record_message_id("conv_1", "job_1", "accepted_1")
    tracker.record_message_id("conv_1", "job_1", "different_receipt")
    tracker.release("conv_1", "job_1")
    restarted = TigerDeliveryTracker(durable_store, "photon_summary")
    assert restarted.get_message_id("conv_1", "job_1") == "accepted_1"
    assert not restarted.check_and_record("conv_1", "job_1")


def test_http_event_and_canned_reply_claims_survive_tracker_restart(
    harness, durable_store
):
    provider, runs, _ = harness
    app.dependency_overrides[photon_webhook.get_inbound_tracker] = lambda: (
        TigerDeliveryTracker(durable_store, "photon_inbound")
    )
    assert post(message(POST_URL)).json()["status"] == "accepted"
    assert post(message(POST_URL)).json()["status"] == "duplicate"
    assert post(message("hello")).json()["status"] == "needs_url"
    assert post(message("hello")).json()["status"] == "duplicate"
    assert len(runs) == 1 and provider.sent == [
        ("conv_1", ACK_REPLY),
        ("conv_1", NO_URL_REPLY),
    ]


def test_two_tiger_tracker_instances_atomically_claim_one_event(durable_store):
    ready = Barrier(2)

    def claim(_index):
        tracker = TigerDeliveryTracker(durable_store, "photon_inbound")
        ready.wait(timeout=3)
        return tracker.check_and_record("conv_1", "event_concurrent")

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(claim, range(2)))
    assert sorted(results) == [False, True]


def test_failed_job_release_never_releases_durable_raw_event(
    harness, durable_store, monkeypatch
):
    provider, runs, _ = harness
    app.dependency_overrides[photon_webhook.get_inbound_tracker] = lambda: (
        TigerDeliveryTracker(durable_store, "photon_inbound")
    )

    def fail(*args, **_kwargs):
        runs.append(args)
        raise QuantroError(
            ErrorCode.SEARCH_FAILED, Stage.SEARCH_CANDIDATES, retryable=True
        )

    monkeypatch.setattr(photon_webhook, "run_analysis", fail)
    assert post(message(POST_URL)).json()["status"] == "accepted"
    assert post(message(POST_URL)).json()["status"] == "duplicate"
    tracker = TigerDeliveryTracker(durable_store, "photon_inbound")
    assert not tracker.has_record(
        "conv_1", photon_webhook.job_id_for("conv_1", POST_URL)
    )
    assert len(runs) == 1 and len(provider.sent) == 2  # One ACK and one failure reply.
