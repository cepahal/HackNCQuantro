# Quantro Photon messaging

Owns managed iMessage transport, durable local intake, URL dispatch to the Python analysis
endpoint, and `send_summary`. Uses the official `spectrum-ts@12.10.1` SDK on Node 24.
Gemini, source search, report construction, and job persistence run in the Python backend.

## Setup

From the repository root:

```powershell
npm.cmd --prefix backend/services/photon ci --ignore-scripts
# Only for a fresh checkout; never replace an existing private env.
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
```

Privately configure the root `.env`:

```dotenv
SPECTRUM_PROJECT_ID=
SPECTRUM_PROJECT_SECRET=
PHOTON_BRIDGE_TOKEN=
PHOTON_BRIDGE_PORT=8766
PHOTON_BACKEND_PORT=8000
PHOTON_DATA_DIR=.data/photon
```

The project ID and secret come from Photon project Settings. Generate the bridge token
locally with `node -e "console.log(require('node:crypto').randomBytes(32).toString('hex'))"`
and save its output only in `.env`. It authenticates the Python-to-Node bridge, not Photon.
The old `PHOTON_API_KEY`, `PHOTON_BASE_URL`, and `PHOTON_SENDER_ID` placeholders are not
Spectrum credentials. The SDK discovers managed cloud endpoints and sending lines.

For this repository, the three private values are also stored as encrypted GitHub Actions
repository secrets with the same names. GitHub does not copy those secrets into a running
service automatically: inject them into the server environment through your deployment
process, or use the ignored local `.env`. Never paste them into a GitHub file or PR.

The original public `.env` contained blank credential slots. It is replaced by a blank
`.env.example`; real `.env`, media, and delivery state are ignored by Git.

## Run

```powershell
# Safe local transport: no cloud connection, no iMessage delivery.
npm.cmd --prefix backend/services/photon start

# Managed iMessage: receives messages and sends acknowledgements.
npm.cmd --prefix backend/services/photon run start:imessage
```

Use one transport process per project. Both commands load root `.env` and then
`backend/.env` (if present), with backend values taking precedence. They bind the summary API
to `127.0.0.1:8766`, and leave secrets out of startup output. Terminal mode accepts URL text;
it does not simulate media uploads. Stop with Ctrl+C. A first terminal run may download the
official SDK's terminal helper.

Reuse the existing authenticated Photon project. It already has a shared iMessage platform;
do not create or rotate credentials to activate this code. For Free/Pro shared-pool delivery,
add each genuine tester's exact iMessage phone/email handle to project **Users**. A shared
pool assigns the sender number automatically. This integration targets shared-pool DMs;
dedicated multi-line routing and group workflows need a separate contract.

### Shared-number rejection

Photon's "This number didn't recognize yours" reply is sent before Quantro receives
the message. Free/Pro shared lines route by the registered sender and assigned line.
Register the tester under Users, disable the optional email invite unless requested,
and have them text their assigned number. A shared number cannot be made open to
unknown senders by changing a webhook or disabling an application check. A public
dedicated number requires Photon Business. No plan upgrade is needed for registered
demo testers, and this integration never provisions or purchases lines.

### Existing Render service

The Docker image includes the existing Node 24 Spectrum listener. API-only startup
remains the default. Set `PHOTON_ENABLED=true` in the service's private environment,
alongside the existing `SPECTRUM_PROJECT_ID`, `SPECTRUM_PROJECT_SECRET` and matching
`PHOTON_BRIDGE_TOKEN`. The runtime starts the API first, waits for its existing routes,
then starts Spectrum. `PHOTON_BACKEND_PORT` follows Render's `PORT`; the summary
bridge remains private on `127.0.0.1:8766`. Keep one service instance and stop any
old listener for this project before enabling the hosted one.

No public webhook is required by Spectrum. `scripts/photon_listen.py` starts only
the separate HTTP-webhook API and does not start the supported Spectrum receiver.
Successful hosting requires a `Quantro Photon (iMessage) ready` log, then an actual
inbound message, acknowledgment and report-link receipt. API health alone is not
proof that iMessage works.

