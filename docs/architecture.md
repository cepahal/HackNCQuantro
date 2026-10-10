# Quantro architecture

The canonical app is `backend/` (FastAPI/Python) and `frontend/` (Next.js).
`backend/services/reporting/run_analysis.py` connects the contract functions;
`docs/MVP_INTEGRATION_AUDIT.md` records exact imports, types, routes and live gates.

All contract models and enums now live in `backend/services/shared/types.py`;
`shared/errors.py` owns QuantroError and stage normalization. Historical type/error
modules only re-export canonical classes. Each public contract function validates
its return against these models, retaining JSON dict serialization at existing
upstream boundaries. Startup checks all twelve team functions plus run_analysis.
The four empty merge scaffolds are retained under `services/_archive/`.

## Request flow

1. Website submits `{url}` to `POST /analyze/{job_id}/run`. Photon URL intake
   submits the same request with its real `conversation_id`.
2. Retrieval downloads one accessible JPEG/MP4, commits its bytes/Post to Tiger
   Data, and returns the internal Post with local media path.
3. Extraction produces contract MediaFeatures. Requested transcription/OCR
   provider failures are errors; missing speech is explicitly unavailable.
4. Query generation searches the stored Tiger Post metadata corpus. Candidate
   downloads reuse retrieval. The submitted URL is excluded from source selection.
5. Transcript alignment verifies candidates conservatively. Only a verified
   candidate proceeds to surrounding context and caption comparison.
6. Report schema 2.1 retains provenance, complete transcript data, confidence
   limitations and evidence. Filesystem paths are removed from public results.
7. Report is saved before the job is completed. Repeated completed requests reuse
   it; concurrent first-save-wins requests deliver the actual stored report.
8. The frontend loads `GET /reports/{report_id}` and displays the saved result.
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
The optional SnowflakePostStore remains a real compatibility adapter; Tiger is
the active default. REPORT_STORE supports tiger and explicit local memory only.

## Frontend and API

`frontend/lib/api.ts` uses `NEXT_PUBLIC_API_BASE_URL` (localhost:8000 by default).
The homepage handles URL submission, loading, safe errors and saved results.
`/reports/[report_id]` loads persisted reports. The UI shows transcripts, poster
metadata, source/context confidence, clip coverage, matching/edit information,
findings and limitations; unknown fields are displayed as unavailable.

API routes are `/analyze/{job_id}/run`, `/reports/{report_id}`,
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

See its README and `docs/RUNBOOK.md` for operator steps. The live iMessage intake,
acknowledgment, matched analysis, and report reply were verified on October 10.
Gemini text/comparison is working with the privately configured model; transient
provider failures can still exceed the 60-second demo target.

The optional `frontend/scripts/report-gateway.mjs` serves only explicitly shared
report pages, sanitized report/display JSON, linked media, and frontend static
assets. It binds to loopback port 3100; analysis and raw job endpoints stay private.
`PHOTON_PUBLIC_REPORTS_FILE` enables atomic allowlisting before a report reply is
sent. The gateway and sidecar share the ignored JSON array at that path. Public
links are readable by anyone who has them; revoke a report by removing its ID.
Gateway limits are 32 report IDs and 16,384 bytes. Quick Tunnel URLs are temporary.
`NEXT_PUBLIC_REPORT_API_BASE_URL` can point report/media reads at that gateway
while `NEXT_PUBLIC_API_BASE_URL` retains the local analysis endpoint.
Independent public-search HTTP requests overlap with at most three workers;
results, provenance, warnings, and ranking retain original query order.


## Prompt 2.5 integration branch (2026-10-10)

Retrieval and candidate retrieval share the guarded downloader plus actual ffprobe
measurement and return canonical Post models. Tiger storage receives JSON dictionaries.
Extraction defaults to local cached faster-whisper base CPU/int8 and Tesseract OCR;
explicit options.provider=gemini preserves the previous Gemini adapter. Local speech
or OCR failures produce unavailable features and warnings, per Prompt 2.5. Comparison
still uses Gemini and requires funded credits.

Search defaults to explicitly configured public-web Brave, SerpAPI or Google CSE.
SEARCH_PROVIDER=tigerdata selects the real stored corpus; no silent fallback occurs.
SerpAPI credentials are configured only in the ignored local environment. Startup rejects functions
whose implementation filename includes stub or mock. Real YouTube retrieval, local transcription/frames, public search, candidate retrieval
and verified alignment passed; see PROMPT_2_5_NETWORK_TEST_OUTPUT.txt. No Prompt 3 or main merge.


## Prompt 3 persistence/ingest reconciliation

API startup now requires TIGER_DATABASE_URL explicitly. Production (APP_ENV or
ENVIRONMENT=production) requires PostgreSQL with require/verify-ca/verify-full TLS,
including loopback. save_report accepts (report_id, job_id, report_json), preserving
legacy callers. GET /ingest/{job_id} returns {job, post, features} with exact persisted
JSON, including internal worker paths; use sanitized /reports endpoints for public UI.
Real job_test_001: ingested, complete transcript, 3 sentences, 12 frames; exact Tiger
JSON round-trip passed. Missing Gemini credits and live iMessage delivery remain MVP gates.


## Prompt 4 orchestration reconciliation

run_analysis calls public run_ingest and reloads its exact stored Post/MediaFeatures.
Search exceptions always become retryable SEARCH_FAILED; all viable alternatives
are disclosed, ordered by confidence then coverage. Comparison receives the selected
source caption as Prompt 4 specifies. All teammate failures are normalized by stage.
Production report storage is Tiger only; in-memory stores require explicit test injection.
Delivery occurs after durable report save; even untyped delivery failures retain reports
and return a safe warning. See PROMPT_4_VERIFICATION.md for real no-match/save/GET proof.
Live matched comparison still needs funded Gemini; real Photon acceptance remains unverified.

## Runtime readiness and Photon failure handling

Backend startup resolves optional `MEDIA_TOOLS_DIR` and requires both FFmpeg and
ffprobe before connecting to Tiger or accepting work. Managed Photon startup
checks the required local API routes before connecting Spectrum; terminal mode
remains independent. Recognized structured HTTP failures persist as failed with
safe diagnostics. Lost or malformed responses trigger GET-only reconciliation of
the saved report and job; an ambiguous analysis POST is never automatically replayed.
See [../temporary-fixes.md](../temporary-fixes.md) for current verification and open work.
