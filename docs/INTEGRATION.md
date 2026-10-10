# Quantro integration map

How one submitted URL becomes a saved report and an iMessage reply, who owns each
step, and what to check when it breaks. Operator commands live in [RUNBOOK.md](RUNBOOK.md).

## Flow

```text
POST /analyze/{job_id}/run  {url, conversation_id?}
        │
        ▼
URL ─► retrieve_post ─► extract_media_features ─► Tiger Data
       (Abdullah)       (Jason)                   quantro_jobs  status=running → ingested
                                                  quantro_posts post_json + features_json (internal, keeps local paths)
        │
        ▼
build_search_queries ─► search_candidates(limit=5) ─► exclude the submitted post itself
(Abdullah)              (Abdullah)
        │                 ├─ raises            → SEARCH_FAILED (retryable). Job failed. No report. Never "no match".
        │                 └─ [] candidates     → outcome = no_verified_match, jump to build_report
        ▼
[candidate loop, for each candidate]
    retrieve_candidate_media ─► extract_media_features ─► verify_and_align
    (Abdullah)                  (Jason)                   (Jason)
    any failure → skip candidate, add a warning + limitation
    viable = status "verified" AND confidence level in {high, moderate}
        │
        ▼
pick best viable source (level, then clip_coverage_ratio; a tie adds "Multiple plausible sources remain.")
        │
        ▼
select_surrounding_context ─► compare_context_and_caption(clip_features, context, submitted caption)
(Jason)                       (Jason)
        │
        ▼
build_report ─► save_report_and_complete_job ─► send_summary (only if conversation_id)
(Bhrigu)        (Bhrigu)                         (Bhrigu → Photon)
                quantro_reports INSERT … ON CONFLICT (report_id) DO NOTHING
                quantro_jobs status=completed
                                                 failure → delivery_status "failed" + warning; report is kept
        │
        ▼
AnalysisResult {job_id, outcome, report_id, report_url, delivery_status, delivery_warning, warnings}
GET /reports/{report_id} → sanitized ReportV2 JSON (no local_path / image_path / "/tmp/")
```

`report_id` is always `report_<job_id>`, so re-running a job returns the same report
and never adds rows. Completed jobs reuse the saved report without downloading,
transcribing, or searching again; a different URL needs a new job ID. Failed submitted
retrievals retain a failed job row. Every stage failure is a `QuantroError {code, stage, message,
retryable}`, and the job row records `error_code` and `error_stage`.

## Upstream functions

| # | Function | Import path | Owner |
|---|---|---|---|
| 1 | `retrieve_post(url)` | `backend.services.external_apis.retrieve_post` | Abdullah |
| 2 | `extract_media_features(media, options)` | `backend.services.processing.preprocessing` | Jason |
| 3 | `build_search_queries(features, post)` | `backend.services.external_apis.build_search_queries` | Abdullah |
| 4 | `search_candidates(queries, limit)` | `backend.services.external_apis.search_candidates` | Abdullah |
| 5 | `retrieve_candidate_media(candidate)` | `backend.services.external_apis.retrieve_candidate_media` | Abdullah |
| 6 | `verify_and_align(clip_features, candidate_features)` | `backend.services.specialist.alignment` | Jason |
| 7 | `select_surrounding_context(alignment, candidate_features, options)` | `backend.services.specialist.context` | Jason |
| 8 | `compare_context_and_caption(clip_features, context, caption)` | `backend.services.specialist.comparison` | Jason |

`run_analysis.py` calls all eight; submitted retrieval is delegated through
`pipeline/run_ingest.py`. Their outputs (`{queries, warnings}`,
`{candidates, warnings}`, Post, MediaFeatures, Alignment, Context, Comparison) are
validated against `backend/services/shared/types.py` at the reporting boundary.
Search uses Exa public-web discovery with `SEARCH_PROVIDER=exa` (production), or
Abdullah's Tiger Data post corpus (`quantro_retrieval.posts`) with
`SEARCH_PROVIDER=tiger`; see `docs/PROVIDERS.md`.

## Environment variables

Real values belong in the git-ignored repo-root `.secrets/.env` or `.env.local`.
`.env.local` can be a regular private file or a symlink to the vault. The API and
Tiger CLI load the vault, then `.env.local`, without overriding process variables.
Retrieval and Gemini also read `.env.local`. The tracked `.env.example` is the
blank template; keep credentials out of tracked files.

