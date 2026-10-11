"""send_summary stage of run_analysis.

Sends the short verdict and report link back to the user's conversation after
the report has been saved. A failure here is reported as MESSAGE_SEND_FAILED
with transport-specific retryability and never touches the saved report. When a job_id is given,
each (job_id, conversation_id) pair is sent at most once; repeats return the
original message_id instead of messaging the user again.
"""

import hashlib
import re
import threading
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from ..external_apis.photon import send_summary_request
from ..shared.contracts import validate_contract_result
from ..shared.errors import ErrorCode, QuantroError, Stage
from ..shared.errors import QuantroError as _ContractError
from ..shared.errors import Stage as _ContractStage
from ..shared.errors import normalize_exception as _normalize_contract_exception
from ..shared.types import (
    ConfidenceLevel,
    DeliveryResult,
    Outcome,
    ReportV2,
    TranscriptSentence,
)
from ..shared.types import DeliveryResult as _ReturnModel


@dataclass(frozen=True)
class SendReceipt:
    accepted: bool
    message_id: str | None


class MessagingProvider(Protocol):
    def send(self, conversation_id: str, text: str) -> SendReceipt: ...


class PhotonProvider:
    def send(self, conversation_id: str, text: str) -> SendReceipt:
        raise ValueError(
            "Photon report delivery requires a summary, report URL, and stable job ID."
        )

    def send_report(
        self, conversation_id: str, summary: str, report_url: str, *, job_id: str | None
    ) -> SendReceipt:
        result = send_summary_request(
            conversation_id, summary, report_url, job_id=job_id or ""
        )
        if "error" in result:
            error = result["error"]
            raise QuantroError(
                code=ErrorCode.MESSAGE_SEND_FAILED,
                stage=Stage.SEND_SUMMARY,
                message=error["message"],
                retryable=error["retryable"],
            )
        return SendReceipt(accepted=True, message_id=result["message_id"])


class DeliveryTracker(Protocol):
    def check_and_record(self, conversation_id: str, job_id: str) -> bool:
        """Return True if this pair was not seen before and is now reserved."""
        ...

    def record_message_id(
        self, conversation_id: str, job_id: str, message_id: str
    ) -> None: ...

    def get_message_id(self, conversation_id: str, job_id: str) -> str | None: ...

    def has_record(self, conversation_id: str, job_id: str) -> bool: ...

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

    def record_message_id(
        self, conversation_id: str, job_id: str, message_id: str
    ) -> None:
        with self._lock:
            self._deliveries[(conversation_id, job_id)] = message_id

    def get_message_id(self, conversation_id: str, job_id: str) -> str | None:
        with self._lock:
            return self._deliveries.get((conversation_id, job_id))

    def has_record(self, conversation_id: str, job_id: str) -> bool:
        """Distinguish a never-attempted delivery from a pending or unconfirmed attempt."""
        with self._lock:
            return (conversation_id, job_id) in self._deliveries

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


def format_context_summary(report: ReportV2) -> str:
    """Quote only observed source sentences outside the verified matched passage."""
    missing = "Not available in the source transcript."
    unavailable = "No verified source context available."

    def message(before: str, after: str) -> str:
        return f"Context before: {before}\n\nContext after: {after}"

    source, alignment, context = report.source, report.alignment, report.context
    if (
        report.outcome != Outcome.MATCHED or source is None or alignment is None or context is None
        or alignment.status != "verified" or not alignment.matches
        or alignment.match_quality.confidence.level not in {ConfidenceLevel.HIGH, ConfidenceLevel.MODERATE}
        or source.post.media.media_id == report.submitted.post.media.media_id
        or alignment.clip_media_id != report.submitted.post.media.media_id
        or alignment.source_media_id != source.post.media.media_id
        or context.source_media_id != source.post.media.media_id
        or source.transcript.status == "unavailable"
    ):
        return message(unavailable, unavailable)
    matches = {item.match_id: item for item in alignment.matches}
    if len(matches) != len(alignment.matches) or any(
        not item.match_id or item.source_start_seconds >= item.source_end_seconds
        for item in matches.values()
    ):
        return message(unavailable, unavailable)
    first = min(item.source_start_seconds for item in matches.values())
    last = max(item.source_end_seconds for item in matches.values())
    canonical: dict[str, TranscriptSentence] = {}
    for sentence in source.transcript.sentences:
        if sentence.sentence_id in canonical and canonical[sentence.sentence_id] != sentence:
            return message(unavailable, unavailable)
        canonical[sentence.sentence_id] = sentence
    observed: dict[str, TranscriptSentence] = {}
    for window in context.windows:
        if (window.coverage == "unavailable" or not window.match_ids
                or window.requested_start_seconds >= window.requested_end_seconds
                or any(identity not in matches for identity in window.match_ids)):
            continue
        if any(not window.requested_start_seconds <= matches[identity].source_start_seconds
                   < matches[identity].source_end_seconds <= window.requested_end_seconds
               for identity in window.match_ids):
            continue
        for sentence in window.sentences:
            if (canonical.get(sentence.sentence_id) == sentence and sentence.text.strip()
                    and sentence.start_seconds < sentence.end_seconds
                    and sentence.start_seconds < window.requested_end_seconds
                    and sentence.end_seconds > window.requested_start_seconds
                    and any(interval.start_seconds <= sentence.start_seconds
                            < sentence.end_seconds <= interval.end_seconds
                            for interval in source.transcript.processed_ranges)):
                observed[sentence.sentence_id] = sentence
    before = [sentence for sentence in observed.values() if sentence.end_seconds <= first]
    after = [sentence for sentence in observed.values() if sentence.start_seconds >= last]

    def excerpt(sentences: list[TranscriptSentence], *, preceding: bool) -> str:
        if not sentences:
            return missing
        sentence = (max(sentences, key=lambda item: (item.end_seconds, item.start_seconds, item.sentence_id))
                    if preceding else min(sentences, key=lambda item: (item.start_seconds, item.end_seconds, item.sentence_id)))
        text = re.sub(r"\s+", " ", sentence.text).strip()
        return text if len(text) <= 650 else text[:649].rstrip() + "…"

    return message(excerpt(before, preceding=True), excerpt(after, preceding=False))


