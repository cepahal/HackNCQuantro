# Quantro architecture

The canonical app is `backend/` (FastAPI/Python) and `frontend/` (Next.js).
`backend/services/reporting/run_analysis.py` connects the contract functions;
`docs/INTEGRATION.md` records the function map, routes and environment variables.

All contract models and enums now live in `backend/services/shared/types.py`;
`shared/errors.py` owns QuantroError and stage normalization; import them directly
(the old re-export shims were removed). Each public contract function validates
its return against these models, retaining JSON dict serialization at existing
upstream boundaries. Startup checks all twelve team functions plus run_analysis.
The four empty merge scaffolds formerly under `services/_archive/` were deleted
(see `docs/CLEANUP_LOG.md`).

## Request flow

1. Website submits `{url}` to `POST /analyze/{job_id}/run`. Photon URL intake
   submits the same request with its real `conversation_id`.
2. Retrieval downloads one accessible JPEG/MP4, commits its bytes/Post to Tiger
   Data, and returns the internal Post with local media path.
3. Extraction produces contract MediaFeatures. Requested transcription/OCR
   provider failures are errors; missing speech is explicitly unavailable.
4. Query generation feeds the configured search provider: Exa public-web search
   or the legacy Tiger Post metadata corpus. Query ordering mixes spoken, OCR, and title evidence. Exa uses the first three
   clues plus a video-host search, with at most four requests. Exa prioritizes recording-shaped URLs within fetched results
   before applying the candidate limit; this is not verification. Candidate webpages are attempted
   through the guarded downloader; unsupported media is skipped with warnings. The submitted URL is excluded from source selection.
5. Transcript alignment verifies candidates conservatively. Only a verified
   candidate proceeds to surrounding context and caption comparison.
6. Report schema 2.1 retains provenance, complete transcript data, confidence
   limitations and evidence. Filesystem paths are removed from public results.
7. Report is saved before the job is completed. Repeated completed requests reuse
   it; concurrent first-save-wins requests deliver the actual stored report.
8. Job-owned temporary downloads and frames are removed after reporting or failure;
   original media bytes remain in Tiger for playback. Paths in stored internal feature
   metadata are not a durable file cache.
9. The frontend loads `GET /reports/{report_id}` and displays the saved result.
   With a conversation ID, summary delivery runs after save; delivery failure
   does not discard a successfully saved report.

No-match is a normal corpus-search outcome, not a provider-error substitute.
Confidence, coverage and context assessment remain separate; there is no overall
credibility score.

## Tiger Data storage

`external_apis/tigerdata_storage.py` stores original media bytes in BYTEA and the
unchanged internal Post in JSONB in `quantro_retrieval.posts`. Bound SQL, SHA-256,
byte limits, verified TLS and commit-before-success protect this handoff. Apply
`external_apis/tigerdata_schema.sql` for this media/corpus store; it also contains
the full-text index used by source search. Retrieval never creates the schema.

`services/storage/tiger.py` manages jobs, extracted features, reports and delivery
state through SQLAlchemy in the same Tiger service. Its tables use the configurable
`quantro_` prefix and are initialized at API startup. Configure `TIGERDATA_DSN`
for retrieval and either the same DSN or `TIGER_DATABASE_URL` for job/report state.
Private values belong in ignored root `.env.local` or the process environment;
see the audit for the existing loader precedence differences.

`GET /media/{media_id}` reads real stored JPEG/MP4 bytes for browser playback,
including HTTP byte ranges, when the Post has no public playback_url. IDs are
validated and SQL parameters are bound. No S3 upload is implemented or claimed.
Tiger is the only storage backend. REPORT_STORE supports tiger and explicit local
memory only.

## Frontend and API

`frontend/lib/api.ts` uses `NEXT_PUBLIC_API_BASE_URL` (localhost:8000 by default).
The homepage handles URL submission, loading, safe errors and saved results.
`/reports/[report_id]` loads persisted reports. The UI shows transcripts, poster
metadata, source/context confidence, clip coverage, matching/edit information,
findings and limitations; unknown fields are displayed as unavailable.

