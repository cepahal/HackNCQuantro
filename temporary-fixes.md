# Temporary fixes and current integration handoff

Updated 2026-10-10. Repository: `cepahal/HackNCQuantro`. This note distinguishes temporary demo infrastructure from tested code fixes. It contains no credentials; real configuration stays in ignored environment files.

## Current priority: Instagram source and assessment correctness

**Owner: Codex. Please do not replay the original job concurrently.**

The Instagram request failed in about eight seconds during `retrieve_post`. Tiger recorded `failed / PROCESSING_FAILED / retrieve_post`, with no post or report. Photon incorrectly discarded the structured HTTP error and called the result "uncertain."

Controlled reproduction of the same Reel confirmed that the old backend process could not find either FFmpeg or ffprobe. Its exact error was `ffmpeg is required to merge downloaded media.` After making the installed tools visible, the Reel downloaded successfully: real MP4, 1,565,234 bytes, 22.27 seconds, 720 x 720, 30 fps, audio present; independent ffprobe exited successfully.

One intermediate retry returned a media-inspection error. A subsequent download and independent inspection succeeded; the intermediate failure has not been reproduced or explained. Validation was not weakened.

The runtime recovery completed in 234.479 seconds: HTTP 200, one completed Tiger job, one post, one report, and a Photon-accepted reply. However, browser review found that the selected source was the submitted Reel itself with a different tracking query, and the comparison treated missing visual evidence as a contradiction. That result is not accepted as a valid source verification. Its public sharing was withdrawn; the original report and delivery ledger were preserved, with a private before-image.

Implemented corrections: compare platform post identity before retrieval and after URL resolution; reject self-matches at creation, cached reuse, serving, display, and delivery; constrain transcript-only caption assessment with a separate positive grounding check. Unavailable gestures/audience behavior cannot be justified merely by transcript non-mention. Offline regressions and the real regressions below pass.

**Fresh Instagram end-to-end regression:** HTTP 200 in 181.458 seconds, `no_verified_match`, complete submitted transcript, report/display GETs 200, one completed Tiger job, one post, one report, and no sanitization leaks. The submitted Reel was explicitly excluded as its own source. Source and comparison are null, so no unsupported verdict is published. No conversation ID was supplied and no message was sent.

**Real Gemini comparison regression:** reused the original stored features, source context, and caption without modifying Tiger. One invocation, with its normal bounded two generation attempts, completed in 5.36 seconds. Both actual provider responses classified the claims as requiring an unobserved modality. The canonical result is `not_assessed` for context, caption, and confidence, with empty findings/basis. Private generation metadata is absent from the public result.

**Adversarial live grounding regression:** replayed the actual old incorrect primary assessment and deliberately set its private basis to `direct_transcript`. The separate checker used one live Gemini request. The final canonical result was `not_assessed` for context, caption, and confidence, with zero findings, in 4.73 seconds. No report was written or message sent. This tests the exact erroneous primary response against the new checker; it is not a new complete retrieval/analysis run. Both checks use Gemini, so correlated semantic errors remain possible.

The corrected report is available locally. Publishing this replacement through the external demo gateway is pending explicit approval; automatic approval review rejected expanding the public allowlist for this payload. The original inaccurate report remains unshared. Accepted delivery ledgers and original Tiger evidence remain intact.

## Changes implemented

