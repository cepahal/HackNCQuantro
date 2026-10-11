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
<title>Zoomout | iMessage signup</title><style>
:root{--ink:#0e0e11;--body:#5b5b61;--muted:#8a8a90;--line:#e4e4e7;--black:#0b0b0f;--red:#e63950}
*{box-sizing:border-box}body{margin:0;background:#fff;color:var(--ink);font:16px/1.5 Arial,Helvetica,sans-serif;-webkit-font-smoothing:antialiased}
::selection{background:#fbd5dc}.navbar{height:72px;background:var(--black);color:#fff}.wrap{width:min(1120px,100%);margin:auto;padding-inline:clamp(24px,5vw,64px)}
.nav-inner{height:100%;display:flex;align-items:center;justify-content:space-between;gap:24px}.brand{font-size:32px;font-weight:600;line-height:1;letter-spacing:-.06em;border-bottom:3px solid var(--red);padding-bottom:5px}
.nav-note{font-size:13px;color:#d5d5d9}.layout{display:grid;grid-template-columns:1.05fr 1fr;gap:clamp(32px,6vw,80px);padding-block:64px 48px;align-items:start}
.eyebrow{margin:0 0 20px;color:var(--red);font-size:12px;font-weight:600;letter-spacing:.1em;text-transform:uppercase}
h1{font-size:clamp(48px,6.7vw,76px);font-weight:400;line-height:1;letter-spacing:-.055em;margin:0 0 26px}h2{font-size:26px;font-weight:400;letter-spacing:-.04em;line-height:1.15;margin:0 0 12px}
p{margin:0 0 16px}.intro{font-size:19px;line-height:1.55;color:var(--body);max-width:440px}.steps{list-style:none;padding:0;margin:32px 0 0;counter-reset:steps;display:grid;gap:20px}
.steps li{counter-increment:steps;display:grid;grid-template-columns:28px 1fr;column-gap:12px}.steps li::before{content:"0" counter(steps);font-size:12px;color:var(--red);padding-top:4px}.steps strong{display:block;font-weight:600}.steps span{font-size:14px;color:var(--body)}
.signup-card{border:1px solid var(--line);padding:28px}.card-copy{font-size:14px;color:var(--body)}label{display:block;margin:20px 0 8px;font-size:14px;font-weight:600}
input[type=tel]{width:100%;padding:14px;border:1px solid #c9c9ce;border-radius:6px;background:#fff;color:var(--ink);font:inherit;min-height:48px}
.consent{display:flex;align-items:flex-start;gap:10px;font-size:13px;font-weight:400;line-height:1.5;color:var(--body)}.consent input{flex:none;width:17px;height:17px;margin:2px 0 0;accent-color:var(--red)}
button,.messages{display:inline-flex;justify-content:center;align-items:center;min-height:48px;padding:14px 20px;border:1px solid var(--black);border-radius:6px;background:var(--black);color:#fff;font:600 14px Arial,Helvetica,sans-serif;text-decoration:none;touch-action:manipulation}
button{width:100%;margin:20px 0 14px;cursor:pointer}button:disabled{opacity:.5;cursor:wait}button:hover:not(:disabled),.messages:hover{background:#28282d}
button:focus-visible,a:focus-visible,input:focus-visible{outline:3px solid var(--red);outline-offset:3px}.privacy,small{font-size:12px;color:var(--body)}
#status{white-space:pre-line;font-size:14px;overflow-wrap:anywhere;border-left:3px solid var(--red);padding-left:12px;margin:18px 0 0}#status:empty{display:none}
#result{padding:20px;margin-top:20px;border:1px solid var(--line);background:#fafafa}#result[hidden]{display:none}#assigned{font-size:14px;font-weight:600;overflow-wrap:anywhere}.messages{background:#fff;color:var(--ink);border-color:#c9c9ce;margin:0 0 14px}.messages:hover{background:#f1f1f3}
.result-copy{font-size:14px;color:var(--body)}#result .messages{width:100%}.troubleshooting{border-top:1px solid var(--line);margin-top:24px;padding-top:18px;font-size:13px;color:var(--body)}.troubleshooting summary{cursor:pointer;font-weight:600;color:var(--ink)}.troubleshooting ol{padding-left:20px;margin:12px 0}.troubleshooting li{margin-bottom:10px}.troubleshooting a{text-underline-offset:3px}
.qr{margin-top:28px;border-top:1px solid var(--line);padding-top:24px;display:grid;grid-template-columns:112px 1fr;align-items:center;gap:20px}.qr img{display:block;width:112px;height:112px}.qr h2{font-size:20px}.qr p{font-size:13px;color:var(--body);margin:0}
footer{border-top:1px solid var(--line);padding-block:22px;color:var(--body);font-size:12px}.footer-inner{display:flex;justify-content:space-between;gap:20px}
@media(max-width:720px){.navbar{height:64px}.layout{grid-template-columns:1fr;gap:32px;padding-block:36px}.intro{font-size:17px}.steps{gap:16px;margin-top:24px}.signup-card{padding:24px}.brand{font-size:28px}.footer-inner{flex-wrap:wrap}}
@media(max-width:380px){.signup-card{padding:20px}.qr{grid-template-columns:88px 1fr;gap:14px}.qr img{width:88px;height:88px}.nav-note{font-size:12px}}
</style></head><body><header class="navbar"><div class="wrap nav-inner"><span class="brand" aria-label="Zoomout">zoomout</span><span class="nav-note">iMessage signup</span></div></header>
<main class="wrap layout"><section aria-labelledby="signup-heading"><p class="eyebrow">The bigger picture</p><h1 id="signup-heading">Context.<br>In iMessage.</h1>
<p class="intro">Send a public video link. Review the available source and surrounding context, right from Messages.</p>
<ol class="steps" aria-label="How to get started"><li><div><strong>Register</strong><span>Enter your own iMessage number and tap Register for iMessage.</span></div></li><li><div><strong>Open Messages</strong><span>Use the number assigned to you after registration.</span></div></li><li><div><strong>Send a video</strong><span>Paste a public video link to request an analysis.</span></div></li></ol></section>
<div><section class="signup-card" aria-labelledby="register-heading"><h2 id="register-heading">Your next step: register.</h2><p class="card-copy">Use the number you send iMessages from, including its country code.</p>
<form id="signup" method="post" action="/imessage/signup"><label for="phone">Your iMessage phone number</label>
<input id="phone" name="phone_number" type="tel" inputmode="tel" autocomplete="tel" placeholder="(919) 555-1234" maxlength="40" required aria-describedby="phone-help">
<p id="phone-help" class="privacy">US/Canada: enter 10 digits and we add +1. For international numbers, include + and the country code. Spaces, parentheses and dashes are okay.</p>
<label class="consent"><input id="consent" type="checkbox" required> I own this number and agree to receive Zoomout iMessages, including a welcome and replies to links I send.</label>
<button id="submit" type="submit">Register for iMessage</button></form>
<p class="privacy">Your number is shared with our messaging provider for registration. Only register a number you own.</p>
<p id="status" role="status" aria-live="polite"></p><section id="result" hidden aria-labelledby="result-heading">
<h2 id="result-heading">Next: open Messages.</h2><p class="result-copy">Send a public video link to the number below. You can start without waiting for a welcome message.</p>
<p id="assigned"></p><a id="messages" class="messages" aria-describedby="assigned">Open Messages</a>
<p class="result-copy">Your messaging line is assigned during registration. Use the number shown here, not a number copied from a friend.</p>
<p><small>Sending and receiving a message verifies the connection. Registration alone does not confirm delivery.</small></p></section></section>
<section class="qr"><a href="/imessage/qr.svg"><img src="/imessage/qr.svg" alt="QR code linking to Zoomout iMessage signup"></a><div><h2>On your computer?</h2>
<p>Scanning only opens this form. Enter your number and tap Register for iMessage to finish registration.</p></div></section>
<details class="troubleshooting"><summary>No reply in Messages?</summary><ol><li>Check that you registered the phone number you send iMessages from.</li>
<li>On your iPhone, open Settings &gt; Apps &gt; Messages &gt; Send &amp; Receive. Under Start New Conversations From, select your registered phone number rather than an Apple Account email address, then start a new conversation.</li>
<li>Send the link to your assigned number shown after registration. A saved registration or accepted welcome does not confirm message delivery.</li></ol>
<a href="https://support.apple.com/en-us/101744" target="_blank" rel="noopener noreferrer">Apple's guide to your Messages sender address</a></details></div></main>
<footer><div class="wrap footer-inner"><span>The full record, in context.</span><span>Zoomout · Built at HackNC</span></div></footer>
<script>
const form=document.getElementById('signup'),button=document.getElementById('submit'),status=document.getElementById('status'),result=document.getElementById('result');
function normalizePhone(value){const text=value.trim();if(!/^[+0-9 ().-]+$/.test(text))return null;const compact=text.replace(/[ ().-]/g,'');if(/^\\+[1-9][0-9]{6,14}$/.test(compact))return compact;if(/^[0-9]{10}$/.test(compact))return '+1'+compact;return null;}
form.addEventListener('submit',async(event)=>{event.preventDefault();result.hidden=true;const phone=document.getElementById('phone'),normalized=normalizePhone(phone.value);
if(!normalized){phone.setAttribute('aria-invalid','true');status.textContent='Enter a US/Canada 10-digit number, or include + and the country code for an international number.';phone.focus();return;}phone.removeAttribute('aria-invalid');phone.value=normalized;button.disabled=true;status.textContent='Registering your number…';
try{const response=await fetch('/imessage/signup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone_number:normalized,consent:document.getElementById('consent').checked})});
const body=await response.json();if(!response.ok)throw new Error(body.error?.message||'Signup is temporarily unavailable.');
document.getElementById('assigned').textContent='Your assigned Zoomout number: '+body.assigned_phone_number;
document.getElementById('messages').href=body.open_messages_url;result.hidden=false;
status.textContent=(body.already_enrolled?'Your number is already registered. ':'Your number is registered. ')+(body.welcome_status==='accepted'?'A welcome message was accepted for sending; receipt is not confirmed. Tap Open Messages below, then send a public video link.':'Welcome delivery is unconfirmed. Tap Open Messages below, then send a public video link.');
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