Set `PHOTON_DATA_DIR` to an existing persistent mount when available. Without one,
intake and delivery markers live only for that container and are lost on a deploy.
Saved Tiger jobs/reports survive, but transport deduplication across deployments is
not guaranteed. Do not replay old requests or resend uncertain deliveries. This
change does not buy storage, add services, enable saved-request replay or change the
analysis algorithm, report schema or frontend.

`GET http://127.0.0.1:8766/health` checks the bridge process only, not iMessage delivery.

### QR signup for the existing Pro plan

Share `https://quantro-backend.onrender.com/imessage` or display its QR code at
`https://quantro-backend.onrender.com/imessage/qr.svg`. The checked-in QR encodes
that signup URL, with a four-module quiet zone and medium error correction. If
the backend hostname changes, regenerate the QR before sharing it.

The standalone backend page asks for an international phone number and consent
to a welcome iMessage. It enrolls a shared user through Photon's documented
Stable API, then shows that user's assigned Photon number and an Open Messages
link. The user replies with a public video URL to enter the existing analysis
flow. No frontend files or analysis functions are involved in signup.

The existing plan permits 100 registered users, including existing project
users. Photon enforces capacity; this integration never upgrades the plan or
buys a line. Different people can receive different assigned numbers. Posting
one person's assigned number publicly does not register other senders.

Signup has input/body bounds, per-process throttling and private bridge
authentication. Welcome attempts are recorded before sending; repeated signup
does not resend an accepted or uncertain welcome while that ledger exists.
Without a persistent mount, these markers and throttle counters reset on a
deploy. Entering a number is consent to contact, not proof of phone ownership
or authentication to private reports. An accepted send is not a delivery receipt.