| Change | Kind | Behavior and limits |
|---|---|---|
| Media runtime preflight | Code fix | `backend/services/shared/runtime.py` requires working FFmpeg and ffprobe before API startup connects to Tiger. Both must return the expected version banner within five seconds; absent, corrupt, wrong, or hung executables fail startup. Optional private `MEDIA_TOOLS_DIR` makes an installed tools directory explicit, avoiding stale Windows PATH inheritance. It does not install tools. |
| Photon failure classification | Code fix | Validated structured API failures persist as `failed`, including safe code/stage, HTTP status, retryability, and timestamps. Raw provider/error text is not forwarded. The old uncertain message is reserved for genuinely unverified completion. |
| Lost-response reconciliation | Code fix | Photon checks the existing report and job with GET requests. A saved report confirms completion; a failed Tiger job confirms failure. It never automatically replays an ambiguous analysis POST. Existing markers continue to suppress duplicate processing. |
| Durable restart recovery | Code fix with bounded/manual backlog handling | Managed startup reconciles existing markers with GET-only lookups, without delaying acknowledgments, replaying POSTs, or sending notices. Only an explicit cloud-only `--resume-saved` flag can resume validated native marker-free requests after two authoritative absence checks and original-conversation resolution; an exclusive durable marker precedes each POST. Scans stop at 256 entries / 32 jobs / 60 seconds; large histories can exhaust the scan before reaching the inbox and require manual review. Startup exposes truncation. |
| Automated regression gate | Code fix | Push/PR CI uses an isolated SQLite database and no provider credentials, runs the offline backend suite, dependency/startup checks, Photon/frontend tests and typechecks, and a frontend build. Cloud runner results must be checked separately; SQLite is not proof of live Tiger semantics. |
| Managed Photon startup readiness | Code fix | iMessage mode verifies the required local backend routes before connecting Spectrum. Terminal mode remains independent. Start the healthy backend first. |
| Bounded public-search concurrency | Code fix | Up to three independent HTTP queries overlap. Results merge in original query order; ranking, provenance, and failure semantics are retained. This does not parallelize Whisper or change confidence. |
| Gemini evidence generation | Code fix | The per-request response schema limits evidence quotes to real transcript sentences. Local citation/timestamp validation remains mandatory; invalid findings are never saved as success. No confidence is inflated. |
| Same-post source exclusion | Code fix | Instagram/YouTube aliases and canonical TikTok video identities are compared independently of tracking queries. Search aliases are excluded before download; provider-resolved post identities are checked before extraction/alignment. Self-match reports fail clearly during direct construction, cached reuse, report/display GETs, direct display, and the first-save-wins reread before delivery. Stored evidence is not rewritten and no summary is sent. Strict job-URL ownership comparison remains unchanged. |
| Caption evidence scope | Code fix with a model limitation | Gemini must classify the basis privately as direct speech, non-mention, unobserved modality, or insufficient evidence. Missing/invalid/non-direct bases with a caption yield `not_assessed`, empty findings, and a safe summary. Public types and citation validation are unchanged. This deliberately also withholds context findings for a caption that cannot be assessed. A separate unanchored Gemini check must agree with definitive caption verdicts and cite actual complete source sentences. Disagreement, missing/malformed/cross-media citations, or secondary provider failure yields canonical not assessed. The secondary request has no automatic retries. Both interpretations use the same model, so correlated semantic errors remain possible; valid citations establish provenance, not logical entailment. |
| Read-only report gateway | Temporary demo infrastructure | `frontend/scripts/report-gateway.mjs` binds to loopback port 3100. It exposes only explicitly shared report pages, sanitized report/display JSON, associated media, and fixed static assets. Mutations, ingest, analysis, raw job inspection, and unlisted reports are denied. |
| Public sharing before report delivery | Code fix supporting the gateway | Optional `PHOTON_PUBLIC_REPORTS_FILE` atomically publishes the report ID before its Photon reply. The ignored JSON allowlist is bounded to 32 IDs and 16,384 bytes, fails closed, and does not contain credentials. |
| Temporary HTTPS tunnel | Temporary workaround | Cloudflare Quick Tunnel uses verified QUIC; HTTP/2 failed here. The Windows custom CA bundle retains both existing Windows trusted roots and the release's three embedded Cloudflare roots. Certificate verification stays enabled. Stopping the tunnel breaks its links; a new tunnel has a new origin. |
| Local versus public report reads | Code fix | `NEXT_PUBLIC_API_BASE_URL` retains the analysis API. Optional `NEXT_PUBLIC_REPORT_API_BASE_URL=/api` supports same-origin public report reads. Loopback browser pages continue to read the local API, including web-created reports that are not publicly shared. |
| Separate CORS origin | Code fix | Optional `FRONTEND_ORIGIN` keeps local browser submissions working when `REPORT_BASE_URL` points at the public report tunnel. Unset configuration retains the previous fallback. |

Operator setup and rollback: [docs/PHOTON_PUBLIC_DEMO.md](docs/PHOTON_PUBLIC_DEMO.md) and [docs/RUNBOOK.md](docs/RUNBOOK.md). Do not publish environment values, CA/private configuration files, delivery ledgers, or downloaded media in Git.

## Verification already completed

