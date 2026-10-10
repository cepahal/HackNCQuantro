# Quantro v2 repository audit and integration handoff

> Reconciliation update, 2026-10-10: canonical definitions have moved to
> `backend/services/shared/types.py` and `shared/errors.py`. Legacy modules now
> re-export the same classes, rather than defining competing models. Public
> contract bodies enforce output validation and shared errors; JSON-native
> upstream boundaries remain dicts. The four Section C files moved into
> `backend/services/_archive/` with dated headers. Startup checks all 13
> callables. `tests/test_contract_shapes.py` pins exact fields, invalid-input
> errors, canonical class identity, schema version and null serialization.
> File/line locations below describe the original audit snapshot and should be
> resolved by function name after this consolidation. Provider gates remain.

Date: 2026-10-10. Repository: `cepahal/HackNCQuantro`, branch `main`.
First action was `git pull --ff-only origin main`, which returned up to date at
`c32d418545299858eced93f089d6476f2be53c3e`. The initial request was read-only;
the subsequent request to finish integrations in parallel authorized the changes
described here. This report describes the resulting implementation and records
the remaining live-service gates separately from tested code.

Contract reference: supplied **HackNC (4).pdf**, 27 pages, contracts v2/report
schema 2.1, together with the user's explicit function signatures. The list
contains **12 team functions plus one orchestrator**, so all 13 are audited.
Search included backend, frontend, tests and docs, snake_case, camelCase and
alternative verbs. No second callable implementation was found under an alias.

## Contract function inventory

Paths below are relative to the repository root. Imports assume that root is on
`PYTHONPATH`. “Compatible” means the requested call works; it does not claim
literal signature equality where optional keyword-only dependencies are exposed.
“JSON yes” means a Pydantic result must be serialized with
`.model_dump(mode="json")`; routes already do this.