Official enrollment contract:
[Create user](https://photon.codes/docs/api-reference/users/create-user).

## Incoming submission boundary

The receiver uses actual SDK `Message`/`Content` types. It ignores outbound messages,
unwraps reply content, and handles grouped caption/media messages. Send one public HTTP/S
URL, or JPEG/PNG/WebP/MP4/MOV/WebM media up to **25 MiB total** per message. URLs are
validated but never downloaded by this service. Hostname validation does not replace the
backend retriever's DNS, redirect, and SSRF checks.

Spectrum attachments expose `stream()`; they do not expose an `attachmentUrl`. Media is
streamed to generated filenames, with actual byte limits and a bounded read timeout.
Only after saving does the receiver acknowledge receipt. In the running service, public
URL submissions are queued for the existing `POST /analyze/{job_id}/run` endpoint on
`127.0.0.1:8000` (override the port with `PHOTON_BACKEND_PORT`). The event hash supplies a
stable job ID; the actual conversation ID is preserved for the report reply. Invalid inputs
receive a useful correction. Attachments are saved, but automatic analysis currently requires
a public post URL; their acknowledgement explicitly explains this boundary.

Each completed submission is an immutable file at
`.data/photon/inbox/<event-hash>/request.json`:

```json
{
  "schema_version": "photon-intake-1",
  "conversation_id": "native Spectrum chat GUID",
  "message_id": "native Spectrum inbound message GUID",
  "receiving_line": null,
  "input_type": "url",
  "input_url": "https://example.org/post",
  "text": "Check this post: https://example.org/post",
  "attachments": [],
  "status": "received",
  "received_at": "2026-10-10T17:00:00.000Z"
}
```

Attachments use `{path, mime_type, name, size}`. `path` points to saved local bytes; never
accept a user filename as a filesystem path. `receiving_line` is the SDK's managed line
when available. This intake schema is a new explicit transport boundary, not a claim
that attachments can be analyzed by the URL pipeline. The service dispatches completed URL
records only, retains `conversation_id`, and lets the backend produce the report. Do not consume
directories without a completed record. No extraction, verdict, or report is generated by this receiver.

URL analyses are serialized with a maximum of 32 queued requests, keeping message reception
responsive while backend work runs. Processing markers are in `.data/photon/analysis/`.
They record `pending`, `completed`, `failed`, or `uncertain`. Known structured API errors
retain safe code/stage/status fields; raw provider diagnostics are not forwarded. A saved
report takes precedence over a late HTTP error. A request whose completion cannot be
confirmed is never automatically replayed. The backend request deadline is 30 minutes;
notification sends retain a 12-second deadline.

Managed startup reconciles existing markers with GET-only report/job lookups in the
background, without delaying acknowledgments or resending messages. Default recovery
never starts an old marker-free request. Explicit operator opt-in is available after
reviewing saved intake and any external processing/delivery permissions:

```powershell
npm.cmd --prefix backend/services/photon run start:imessage -- --resume-saved
```

This cloud-only flag resumes a saved native URL request only when its event hash and
intake validate, no marker exists, both backend lookups explicitly confirm absence,
and the native conversation resolves to the original ID. An exclusive fsynced marker
still precedes the POST. Existing markers are never re-posted. Scans are bounded to
256 entries, 32 jobs, and a 60-second lookup budget; unresolved/limited work needs
operator review. Run one listener instance and preserve all processing/delivery state.

Duplicate event IDs are suppressed across restarts. A crash can leave a directory without
`request.json`; review such incomplete intake before cleanup/replay. A failed acknowledgement
does not delete a saved request. Acknowledgements themselves do not claim exactly-once delivery.

## Send the completed report

The backend must **save the report first**, then call:

```python
from backend.services.reporting.send_summary import send_summary

delivery = send_summary(
    conversation_id,
    summary,
    report_url,
    job_id=job_id,
)
```

The Python process must load the same private env through its normal configuration.
The existing reporting stage delegates its `PhotonProvider` to a standard-library HTTP
adapter, `external_apis.photon.send_summary_request`, and the bridge's durable delivery ledger.
Custom injected providers retain the existing reporting interface. No automatic retry occurs. A delivery
error must be stored separately from analysis status; preserve the completed report.

The new private bridge endpoint is:

```http
POST /send-summary
Authorization: Bearer <PHOTON_BRIDGE_TOKEN>
Idempotency-Key: <stable job ID>
Content-Type: application/json

{"conversation_id":"original chat GUID","summary":"Evidence-backed summary","report_url":"https://your-host/reports/report_001"}
```

Response after the SDK accepts the send and supplies a message ID:

```json
{"conversation_id":"original chat GUID","message_id":"provider message GUID","status":"accepted"}
```

`accepted` is not a delivery receipt. No `delivered` claim is made. Conversation IDs come
from intake, not a guessed phone number. After restart, cloud sends require a saved intake
for that conversation; terminal conversations must still be active. Summary text is
limited to 2,000 characters. The HTTP request is limited to 16 KiB and requires a private
token and stable job ID. No CORS or public listener is enabled.

Delivery records are keyed by job and conversation. Repeating an accepted request returns
the cached provider ID. Changing its payload conflicts. A pending or failed provider attempt
may have sent a message; it must be reconciled before another send. Never bypass this by
changing the job ID or deleting its delivery record to force a retry. Errors use
`{error: {code, stage: "send_summary", message, retryable}}` with safe fixed messages.
Provider sends have a 12-second deadline; a timeout remains an uncertain outcome.

Persist `.data/photon` on the deployment host; keep it private and share its delivery directory
if more than one bridge instance is used. Do not expose the bridge port publicly. The service
does not implement account linking or a public webhook; the backend provides the analysis
endpoint. Start the Python backend before the messaging service.

## Verify

```powershell
npm.cmd --prefix backend/services/photon run typecheck
npm.cmd --prefix backend/services/photon test
python -m pytest tests -q
```

Offline tests use fake Spectrum spaces/attachments and temporary directories. A live acceptance
test still requires an allowlisted supported iMessage client: send a known URL/media, confirm its
saved intake and acknowledgement, then send a completed test report through the Python callable.
Account authentication, offline tests, and live message delivery are separate checks.

Sources: [Photon setup](https://photon.codes/docs/spectrum-ts/getting-started),
[attachments](https://photon.codes/docs/spectrum-ts/content/attachments),
[cloud routing](https://photon.codes/docs/spectrum-ts/providers/imessage/connection-and-routing),
[shared-pool allowlist](https://photon.codes/docs/spectrum-ts/troubleshooting/imessage).

Recovery limits apply across both marker and inbox scans. A large marker history
can exhaust the scan before the inbox is reached. Startup logs `limited=true`
and a manual-review instruction in that case; do not treat a truncated scan as
a fully drained queue. Automated pagination of large backlogs is not implemented.