| Check | Verified result |
|---|---|
| Test 7: invalid URL | HTTP 400 `INVALID_URL`; one failed job; zero post rows. |
| Test 8: unavailable YouTube media | HTTP 404 `MEDIA_UNAVAILABLE`; one failed job; zero post rows. |
| Test 9: idempotency | Two HTTP 200 responses with the same report ID; exactly one Tiger report row, min ID equals max ID. An earlier fresh run failed Gemini quote validation; after the schema fix a fresh run completed in about 185 seconds. |
| Test 10: served-report sanitization | Actual report JSON contained none of `/tmp/`, `local_path`, `api_key`, `secret`, or `file://`. |
| Test 11: no-match | A fresh real silent flower video completed with `no_verified_match`; submitted post/transcript present, source and alignment null. Later reruns reused the saved report. This does not prove public search returns zero candidates for a spoken obscure video. |
| Test 12: null versus zero | Unknown author counts serialized as null. The YouTube author's known follower count was 6,680,000; following/posts remained null. Source and no-match unknown counts were null. |
| Test 13: real extraction | Fresh real YouTube download, actual Whisper transcript, and three existing frame files; no contract-example transcript. Exact-word fidelity remains imperfect: Whisper transcribed "trunks" as "punks." |
| Test 14: website flow structure | Rerun completed HTTP 200 in 35.127 seconds with `no_verified_match`; report/display GETs returned 200, one post/report persisted, no leaks. Six of the eight literal checks passed; the two matched-only confidence checks fail on the required null alignment/comparison in a no-match report. Null-aware no-match handling passed. This is not a matched-source rendering proof. |
| Corrected Instagram local browser | Edge rendered the real transcript, no-source state, unknown metadata as Unavailable, and all three confidence blocks as Not assessed. Automated screenshot capture timed out; a screenshot artifact is still missing. |
| Photon live exchange | User confirmed the acknowledgment and initial report message arrived. The initial localhost link was not phone-accessible. A temporary public page later rendered both metadata cards, three confidence blocks, findings, and media in Edge. Latest phone-link rendering and the under-60-second target remain acceptance gates. |
| Focused Python regressions | 93 search/comparison/pipeline tests passed. Separately, 33 runtime/ingest tests passed; Ruff and diff checks passed for the new runtime work. |
| Instagram correctness regressions | 42 pipeline tests passed; 88 specialist tests passed and one skipped. These cover aliases, resolved provenance, cached self-match rejection, actual gesture/applause evidence, missing classifications, and preservation of genuine spoken contradictions. Fresh real Instagram analysis passed with honest no-match in 181.458 seconds. A separate real Gemini comparison of the original failure returned not assessed in 5.36 seconds. |
| Photon regressions | 73 tests passed; TypeScript typecheck passed. |
| Frontend regressions | 18 tests passed; typecheck and production build passed. Local no-match rendering and local CORS preflight were checked with sharing enabled. |

Latest full offline backend suite: **751 passed, 5 skipped, 45 subtests passed in 20.02 seconds**. Four skips are opt-in network tests; one requires Windows file-symlink permission. It used isolated temporary SQLite, with NETWORK unset, and did not exercise live Tiger/provider calls. Dependency `pip check`, affected Python Ruff checks, YAML structure validation, and actual FFmpeg/ffprobe execution passed. Frontend: 18 tests and typecheck passed. Photon: **123 passed, 1 Windows file-symlink permission skip**, TypeScript typecheck passed. Managed listener restarted successfully in GET-only recovery mode: four entries scanned, one reconciled, zero resumed, zero unresolved, three skipped, no limit reached; existing invalid report rejection is recorded as a safe failed marker, preserving the completed Tiger job.

## What remains unaddressed and parallel ownership

| Owner / area | Remaining work | Coordination boundary |
|---|---|---|
| Codex: Instagram correctness | Code guards, full offline backend tests, real no-match analysis, and the adversarial live caption-grounding regression are complete. Corrected external sharing awaits explicit approval. A transient intermediate media-inspection failure remains unexplained and was not reproduced. | Preserve original Tiger rows and accepted delivery ledgers. The original invalid report remains revoked; use a fresh job for new analyses. Do not blindly republish it. |
| Jason / media specialist | Improve and evaluate ASR fidelity on representative clips; the current base Whisper model misheard "trunks." Preserve actual audio-derived output and transcript limitations. | Do not hand-edit transcripts to match expected words. |
| Frontend / deployment owner | Replace the temporary tunnel with stable hosting, coordinate backend reachability, and check the report from a real phone. The demo gateway does not expose public analysis submissions or the homepage. | Avoid changing the active gateway, API-base configuration, or report origin without coordinating restart and link delivery. |
| Photon owner | Measure a new live acknowledgment and report-delivery run, confirm mobile rendering, and address the 60-second budget. Bounded restart reconciliation and explicit never-started intake recovery are implemented and tested. Large-backlog pagination remains unimplemented; truncated scans require manual review. | Preserve accepted/ambiguous delivery ledgers; do not blindly resend or delete processing markers. |
| Integrator / QA | Full offline backend suite is green. Finish the live pre-demo acceptance checks and missing final UI screenshot. Exercise a spoken real no-match case, a genuinely distinct verified source, live provider disruption and a genuinely distinct matched-source result. Offline failure/restart cases are covered. | Test 14 now verifies the real no-match route; its matched-only checks still need a genuine matched example. Existing Test 11 is a valid silent/no-search-evidence case, not a universal search proof. |
| Platform scope | TikTok returned an IP-blocked `MEDIA_UNAVAILABLE` error. One Instagram Reel and the latest failed Reel have downloaded successfully, but broad Instagram reliability is not established. | Do not advertise TikTok support. Keep the conservative pitch scope "YouTube + direct video URLs" until broader platform tests pass. |