| Variable | Read by | Owner | Notes |
|---|---|---|---|
| `TIGER_DATABASE_URL` | `storage/tiger.py` (API startup, CLI) | Bhrigu | Preferred operational URL. Falls back to `TIGERDATA_DSN`, then `DATABASE_URL`; fallback Postgres connections enforce `verify-full`, `TIGERDATA_SSL_ROOT_CERT`, and a 10-second connection timeout. |
| `TIGER_TABLE_PREFIX` | `storage/tiger.py` | Bhrigu | Default `quantro_`. Lowercase letters, digits, `_`. |
| `REPORT_STORE` | `save_report.get_report_store()` | Bhrigu | `tiger` (default) or `memory` (tests). The API routes always use the Tiger store. Any other value fails with a storage error. |
| `REPORT_BASE_URL` | `save_report.py`, CORS in `main.py` | Bhrigu | Frontend origin; report links are `{REPORT_BASE_URL}/reports/{report_id}`. |
| `TIGERDATA_DSN` (fallback `DATABASE_URL`) | `external_apis/tigerdata_storage.py` | Abdullah | Retrieval media store and the search corpus (`quantro_retrieval.posts`). Same Tiger service as `TIGER_DATABASE_URL`; the code enforces `verify-full` TLS. |
| `TIGERDATA_SSL_ROOT_CERT` | `external_apis/tigerdata_storage.py` | Abdullah | Default `system`. |
| `SEARCH_PROVIDER` | `external_apis/search_candidates.py` | Jason/Abdullah | `exa` (production) or `tiger` (stored corpus; also the default when unset). Any other value fails with `SEARCH_FAILED`. |
| `EXA_API_KEY` | `external_apis/exa_search.py` | Jason | Required when `SEARCH_PROVIDER=exa`. |
| `GEMINI_API_KEY` | `specialist/gemini.py` | Jason | Required for frame OCR and comparison (function 8), and for transcription when `TRANSCRIPTION_PROVIDER=gemini`. |
| `GEMINI_MODEL` | `specialist/gemini.py` | Jason | Default `gemini-flash-latest`. Ignore the old `gemini-1.5-pro` example. |
| `TRANSCRIPTION_PROVIDER` | `specialist/transcription.py` | Jason | `whisper` (production; local faster-whisper) or `gemini` (default when unset). |
| `WHISPER_MODEL` | `specialist/transcription.py` | Jason | Default `small`; local example `small.en`. The Docker image sets it to the baked-in `/opt/quantro/whisper`. |
| `SPECTRUM_PROJECT_ID`, `SPECTRUM_PROJECT_SECRET` | Photon sidecar `backend/services/photon` (`--imessage` mode) | Photon sidecar: `iner54` | From Photon project Settings. Only needed for real iMessage; terminal mode runs without them. |
| `PHOTON_BRIDGE_TOKEN`, `PHOTON_BRIDGE_PORT`, `PHOTON_DATA_DIR` | `external_apis/photon.py` and the sidecar | Photon sidecar: `iner54`; `send_summary` caller: Bhrigu | Token authenticates Python → loopback bridge `127.0.0.1:8766`. Delivery ledger persists in `PHOTON_DATA_DIR`. |
| `PHOTON_API_KEY`, `PHOTON_BASE_URL`, `PHOTON_SENDER_ID` | `services/messaging/photon.py` (`POST /webhooks/photon`) | Bhrigu | Alternative HTTP webhook path. Assumed contract: `POST {base}/messages` with bearer key; inbound `X-Photon-Signature` = hex HMAC-SHA256 of the raw body keyed with `PHOTON_API_KEY`. Confirm with Photon. |
| `NEXT_PUBLIC_API_BASE_URL`, `NEXT_PUBLIC_PHOTON_NUMBER` | `frontend/lib/api.ts`, `frontend/app/page.tsx` (hero note) | Grant | `http://localhost:8000` locally. Inlined at build time. `NEXT_PUBLIC_REPORT_BASE_URL` is no longer read; the report page's Copy link uses the page URL. |

## What to check when it breaks

Find the failing stage first. The API error body and the job row both carry it:

```bash
cd backend && python -m services.storage.tiger list          # status, error_code, error_stage per job
cd backend && python -m services.storage.tiger show <job_id> # full job, post, features and report JSON
```