def _send_failed(message: str, *, retryable: bool = True) -> QuantroError:
    return QuantroError(
        code=ErrorCode.MESSAGE_SEND_FAILED,
        stage=Stage.SEND_SUMMARY,
        message=message,
        retryable=retryable,
    )


def _delivery_job_id(report_url: str) -> str:
    """Keep the public three-argument call idempotent using the saved report URL."""
    try:
        path = urlsplit(report_url).path
    except (TypeError, ValueError):
        return ""
    match = re.fullmatch(r"/reports/report_([A-Za-z0-9_-]{1,128})/?", path)
    if match:
        return match.group(1)
    if isinstance(report_url, str):
        return "delivery_" + hashlib.sha256(report_url.encode("utf-8")).hexdigest()[:32]
    return ""


def send_summary(
    conversation_id: str,
    summary: str,
    report_url: str,
    *,
    job_id: str | None = None,
    provider: MessagingProvider | None = None,
    tracker: DeliveryTracker | None = None,
) -> DeliveryResult:
    # aligned with shared/types.py on 2026-10-10
    try:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (conversation_id, summary, report_url)
        ):
            raise _ContractError("MESSAGE_SEND_FAILED", _ContractStage.SEND_SUMMARY)
        if job_id is None:
            job_id = _delivery_job_id(report_url)
        provider = provider if provider is not None else get_messaging_provider()
        tracker = tracker if tracker is not None else get_delivery_tracker()

        # The Photon bridge owns durable, payload-aware delivery tracking across restarts.
        if isinstance(provider, PhotonProvider):
            receipt = provider.send_report(
                conversation_id, summary, report_url, job_id=job_id
            )
            return validate_contract_result(
                _ReturnModel,
                {
                    "conversation_id": conversation_id,
                    "message_id": receipt.message_id,
                    "status": "accepted",
                },
                _ContractStage.SEND_SUMMARY,
                json_native=False,
            )

        if job_id is not None and not tracker.check_and_record(conversation_id, job_id):
            existing_id = tracker.get_message_id(conversation_id, job_id)
            if existing_id is None:
                raise _send_failed(
                    "A summary delivery was already attempted. Verify its outcome before another send.",
                    retryable=False,
                )
            return validate_contract_result(
                _ReturnModel,
                DeliveryResult(
                    conversation_id=conversation_id,
                    message_id=existing_id,
                    status="accepted",
                ),
                _ContractStage.SEND_SUMMARY,
                json_native=False,
            )

        try:
            receipt = provider.send(
                conversation_id, format_summary_message(summary, report_url)
            )
        except Exception as exc:
            # A lost response can follow provider acceptance. Retain the reservation after every attempt.
            raise _send_failed(
                "Summary delivery could not be confirmed. Verify its outcome before another send.",
                retryable=False,
            ) from exc

        if not receipt.accepted or not receipt.message_id:
            raise _send_failed(
                "Summary delivery could not be confirmed. Verify its outcome before another send.",
                retryable=False,
            )

        if job_id is not None:
            tracker.record_message_id(conversation_id, job_id, receipt.message_id)
        return validate_contract_result(
            _ReturnModel,
            DeliveryResult(
                conversation_id=conversation_id,
                message_id=receipt.message_id,
                status="accepted",
            ),
            _ContractStage.SEND_SUMMARY,
            json_native=False,
        )
    except Exception as exc:  # noqa: BLE001 - Public contract boundary redacts and normalizes failures.
        raise _normalize_contract_exception(_ContractStage.SEND_SUMMARY, exc) from None