## Current work order

1. Publish the tested reliability changes and this updated handoff; verify the new CI run.
2. Keep the reviewed bad result unshared; its source-identity and evidence-scope fixes now pass focused tests.
3. Completed: fresh real Instagram no-match regression, adversarial live Gemini grounding regression, and full offline backend verification. Photon recovery is being checked on the actual local listener in GET-only mode.
4. Coordinate stable deployment, the 60-second budget, mobile acceptance, ASR fidelity, and the remaining QA gates. Replacement public sharing awaits explicit approval.

External providers and inaccessible media can still fail. The fixes aim to prevent missing local prerequisites and incorrect failure classification; they do not promise arbitrary URLs always produce a verified source or a report within 60 seconds.


## Backend audit reconciliation and deployment gate — 2026-10-10

Merged teammate main855e0f6 with our tested reliability branch5d002b9. Kept the
canonical Exa/Whisper adapters, word timestamps, artifact cleanup, frontend UI,
status polling, and conservative context findings; preserved our source-identity,
caption-grounding, public-report and durable Spectrum recovery checks. Removed
redundant frontend contract/display copies instead of introducing parallel models.

The supplied16-gap audit was reconciled as follows:

| Gap | Implemented change / current limit |
| --- | --- |
| Search configuration | Private local provider is Exa; key remains ignored. Startup validates the selected provider before analysis. Hosted configuration still needs deployment. |
| Ingest overwrite | Atomic ID reservation; every existing ID returns409 with unchanged stored URL/post/features. |
| Media memory/latency | Shared bounded TLS pool, metadata-only query, PostgreSQL substring reads<=1MiB, response-owned cleanup. No full-blob fetch for tiny ranges. |
| Conflicting dotenv loaders | One cached explicit-path loader, no override/interpolation; process settings win. |
| Missing exception handlers | Canonical validation/HTTP/unexpected handlers and safe type/stage/code diagnostics. |
| Mixed error envelopes | Four keys on error paths, including media and webhook errors; no body echo. |
| Analysis ID conflicts | Different URL on any existing job returns409 rather than500. |
| Lost retry information | Safe message/retryable persisted in additive job columns and returned by status. |
| Missing server diagnostics | Original exception type, normalized code and stage logged; private provider details omitted. |
| Unbounded/extra JSON |32KiB analyze/ingest,64KiB webhook, actual streaming byte count, depth32, extra fields forbidden. |
| Accidental test DB writes | Real database smoke requires explicit QUANTRO_SMOKE=1; ordinary suite uses isolated SQLite. |
| Stuck running jobs | Atomic stale-job expiry on status reads; stage heartbeats prevent expiring recently active jobs. |
| Rejected URL writes | Cheap public URL validation before DB reservation/write; invalid input now leaves no row. |
| Transcription default mismatch | Whisper is the consistent default, Gemini remains an explicit supported choice. |
| Media HTTP/CORS | HEAD, conditional304, single-range206/416, ignored unsupported range forms, exposed playback headers, local host aliases. |
| Webhook hardening/leftovers | Documented separate signing secret+signed timestamp; durable Tiger claims across restarts; dead direct_image comparison removed; browser configuration errors redacted. Archived HTTP transport remains unverified for outbound API; Spectrum remains primary. |

Final frozen-code checks after the teammate retrieval merge:899 backend tests passed,7 intentionally skipped,
45subtests passed; mypy43source files clean with third-party missing stubs excluded;
RuffF/E9 and generated frontend type check passed; pipcheck clean. Frontend40tests,
typecheck/lint/production build passed. Spectrum123tests passed and one intentional
Windows symlink test skipped; typecheck passed. Offline tests do not prove provider
availability or hosted behavior. Logs remain ignored under.data/.

