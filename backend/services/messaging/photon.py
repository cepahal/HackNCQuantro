"""Direct Photon HTTP API transport for the /webhooks/photon route.

This is a second transport alongside the Spectrum sidecar bridge in
external_apis/photon.py. No Photon HTTP contract is documented in this repo, so
the send endpoint (POST {PHOTON_BASE_URL}/messages with a bearer key) and the
webhook signature (hex HMAC-SHA256 of the raw body keyed with PHOTON_API_KEY in
X-Photon-Signature, optional "sha256=" prefix) are assumptions to confirm with Photon.
"""

import hashlib
import hmac
import json
import os
from collections.abc import Mapping
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from ..external_apis.photon import _NoRedirect, _read_json
from ..reporting.send_summary import SendReceipt
from ..shared.errors import ErrorCode, QuantroError, Stage

SIGNATURE_HEADER = "x-photon-signature"
SEND_PATH = "/messages"
_TIMEOUT_SECONDS = 15


def _send_failed(message: str, retryable: bool = True) -> QuantroError:
    return QuantroError(ErrorCode.MESSAGE_SEND_FAILED, Stage.SEND_SUMMARY, message, retryable=retryable)


class PhotonProvider:
    def __init__(
        self, api_key: str | None = None, base_url: str | None = None, sender_id: str | None = None
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("PHOTON_API_KEY", "")).strip()
        self.base_url = (base_url if base_url is not None else os.getenv("PHOTON_BASE_URL", "")).strip().rstrip("/")
        self.sender_id = (sender_id if sender_id is not None else os.getenv("PHOTON_SENDER_ID", "")).strip()

    def send_message(self, conversation_id: str, body: str) -> dict:
        if not (self.api_key and self.base_url and self.sender_id):
            raise _send_failed("Photon messaging is not configured.", retryable=False)
        if urlsplit(self.base_url).scheme not in {"http", "https"}:
            raise _send_failed("PHOTON_BASE_URL must be an HTTP(S) URL.", retryable=False)

        request = Request(
            self.base_url + SEND_PATH,
            data=json.dumps({
                "sender_id": self.sender_id,
                "conversation_id": conversation_id,
                "body": body,
            }).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=_TIMEOUT_SECONDS) as response:
                status = response.status
                payload = _read_json(response) or {}
        except Exception as exc:
            raise _send_failed("The message could not be sent.") from exc

        message_id = payload.get("message_id") or payload.get("id")
        if not 200 <= status < 300 or not isinstance(message_id, str) or not message_id.strip():
            raise _send_failed("Photon did not confirm the message.")
        return {"message_id": message_id, "status": "accepted"}

    def send(self, conversation_id: str, text: str) -> SendReceipt:
        """MessagingProvider adapter so send_summary's tracker dedupes HTTP sends."""
        result = self.send_message(conversation_id, text)
        return SendReceipt(accepted=True, message_id=result["message_id"])

    def verify_webhook_signature(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        if not self.api_key or not isinstance(raw_body, bytes):
            return False
        provided = next((value for key, value in headers.items() if key.lower() == SIGNATURE_HEADER), None)
        if not isinstance(provided, str):
            return False
        provided = provided.strip().lower().removeprefix("sha256=")
        expected = hmac.new(self.api_key.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
