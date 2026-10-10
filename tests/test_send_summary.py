import pytest

from backend.services.reporting.errors import ErrorCode, QuantroError, Stage
from backend.services.reporting.send_summary import (
    InMemoryDeliveryTracker,
    PhotonProvider,
    SendReceipt,
    send_summary,
)


class FakeProvider:
    def __init__(self, receipts=None, error=None):
        self.sent = []
        self._receipts = list(receipts or [])
        self._error = error

    def send(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        if self._error is not None:
            raise self._error
        if self._receipts:
            return self._receipts.pop(0)
        return SendReceipt(accepted=True, message_id=f"msg_{len(self.sent)}")


def test_accepted_delivery_returns_expected_shape():
    provider = FakeProvider()

    result = send_summary(
        "conv_1", "A possible earlier source was found.", "https://app.example/reports/report_job_1",
        job_id="job_1", provider=provider, tracker=InMemoryDeliveryTracker(),
    )

    assert result.model_dump() == {"conversation_id": "conv_1", "message_id": "msg_1", "status": "accepted"}
    assert provider.sent == [
        ("conv_1", "A possible earlier source was found. View the evidence: https://app.example/reports/report_job_1")
    ]


@pytest.mark.parametrize(
    "provider",
    [
        FakeProvider(error=ConnectionError("down")),
        FakeProvider(receipts=[SendReceipt(accepted=False, message_id=None)]),
        FakeProvider(receipts=[SendReceipt(accepted=True, message_id=None)]),
    ],
)
def test_unconfirmed_delivery_raises_retryable_send_failed(provider):
    with pytest.raises(QuantroError) as caught:
        send_summary("conv_1", "s", "u", job_id="job_1", provider=provider, tracker=InMemoryDeliveryTracker())

    assert caught.value.code is ErrorCode.MESSAGE_SEND_FAILED
    assert caught.value.stage is Stage.SEND_SUMMARY
    assert caught.value.retryable is True


def test_failed_send_can_be_retried():
    tracker = InMemoryDeliveryTracker()
    with pytest.raises(QuantroError):
        send_summary("conv_1", "s", "u", job_id="job_1", provider=FakeProvider(error=TimeoutError()), tracker=tracker)

    result = send_summary("conv_1", "s", "u", job_id="job_1", provider=FakeProvider(), tracker=tracker)

    assert result.message_id == "msg_1"


def test_photon_stub_failure_is_normalized():
    with pytest.raises(QuantroError) as caught:
        send_summary("conv_1", "s", "u", provider=PhotonProvider(), tracker=InMemoryDeliveryTracker())

    assert caught.value.code is ErrorCode.MESSAGE_SEND_FAILED


def test_check_and_record_reserves_pair_once():
    tracker = InMemoryDeliveryTracker()

    assert tracker.check_and_record("conv_1", "job_1") is True
    assert tracker.check_and_record("conv_1", "job_1") is False
    assert tracker.check_and_record("conv_1", "job_2") is True


def test_three_argument_delivery_uses_report_identity_for_deduplication():
    tracker = InMemoryDeliveryTracker()
    provider = FakeProvider()
    first = send_summary("conv_1", "Summary", "https://app.example/reports/report_job_1", provider=provider, tracker=tracker)
    second = send_summary("conv_1", "Summary", "https://app.example/reports/report_job_1", provider=provider, tracker=tracker)
    assert first.model_dump() == second.model_dump()
    assert len(provider.sent) == 1