Actual read-only local/Tiger checks: backend startup and required OpenAPI routes
passed; corrected stored reportGET200/schema2.1/no leaks; original invalid self-source
report still blocked with PROCESSING_FAILED. Three2byte media range requests returned
206 with exactly2bytes; HEAD200empty and If-None-Match304. Measured local request times
were1.734s,0.891s,1.672s: network latency remains measurable; no universal speed claim.

Vercel's published build had no backend URL and fell back to each visitor's
localhost:8000. Source now reports missing production configuration explicitly and
pins Node24.x. A real backend HTTPS origin, matching CORS, private hosting settings,
and Vercel rebuild remain required. The guessed quantro-api.onrender.com is an
unrelated trading app and must not receive our credentials or be configured.

No paid hosting was authorized or purchased. User declined paid Render and asked
for Railway/free-tier comparison; see docs/HOSTING.md. A small.en Whisper run near
980MiB plus media/API overhead is unsafe on512MiB free services and not proven on
Railway's1GiB service trial. File cap remains100,000,000bytes. Hosted memory, cloud
platform download access, container build and actual Vercel-to-host E2E remain gates.
The Docker CLI exists locally but its daemon is not running, so no image-build pass
is claimed. HTTP background tasks are not a durable queue; a crash after claiming
an event requires operator review. No arbitrary confidence uplift or unsupported
Instagram/TikTok success is claimed.


### Final teammate merge and Render handoff (2026-10-10)

Preserved teammate commit `2d496b1` for explicit separate YouTube audio/video
downloads and local MP4 remuxing, using one canonical split-download path.
Retained our typed contracts, actual FFprobe metadata, combined byte budget,
shared timeout, file-only FFmpeg inputs and artifact cleanup. Two former private
helper tests now exercise the canonical path and retain real FFmpeg verification.
Final isolated suite: 899 passed, 7 skipped, 45 subtests passed. Mypy (43 files),
Ruff, generated frontend types and dependency checks passed. The seven skips are
four opt-in network tests, one opt-in database smoke test, and two Windows
symlink-privilege tests.

The user selected paid Render. The blueprint targets one 1 CPU / 2 GB backend
instance, independently billed from the Pro workspace. Public deployment,
private Tiger TLS variables, the production backend URL and Vercel redeployment
remain operator acceptance gates; no hosted memory or cloud-IP download success
is implied by local tests. See `docs/HOSTING.md` for current configuration steps.


### Latest main reconciliation (2026-10-10)

Merged teammate backend commit `45ebd9e` and frontend changes through `fa73a3a`.
Preserved optional word-timestamp recovery, contextual search, known-size stream
selection, homepage design and cursor. Retained unknown-size stream fallback
behind the actual download byte cap; added two regression cases. Kept our atomic
job reservation, durable media streaming, typed errors, source identity checks,
caption grounding and durable Spectrum recovery; these are complementary fixes.

Backend verification after the backend merge: 911 passed, 7 skipped, 45 subtests.
Mypy (43 files), Ruff F/E9, generated frontend types, dependency and media runtime
checks passed. Latest frontend merge: 40 tests passed; fixed its unescaped JSX
apostrophe and restored the configured messaging destination in the homepage note.
Frontend lint, typecheck and production build all passed after the fix.

Render has a paid Pro workspace, but no backend service is deployed yet. The
prepared service uses one 1 CPU / 2 GB instance, private Tiger TLS configuration,
and Exa. Service deployment and the Vercel production URL/rebuild remain gates.
No credentials are committed.


### Performance push reconciliation (2026-10-10)

Merged teammate commits `22f469d` and `98079a8`: content/model-keyed transcript
cache, faster input seeking, stage timings, and transcript-only candidate analysis
with explicit visual-extraction limitations. Preserved bounded decoded audio and
source identity checks. Fixed candidate handling for both shared MediaFeatures
models and legacy dictionaries without mutating upstream results. Updated two
mocked audio fixtures for the content cache. Search now receives the canonical
Queries model, and provider exceptions remain retryable SEARCH_FAILED.

Full merged suite: 913 passed, 9 skipped, 45 subtests in 31.09 seconds; skips are
not claims of live coverage. Final affected pipeline checks: 78 passed. Mypy (22 affected source files),
Ruff and generated frontend contracts passed. With the configured FFmpeg path,
59 media/specialist checks passed and one Windows symlink test skipped. The two
extra full-suite skips were from the fresh shell lacking the configured tool path. Render secrets were explicitly approved and loaded privately;
the user also approved the separate $25/month backend compute deployment.


### Overlapping audit push and hosted deployment (2026-10-10)

