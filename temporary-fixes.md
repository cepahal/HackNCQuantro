# Temporary fixes and current integration handoff

Updated 2026-10-10. Repository: `cepahal/HackNCQuantro`. This note distinguishes temporary demo infrastructure from tested code fixes. It contains no credentials; real configuration stays in ignored environment files.

## Current priority: Instagram source and assessment correctness

**Owner: Codex. Please do not replay the original job concurrently.**

The Instagram request failed in about eight seconds during `retrieve_post`. Tiger recorded `failed / PROCESSING_FAILED / retrieve_post`, with no post or report. Photon incorrectly discarded the structured HTTP error and called the result "uncertain."

Controlled reproduction of the same Reel confirmed that the old backend process could not find either FFmpeg or ffprobe. Its exact error was `ffmpeg is required to merge downloaded media.` After making the installed tools visible, the Reel downloaded successfully: real MP4, 1,565,234 bytes, 22.27 seconds, 720 x 720, 30 fps, audio present; independent ffprobe exited successfully.

One intermediate retry returned a media-inspection error. A subsequent download and independent inspection succeeded; the intermediate failure has not been reproduced or explained. Validation was not weakened.

The runtime recovery completed in 234.479 seconds: HTTP 200, one completed Tiger job, one post, one report, and a Photon-accepted reply. However, browser review found that the selected source was the submitted Reel itself with a different tracking query, and the comparison treated missing visual evidence as a contradiction. That result is not accepted as a valid source verification. Its public sharing was withdrawn; the original report and delivery ledger were preserved, with a private before-image.

Implemented corrections: compare platform post identity before retrieval and after URL resolution; reject cached self-matches; constrain transcript-only caption assessment so unavailable gestures/audience behavior cannot count as contradicted evidence. Focused regressions pass. A fresh real Instagram job is running without a conversation ID, so no new message is sent before review.

## Changes implemented

| Change | Kind | Behavior and limits |
|---|---|---|
| Media runtime preflight | Code fix | `backend/services/shared/runtime.py` requires FFmpeg and ffprobe before API startup connects to Tiger. Optional private `MEDIA_TOOLS_DIR` makes an installed tools directory explicit, avoiding stale Windows PATH inheritance. It does not install tools. |
| Photon failure classification | Code fix | Validated structured API failures persist as `failed`, including safe code/stage, HTTP status, retryability, and timestamps. Raw provider/error text is not forwarded. The old uncertain message is reserved for genuinely unverified completion. |
| Lost-response reconciliation | Code fix | Photon checks the existing report and job with GET requests. A saved report confirms completion; a failed Tiger job confirms failure. It never automatically replays an ambiguous analysis POST. Existing markers continue to suppress duplicate processing. |
| Managed Photon startup readiness | Code fix | iMessage mode verifies the required local backend routes before connecting Spectrum. Terminal mode remains independent. Start the healthy backend first. |
| Bounded public-search concurrency | Code fix | Up to three independent HTTP queries overlap. Results merge in original query order; ranking, provenance, and failure semantics are retained. This does not parallelize Whisper or change confidence. |
| Gemini evidence generation | Code fix | The per-request response schema limits evidence quotes to real transcript sentences. Local citation/timestamp validation remains mandatory; invalid findings are never saved as success. No confidence is inflated. |
| Same-post source exclusion | Code fix | Instagram/YouTube aliases and canonical TikTok video identities are compared independently of tracking queries. Search aliases are excluded before download; provider-resolved post identities are checked before extraction/alignment. Cached self-match reports fail clearly without changing stored evidence or sending a summary. Strict job-URL ownership comparison remains unchanged. |
| Caption evidence scope | Code fix with a model limitation | Gemini must classify the basis privately as direct speech, non-mention, unobserved modality, or insufficient evidence. Missing/invalid/non-direct bases with a caption yield `not_assessed`, empty findings, and a safe summary. Public types and citation validation are unchanged. This deliberately also withholds context findings for a caption that cannot be assessed. The category is model-generated; it is not universal semantic verification. |
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
| Photon live exchange | User confirmed the acknowledgment and initial report message arrived. The initial localhost link was not phone-accessible. A temporary public page later rendered both metadata cards, three confidence blocks, findings, and media in Edge. Latest phone-link rendering and the under-60-second target remain acceptance gates. |
| Focused Python regressions | 93 search/comparison/pipeline tests passed. Separately, 33 runtime/ingest tests passed; Ruff and diff checks passed for the new runtime work. |
| Instagram correctness regressions | 42 pipeline tests passed; 88 specialist tests passed and one skipped. These cover aliases, resolved provenance, cached self-match rejection, actual gesture/applause evidence, missing classifications, and preservation of genuine spoken contradictions. |
| Photon regressions | 73 tests passed; TypeScript typecheck passed. |
| Frontend regressions | 18 tests passed; typecheck and production build passed. Local no-match rendering and local CORS preflight were checked with sharing enabled. |

These focused checks do not claim the full repository test suite was rerun after every latest change.

## What remains unaddressed and parallel ownership

| Owner / area | Remaining work | Coordination boundary |
|---|---|---|
| Codex: Instagram correctness | Code guards and focused regressions are complete; finish the one fresh real regression. The original technically completed report was withdrawn from public sharing because review found these defects. Investigate any reproducible media-inspection failure. | Preserve original Tiger rows and accepted delivery ledgers. Do not replay the original job or publish an unreviewed result. |
| Jason / media specialist | Improve and evaluate ASR fidelity on representative clips; the current base Whisper model misheard "trunks." Preserve actual audio-derived output and transcript limitations. | Do not hand-edit transcripts to match expected words. |
| Frontend / deployment owner | Replace the temporary tunnel with stable hosting, coordinate backend reachability, and check the report from a real phone. The demo gateway does not expose public analysis submissions or the homepage. | Avoid changing the active gateway, API-base configuration, or report origin without coordinating restart and link delivery. |
| Photon owner | Measure a new live acknowledgment and report-delivery run, confirm mobile rendering, and address the 60-second budget. Durable recovery of saved-but-unstarted queue items after a process restart remains unimplemented. | Preserve accepted/ambiguous delivery ledgers; do not blindly resend or delete processing markers. |
| Integrator / QA | Finish the full agreed pre-demo checklist and test suite. Exercise a spoken real no-match case, a genuinely distinct verified source, transient provider failures, and restart behavior. | Test 14 now verifies the real no-match route; its matched-only checks still need a genuine matched example. Existing Test 11 is a valid silent/no-search-evidence case, not a universal search proof. |
| Platform scope | TikTok returned an IP-blocked `MEDIA_UNAVAILABLE` error. One Instagram Reel and the latest failed Reel have downloaded successfully, but broad Instagram reliability is not established. | Do not advertise TikTok support. Keep the conservative pitch scope "YouTube + direct video URLs" until broader platform tests pass. |

## Current work order

1. Publish these tested fixes and this handoff so teammates can work on the separate items above.
2. Keep the reviewed bad result unshared; its source-identity and evidence-scope fixes now pass focused tests.
3. Verify one fresh real Instagram regression with source identity checked and unsupported assessments suppressed.
4. Publish the exact regression results and coordinate the remaining deployment, latency, and QA gates.

External providers and inaccessible media can still fail. The fixes aim to prevent missing local prerequisites and incorrect failure classification; they do not promise arbitrary URLs always produce a verified source or a report within 60 seconds.