| # | function | file:line | exact import statement | signature matches v2? | return shape matches v2? | reads env vars (which) | raises which error class |
|---|---|---|---|---|---|---|---|
| 1 | retrieve_post | backend/services/external_apis/retrieve_post.py:390 | `from backend.services.external_apis.retrieve_post import retrieve_post` | Compatible; optional storage/output_dir/timeout_seconds/max_bytes | Yes, Post dict | Indirect: TIGERDATA_DSN, DATABASE_URL, TIGERDATA_SSL_ROOT_CERT; rejects PGSERVICE/PGHOSTADDR | RetrievalError; ValueError for invalid optional configuration; ingest converts to QuantroError |
| 2 | extract_media_features | backend/services/processing/preprocessing.py:143 | `from backend.services.processing.preprocessing import extract_media_features` | Compatible; options defaults to None | Yes, MediaFeatures dict; JPEG semantic extension below | Indirect: GEMINI_API_KEY, GEMINI_MODEL when a model is needed | ExtractionError (QuantroError subclass), QuantroError |
| 3 | build_search_queries | backend/services/external_apis/build_search_queries.py:63 | `from backend.services.external_apis.build_search_queries import build_search_queries` | Yes, (features, post) | Yes, {queries,warnings} | None | QuantroError |
| 4 | search_candidates | backend/services/external_apis/search_candidates.py:44 | `from backend.services.external_apis.search_candidates import search_candidates` | Compatible; optional provider | Yes, {candidates,warnings} | Indirect: retrieval Tiger variables in row 1 | QuantroError |
| 5 | retrieve_candidate_media | backend/services/external_apis/retrieve_candidate_media.py:44 | `from backend.services.external_apis.retrieve_candidate_media import retrieve_candidate_media` | Compatible; same optional retrieval dependencies as #1 | Yes, Post dict, candidate_id persisted before success | Indirect: retrieval Tiger variables in row 1 | QuantroError |
| 6 | verify_and_align | backend/services/specialist/alignment.py:37 | `from backend.services.specialist.alignment import verify_and_align` | Yes, (clip_features, candidate_features) | Yes for specified v2 fields; inherited base details caveat below | None | QuantroError |
| 7 | select_surrounding_context | backend/services/specialist/context.py:52 | `from backend.services.specialist.context import select_surrounding_context` | Compatible; options defaults to None | Yes, Context dict | None | ValueError, KeyError, TypeError; orchestrator normalizes |
| 8 | compare_context_and_caption | backend/services/specialist/comparison.py:148 | `from backend.services.specialist.comparison import compare_context_and_caption` | Yes, (clip_features, context, caption) | Yes, Comparison dict | Indirect: GEMINI_API_KEY, GEMINI_MODEL | QuantroError |
| 9 | build_report | backend/services/reporting/build_report.py:81 | `from backend.services.reporting.build_report import build_report` | Compatible; eight requested args plus optional extra_limitations | JSON yes, ReportV2 model; inherited base details caveat below | None | QuantroError |
| 10 | save_report_and_complete_job | backend/services/reporting/save_report.py:111 | `from backend.services.reporting.save_report import save_report_and_complete_job` | Compatible; optional report_store/job_store | JSON yes, JobCompletionResult model | REPORT_STORE, REPORT_BASE_URL; indirect job/report Tiger variables below | QuantroError |
| 11 | display_report | backend/services/reporting/display_report.py:237 | `from backend.services.reporting.display_report import display_report` | Yes, (report) | DisplayModel model per user's request; PDF describes rendered website instead | None | QuantroError |
| 12 | send_summary | backend/services/reporting/send_summary.py:129 | `from backend.services.reporting.send_summary import send_summary` | Compatible; exact three-argument call works, optional job_id/provider/tracker | JSON yes, DeliveryResult model | Indirect: PHOTON_BRIDGE_TOKEN, PHOTON_BRIDGE_PORT | QuantroError |
| O | run_analysis | backend/services/reporting/run_analysis.py:191 | `from backend.services.reporting.run_analysis import run_analysis` | Compatible; (job_id,url,conversation_id=None), optional store/provider/tracker | JSON yes, AnalysisResult model | All applicable upstream/store/delivery variables | QuantroError |

Actual positional signatures are the signatures in the request. Optional keyword
extensions do not change their order. Upstream stages return JSON-native dicts;
reporting stages accept those dicts as well as shared Pydantic models.

## SECTION A — MISSING

**No listed callable is missing.** All 13 are implemented and imported by a
consumer or tested directly. The following live capabilities are still gates:

- **Gemini generation:** the replacement key returned HTTP **402**,
  `RESOURCE_EXHAUSTED`, with the explicit message that prepaid credits are
  depleted. User requested that model calls be paused until a funded key is
  supplied. The earlier key returned 429 for vision. These are actual provider
  responses, not a mocked rate-limit test. No key value is included here.
- **Source corpus:** production discovery searches actual stored Tiger Data Post
  metadata. It does not search YouTube/the public web or index transcripts.
  A useful matched-source demo requires an accessible source recording to be
  ingested first, with metadata matching generated queries. There is no hidden
  implementation behind SEARCH_PROVIDER_API_KEY.