Reconciled teammate `bececa9` without adding parallel settings or request guards:
`shared/settings.py` and `routes/request_guard.py` are compatibility re-exports.
Preserved atomic reservations, pooled response-owned media streaming, durable
message claims, explicit persisted retryability and atomic stale-job expiration.
Kept the teammate's timestamp parsing, HTTP Allow headers, URL-field error code,
Content-Length CORS exposure, Exa operator diagnostics and exception causes.
Added pre-ingest search configuration checks through the existing validation.
Their audit tests use the canonical run_ingest boundary, stricter 32 KiB limit,
explicit retryability and persisted expiration; offline webhook tests inject
in-memory trackers. Complete merged suite: 943 passed, 7 skipped, 45 subtests in
40.61 seconds, with real FFmpeg available. Ruff, mypy (24 affected source files),
and generated frontend types passed; focused audit tests: 100 passed.

Render first deployment is live at https://quantro-backend.onrender.com on
`bececa9`; private Tiger/provider settings and CA were explicitly approved.
The reconciled commit must be deployed next. Vercel backend URL/rebuild and a
hosted real analysis/report/playback test remain acceptance gates.


### Public Render/Vercel connection and submitted-caption correction (2026-10-10)

Both production services deployed reconciled source `9098a05`: Render at
https://quantro-backend.onrender.com and Vercel at https://quantro-ten.vercel.app.
Vercel's production API and display-model base URLs now point at Render;
Render's report/CORS origins point at Vercel. Tiger TLS and provider credentials
remain private. Vercel was deployed using the approved CLI because this project
is not connected to the team's GitHub repository. Render automatic deployment
is intentionally disabled while the tested revision is frozen.

Verified through the public website: submission, asynchronous status recovery,
saved report loading, three separate confidence blocks, null metadata display,
and actual video playback from Render. A real W3Schools MP4 completed in 74.9
seconds, persisted to Tiger, returned schema 2.1, contained no private path/key
markers, and reused the same report ID with exactly one report row on retry.
Media HEAD and byte-range requests returned 200 and 206 respectively. Two
additional browser submissions completed and loaded their saved reports.
These music-only samples produced no speech sentences and no verified source;
they do not establish fresh matched-source transcription quality.

Fixed the comparison input, with explicit user approval: assess the submitted
caption against recovered source context, while retaining the source caption
as metadata. Regression cases cover different captions, a null submitted
caption, and an empty submitted caption. No function signature or schema changed.

The homepage demo now links to `report_job_demo_caption_v2_20261010`, reassessed
by the live Gemini comparison service using the saved submitted caption,
transcript and context. The unrelated caption correctly remains not assessed.
The prior report was preserved unchanged; the refreshed report is a new job
with one immutable report row. Its limitations disclose that media/transcripts
were reused and no fresh cloud download occurred. Source confidence was not
inflated or changed.

Remaining gates: fresh YouTube retrieval on Render returns YouTube's
"Sign in to confirm you're not a bot" rejection; cloud source-candidate downloads
also failed. This needs an authorized retrieval provider/egress solution.
The Node Photon bridge is still separate from the Python Docker service, and
hosted iMessage delivery is not verified by these web tests. No capacity/load
test was performed. Release uses a manual Render deployment of the tested
commit and the approved Vercel CLI deployment; automatic deployments stay off.

Caption-fix verification: 945 backend tests passed, 7 explicitly skipped,
45 subtests passed; 104 focused pipeline/report/contract tests passed. All 40
frontend tests, frontend lint/typecheck, Ruff F/E9 and diff checks passed.
Render served the refreshed demo with HTTP 200 and schema 2.1; sanitization
passed, source confidence stayed moderate, and the original report was unchanged.


### Production release and approved UI reconciliation (2026-10-10)

Caption fix `b5e29c9` was published on the release branch and deployed successfully:
Render deployment `dep-db5dde2jnfac73a38gig` is live; Vercel production deployment
`dpl_BmBA6TTjmptQK2nMCCd5aqkChX1U` serves the refreshed demo. Browser verification
confirmed both videos ready without media errors, safe report JSON, the distinct
submitted/source captions and honest not-assessed context confidence.

After deployment the user explicitly lifted the freeze on newer teammate pushes.
Reconciled UI merge `e72d0af`: preserved accessibility, saved-report loading,
structured not-found errors, safe formatting, transcript panels, correct post-date
ordering, and the refreshed demo ID. Kept the already verified webpack build
command so this demo release does not also switch production bundlers. Backend
code from the tested caption release is unchanged by this UI reconciliation.

Reconciled frontend verification: 51 tests passed; lint, typecheck and webpack
production build passed. The backend tree matches the 945-test caption release.

