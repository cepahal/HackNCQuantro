import hashlib
import hmac
import json
import re
from unittest.mock import Mock, patch
from urllib.error import URLError

import pytest
from fastapi.testclient import TestClient

import backend.routes.photon_webhook as photon_webhook
from backend.main import app
from backend.routes.common import get_tiger_store
from backend.services.messaging import photon
from backend.services.messaging.delivery_tracker import DeliveryTracker
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

KEY = "test-only-photon-key"
POST_URL = "https://x.com/user/status/1"
client = TestClient(app)


class RecordingProvider(PhotonProvider):
    def __init__(self):
        super().__init__(api_key=KEY, base_url="https://photon.invalid", sender_id="sender_test")
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
    provider = RecordingProvider()
    runs = []
    monkeypatch.setattr(photon_webhook, "run_analysis", lambda *args, **kwargs: runs.append((args, kwargs)))
    inbound_tracker, summary_tracker = DeliveryTracker(), DeliveryTracker()
    app.dependency_overrides[get_tiger_store] = lambda: None
    app.dependency_overrides[photon_webhook.get_photon_provider] = lambda: provider
    app.dependency_overrides[photon_webhook.get_inbound_tracker] = lambda: inbound_tracker
    app.dependency_overrides[photon_webhook.get_summary_tracker] = lambda: summary_tracker
    yield provider, runs, summary_tracker
    app.dependency_overrides.clear()


def post(payload, key=KEY):
    raw = json.dumps(payload).encode()
    signature = hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/photon", content=raw,
        headers={"Content-Type": "application/json", "X-Photon-Signature": f"sha256={signature}"},
    )


def message(text, conversation_id="conv_1"):
    return {"conversation_id": conversation_id, "sender_handle": "+15550000000", "text": text}


def test_valid_signature_with_url_schedules_analysis_and_acks(harness):
    provider, runs, summary_tracker = harness

    response = post(message(f"is this real? {POST_URL}."))

    job_id = "job_" + hashlib.sha1(("conv_1" + POST_URL).encode()).hexdigest()[:12]
    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "job_id": job_id}
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id)
    assert runs == [((job_id, POST_URL, "conv_1"), {"store": None, "provider": provider, "tracker": summary_tracker})]
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
        raise QuantroError(ErrorCode.SEARCH_FAILED, Stage.SEARCH_CANDIDATES, retryable=True)

    monkeypatch.setattr(photon_webhook, "run_analysis", fail)

    assert post(message(POST_URL)).status_code == 200
    assert provider.sent == [("conv_1", ACK_REPLY), ("conv_1", "Sorry, I couldn't check that post. The source search failed.")]


def test_send_summary_twice_for_same_job_makes_one_photon_call():
    provider = PhotonProvider(api_key=KEY, base_url="https://photon.invalid", sender_id="sender_test")
    tracker = DeliveryTracker()
    with patch.object(photon, "build_opener") as build_opener:
        opener = Mock()
        build_opener.return_value = opener
        opener.open.return_value = Response({"message_id": "photon_msg_1", "status": "queued"})

        results = [
            send_summary("conv_1", "No verified source was found.", "https://app.example/reports/report_job_1",
                         job_id="job_1", provider=provider, tracker=tracker)
            for _ in range(2)
        ]

    assert opener.open.call_count == 1
    assert [result.message_id for result in results] == ["photon_msg_1", "photon_msg_1"]
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://photon.invalid/messages"
    assert request.get_header("Authorization") == f"Bearer {KEY}"
    assert json.loads(request.data) == {
        "sender_id": "sender_test", "conversation_id": "conv_1",
        "body": "No verified source was found. View the evidence: https://app.example/reports/report_job_1",
    }


def test_send_message_failure_raises_retryable_send_failed():
    provider = PhotonProvider(api_key=KEY, base_url="https://photon.invalid", sender_id="sender_test")
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

    assert provider.verify_webhook_signature({"X-Photon-Signature": signature}, raw) is False


def test_parse_inbound_and_extract_url():
    parsed = parse_inbound({"data": {"chatGuid": "conv_9", "from": "+1555", "text": "see (https://a.example/p?q=1)",
                                     "attachments": [{"url": "https://cdn.example/1.jpg"}, {"url": "file:///etc"}]}})

    assert parsed == {"conversation_id": "conv_9", "sender_handle": "+1555", "text": "see (https://a.example/p?q=1)",
                      "media_urls": ["https://cdn.example/1.jpg"]}
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
            raise QuantroError(ErrorCode.SEARCH_FAILED, Stage.SEARCH_CANDIDATES, retryable=True)
    monkeypatch.setattr(photon_webhook, "run_analysis", fail_once)
    assert post(message(POST_URL)).json()["status"] == "accepted"
    assert post(message(POST_URL)).json()["status"] == "accepted"
    assert post(message(POST_URL)).json()["status"] == "duplicate"
    assert len(attempts) == 2


def test_failed_delivery_retries_saved_analysis(harness, monkeypatch):
    monkeypatch.setattr(photon_webhook, "run_analysis", lambda *a, **kw: Mock(delivery_status="failed"))
    assert post(message(POST_URL)).json()["status"] == "accepted"
    assert post(message(POST_URL)).json()["status"] == "accepted"
