"""Public opt-in enrollment tests never contact Photon or send messages."""

import hashlib
import io
import json
import shutil
import subprocess
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from backend.routes import photon_signup as signup

PHONE = "+19195550123"
ASSIGNED = "+17735550199"
TOKEN = "test-bridge-token-" + "a" * 32
RESULT = {"assigned_phone_number": ASSIGNED, "open_messages_url": f"sms:{ASSIGNED}&body=Hi%20Quantro",
          "welcome_status": "accepted", "already_enrolled": False}


class Response:
    status = 200

    def __init__(self, payload):
        self.raw = json.dumps(payload).encode()

    def read(self, limit):
        return self.raw[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("PHOTON_ENABLED", "true")
    monkeypatch.setenv("PHOTON_BRIDGE_TOKEN", TOKEN)
    monkeypatch.setenv("PHOTON_BRIDGE_PORT", "8766")
    signup._attempts.clear()
    opener = Mock()
    opener.open.return_value = Response(RESULT)
    monkeypatch.setattr(signup, "build_opener", Mock(return_value=opener))
    yield opener
    signup._attempts.clear()


@pytest_asyncio.fixture
async def client():
    app = FastAPI()
    app.include_router(signup.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://quantro.test") as connection:
        yield connection


@pytest.mark.asyncio
async def test_html_has_explicit_consent_same_origin_signup_and_qr(client):
    response = await client.get("/imessage")
    assert response.status_code == 200
    assert 'id="consent" type="checkbox" required' in response.text
    assert '<form id="signup" method="post" action="/imessage/signup">' in response.text
    assert "I own this number" in response.text
    assert "fetch('/imessage/signup'" in response.text
    assert 'src="/imessage/qr.svg"' in response.text
    assert "SPECTRUM_PROJECT_SECRET" not in response.text
    assert TOKEN not in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_signup_uses_zoomout_branding_and_clear_mobile_steps_without_external_assets(client):
    response = await client.get("/imessage")
    html = response.text
    assert "<title>Zoomout | iMessage signup</title>" in html
    assert 'class="brand" aria-label="Zoomout">zoomout</span>' in html
    assert "--black:#0b0b0f" in html and "--red:#e63950" in html
    assert "background:#fff" in html
    assert "@media(max-width:720px)" in html
    assert "button:focus-visible,a:focus-visible,input:focus-visible" in html
    assert html.index("<strong>Register</strong>") < html.index("<strong>Open Messages</strong>") < html.index("<strong>Send a video</strong>")
    assert 'aria-labelledby="signup-heading"' in html
    assert 'aria-labelledby="register-heading"' in html
    assert 'id="result" hidden' in html
    assert "Registration alone does not confirm delivery" in html
    assert "Your number is shared with our messaging provider" in html
    assert "<script src=" not in html and "<link " not in html
    assert html.count("https://") == 1
    assert 'href="https://support.apple.com/en-us/101744"' in html
    assert "http://" not in html


@pytest.mark.asyncio
async def test_signup_explains_qr_registration_assigned_number_and_unconfirmed_delivery(client):
    html = (await client.get("/imessage")).text
    assert "Scanning only opens this form" in html
    assert "tap Register for iMessage to finish registration" in html
    assert 'id="result" hidden aria-labelledby="result-heading"' in html
    assert 'id="result-heading">Next: open Messages.</h2>' in html
    assert "You can start without waiting for a welcome message" in html
    assert "not a number copied from a friend" in html
    assert 'id="messages" class="messages" aria-describedby="assigned"' in html
    assert "body.assigned_phone_number" in html and "body.open_messages_url" in html
    assert "receipt is not confirmed" in html
    assert "Welcome delivery is unconfirmed" in html
    assert "Settings &gt; Apps &gt; Messages &gt; Send &amp; Receive" in html
    assert "Start New Conversations From" in html
    assert "rather than an Apple Account email address, then start a new conversation" in html
    assert 'target="_blank" rel="noopener noreferrer"' in html


def test_actual_signup_script_normalizes_formatted_numbers_before_sending_strict_json():
    """Execute the page's real submit handler with a local DOM/fetch double, not a second normalizer."""
    script = signup._HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    cases = [
        {"input": "+1 (919) 555-0123", "expected": "+19195550123"},
        {"input": "919-555-0123", "expected": "+19195550123"},
        {"input": "9195550123", "expected": "+19195550123"},
        {"input": "  +44 (20) 7946 0958  ", "expected": "+442079460958"},
        {"input": "+683 1234", "expected": "+6831234"},
        *({"input": value, "expected": None} for value in (
            "", "19195550123", "00442079460958", "++19195550123", "+01995550123",
            "+123", "+19195550123 ext 5", "９１９５５５０１２３", "919<script>5550123",
        )),
    ]
    harness = """
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const {script,cases,response}=JSON.parse(readFileSync(0,'utf8'));
const results=[];
for(const item of cases){
  const handlers={},requests=[];
  const elements=Object.fromEntries(['signup','submit','status','result','phone','consent','assigned','messages'].map(id=>[id,{textContent:'',hidden:true,disabled:false,attributes:{},addEventListener(name,handler){handlers[name]=handler;},setAttribute(name,value){this.attributes[name]=value;},removeAttribute(name){delete this.attributes[name];},focus(){this.focused=true;}}]));
  elements.phone.value=item.input;elements.consent.checked=true;
  vm.runInNewContext(script,{window:{addEventListener(){}},document:{getElementById:id=>elements[id]},fetch:async(url,options)=>{requests.push({url,body:JSON.parse(options.body)});return {ok:true,json:async()=>response};}},{timeout:1000});
  await handlers.submit({preventDefault(){}});
  results.push({requests,status:elements.status.textContent,invalid:elements.phone.attributes['aria-invalid'],hidden:elements.result.hidden,disabled:elements.submit.disabled,phone:elements.phone.value});
}
process.stdout.write(JSON.stringify(results));
"""
    node = shutil.which("node")
    assert node, "Node is required to verify the real signup browser script."
    executed = subprocess.run(
        [node, "--input-type=module", "-e", harness],
        input=json.dumps({"script": script, "cases": cases, "response": RESULT}),
        text=True, encoding="utf-8", capture_output=True, check=True, timeout=10,
    )
    results = json.loads(executed.stdout)
    for case, actual in zip(cases, results, strict=True):
        assert actual["disabled"] is False
        if case["expected"] is None:
            assert actual["requests"] == [], case
            assert actual["invalid"] == "true"
            assert "include + and the country code" in actual["status"]
            assert actual["hidden"] is True
        else:
            assert actual["requests"] == [{"url": "/imessage/signup", "body": {"phone_number": case["expected"], "consent": True}}], case
            assert actual["phone"] == case["expected"]
            assert actual["hidden"] is False
    assert 'pattern=' not in signup._HTML.split('<input id="phone"', 1)[1].split('>', 1)[0]
    assert "US/Canada: enter 10 digits and we add +1" in signup._HTML


def test_actual_signup_script_clears_restored_confirmation_and_ignores_late_results():
    script = signup._HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = """
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const {script,response}=JSON.parse(readFileSync(0,'utf8'));
for(const reset of ['pageshow','input']){
  const handlers={},windowHandlers={},requests=[];
  const elements=Object.fromEntries(['signup','submit','status','result','phone','consent','assigned','messages'].map(id=>[id,{textContent:'',hidden:true,disabled:false,attributes:{},addEventListener(name,handler){handlers[id+':'+name]=handler;},setAttribute(name,value){this.attributes[name]=value;},removeAttribute(name){delete this.attributes[name];delete this[name];},focus(){}}]));
  elements.phone.value='9195550123';elements.consent.checked=true;
  let release,entered;
  let bodyReady=new Promise(resolve=>{entered=resolve;});
  vm.runInNewContext(script,{window:{addEventListener(name,handler){windowHandlers[name]=handler;}},document:{getElementById:id=>elements[id]},fetch:async(url,options)=>{requests.push({url,cache:options.cache});return {ok:true,json:async()=>new Promise(resolve=>{release=()=>resolve(response);entered();})};}},{timeout:1000});
  assert.equal(requests.length,0);
  const first=handlers['signup:submit']({preventDefault(){}});
  await bodyReady;
  release();await first;
  assert.equal(elements.result.hidden,false);
  assert.match(elements.status.textContent,/Registration checked just now/);
  const trigger=()=>reset==='pageshow'?windowHandlers.pageshow({persisted:true}):handlers['phone:input']();
  trigger();
  assert.equal(elements.result.hidden,true);assert.equal(elements.status.textContent,'');
  assert.equal(elements.messages.href,undefined);assert.equal(elements.assigned.textContent,'');
  bodyReady=new Promise(resolve=>{entered=resolve;});
  const second=handlers['signup:submit']({preventDefault(){}});
  await bodyReady;
  trigger();release();await second;
  assert.equal(elements.result.hidden,true);assert.equal(elements.status.textContent,'');
  assert.equal(elements.submit.disabled,false);
  assert.equal(requests.length,2);assert.ok(requests.every(request=>request.cache==='no-store'));
}
process.stdout.write('restored and late signup results cleared');
"""
    executed = subprocess.run(
        [shutil.which("node"), "--input-type=module", "-e", harness],
        input=json.dumps({"script": script, "response": {**RESULT, "already_enrolled": True}}),
        text=True, encoding="utf-8", capture_output=True, check=True, timeout=10,
    )
    assert executed.stdout == "restored and late signup results cleared"


@pytest.mark.asyncio
async def test_success_proxies_only_phone_and_consent_to_authenticated_loopback(client, environment):
    response = await client.post("/imessage/signup", json={"phone_number": PHONE, "consent": True})
    assert response.status_code == 200
    assert response.json() == RESULT
    request = environment.open.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:8766/enroll"
    assert json.loads(request.data) == {"phone_number": PHONE, "consent": True}
    assert request.get_header("Authorization") == "Bearer " + TOKEN
    assert request.get_header("Idempotency-key") == "enroll_" + hashlib.sha256(PHONE.encode()).hexdigest()
    assert environment.open.call_args.kwargs["timeout"] == 30
    handlers = signup.build_opener.call_args.args
    assert handlers[0].proxies == {}
    assert isinstance(handlers[1], signup._NoRedirect)
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize("consent", [False, None, 1, "true"])
async def test_consent_must_be_explicit_boolean_true(consent, client, environment):
    response = await client.post("/imessage/signup", json={"phone_number": PHONE, "consent": consent})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CONSENT_REQUIRED"
    environment.open.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("phone", ["9195550123", "+01955550123", "+1 9195550123", "+123", "+" + "1" * 16, True, None])
async def test_invalid_phone_never_reaches_private_bridge(phone, client, environment):
    response = await client.post("/imessage/signup", json={"phone_number": phone, "consent": True})
    assert response.status_code == 400
    environment.open.assert_not_called()


@pytest.mark.asyncio
async def test_extra_identity_fields_are_rejected(client, environment):
    response = await client.post("/imessage/signup", json={"phone_number": PHONE, "consent": True, "email": "private@example.test"})
    assert response.status_code == 400
    environment.open.assert_not_called()


@pytest.mark.asyncio
async def test_seven_digit_e164_number_is_accepted(client, environment):
    response = await client.post("/imessage/signup", json={"phone_number": "+6831234", "consent": True})
    assert response.status_code == 200
    assert json.loads(environment.open.call_args.args[0].data)["phone_number"] == "+6831234"


@pytest.mark.asyncio
@pytest.mark.parametrize("path,method", [("/imessage", "get"), ("/imessage/signup", "post"), ("/imessage/qr.svg", "get")])
async def test_disabled_feature_is_clear_503(path, method, client, environment, monkeypatch):
    monkeypatch.delenv("PHOTON_ENABLED", raising=False)
    response = await getattr(client, method)(path)
    assert response.status_code == 503
    assert "not enabled" in response.text
    environment.open.assert_not_called()


@pytest.mark.asyncio
async def test_chunked_body_is_bounded_and_non_json_is_rejected(client, environment):
    async def chunks():
        for _ in range(4):
            yield b"x" * 600

    response = await client.post("/imessage/signup", content=chunks(), headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    response = await client.post("/imessage/signup", content="phone=value", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == 415
    environment.open.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_json_is_safe(client, environment):
    response = await client.post("/imessage/signup", content="{broken", headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    environment.open.assert_not_called()


@pytest.mark.asyncio
async def test_failed_bridge_attempts_still_throttle_each_phone(client, environment):
    environment.open.side_effect = URLError("PRIVATE_PROVIDER_DIAGNOSTIC")
    for _ in range(signup._PHONE_LIMIT):
        response = await client.post("/imessage/signup", json={"phone_number": PHONE, "consent": True})
        assert response.status_code == 503
        assert "PRIVATE_PROVIDER_DIAGNOSTIC" not in response.text
    response = await client.post("/imessage/signup", json={"phone_number": PHONE, "consent": True})
    assert response.status_code == 429
    assert environment.open.call_count == signup._PHONE_LIMIT
    assert all(PHONE not in key for key in signup._attempts)


@pytest.mark.asyncio
async def test_invalid_input_counts_toward_ip_throttle_and_forwarded_header_cannot_bypass(client, environment, monkeypatch):
    monkeypatch.setattr(signup, "_IP_LIMIT", 2)
    for number in range(2):
        response = await client.post("/imessage/signup", json={}, headers={"X-Forwarded-For": f"198.51.100.{number}"})
        assert response.status_code == 400
    response = await client.post("/imessage/signup", json={}, headers={"X-Forwarded-For": "198.51.100.250"})
    assert response.status_code == 429
    environment.open.assert_not_called()


def test_throttle_state_is_bounded_and_expires(monkeypatch):
    clock = Mock(return_value=0.0)
    monkeypatch.setattr(signup.time, "monotonic", clock)
    monkeypatch.setattr(signup, "_MAX_THROTTLE_KEYS", 2)
    assert signup._allow_attempt("one", 1, 600)
    assert signup._allow_attempt("two", 1, 600)
    assert not signup._allow_attempt("three", 1, 600)
    assert not signup._allow_attempt("one", 1, 600)
    clock.return_value = 3601.0
    assert signup._allow_attempt("three", 1, 600)
    assert len(signup._attempts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"assigned_phone_number": "https://evil.test"},
    {"open_messages_url": "javascript:alert(1)"},
    {"open_messages_url": "sms:+17735550000&body=Hi%20Quantro"},
    {"welcome_status": "delivered"}, {"welcome_status": {}}, {"already_enrolled": 1},
])
async def test_malformed_or_unsafe_bridge_response_fails_closed(changes, client, environment):
    environment.open.return_value = Response({**RESULT, **changes, "secret": "PRIVATE_PROVIDER_SECRET"})
    response = await client.post("/imessage/signup", json={"phone_number": PHONE, "consent": True})
    assert response.status_code == 503
    assert "PRIVATE_PROVIDER_SECRET" not in response.text


@pytest.mark.asyncio
async def test_uncertain_welcome_remains_uncertain_and_private_fields_are_not_forwarded(client, environment):
    environment.open.return_value = Response({**RESULT, "welcome_status": "uncertain", "already_enrolled": True,
                                              "project_secret": "DO_NOT_RETURN", "user_id": "private-user"})
    response = await client.post("/imessage/signup", json={"phone_number": PHONE, "consent": True})
    assert response.status_code == 200
    assert response.json() == {**RESULT, "welcome_status": "uncertain", "already_enrolled": True}
    assert "DO_NOT_RETURN" not in response.text and "private-user" not in response.text


@pytest.mark.asyncio
async def test_capacity_error_is_useful_without_exposing_provider_diagnostics(client, environment):
    body = {"error": {"code": "ENROLLMENT_CAPACITY_OR_CONFLICT", "message": "PRIVATE_PROVIDER_SECRET"}}
    environment.open.side_effect = HTTPError("http://127.0.0.1:8766/enroll", 409, "private", {}, io.BytesIO(json.dumps(body).encode()))
    response = await client.post("/imessage/signup", json={"phone_number": PHONE, "consent": True})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ENROLLMENT_CAPACITY_OR_CONFLICT"
    assert "available seats" in response.text
    assert "PRIVATE_PROVIDER_SECRET" not in response.text


@pytest.mark.asyncio
async def test_qr_only_serves_the_fixed_svg(client, monkeypatch, tmp_path):
    svg = tmp_path / "signup-qr.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
    monkeypatch.setattr(signup, "_QR_PATH", svg)
    response = await client.get("/imessage/qr.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.text == svg.read_text()
    assert (await client.get("/imessage/qr.svg/../../.env")).status_code == 404


def test_router_is_registered_in_canonical_api():
    from backend.main import app

    paths = app.openapi()["paths"]
    assert "get" in paths["/imessage"]
    assert "post" in paths["/imessage/signup"]
    assert "get" in paths["/imessage/qr.svg"]