### Bounded YouTube transcript fallback (2026-10-10)

Render's downloader rejected the public recording `TCm9788Tb5g` with YouTube's
bot-verification challenge. This occurred before transcription or source search.
Download failures now retain a private classification and expose fixed, safe
messages instead of discarding the cause or returning raw provider diagnostics.

After a specifically classified YouTube bot challenge, `run_analysis` uses
Gemini's native public YouTube input for a requested 0–60 second window. One
token preflight caps input at 12,000 tokens; one generation caps output at 4,096
tokens, with thinking disabled and no application retries. Returned speech,
numeric bounds, completion and audio/video usage are validated before saving.
Provider schema restrictions are handled separately from stricter local
validation. Unsupported, deleted, private, invalid and storage failures retain
their structured errors and do not trigger this path.

This is a **limited transcript-only report**, not a completed source/context
verification. It preserves report schema 2.1 and labels text/timestamps as
provider estimates. Source, alignment, context and comparison remain null;
duration, playback and unknown metadata remain null. No fake downloaded file,
media bytes, audio fingerprint or ASR cache row is created. The public outcome
is `no_verified_match`, with explicit limitations that source search and
alignment were not performed. The normal downloaded-media pipeline is intact.

`/ingest` keeps its actual-download contract. During the analysis fallback the
job stays running so polling cannot observe a premature terminal failure.
Reports persist through the existing Tiger report boundary, remain idempotent
for the same job ID, and retain the existing post-save messaging behavior.
There are no frontend or shared-schema changes and no new keys or dependencies.

Verification before publication: 364 focused retrieval/ingest/orchestration/API
and contract tests passed; the final fallback matrix separately passed 55 tests.
Ruff F/E9, diff checks and read-only frontend wire-type export checks passed.
The exact blocked public recording returned four validated speech segments via
the live helper in 2.97 seconds. This checks access and schema enforcement,
not independent transcript or timing accuracy. Hosted verification follows the
manual Render deployment of this backend revision.

Remaining at that release: independently validate transcript fidelity/timestamps; extend remote
evidence into verified source search/alignment without inventing recording
duration; validate capacity and other platform restrictions. Gemini access,
quota and intelligible speech are still required: upstream unavailability can
still produce a structured failure. This change does not guarantee every URL
can be processed and does not provide a deepfake/authenticity determination.

### Bounded source discovery and partial-speech interpretation (2026-10-10)

This extends the immediately preceding transcript-only fallback baseline; its
earlier measured results remain historical. The classified YouTube download
block now leads to a native partial 0–60-second transcript, immutable spoken
search clues, Tiger/Exa discovery, and an optional single lexical YouTube search
with the existing SerpAPI credential. Discovery excludes the submitted recording
and duplicate sources. It then examines at most two distinct native YouTube
candidate windows, each restricted to its first 0–60 seconds.

Accepted correspondence requires distinctive exact normalized speech uniquely
located in the observed source window. Source confidence is capped at moderate;
full-clip coverage remains null, edit analysis remains not assessed, and confirmed
edit counts remain null. No downloaded file, audio fingerprint, complete recording
duration or earliest-original claim is manufactured. Best-effort public oEmbed
metadata may supply title/uploader fields, never a fabricated caption or speaker.

A matched source permits one bounded, source-cited partial-context interpretation.
Actual supplied sentences/timestamps are expanded locally from validated citation
IDs. Findings explain observed speech; context assessment stays uncertain, caption
assessment is uncertain or not provided when absent, and assessment confidence
stays low. If that call fails, valid source correspondence is
retained. There is no physical-splicing, deepfake, external-fact or deceptive-intent
determination.

`SUBMITTED_INTERPRETATION_ENABLED=true` also permits one additional standalone
interpretation before saving, including when no source matches. It explicitly
conditions its reading on "Assuming this is political speech", bounds input and
output, quotes only short complete cited sentences, keeps implementation IDs
private and labels possible references as unverified hypotheses. It states that
factual accuracy and deceptive intent are not established. A failed provider,
invalid citation or invalid response preserves the preceding report with a safe
limitation; it cannot discard saved evidence. The current flag applies to matched
reports too, so this is an additional optional call, not a free replacement for
source-context interpretation. Completed-job retries reuse the saved report and
do not repeat the optional standalone call or change its persisted summary.

