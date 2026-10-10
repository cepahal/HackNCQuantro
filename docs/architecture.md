# Architecture

## Submitted-post retrieval module

`backend/services/external_apis/retrieve_post.py` accepts a URL trigger, downloads
one supported JPG/MP4, persists it, and returns the team's complete v2
Post/MediaAsset contract for extraction. Default `retrieve_post(url)` requires
configured Tiger Data storage and returns success only after its transaction
commits. `handle_post_trigger(url)` returns the shared error envelope on failure.

`tigerdata_storage.py` performs one bound insert into `quantro_retrieval.posts`,
storing actual media bytes in `BYTEA` and the unchanged full Post in `JSONB`, with
post/media IDs, MIME type, SHA-256, byte count and creation time. The insert's
returned post ID is verified before commit. Failure rolls back, returns
`STORAGE_FAILED`, and cleans up the local download. Successful files remain on
the shared backend filesystem until downstream consumers finish.

Apply `backend/services/external_apis/tigerdata_schema.sql` once before use;
runtime performs no DDL. Configuration loads root `.env`, ignored `.env.local`,
then process values in that precedence. The current root `.env` is tracked, so
credentials belong in `.env.local` or the process environment. Default connections
use `TIGERDATA_DSN` (absent-only `DATABASE_URL` fallback), verified TLS, and bounded
connection/query timeouts. No S3 integration or live cloud save is claimed.

Callers can inject `storage=store` for tests or a managed connection and own that
store's lifecycle. An injected database connection must be dedicated, idle on
entry, and have autocommit disabled; the store commits or rolls back its transaction.
The default Tiger Data connection is closed by retrieval.
The Snowflake helper remains available for compatibility and is not the default.
No HTTP routes or other stages are implemented by this module.
See [retrieve_post handoff](retrieve_post.md) for configuration and exact fields.

## Source discovery modules (v2 functions 3–5)

`build_search_queries.py` derives bounded searches from MediaFeatures and the
submitted Post. `search_candidates.py` searches the existing Tiger Data Post
corpus through `TigerDataPostStore.find_posts`; a GIN index supports bound
full-text queries over title, caption and author clues. Results retain actual
query provenance and are unverified candidate sources, with normal empty results.
`retrieve_candidate_media.py` reuses submitted-post downloading and commits the
candidate relationship alongside its fresh Post and media bytes before success.
No separate corpus or external web-search service is assumed. See
[source-search setup and contracts](source_search.md).

## Photon transport

`backend/services/photon/` is a Node 24 sidecar using the official Spectrum SDK.
It receives iMessage submissions into private `.data/photon/inbox/<event-hash>/request.json`
records and acknowledges only saved inputs. This is an intake boundary; backend job creation
and analysis are not implemented by the sidecar.

After a report is saved, the existing `reporting.send_summary` stage uses `PhotonProvider`
and `external_apis.photon.send_summary_request` to call the
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