### 1. Tiger Data connection

- **Symptom:** the API refuses to start with `TIGER_DATABASE_URL is not set` or
  `must include sslmode=require`. Or the CLI says `could not connect to Tiger Data`.
  Or requests fail with `STORAGE_FAILED` (HTTP 500).
- **Check:**
  - The variable is a single line in `.secrets/.env`: `postgres://user:pass@host:port/db?sslmode=require`.
  - Run `psql "<that URL>" -c 'select 1'`.
  - The password was rotated after the old one leaked to git.
  - Your network can reach the Tiger service.
- **Note:** retrieval uses a different variable, `TIGERDATA_DSN`. A working API with
  failing retrieval usually means that one is missing from the private environment.
  Setting `TIGERDATA_DSN` also supplies the operational store when `TIGER_DATABASE_URL`
  is absent; setting only `TIGER_DATABASE_URL` does not supply retrieval credentials.

### 2. Transcription key missing

- **Symptom:** `TRANSCRIPTION_FAILED` at stage `extract_media_features` (HTTP 502).
  The job is marked `failed`, and no post row is written.
- **Check:**
  - `GEMINI_API_KEY` is set in the environment the API runs in.
  - `GEMINI_MODEL` names a model your key can use with audio input.
  - `ffmpeg` and `ffprobe` are on `PATH` (`which ffmpeg`); without them extraction fails the same way.

### 3. Search provider failure or rate limit

- **Symptom:** `SEARCH_FAILED` at stage `search_candidates` (HTTP 500, `retryable: true`),
  with no report saved. This is deliberately never reported as `no_verified_match`.
- **Check:**
  - The provider today is Abdullah's Tiger Data post corpus, so failures are usually
    `TIGERDATA_DSN`, missing grants, or the schema/GIN index not applied
    (`external_apis/tigerdata_schema.sql`).
  - A third-party provider added later will surface HTTP 429 the same way. Wait and re-run
    the same `job_id`; it is idempotent.
- **Note:** an empty corpus legitimately returns `no_verified_match`. Seed it by calling
  `retrieve_post(source_url)` for known sources.

### 4. Photon sender ID wrong

- **Symptom:** `AnalysisResult.delivery_status == "failed"` with
  `delivery_warning: "The summary message could not be sent."`. The report is still
  saved and opens at `report_url`.
- **Check:**
  - The sidecar is running: `curl http://127.0.0.1:8766/health`. If it isn't, sends fail
    with `PHOTON_NOT_CONFIGURED` or a connection error.
  - `PHOTON_BRIDGE_TOKEN` and `PHOTON_BRIDGE_PORT` are identical for the API and the
    sidecar. Both read them from `.secrets/.env`; the sidecar reads it through `.env.local`.
  - For real iMessage, `SPECTRUM_PROJECT_ID`/`SPECTRUM_PROJECT_SECRET` belong to the
    Photon project whose sender line you expect. `PHOTON_SENDER_ID`, `PHOTON_API_KEY`
    and `PHOTON_BASE_URL` are read only by the separate HTTP webhook path
    (`POST /webhooks/photon`, `services/messaging/`), not by the sidecar.
  - Re-running a completed job reuses its saved report and re-attempts delivery.
    The Photon bridge durably tracks the payload and confirmed or uncertain
    delivery state per `(job_id, conversation_id)`; an uncertain outcome is never
    automatically re-sent. The public three-argument `send_summary` call derives
    its stable identity from the report URL when `job_id` is not supplied.

### 5. JSONB too large

- **Symptom:** `STORAGE_FAILED` at `save_report_and_complete_job`, or at ingest, on a very
  long recording. Postgres rejects a single `jsonb` value above about 255 MB, and large
  values also slow inserts until statement timeouts fire.
- **Check:** find the biggest stored values:

  ```sql
  select report_id, pg_size_pretty(pg_column_size(report_json)::bigint)
  from quantro_reports order by pg_column_size(report_json) desc limit 5;
  select job_id, pg_size_pretty(pg_column_size(features_json)::bigint)
  from quantro_posts order by pg_column_size(features_json) desc limit 5;
  ```

- **Note:** reports hold transcripts and frame metadata, never image bytes, and
  `EXTRACTION_OPTIONS` caps frames at 12. Transcript length is the variable to limit,
  for example by capping recording duration before extraction.
