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
