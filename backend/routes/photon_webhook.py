import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from backend.routes.common import get_tiger_store, request_error_response
from backend.services.messaging.delivery_tracker import TigerDeliveryTracker
from backend.services.messaging.inbound import (
    ACK_REPLY,
    canned_reply,
    extract_url,
    is_outbound_echo,
    parse_inbound,
    message_timestamp,
)
from backend.services.messaging.photon import PhotonProvider
from backend.services.resolution.run import run_analysis
from backend.services.reporting.send_summary import DeliveryTracker
from backend.services.shared.errors import QuantroError, Stage
from backend.services.storage.tiger import TigerStore

logger = logging.getLogger(__name__)
router = APIRouter()

_MAX_BODY_BYTES = 64 * 1024
_PRE_DELIVERY_STAGES = frozenset(
    {
        Stage.RETRIEVE_POST,
        Stage.EXTRACT_MEDIA_FEATURES,
        Stage.BUILD_SEARCH_QUERIES,
        Stage.SEARCH_CANDIDATES,
        Stage.RETRIEVE_CANDIDATE_MEDIA,
        Stage.VERIFY_AND_ALIGN,
        Stage.SELECT_SURROUNDING_CONTEXT,
        Stage.COMPARE_CONTEXT_AND_CAPTION,
        Stage.BUILD_REPORT,
    }
)


def get_photon_provider() -> PhotonProvider:
    return PhotonProvider()


def get_inbound_tracker(request: Request) -> DeliveryTracker:
    return TigerDeliveryTracker(get_tiger_store(request), "photon_inbound")


def get_summary_tracker(request: Request) -> DeliveryTracker:
    return TigerDeliveryTracker(get_tiger_store(request), "photon_summary")


def job_id_for(conversation_id: str, url: str) -> str:
    return (
        "job_" + hashlib.sha1((conversation_id + url).encode("utf-8")).hexdigest()[:12]
    )


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
        run_analysis(
            job_id,
            url,
            conversation_id,
            store=store,
            provider=provider,
            tracker=tracker,
        )
        # delivery_status="failed" may mean accepted-but-unconfirmed. Never release that attempt.
    except QuantroError as error:
        if (
            inbound_tracker is not None
            and error.retryable
            and error.stage in _PRE_DELIVERY_STAGES
            and not tracker.has_record(conversation_id, job_id)
        ):
            # Retry only a confirmed early failure with no summary attempt, including no pending reservation.
            inbound_tracker.release(conversation_id, job_id)
        logger.warning(
            "Photon analysis %s failed at %s: %s",
            job_id,
            error.stage.value,
            error.code.value,
        )
        _reply(
            provider,
            conversation_id,
            f"Sorry, I couldn't check that post. {error.message}",
        )


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
        return request_error_response(413, stage=Stage.RUN_ANALYSIS)
    if not provider.verify_webhook_signature(dict(request.headers), raw_body):
        logger.warning(
            "Rejected Photon webhook signature; header names: %s",
            sorted(request.headers.keys()),
        )
        return request_error_response(
            401, stage=Stage.RUN_ANALYSIS, message="Invalid webhook signature"
        )

    try:
        payload = json.loads(raw_body)
        # The documented bridge forwards many event types; only new inbound messages start work.
        if isinstance(payload, dict) and payload.get("event") not in (
            None,
            "new-message",
        ):
            return JSONResponse(content={"status": "ignored"})
        message = parse_inbound(payload)
        logger.info("Photon inbound payload parsed; private contents omitted")
    except ValueError:
        return request_error_response(
            400, stage=Stage.RUN_ANALYSIS, message="Invalid Photon payload"
        )
    timestamp = message_timestamp(payload)
    if timestamp is not None and abs((datetime.now(UTC) - timestamp).total_seconds()) > 300:
        return request_error_response(401, stage=Stage.RUN_ANALYSIS, message="The signed message timestamp is stale.")
    if is_outbound_echo(payload, provider.sender_id):
        return JSONResponse(content={"status": "ignored"})

    conversation_id = message["conversation_id"]
    url = extract_url(message["text"])
    event_id = "event_" + hashlib.sha256(raw_body).hexdigest()
    if not await run_in_threadpool(tracker.check_and_record, conversation_id, event_id):
        duplicate = {"status": "duplicate"}
        if url is not None:
            duplicate["job_id"] = job_id_for(conversation_id, url)
        return JSONResponse(content=duplicate)
    if url is None:
        reply = canned_reply(message)
        if reply is not None:
            await run_in_threadpool(_reply, provider, conversation_id, reply)
        return JSONResponse(content={"status": "needs_url"})

    job_id = job_id_for(conversation_id, url)
    if not await run_in_threadpool(tracker.check_and_record, conversation_id, job_id):
        return JSONResponse(content={"status": "duplicate", "job_id": job_id})
    background_tasks.add_task(
        analyze_and_reply,
        job_id,
        url,
        conversation_id,
        store,
        provider,
        summary_tracker,
        tracker,
    )
    await run_in_threadpool(_reply, provider, conversation_id, ACK_REPLY)
    return JSONResponse(content={"status": "accepted", "job_id": job_id})
