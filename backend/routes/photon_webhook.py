import hashlib
import json
import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from backend.routes.common import get_tiger_store
from backend.services.messaging.delivery_tracker import DeliveryTracker
from backend.services.messaging.inbound import ACK_REPLY, canned_reply, extract_url, is_outbound_echo, parse_inbound
from backend.services.messaging.photon import PhotonProvider
from backend.services.reporting.run_analysis import run_analysis
from backend.services.shared.errors import QuantroError
from backend.services.storage.tiger import TigerStore

logger = logging.getLogger(__name__)
router = APIRouter()

_MAX_BODY_BYTES = 64 * 1024
_MAX_LOGGED_PAYLOAD_CHARS = 4096
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
) -> None:
    try:
        run_analysis(job_id, url, conversation_id, store=store, provider=provider, tracker=tracker)
    except QuantroError as error:
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
        return JSONResponse(status_code=413, content={"error": {"message": "Payload too large"}})
    if not provider.verify_webhook_signature(dict(request.headers), raw_body):
        logger.warning("Rejected Photon webhook signature; header names: %s", sorted(request.headers.keys()))
        return JSONResponse(status_code=401, content={"error": {"message": "Invalid webhook signature"}})

    try:
        payload = json.loads(raw_body)
        logger.info("Photon inbound payload: %s", json.dumps(payload)[:_MAX_LOGGED_PAYLOAD_CHARS])
        message = parse_inbound(payload)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": {"message": "Invalid Photon payload"}})
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
    background_tasks.add_task(analyze_and_reply, job_id, url, conversation_id, store, provider, summary_tracker)
    await run_in_threadpool(_reply, provider, conversation_id, ACK_REPLY)
    return JSONResponse(content={"status": "accepted", "job_id": job_id})
