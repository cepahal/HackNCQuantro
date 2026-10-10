import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from backend.routes.common import error_response, get_tiger_store
from backend.services.messaging.delivery_tracker import DeliveryTracker
from backend.services.messaging.inbound import (
    ACK_REPLY,
    canned_reply,
    extract_url,
    is_outbound_echo,
    message_timestamp,
    parse_inbound,
)
from backend.services.messaging.photon import PhotonProvider
from backend.services.reporting.run_analysis import run_analysis
from backend.services.shared.errors import ErrorCode, QuantroError, Stage
from backend.services.storage.tiger import TigerStore

logger = logging.getLogger(__name__)
router = APIRouter()

_MAX_BODY_BYTES = 64 * 1024
_MAX_LOGGED_PAYLOAD_CHARS = 4096
# Applied only when the signed body carries a recognizable timestamp; no header is assumed.
MAX_WEBHOOK_CLOCK_SKEW = timedelta(minutes=10)
# Replay/duplicate protection is these process-local trackers: forgotten on restart and
# not shared between instances, so run a single API instance for the Photon webhook.
_inbound_tracker = DeliveryTracker()
_summary_tracker = DeliveryTracker()


def get_photon_provider() -> PhotonProvider:
    return PhotonProvider()


def get_inbound_tracker() -> DeliveryTracker:
    return _inbound_tracker


def get_summary_tracker() -> DeliveryTracker:
    return _summary_tracker


def job_id_for(conversation_id: str, url: str) -> str:
    return "job_" + hashlib.sha1((conversation_id + url).encode("utf-8")).hexdigest()[:12]


def _reply(provider: PhotonProvider, conversation_id: str, body: str) -> None:
    try:
        provider.send_message(conversation_id, body)
    except QuantroError as error:
        logger.warning("Photon reply failed: %s", error.code.value)


def analyze_and_reply(
    job_id: str,
    url: str,
    conversation_id: str,
    store: TigerStore | None,
    provider: PhotonProvider,
    tracker: DeliveryTracker,
    inbound_tracker: DeliveryTracker | None = None,
) -> None:
    try:
        result = run_analysis(job_id, url, conversation_id, store=store, provider=provider, tracker=tracker)
        if getattr(result, "delivery_status", None) == "failed" and inbound_tracker is not None:
            # Reuse the saved report on retry; do not repeat a successful delivery.
            inbound_tracker.release(conversation_id, job_id)
    except QuantroError as error:
        if inbound_tracker is not None:
            inbound_tracker.release(conversation_id, job_id)
        logger.warning("Photon analysis %s failed at %s: %s", job_id, error.stage.value, error.code.value)
        _reply(provider, conversation_id, f"Sorry, I couldn't check that post. {error.message}")


@router.post("/webhooks/photon")
async def photon_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    store: Annotated[TigerStore, Depends(get_tiger_store)],
    provider: Annotated[PhotonProvider, Depends(get_photon_provider)],
    tracker: Annotated[DeliveryTracker, Depends(get_inbound_tracker)],
    summary_tracker: Annotated[DeliveryTracker, Depends(get_summary_tracker)],
) -> JSONResponse:
    raw_body = await request.body()
    if len(raw_body) > _MAX_BODY_BYTES:
        return error_response(413, ErrorCode.PROCESSING_FAILED, Stage.RUN_ANALYSIS, "Payload too large")
    if not provider.verify_webhook_signature(dict(request.headers), raw_body):
        logger.warning("Rejected Photon webhook signature; header names: %s", sorted(request.headers.keys()))
        return error_response(401, ErrorCode.PROCESSING_FAILED, Stage.RUN_ANALYSIS, "Invalid webhook signature")

    try:
        payload = json.loads(raw_body)
        logger.info("Photon inbound payload: %s", json.dumps(payload)[:_MAX_LOGGED_PAYLOAD_CHARS])
        message = parse_inbound(payload)
    except (ValueError, RecursionError):
        return error_response(400, ErrorCode.PROCESSING_FAILED, Stage.RUN_ANALYSIS, "Invalid Photon payload")
    sent_at = message_timestamp(payload)
    if sent_at is not None and abs(datetime.now(UTC) - sent_at) > MAX_WEBHOOK_CLOCK_SKEW:
        logger.warning("Rejected Photon webhook with a stale timestamp")
        return error_response(401, ErrorCode.PROCESSING_FAILED, Stage.RUN_ANALYSIS, "Stale webhook timestamp")
    if is_outbound_echo(payload, provider.sender_id):
        return JSONResponse(content={"status": "ignored"})

    conversation_id = message["conversation_id"]
    url = extract_url(message["text"])
    if url is None:
        await run_in_threadpool(_reply, provider, conversation_id, canned_reply(message))
        return JSONResponse(content={"status": "needs_url"})

    job_id = job_id_for(conversation_id, url)
    if not tracker.check_and_record(conversation_id, job_id):
        return JSONResponse(content={"status": "duplicate", "job_id": job_id})
    background_tasks.add_task(analyze_and_reply, job_id, url, conversation_id, store, provider, summary_tracker, tracker)
    await run_in_threadpool(_reply, provider, conversation_id, ACK_REPLY)
    return JSONResponse(content={"status": "accepted", "job_id": job_id})
