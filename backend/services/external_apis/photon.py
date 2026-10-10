"""Call the local Photon bridge after an analysis report has been saved."""

import json
import os
import re
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


_MAX_BYTES = 16 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SAFE_ERRORS = {
    "INVALID_SUMMARY_INPUT": "Provide a valid conversation, summary, and HTTP(S) report URL.",
    "INVALID_IDEMPOTENCY_KEY": "Provide a valid job ID for summary delivery.",
    "IDEMPOTENCY_CONFLICT": "This job and conversation already use a different summary payload.",
    "DELIVERY_UNCERTAIN": "Delivery was already attempted. Verify its outcome before requesting another send.",
    "DELIVERY_STORAGE_UNAVAILABLE": "Delivery state could not be safely stored. No automatic resend is permitted.",
    "CONVERSATION_UNAVAILABLE": "The conversation could not be resolved.",
    "MESSAGE_SEND_FAILED": "The message could not be sent. Verify delivery before requesting another send.",
    "REQUEST_TIMEOUT": "The bridge request body timed out before processing.",
    "REQUEST_TOO_LARGE": "The bridge request body exceeds the supported size.",
    "INVALID_JSON": "The bridge request body must be valid JSON.",
    "INVALID_REQUEST": "The bridge request could not be read.",
    "UNAUTHORIZED": "The Photon bridge authentication token was rejected.",
    "UNSUPPORTED_MEDIA_TYPE": "The bridge request must use application/json.",
    "NOT_FOUND": "The Photon bridge route was not found.",
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _error(code: str, message: str, retryable: bool = False) -> dict:
    return {"error": {
        "code": code,
        "stage": "send_summary",
        "message": message,
        "retryable": retryable,
    }}


def _read_json(response) -> dict | None:
    raw = response.read(_MAX_BYTES + 1)
    if not isinstance(raw, bytes) or len(raw) > _MAX_BYTES:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _bridge_error(payload: dict | None) -> dict | None:
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    message = error.get("message")
    if (
        not isinstance(code, str) or code not in _SAFE_ERRORS
        or error.get("stage") != "send_summary"
        or not isinstance(message, str) or not message.strip()
        or len(message) > 512 or _CONTROL.search(message)
        or not isinstance(error.get("retryable"), bool)
    ):
        return None
    retryable = error["retryable"] and code in {"CONVERSATION_UNAVAILABLE", "REQUEST_TIMEOUT"}
    return _error(code, _SAFE_ERRORS[code], retryable)


def _valid_input(conversation_id: str, summary: str, report_url: str) -> bool:
    if (
        not isinstance(conversation_id, str) or not conversation_id.strip()
        or len(conversation_id) > 512 or _CONTROL.search(conversation_id)
        or not isinstance(summary, str) or not summary.strip() or len(summary) > 2000
        or not isinstance(report_url, str) or not report_url.strip()
        or len(report_url) > 4096 or _CONTROL.search(report_url)
    ):
        return False
    try:
        url = urlsplit(report_url)
        return (
            url.scheme in {"http", "https"} and bool(url.hostname)
            and url.username is None and url.password is None
        )
    except ValueError:
        return False


def send_summary_request(conversation_id: str, summary: str, report_url: str, *, job_id: str) -> dict:
    """Return provider acceptance or a safe error; never retry or modify analysis state."""
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
        return _error("INVALID_IDEMPOTENCY_KEY", _SAFE_ERRORS["INVALID_IDEMPOTENCY_KEY"])
    if not _valid_input(conversation_id, summary, report_url):
        return _error("INVALID_SUMMARY_INPUT", _SAFE_ERRORS["INVALID_SUMMARY_INPUT"])

    token = os.environ.get("PHOTON_BRIDGE_TOKEN", "")
    port = os.environ.get("PHOTON_BRIDGE_PORT", "8766")
    if not token.strip() or len(token) > 4096 or _CONTROL.search(token):
        return _error("PHOTON_NOT_CONFIGURED", "Configure the local Photon bridge authentication token.")
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1024 <= int(port) <= 65535:
        return _error("PHOTON_NOT_CONFIGURED", "Configure a local Photon bridge port between 1024 and 65535.")

    body = json.dumps({
        "conversation_id": conversation_id,
        "summary": summary,
        "report_url": report_url,
    }).encode("utf-8")
    if len(body) > _MAX_BYTES:
        return _error("INVALID_SUMMARY_INPUT", "The summary request exceeds the supported size.")
    request = Request(
        f"http://127.0.0.1:{int(port)}/send-summary",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Idempotency-Key": job_id,
        },
        method="POST",
    )
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=15) as response:
            payload = _read_json(response)
            status = response.status
        if not 200 <= status < 300:
            return _bridge_error(payload) or _error("MESSAGE_SEND_FAILED", _SAFE_ERRORS["MESSAGE_SEND_FAILED"])
    except HTTPError as error:
        try:
            with error:
                payload = _read_json(error)
        except Exception:
            payload = None
        return _bridge_error(payload) or _error("MESSAGE_SEND_FAILED", _SAFE_ERRORS["MESSAGE_SEND_FAILED"])
    except Exception:
        return _error("DELIVERY_UNCERTAIN", "Could not confirm Photon delivery. Verify the outcome before requesting another send.")

    if (
        isinstance(payload, dict)
        and payload.get("conversation_id") == conversation_id
        and payload.get("status") == "accepted"
        and isinstance(payload.get("message_id"), str)
        and payload["message_id"].strip()
        and len(payload["message_id"]) <= 512
        and not _CONTROL.search(payload["message_id"])
    ):
        return {
            "conversation_id": conversation_id,
            "message_id": payload["message_id"],
            "status": "accepted",
        }
    return _error("INVALID_BRIDGE_RESPONSE", "The Photon bridge returned an invalid response. Verify delivery before requesting another send.")