No new API, key or dependency is required. Optional lexical search is selected
with `SEARCH_FALLBACK_PROVIDER=serpapi`; `SEARCH_FALLBACK_API_KEY` uses the existing
private SerpAPI credential or falls back to `SEARCH_PROVIDER_API_KEY` when blank.
Primary Exa/Tiger discovery and the existing budget comparison-model setting
remain in use. Secrets stay outside tracked files. All reports still use the
existing sanitized, idempotent Tiger persistence and post-save delivery boundary;
there are no frontend or public report-schema changes in this work.

Verification reported for this extension: the initial focused backend run passed
371 tests with one skip; the standalone interpretation component passed 30 tests
and Ruff. A later local diagnostic using saved submitted speech took 11.37 seconds,
accepted candidate `pJvRUL81ZU8` with one speech match and returned one cited
finding. It did not establish hosted operation, fresh full-pipeline latency or
independent speech/timestamp accuracy. A newer final full-suite or deployment
pass is not claimed by this entry.

Remaining: verify the final integrated revision and hosted behavior; independently
check native transcript/timestamp fidelity; widen source coverage beyond first
minute windows; measure real labeled source/context accuracy and concurrent load.
Provider access/quota failures remain possible. A no-match means no source was
verified within this bounded search, not that a source does not exist. Factual
checking, intent attribution, deepfake analysis and confirmed physical-edit
detection remain unimplemented by this path.

Final pre-release verification: 445 focused backend tests passed, one skipped (23.32 seconds). Ruff F/E9, read-only frontend wire-type comparison, staged configured-secret scan and diff review passed. Final query/eligibility regression group separately passed 83 tests. Hosted verification is pending this release's manual Render deployment.

Hosted verification: Render deployment dep-db5gavtckfvc73a46bu0 is Live on backend d1a0310. Fresh job_remote_semantics_20261010_v2 passed all 11 checks in19.73seconds: analysis/report/display HTTP200, completed status, schema2.1, real partial submitted speech, distinct matched YouTube recording pJvRUL81ZU8, one cited context finding, conditional political-speech interpretation, unknown duration/coverage, identical saved report on retry, and sanitization. Edge confirmed the summary, both transcripts and finding render. Public report: https://quantro-ten.vercel.app/reports/report_job_remote_semantics_20261010_v2 . This is one live functional regression, not measured population accuracy or proof of earliest original. Frontend files were preserved.


## 2026-10-10 contextual-source and report readability update

Matching speech alone no longer qualifies a recording as the contextual source.
Both downloaded and transcript-only candidates must contain additional observed
speech outside aligned passages and within the context window. The conservative
eligibility check requires four words and two new informative words, preserves
negation, and excludes repeated speech and subscription outros. It does not
establish originality, factual accuracy, or a physical edit. Duplicate-only
results retain no verified source; another candidate is checked within the
existing two-candidate remote budget.

The optional lexical query now seeks fuller recordings using an exact spoken
quote. Unverified full-recording titles and duration hints prioritize inspection,
never supply verified duration or prove context. Context outside the inspected
windows can still be missed.

Reports show safe fixed-host YouTube embeds when downloaded playback is absent,
with an always-visible original link and a visible unavailable state. Analysis
uses Takeaways, What this means, Reliability, and cited Evidence, readable notes,
plain body text, and expandable long interpretations. Model speculation remains
unverified. The public schema and idempotent Tiger boundary are unchanged. Older
saved reports remain historical results; use a new job to apply the new gate.

Verified locally: 375 backend regression tests and 73 frontend tests passed.
Frontend TypeScript and scoped ESLint passed. Hosted rollout is a separate step;
these results do not establish population accuracy or recovery of an original
full-length recording.

## 2026-10-10 clip interpretation and source suggestions

Removed the forced political-speech assumption. The existing cheap interpretation
call now reads bounded title/caption/uploader clues alongside actual speech, with
explicit attribution and no verified identity or deception claims. No-match
reports still explain what the clip says or does. Frontend Takeaways show that
reading; source status belongs in Reliability.

Search retains exact words, uses metadata to locate the event, and preserves
short numeric phrases. The report displays up to five safe YouTube discovery
leads, including weaker ones, separately from verified source context. Discovery
metadata cannot satisfy the speech/context verification gate. The two-recording
native assessment budget and public report schema are unchanged.

Local verification: 446 backend tests, 81 frontend tests, ESLint, TypeScript and
production build passed. The hosted optional SerpAPI key was replaced privately
with the already-authorized working credential; no secret enters this commit.
Remaining: remote analysis inspects only the first 60 seconds of each candidate,
source discovery is incomplete, external platform access can fail, and text
matching does not verify authenticity or factual truth. No model was trained or
population accuracy measured by this change. Hosted smoke results follow after
manual deployment.
