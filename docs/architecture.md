# Architecture

## Photon transport

`backend/services/photon/` is a Node 24 sidecar using the official Spectrum SDK.
It receives iMessage submissions into private `.data/photon/inbox/<event-hash>/request.json`
records and acknowledges only saved inputs. This is an intake boundary; backend job creation
and analysis are not implemented by the sidecar.

After a report is saved, `backend/services/external_apis/photon.py:send_summary` calls the
new authenticated loopback-only `POST /send-summary` bridge. Its body is
`{conversation_id, summary, report_url}` and a stable job ID is supplied in `Idempotency-Key`.
The response is `{conversation_id, message_id, status: "accepted"}`. Delivery state is
persisted independently of analysis; ambiguous sends are not automatically retried.

See [transport contracts and operator steps](../backend/services/photon/README.md).
The main backend endpoints and the specialist pipeline below retain their existing ownership.

## High-level flow

USER
  |
  v
FRONTEND
  |
  | HTTP request
  v
BACKEND API
  |
  | validates and processes input
  v
SPECIALIST / CORE LOGIC
  |
  | ML / AI / algorithm / external model
  v
BACKEND
  |
  | combines results into JSON
  v
FRONTEND
  |
  v
USER SEES RESULT


## Example request flow

1. User enters or uploads data in the frontend.
2. Frontend sends request to backend route.
3. Backend validates the input.
4. Backend calls any preprocessing function.
5. Backend calls specialist/core analysis function.
6. Specialist function returns structured result.
7. Backend combines that result with other data.
8. Backend returns JSON to frontend.
9. Frontend displays result.


## Component responsibilities

Frontend
- User input
- File upload/forms
- Loading states
- Result display
- Error display
- Visualizations

Backend
- API routes
- Request validation
- Authentication if needed
- Database if needed
- Calls specialist functions
- Combines results
- Returns structured JSON

Specialist
- ML models
- AI calls
- algorithms
- embeddings
- ranking
- computer vision
- data processing
- technically novel component

Integration/generalist
- deployment
- testing
- API wiring
- external services
- environment variables
- end-to-end debugging
- demo reliability


## API contract

POST /analyze

INPUT

Depends on project.

Examples:
- text
- image
- audio
- video
- JSON data

OUTPUT

{
  "success": true,
  "result": {},
  "error": null
}

The exact result structure must be agreed on before development.


## Team rule

Every major component must have a defined:

INPUT
OUTPUT
OWNER

before implementation begins.