- **YouTube demonstration:** selected official TED pair:
  [full Ken Robinson talk](https://www.youtube.com/watch?v=iG9CE55wbtY) and
  [TED excerpt](https://www.youtube.com/watch?v=g4IAa8wZlqU). The excerpt identifies
  the full talk as its source. Actual download encountered YouTube sign-in/bot
  verification, so downloading, transcription and matched analysis of this pair
  are **not verified**. No invented transcript or source record was seeded.
- **Photon end-to-end:** real SDK authentication/initialization passed. No real
  inbound message was consumed or summary sent during verification. A real
  allowlisted tester and a phone-accessible frontend URL remain necessary.
- **Deployment:** frontend/backend run locally. Localhost report links are not
  reachable from a remote phone. Configure deployed API/frontend origins before
  that test. Attachment intake exists, but automatic analysis requires a URL.

There are no fake success adapters in the active reporting pipeline. Empty source
results mean no result in the indexed corpus, not proof that no source exists.

## SECTION B — DRIFT

### Exact serialized field sets

No missing, renamed or extra keys were found in the explicitly specified
upstream v2 field sets after integration. Unknown optional metadata is retained
as null, unavailable collections as [], and provenance/warnings remain separate.
The audited shapes are:

- **Post:** `post_id,candidate_id,post_url,platform,title,caption,published_at,author,engagement,media,retrieval`.
  Author: `id,handle,display_name,profile_url,bio,verified,followers_count,following_count,posts_count`.
  Engagement: `views_count,likes_count,comments_count,shares_count`.
  MediaAsset: `media_id,local_path,mime_type,playback_url,duration_seconds,width,height,fps,has_audio`.
  Retrieval: `retrieved_at,provider,metadata_source_urls,unavailable_fields,warnings`.
  IDs/URLs/paths/MIME are strings; optional metadata uses the declared nulls,
  counts are nonnegative integers, durations/fps are numbers, booleans stay booleans.
  published_at is the post date, not the recorded event date.
- **Queries:** `queries:list[str],warnings:list[str]`.
- **CandidateList:** `candidates:list[Candidate],warnings:list[str]`.
  Candidate: `candidate_id,url,title,publisher,published_at,snippet,discovered_by_queries`;
  title/publisher/date/snippet are nullable strings, provenance is list[str].
- **MediaFeatures:** `media_id,duration_seconds,language,transcript,frames,visual_text,warnings`.
  Transcript: `status,processed_ranges,sentences`; each range has numeric
  start_seconds/end_seconds; each sentence has sentence_id/start_seconds/end_seconds/text/speaker_id.
  Frame: `frame_id,timestamp_seconds,image_path,image_url`.
  Visual text: `frame_id,timestamp_seconds,text`. Language/speaker/image_url may be null.
- **Alignment:** `status,clip_media_id,source_media_id,matches,match_quality,edit_analysis,limitations`.
  Match quality: `confidence,matched_clip_seconds,clip_coverage_ratio,metrics,methods_used`.
  Confidence: `level,method,basis,limitations`, with level high/moderate/low/not_assessed.
  Coverage is a numeric ratio or null, not a confidence percentage.
  Edit counts: `matched_passages,source_gaps,unmatched_clip_segments,confirmed_added_segments,confirmed_deleted_segments`.
  Every count is nullable; unassessed counts are null, including matched_passages.
- **Context:** `source_media_id,windows,limitations`.
  Window: `context_id,match_ids,requested_start_seconds,requested_end_seconds,coverage,sentences`.
- **Comparison:** `context_assessment,caption_assessment,assessment_confidence,summary,findings,limitations`.
  Finding: `finding_id,type,explanation,evidence`.
  Evidence: `media_id,sentence_ids,start_seconds,end_seconds,quote`.
- **ReportV2:** `schema_version,job_id,outcome,submitted,source,alignment,context,comparison,summary,limitations`.
  schema_version is "2.1"; outcome is matched/no_verified_match. Submitted/source:
  `post,transcript,frames,visual_text`. Public report Post intentionally omits
  `media.local_path`; public frames intentionally omit `image_path`, as required
  by the PDF's filesystem privacy rule. Internal Post/MediaFeatures retain them.
- **JobCompletionResult:** `job_id,status,report_id,report_url,completed_at`;
  status is completed, completed_at is a UTC timestamp string.
- **DeliveryResult:** `conversation_id,message_id,status`; status is accepted,
  representing provider acceptance rather than a read receipt.
- **AnalysisResult:** `job_id,outcome,report_id,report_url,delivery_status,delivery_warning,warnings`;
  delivery_status is accepted/failed/skipped, warning is nullable string.
- **DisplayModel:** `source_match,clip_coverage,context_assessment,confidence_note,no_source_message,context_unavailable_message,submitted_panel,source_panel,poster_info,matches,edit_analysis,findings,limitations`.

### Remaining differences and limits of exact certification

1. **Python return representation:** #9–#12 and orchestrator return Pydantic
   models, not dicts. Their serialized keys match the wire contract. An integrator
   indexing them as `result["report_id"]` must serialize first or use attributes.
2. **Literal signatures:** #1/#4/#5/#9/#10/#12/O expose optional keyword-only
   injection/configuration parameters; #2/#7 make options optional. These are
   compatible extensions, not literal signature equality.
3. **Error classes:** #1 uses RetrievalError and #7 uses builtin validation
   exceptions. The orchestrator/ingest boundary converts them into the shared
   `{error:{code,stage,message,retryable}}` API form. Direct callers must handle
   the table's actual classes. MEDIA_TIMEOUT is an internal retrieval extension
   and is normalized to MEDIA_UNAVAILABLE at ingest.
4. **Display contract ambiguity:** PDF #11 describes a rendered website, not a
   JSON DisplayModel schema. The explicit user request asks for DisplayModel, so
   Python returns that model while Next.js renders saved ReportV2 directly.
   The two rendering paths use the exact Source-match confidence / Submitted
   clip aligned / Context-assessment confidence labels. No overall score exists.
5. **JPEG semantics:** the PDF's corrected feature examples describe video.
   JPEG support keeps identical keys/types but uses duration_seconds=0.0,
   transcript unavailable, no speech ranges/sentences, frame timestamp 0.
   This is a documented still-image extension, not evidence of speech matching.
6. **Inherited unspecified structures:** PDF asks to preserve existing
   Alignment.matches/edit_analysis and existing ReportV2 structure without
   listing every inherited field. Their complete historical v1 specification
   is absent; exact identity to an unseen baseline cannot be independently
   certified. Current structures are enumerated in reporting/types.py and pinned
   by contract tests. Source gap items include before_match_id/after_match_id;
   match evidence includes method/clip_refs/source_refs/description.
7. **Matching capability:** exact distinctive transcript matching is conservative,
   capped at moderate confidence, and is not visual/audio/original-upload
   verification. Silent/JPEG inputs remain not_assessed. This is a capability
   limitation, not a renamed field or synthetic match.
8. **HTTP lookup errors:** missing report/job/media uses
   `{error:{message}}` (404); request validation uses FastAPI `{detail:[...]}`
   (422), and invalid byte ranges return 416 without JSON. These route-level
   forms differ from the four-field contract-function error envelope. Contract
   stage failures use the shared envelope and safe messages.

### Drift corrected in this integration

Dict outputs now cross reporting boundaries without requiring callers to build
Pydantic objects. Unassessed matched_passages changed 0 -> null. Three-argument
send_summary no longer requires a separate injected job_id. Requested OCR provider
failures now raise instead of becoming successful empty OCR. Typed extraction and
search errors retain retryability. Completed retries reuse persisted reports;
concurrent first-save-wins delivery uses that same persisted report. Submitted
URLs are filtered from candidate selection to prevent trivial self-matches.
Private paths are removed from public ingest/report responses. The unused
Snowflake reporting placeholder was removed; Tiger is the real default store.

## SECTION C — ORPHANS

These Python files define no contract function and have no discovered static
imports in backend/frontend/tests. Each is an empty scaffold, not active logic:

| file:line | finding |
|---|---|
| backend/services/external_apis/example_api.py:1 | Empty, unreferenced |
| backend/services/scoring.py:1 | Empty, unreferenced |
| backend/services/specialist/analyze.py:1 | Empty, unreferenced |
| backend/utils/helpers.py:1 | Empty, unreferenced |

`_retrieval_worker.py` is launched dynamically by retrieve_post using `python -m`
and is not dead code. main.py and storage/tiger.py are runnable entry points.
snowflake_storage.py is a real optional adapter imported by its tests, so it does
not satisfy the strict “not imported by anything” orphan test. No files were
deleted merely because a static scan did not find an import.

## SECTION D — ENV VARS

Every explicit application environment read under backend is listed below,
including dotenv-mapped reads and the Photon TypeScript sidecar. Grouped rows
list each named variable; locations identify the read/configuration block.

| variable | file:line | fallback / requirement |
|---|---|---|
| REPORT_BASE_URL | backend/main.py:31; backend/services/reporting/save_report.py:107 | http://localhost:3000 |
| REPORT_STORE | backend/services/reporting/save_report.py:83 | tiger; memory is an explicit local alternative |
| TIGER_DATABASE_URL | backend/services/storage/tiger.py:111 | blank -> TIGERDATA_DSN -> DATABASE_URL; requires a usable DB |
| TIGERDATA_DSN | backend/services/storage/tiger.py:113; backend/services/external_apis/tigerdata_storage.py:148 | SQLAlchemy fallback above; media store falls back to DATABASE_URL only if absent, not if explicitly blank |
| DATABASE_URL | backend/services/storage/tiger.py:113; backend/services/external_apis/tigerdata_storage.py:148 | blank; missing connection is an error |
| TIGERDATA_SSL_ROOT_CERT | backend/services/storage/tiger.py:120; backend/services/external_apis/tigerdata_storage.py:161 | configured/DSN CA, otherwise system; TLS verification stays enabled |
| TIGER_TABLE_PREFIX | backend/services/storage/tiger.py:129 | quantro_ |
| PGSERVICE | backend/services/external_apis/tigerdata_storage.py:113 | no fallback; rejected if set for this adapter |
| PGHOSTADDR | backend/services/external_apis/tigerdata_storage.py:114 | no fallback; rejected if set for this adapter |
| GEMINI_API_KEY | backend/services/specialist/gemini.py:25 | required for requested transcription/OCR/comparison |
| GEMINI_MODEL | backend/services/specialist/gemini.py:26 | gemini-flash-latest |
| PHOTON_BRIDGE_TOKEN | backend/services/external_apis/photon.py:100; backend/services/photon/src/config.ts:11 | blank -> explicit error; required private bridge token |
| PHOTON_BRIDGE_PORT | backend/services/external_apis/photon.py:101; backend/services/photon/src/config.ts:20 | 8766 |
| SPECTRUM_PROJECT_ID | backend/services/photon/src/config.ts:15 | required in managed iMessage mode |
| SPECTRUM_PROJECT_SECRET | backend/services/photon/src/config.ts:16 | required in managed iMessage mode |
| PHOTON_BACKEND_PORT | backend/services/photon/src/config.ts:25 | 8000; loopback analysis dispatch |
| PHOTON_DATA_DIR | backend/services/photon/src/config.ts:32 | .data/photon |
| SNOWFLAKE_ACCOUNT | backend/services/external_apis/snowflake_storage.py:103,128 | required only for optional SnowflakePostStore |
| SNOWFLAKE_USER | backend/services/external_apis/snowflake_storage.py:103,129 | required only for optional SnowflakePostStore |
| SNOWFLAKE_TOKEN | backend/services/external_apis/snowflake_storage.py:104 | blank; token preferred if present |
| SNOWFLAKE_PASSWORD | backend/services/external_apis/snowflake_storage.py:105 | blank; either token or password required |
| SNOWFLAKE_MEDIA_STAGE | backend/services/external_apis/snowflake_storage.py:120 | QUANTRO_MEDIA |
| SNOWFLAKE_POSTS_TABLE | backend/services/external_apis/snowflake_storage.py:121 | QUANTRO_POSTS |
| SNOWFLAKE_WAREHOUSE, SNOWFLAKE_DATABASE, SNOWFLAKE_SCHEMA, SNOWFLAKE_ROLE | backend/services/external_apis/snowflake_storage.py:124–125 | blank optional connection settings |
| any variable ending _proxy, case-insensitive | backend/services/external_apis/_retrieval_worker.py:227–229 | removed in downloader child to preserve destination validation |

The helpers also merge/enumerate `os.environ` (Tiger media :102, Snowflake :79)
and `process.env` (Photon index.ts:12); these are not additional named application
settings. SDKs/drivers may read their own environment variables internally.

Load order is currently not uniform: main loads .secrets/.env, .env.local, then
default dotenv without overriding process values. Retrieval merges backend/.env,
root .env, root .env.local, then process values. Gemini loads root .env and
backend/.env, then .env.local with override=True. Photon start scripts load root,
backend and local dotenv files with existing process values taking precedence.
Use ignored .env.local consistently locally; changing process variables alone
will not override Gemini's local file. No real credentials were committed.

Template-only variables without consumers: RETRIEVAL_PROVIDER_API_KEY,
SEARCH_PROVIDER, SEARCH_PROVIDER_API_KEY, TRANSCRIPTION_PROVIDER,
TRANSCRIPTION_API_KEY and NEXT_PUBLIC_REPORT_BASE_URL. Legacy PHOTON_API_KEY /
PHOTON_BASE_URL / PHOTON_SENDER_ID do not configure Spectrum. A template field
does not imply a working provider behind it.

Hard-coded URLs: localhost:3000 is the report/CORS default; localhost:8000 is
the frontend default; 127.0.0.1 bridge/backend addresses are intentional
loopback boundaries (photon.py:115, photon/src/analysis.ts). Allowed social hosts
in retrieve_post.py:31–39 are downloader policy, not credentials. Illustrative
example.com/app.example values appear in tests/older examples. No hard-coded
production key was found in changed tracked content. Production requires actual
frontend/API origins rather than these local defaults.

## SECTION E — SHARED TYPES

| type | canonical validation definition | additional definition / conflict |
|---|---|---|
| Post | backend/services/reporting/types.py:141 | external_apis/post_contract.py:48 TypedDict; same internal field set |
| MediaAsset | reporting/types.py:115 | external_apis/post_contract.py:28 TypedDict; same internal field set |
| MediaFeatures | reporting/types.py:185 | specialist produces plain dicts; no competing class |
| Alignment | reporting/types.py:253 | specialist produces plain dicts; no competing class |
| Context | reporting/types.py:275 | specialist produces plain dicts; no competing class |
| Comparison | reporting/types.py:301 | specialist/comparison.py:42 also defines Comparison with matching fields, stricter evidence validation |
| ReportV2 | reporting/types.py:320 | frontend/lib/api.ts:39 is a partial UI projection, not another backend schema |
| QuantroError | backend/services/reporting/errors.py:51 | RetrievalError and storage errors are adapter classes normalized at boundaries |

Comparison/Evidence/Finding/confidence definitions are duplicated between
specialist and reporting. Current fields agree and tests pin equality, so there
is no demonstrated wire conflict, but parallel definitions can drift. PublicPost
and PublicMediaAsset intentionally remove local paths; do not substitute them for
the internal retrieval handoff. Queries/Candidate/CandidateList live in
external_apis/search_contract.py:6/:11/:21. AnalysisResult is run_analysis.py:66;
DisplayModel is display_report.py:68. frontend/lib/displayModel.ts remains a
view interface; it does not create another backend contract implementation.

## SECTION F — ROUTES

main.py:59–61 registers analyze, ingest and media routers; :48 registers the
QuantroError handler. main lifespan initializes the Tiger job/report schema.

| method | route / file:line | request | successful response | contract calls |
|---|---|---|---|---|
| POST | /analyze/{job_id}/run — backend/routes/analyze.py:27 | {url:string,conversation_id?:string\|null}; URL 1–8192 chars, conversation 1–256 | AnalysisResult seven fields above | run_analysis -> retrieval/extraction/query/search/candidate retrieval/alignment/context/comparison/build_report/save; send_summary only with conversation |
| GET | /reports/{report_id} — backend/routes/analyze.py:36 | no body | sanitized ReportV2 | stored report validation/sanitization; not Python display_report |
| POST | /ingest/{job_id} — backend/routes/ingest.py:23 | {url:string}, 1–8192 chars | {job_id,url,post_id,media_id,transcript_status,sentence_count,frames_count,persisted_at} | retrieve_post, extract_media_features via ingest pipeline |
| GET | /ingest/{job_id} — backend/routes/ingest.py:32 | no body | {id,url,status,created_at,updated_at,error_code,error_stage,post_json,features_json}, sanitized | Tiger job read; no new model call |
| GET | /media/{media_id} — backend/routes/media.py:35 | no body; optional Range:bytes=start-end/open/suffix | actual JPEG/MP4 bytes; 200 or 206, Content-Range/ETag/Accept-Ranges | TigerDataPostStore.read_media, not a contract stage |

Job/media IDs accept only 1–128 ASCII letters/numbers/underscore/hyphen. Report
IDs also require report_ prefix. FastAPI registers GET /openapi.json, /docs,
/docs/oauth2-redirect and /redoc. routes/common.py is shared dependency/error
logic with no router. No POST /analyze endpoint exists; the job-specific route
above is the real integration endpoint.

Photon is a separate authenticated loopback HTTP bridge, not a main.py router:
POST /send-summary takes {conversation_id,summary,report_url} plus stable
Idempotency-Key and returns DeliveryResult. Its received URL event dispatches
the backend's POST analysis route with the real conversation_id. Durable intake,
bounded queue and ambiguous-send handling are described in its README.

## SECTION G — TESTS

All Python test files directly under tests/ are included. Coverage below is
behavioral inventory, not a measured coverage percentage. Provider fixtures
verify integration contracts; they do not establish actual transcription quality.

| test file | contract coverage / important boundary |
|---|---|
| test_build_report.py | #9 sanitization, limitation merging, error normalization |
| test_context.py | #7 coverage windows/ranges/validation |
| test_display_report.py | #11 helpers, confidence labels, null counts |
| test_gemini_environment.py | #2/#8 shared model environment/TLS and safe provider/transport errors |
| test_ingest.py | #1/#2 handoff, persistence, safe failures and retryability |
| test_photon.py | #12 real Python HTTP adapter with mocked bridge, timeout/redaction |
| test_photon_reporting.py | #12 PhotonProvider/reporting handoff with mocked HTTP |
| test_pipeline.py | #10/#12, source selection and post-save delivery |
| test_retrieval_worker.py | #1/#5 downloader guard, public-network policy, worker behavior |
| test_retrieve_post.py | #1 complete Post/media contract, download/storage failures and cleanup |
| test_routes.py | O and report routes, request validation and shared errors; upstream mocked |
| test_run_analysis.py | O and #9–#12, upstream stage adapters, no-match/match/errors, cached jobs and canonical persisted delivery |
| test_save_report.py | #10 save-before-complete ordering, dict inputs, idempotence, identity/errors |
| test_send_summary.py | #12 three-arg call, acceptance, deduplication and safe failures |
| test_smoke_e2e.py | HTTP ingest -> O -> report/job save/get against configured real PostgreSQL; all eight upstream stages mocked; test rows cleaned |
| test_snowflake_storage.py | #1 optional compatibility storage through fake connector, bound SQL/bytes |
| test_source_search.py | #3/#4/#5 real function handoffs, provenance, SQL bindings, provider/storage failures |
| test_specialist.py | #2/#6/#7/#8 fake model outputs plus generated FFmpeg recording when available |
| test_specialist_images.py | #2 JPEG/no-speech behavior, strict OCR failure, #6 unavailable alignment |
| test_tiger_store.py | #10/O job/report/feature CRUD, SQLAlchemy store, CLI/env/TLS/idempotence |
| test_tigerdata_storage.py | #1/#4/#5 media bytes/Post SQL, TLS/env/rollback/security, media readback |
| test_types_contract.py | all shared keys/enums/nullability, actual specialist outputs and retrieval fixtures |
| test_media_route.py | browser media 200/206/416, ID validation, missing data and safe storage failure |

Final Python regression: **559 passed, 45 subtests passed, 1 skipped** (this
Windows account cannot create symlinks). FFmpeg was on PATH. One existing
Starlette/httpx deprecation warning remains. `pip check` passed. Relevant changed
route/store/reporting/Gemini/test files passed Ruff; this is not a claim that
all historical specialist formatting passes a repository-wide lint check.

No listed function has zero behavioral test coverage. Python display_report is
called directly in run_analysis tests in addition to its helper tests. Integration
tests combine real adapters with controlled upstream fixtures. Additional tests:
frontend/tests/api.test.ts (6 API/response/error/URL tests); Photon tests for
analysis/config/receive/server/summary (51 tests). Frontend typecheck and production
build passed. Browser verification also exercised the actual form-to-API loading
and safe error path without a model call, then checked persisted media rendering.

Live checks completed separately: real Tiger JPEG/MP4 persistence and byte/SHA
readback; real bound corpus SQL with a rolled-back synthetic search fixture;
candidate metadata/bytes persistence; saved report read after restart; media Range
playback; Photon SDK auth. An earlier live JPEG report completed with an OCR
warning while the old behavior treated OCR as optional. That report proves
storage/browser wiring, **not a strict successful model-analysis run**; requested
OCR failures now fail correctly. No live Gemini matched-source claim is made.

## SECTION H — FRONTEND WIRING

| file:line | endpoint / use | expected response |
|---|---|---|
| frontend/lib/api.ts:70 | NEXT_PUBLIC_API_BASE_URL, default http://localhost:8000 | build-time public API origin |
| frontend/lib/api.ts:90 | POST /analyze/{job_id}/run, JSON {url} | AnalysisResult; validates job/report IDs, outcome and warning array |
| frontend/lib/api.ts:103 | GET /reports/{report_id} | ReportV2 schema_version 2.1, submitted, outcome, limitations |
| frontend/lib/api.ts:122 | GET /media/{media_id} when playback_url is absent | image/jpeg or video/mp4 bytes, Range supported |
| frontend/app/page.tsx:18 | startAnalysis then getReport :20 | loading -> saved result/report or safe error |
| frontend/components/ReportPage.tsx:13 | getReport | persisted report view with loading/error states |
| frontend/app/reports/[report_id]/page.tsx:5 | report view route | consumes report_id path parameter |
| frontend/app/results/page.tsx:5 | optional report_id query route | same persisted report component |

The UI shows separate confidence/coverage, transcripts, post dates/metadata,
findings/evidence/timestamps, match passages, context and limitations. It links
the persisted report URL and handles no-match and unavailable fields. It does
not call Python display_report; its component renders the public ReportV2. TS
types intentionally project only displayed fields rather than claiming to define
the complete backend model. Backend CORS permits the configured REPORT_BASE_URL.

## PROPOSED INTEGRATION ORDER

The callable/route/store/frontend wiring is now connected. The smallest remaining
sequence is to supply a funded Gemini project key, verify transcription/OCR on
one approved short recording, ingest an accessible full source into the existing
Tiger corpus, submit its real excerpt and inspect alignment/evidence/context,
configure deployed frontend/API origins and then test one allowlisted Photon
URL-to-report conversation. Resolve YouTube access with an accessible authorized
example rather than substituting fabricated data. Only after those live gates
pass should the team call the entire MVP demonstrated end-to-end; current unit,
database and browser checks establish the wiring but cannot replace that proof.
