"""Consent-based iMessage enrollment through the existing private Spectrum bridge."""

import hashlib
import json
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener
from urllib.request import Request as BridgeRequest

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from backend.services.external_apis.photon import _NoRedirect, _read_json

router = APIRouter()
_PHONE = re.compile(r"\+[1-9][0-9]{6,14}")
_BODY_LIMIT = 2_048
_MAX_THROTTLE_KEYS = 4_096
_IP_LIMIT = 30
_PHONE_LIMIT = 3
_attempts: dict[str, deque[float]] = {}
_attempt_lock = threading.Lock()
_QR_PATH = Path(__file__).resolve().parents[1] / "services" / "photon" / "static" / "signup-qr.svg"
_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}

_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quantro iMessage signup</title><style>
body{font:17px/1.5 system-ui,sans-serif;max-width:560px;margin:48px auto;padding:0 24px;color:#14271c;background:#f6f9f7}
h1{line-height:1.1}label{display:block;margin:18px 0 8px}input[type=tel]{box-sizing:border-box;width:100%;padding:12px;font:inherit}
button,.messages{display:inline-block;padding:12px 18px;background:#163d28;color:white;border:0;border-radius:6px;font:inherit;text-decoration:none}
button{margin:18px 0;cursor:pointer}button:disabled{opacity:.5}#status{white-space:pre-line}#result{padding:16px;border:1px solid #b8cbbc}
.qr{margin-top:32px;border-top:1px solid #b8cbbc;padding-top:20px}.qr img{width:200px;height:200px}small{color:#486252}
</style></head><body><h1>Use Quantro in iMessage</h1>
<p>Register your own iMessage phone number, then send Quantro a public video link to request an analysis.</p>
<form id="signup"><label for="phone">Your phone number, including country code</label>
<input id="phone" name="phone_number" type="tel" autocomplete="tel" placeholder="+19195551234" maxlength="16" required pattern="\\+[1-9][0-9]{6,14}">
<label><input id="consent" type="checkbox" required> I own this number and agree to receive Quantro iMessages, including a welcome and replies to links I send.</label>
<button id="submit" type="submit">Register for iMessage</button></form>
<p id="status" role="status" aria-live="polite"></p><section id="result" hidden>
<p id="assigned"></p><a id="messages" class="messages">Open Messages</a>
<p><small>Sending and receiving a message verifies the connection. Registration alone does not confirm delivery.</small></p></section>
<section class="qr"><h2>Open signup on your phone</h2><a href="/imessage/qr.svg"><img src="/imessage/qr.svg" alt="QR code linking to Quantro iMessage signup"></a>
<p>Scan the code or open this page on your iPhone.</p></section>
<script>
const form=document.getElementById('signup'),button=document.getElementById('submit'),status=document.getElementById('status'),result=document.getElementById('result');
form.addEventListener('submit',async(event)=>{event.preventDefault();button.disabled=true;result.hidden=true;status.textContent='Registering your number…';
try{const response=await fetch('/imessage/signup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone_number:document.getElementById('phone').value.trim(),consent:document.getElementById('consent').checked})});
const body=await response.json();if(!response.ok)throw new Error(body.error?.message||'Signup is temporarily unavailable.');
document.getElementById('assigned').textContent='Your assigned Quantro number: '+body.assigned_phone_number;
document.getElementById('messages').href=body.open_messages_url;result.hidden=false;
status.textContent=(body.already_enrolled?'Your number is already registered. ':'Your number is registered. ')+(body.welcome_status==='accepted'?'A welcome message was accepted for sending. Check Messages or use the link below.':'Welcome delivery is unconfirmed. Use Open Messages to start.');
}catch(error){status.textContent=error instanceof Error?error.message:'Signup is temporarily unavailable.';}finally{button.disabled=false;}});
</script></body></html>"""


def _enabled() -> bool:
    return os.getenv("PHOTON_ENABLED", "").strip().lower() == "true"


def _error(status: int, code: str, message: str, *, retryable: bool = False) -> JSONResponse:
    return JSONResponse(status_code=status, headers=_HEADERS, content={"error": {
        "code": code, "stage": "enroll", "message": message, "retryable": retryable,
    }})


def _allow_attempt(key: str, limit: int, period: float) -> bool:
    """Bound local abuse state; hashes avoid retaining raw phone numbers or IPs."""
    now = time.monotonic()
    with _attempt_lock:
        stale = [name for name, attempts in _attempts.items() if not attempts or attempts[-1] <= now - 3_600]
        for name in stale:
            del _attempts[name]
        attempts = _attempts.get(key)
        if attempts is None:
            if len(_attempts) >= _MAX_THROTTLE_KEYS:
                return False
            attempts = _attempts[key] = deque()
        while attempts and attempts[0] <= now - period:
            attempts.popleft()
        if len(attempts) >= limit:
            return False
        attempts.append(now)
        return True


def _enroll(phone: str) -> JSONResponse:
    token = os.getenv("PHOTON_BRIDGE_TOKEN", "").strip()
    port = os.getenv("PHOTON_BRIDGE_PORT", "8766").strip() or "8766"
    def unavailable():
        return _error(503, "PHOTON_UNAVAILABLE", "iMessage signup is temporarily unavailable. Please try again later.", retryable=True)
    if (not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token)
            or not re.fullmatch(r"[0-9]{1,5}", port) or not 1_024 <= int(port) <= 65_535):
        return unavailable()
    request = BridgeRequest(
        f"http://127.0.0.1:{int(port)}/enroll", method="POST",
        data=json.dumps({"phone_number": phone, "consent": True}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                 "Idempotency-Key": "enroll_" + hashlib.sha256(phone.encode()).hexdigest()},
    )
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=30) as response:
            payload = _read_json(response)
            status = response.status
    except HTTPError as exc:
        # Provider diagnostics, account IDs and internal auth details never reach the browser.
        try:
            with exc:
                failure = _read_json(exc)
            error = failure.get("error") if isinstance(failure, dict) else None
            if isinstance(error, dict) and error.get("code") == "ENROLLMENT_CAPACITY_OR_CONFLICT":
                return _error(409, "ENROLLMENT_CAPACITY_OR_CONFLICT", "The Photon project could not accept this registration. Ask the team to check available seats or an existing registration.")
        except Exception:  # noqa: BLE001 - Failed private error parsing stays unavailable.
            return unavailable()
        return unavailable()
    except Exception:  # noqa: BLE001 - Never forward private SDK/transport diagnostics.
        return unavailable()
    if status != 200 or not isinstance(payload, dict):
        return unavailable()
    assigned = payload.get("assigned_phone_number")
    if (not isinstance(assigned, str) or _PHONE.fullmatch(assigned) is None
            or payload.get("open_messages_url") != f"sms:{assigned}&body=Hi%20Quantro"
            or not isinstance(payload.get("welcome_status"), str)
            or payload["welcome_status"] not in {"accepted", "uncertain"}
            or type(payload.get("already_enrolled")) is not bool):
        return unavailable()
    return JSONResponse(headers=_HEADERS, content={key: payload[key] for key in (
        "assigned_phone_number", "open_messages_url", "welcome_status", "already_enrolled",
    )})


@router.get("/imessage", response_class=HTMLResponse)
def signup_page():
    if not _enabled():
        return HTMLResponse("<h1>iMessage signup is not enabled.</h1>", status_code=503, headers=_HEADERS)
    headers = {**_HEADERS, "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"}
    return HTMLResponse(_HTML, headers=headers)


@router.get("/imessage/qr.svg")
def signup_qr():
    if not _enabled():
        return _error(503, "PHOTON_DISABLED", "iMessage signup is not enabled.")
    if not _QR_PATH.is_file():
        return _error(404, "QR_UNAVAILABLE", "The signup QR code is unavailable.")
    return FileResponse(_QR_PATH, media_type="image/svg+xml", headers={"X-Content-Type-Options": "nosniff"})


@router.post("/imessage/signup")
async def signup(request: Request):
    if not _enabled():
        return _error(503, "PHOTON_DISABLED", "iMessage signup is not enabled.")
    address = request.client.host if request.client else "unknown"
    if not _allow_attempt("ip:" + hashlib.sha256(address.encode()).hexdigest(), _IP_LIMIT, 600):
        return _error(429, "SIGNUP_RATE_LIMITED", "Too many signup attempts. Please wait before trying again.", retryable=True)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        return _error(415, "INVALID_SIGNUP", "Send a JSON phone number and explicit consent.")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > _BODY_LIMIT:
            return _error(413, "SIGNUP_TOO_LARGE", "The signup request is too large.")
        raw.extend(chunk)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return _error(400, "INVALID_SIGNUP", "Provide a phone number in international format and explicit consent.")
    if (not isinstance(payload, dict) or set(payload) != {"phone_number", "consent"}
            or not isinstance(payload["phone_number"], str) or _PHONE.fullmatch(payload["phone_number"]) is None):
        return _error(400, "INVALID_SIGNUP", "Provide a phone number in international format, such as +19195551234.")
    phone = payload["phone_number"]
    if not _allow_attempt("phone:" + hashlib.sha256(phone.encode()).hexdigest(), _PHONE_LIMIT, 3_600):
        return _error(429, "SIGNUP_RATE_LIMITED", "Too many signup attempts for this number. Please wait before trying again.", retryable=True)
    if payload["consent"] is not True:
        return _error(400, "CONSENT_REQUIRED", "Confirm that you own this number and consent to Quantro iMessages.")
    return await run_in_threadpool(_enroll, phone)