API routes are `/analyze/{job_id}/run`, `/analyze/{job_id}/status`, `/reports/{report_id}`,
`/ingest/{job_id}` (POST/GET), and `/media/{media_id}`. CORS uses REPORT_BASE_URL,
default localhost:3000. There is no generic POST /analyze route.

## Photon

`backend/services/photon/` is the Node 24 Spectrum sidecar. It persists inbound
URL events before dispatching them to the backend, using stable event-derived
job IDs and a bounded serialized queue. Private loopback `POST /send-summary`
uses PHOTON_BRIDGE_TOKEN and Idempotency-Key and returns provider acceptance.
Delivery state is independent of analysis. Ambiguous sends are not automatically
replayed. Interrupted/overflowed intake records require operator inspection.
Attachments are retained at intake; automatic analysis currently requires a URL.

See its README and `docs/RUNBOOK.md` for operator steps. Local Whisper and Exa
production function calls were verified on 2026-10-10; see `docs/PROVIDERS.md` for
deployment settings and limits. The user reports a successful team Photon round
trip; the new deployed Exa-to-verified-source path still needs a live check.


The website supports public link submission, not file uploads. Reloading a running
results URL polls persisted job state without starting another analysis. The status
route returns only job status, a completed report ID, and a safe failure envelope.
It is not a durable task queue: interrupted jobs still require a new submission.

The HTTP Photon webhook reserves inbound conversation/job pairs while processing.
An analysis or summary-delivery failure releases the reservation for a later retry;
a retry of a completed analysis reuses its report. Successful replies remain deduplicated
in the process-local tracker. Cross-process/restart deduplication still requires durable
inbound tracking; the Spectrum sidecar retains its separate persistent queue.

## Reconciled reliability guards

Shared source identity checks reject self-matches during selection, report creation,
cache reuse, serving, display, and final delivery. Definitive caption verdicts
require a separate transcript grounding check; unassessable evidence abstains.
The Spectrum listener has bounded GET-only restart reconciliation with explicit
operator opt-in for provably never-started saved requests. Runtime exercises
FFmpeg and ffprobe before API startup. Offline push/PR regression checks run
without provider secrets. See [../temporary-fixes.md](../temporary-fixes.md) and
[RUNBOOK.md](RUNBOOK.md) for tested behavior, remaining limits, and operator steps.


### Audit reconciliation (2026-10-10)

The API now atomically reserves public ingest IDs and rejects reused IDs with 409.
All job URLs are immutable through analysis, including failed/running jobs.
Job error messages and retryability persist through two additive nullable columns;
startup upgrades existing job tables without deleting data. Status checks expire
inactive running/ingested jobs after ANALYSIS_STALE_SECONDS (default900), using an
atomic timestamp predicate; analysis stages refresh the timestamp.

All dotenv users share one cached loader; explicit process values always win.
Selected search configuration is validated before accepting work. API validation,
HTTP and unexpected errors use the same four-field envelope. Input byte/depth
limits run before parsing, and request models reject extra keys. Invalid/private/
unsupported submitted URLs are rejected before any job write. This intentionally
supersedes the old Test7 expectation of a failed row for syntactically invalid URLs.

Media playback uses a lifespan-owned three-connection TLS pool, metadata-only
queries and bound PostgreSQL substring reads of at most1MiB. HTTP HEAD, byte ranges,
conditional ETags, CORS playback headers and disconnect cleanup are covered.

The optional HTTP Photon adapter stores hashed inbound/summary reservations in
{TIGER_TABLE_PREFIX}message_deliveries, preventing duplicate sends across restarts.
Signed AdvancedKit v0 requests use a dedicated secret and a300second freshness
window. This ledger provides deduplication, not a durable background-job queue:
a crash after reservation requires operator review. Spectrum remains primary.
