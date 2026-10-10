"""send_summary stage of run_analysis.

Sends the short verdict and report link back to the user's conversation after
the report has been saved. A failure here is reported as a retryable
MESSAGE_SEND_FAILED and never touches the saved report. When a job_id is given,
each (job_id, conversation_id) pair is sent at most once; repeats return the
original message_id instead of messaging the user again.
"""

import threading
from dataclasses import dataclass
from typing import Protocol

from .errors import ErrorCode, QuantroError, Stage
from .types import DeliveryResult


@dataclass(frozen=True)
class SendReceipt:
    accepted: bool
    message_id: str | None


class MessagingProvider(Protocol):
    def send(self, conversation_id: str, text: str) -> SendReceipt: ...


class PhotonProvider:
    def send(self, conversation_id: str, text: str) -> SendReceipt:
        raise NotImplementedError("TODO: send through Photon Spectrum once messaging is configured")


class DeliveryTracker(Protocol):
    def check_and_record(self, conversation_id: str, job_id: str) -> bool:
        """Return True if this pair was not seen before and is now reserved."""
        ...

    def record_message_id(self, conversation_id: str, job_id: str, message_id: str) -> None: ...

    def get_message_id(self, conversation_id: str, job_id: str) -> str | None: ...

    def release(self, conversation_id: str, job_id: str) -> None: ...


class InMemoryDeliveryTracker:
    """Process-local tracker for the hackathon demo; forgets deliveries on restart."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._deliveries: dict[tuple[str, str], str | None] = {}

    def check_and_record(self, conversation_id: str, job_id: str) -> bool:
        key = (conversation_id, job_id)
        with self._lock:
            if key in self._deliveries:
                return False
            self._deliveries[key] = None
            return True

    def record_message_id(self, conversation_id: str, job_id: str, message_id: str) -> None:
        with self._lock:
            self._deliveries[(conversation_id, job_id)] = message_id

    def get_message_id(self, conversation_id: str, job_id: str) -> str | None:
        with self._lock:
            return self._deliveries.get((conversation_id, job_id))

    def release(self, conversation_id: str, job_id: str) -> None:
        with self._lock:
            self._deliveries.pop((conversation_id, job_id), None)


_default_provider = PhotonProvider()
_default_tracker = InMemoryDeliveryTracker()


def get_messaging_provider() -> MessagingProvider:
    return _default_provider


def get_delivery_tracker() -> DeliveryTracker:
    return _default_tracker


def format_summary_message(summary: str, report_url: str) -> str:
    return f"{summary} View the evidence: {report_url}"


def _send_failed(message: str) -> QuantroError:
    return QuantroError(
        code=ErrorCode.MESSAGE_SEND_FAILED, stage=Stage.SEND_SUMMARY, message=message, retryable=True
    )


def send_summary(
    conversation_id: str,
    summary: str,
    report_url: str,
    *,
    job_id: str | None = None,
    provider: MessagingProvider | None = None,
    tracker: DeliveryTracker | None = None,
) -> DeliveryResult:
    provider = provider if provider is not None else get_messaging_provider()
    tracker = tracker if tracker is not None else get_delivery_tracker()

    if job_id is not None and not tracker.check_and_record(conversation_id, job_id):
        existing_id = tracker.get_message_id(conversation_id, job_id)
        if existing_id is None:
            raise _send_failed("A summary for this job is already being sent.")
        return DeliveryResult(conversation_id=conversation_id, message_id=existing_id, status="accepted")

    try:
        receipt = provider.send(conversation_id, format_summary_message(summary, report_url))
    except Exception as exc:
        if job_id is not None:
            tracker.release(conversation_id, job_id)
        raise _send_failed("The summary message could not be sent.") from exc

    if not receipt.accepted or not receipt.message_id:
        if job_id is not None:
            tracker.release(conversation_id, job_id)
        raise _send_failed("The messaging provider did not accept the summary.")

    if job_id is not None:
        tracker.record_message_id(conversation_id, job_id, receipt.message_id)
    return DeliveryResult(conversation_id=conversation_id, message_id=receipt.message_id, status="accepted")
