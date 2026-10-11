# Groq resolver integration

The public analyze, ingest and Photon webhook paths use `backend/services/resolution/run.py`. Existing ReportV2 2.1, Tiger tables, report URLs and Photon transport remain compatible. The supplied prototype handles caption/audio-based source discovery and assessment. The active resolver follows the supplied Groq, title-and-quote search, full-transcript and RapidFuzz method. It does not invoke the legacy Gemini window matcher.

## Active flow

1. Validate the URL and atomically reserve a Tiger job. Completed jobs return their existing report; other existing IDs cannot be overwritten.
2. Attempt the submitted clip download, then read cached/native YouTube captions. If YouTube blocks the server, the configured SerpAPI transcript engine can provide the complete available caption text. If captions are absent, use the downloaded clip or fetch audio and transcribe with Groq `whisper-large-v3-turbo`. FFmpeg converts video to mono 16 kHz MP3 and chunks long audio at 20 minutes. Instagram and TikTok require a successful media download. Downloaded media is saved through the existing Tiger media store.
3. Use Groq `openai/gpt-oss-120b` to propose a speaker/event query and a verbatim phrase. Search that query and the cleaned submitted title, interleave and deduplicate, then inspect at most three recordings of at least 180 seconds. An optional `YOUTUBE_API_KEY` uses the official Data API; otherwise yt-dlp search needs no additional key. Failed or empty direct searches use the configured SerpAPI YouTube engine, retaining the same two queries and ranking.
4. Compare real timed candidate transcripts using RapidFuzz partial-ratio matching. Scores below 75, generic/tiny phrases, repeated ambiguous passages and missing time bounds cannot verify a match. The score is a heuristic, not measured accuracy or factual reliability.
5. Locate the matched passage using actual caption/ASR segments, retain up to 60 seconds of surrounding speech on either side, and reject repeated short excerpts that supply no surrounding context.
6. Use Groq to explain the clip and compare the submitted caption with source context. Every quotation must occur within one supplied sentence. One bounded repair is allowed after invalid schema/evidence; validation is never relaxed. Incomplete coverage cannot establish that no material context is missing. Search/provider failure remains an error rather than being silently reported as no match.
7. Persist the sanitized report in Tiger, complete the job, then deliver through the existing Photon bridge if a conversation was supplied. A delivery failure does not delete a saved report.

The caption and search fallbacks feed the original full-transcript matcher rather than a different analysis pipeline. Provider cue starts delimit caption windows when actual cue ends are absent. These are explicitly labeled as cue windows, not word-level audio endpoints. A final cue without an actual end or known video duration remains available for discovery, but cannot manufacture a timed sentence. Unknown recording duration stays null. Exhausted caption and audio access remains a structured retrieval error; no provider can guarantee access to private, deleted or blocked recordings.

## Configuration and deployment

Set `GROQ_API_KEY` privately on the backend and `GROQ_LLM_MODEL=openai/gpt-oss-120b`. Use `SEARCH_PROVIDER=youtube` for the supplied discovery method, or retain the existing valid provider setting. Pair `SEARCH_FALLBACK_PROVIDER=serpapi` with the private `SEARCH_FALLBACK_API_KEY` for hosted caption and YouTube-search recovery. Gemini is not called by this resolver. `RESOLUTION_DOWNLOAD_VIDEO=false` explicitly selects captions-only submitted retrieval; the default attempts the clip download. Keep the existing Tiger URLs, CA, table prefix, Photon credentials, `REPORT_BASE_URL` and frontend origin. The frontend needs only its existing public backend URL. Never put provider keys in frontend variables or tracked environment files.

The results page is currently in diagnostic mode: the best matched recording or explicitly unverified top lead appears beside a terminal panel. The previous result panels remain in a hidden container in `ReportPage.tsx`, with their components intact. Landing and navigation are unchanged. Remove that wrapper's `hidden` attribute and `legacyPanels` class to restore those panels.

## Diagnostic output

The terminal includes the complete public ReportV2 and DisplayModel JSON without summary truncation, plus newly captured resolver events. Copy and download export the same full text. Transcript text, metadata, candidate queries/scores, selection or rejection reasons, alignment, context, comparison, warnings and measured stage timings are included when recorded. Unknown values remain null. Failed jobs can expose their captured events without requiring a completed report.

`GET /reports/{report_id}/diagnostics` returns `job_id`, `capture_status`, `events` and `terminal_output`. Capture is per job, persisted in the additive Tiger `{prefix}analysis_diagnostics` table, and never changes ReportV2 2.1. Existing `ensure_schema()` creates the table. Diagnostic storage failures must not fail the analysis. The report gateway permits this suffix only under its existing report allowlist.

This is structured output captured from the integrated resolver, not intercepted global server stdout. Historical reports have no retroactive query/score/timing capture and explicitly return `capture_status="unavailable"`; their saved report data is still displayed. Public diagnostics exclude credentials, local file paths, raw provider exceptions and other jobs' logs. Original CLI `FILE` is represented by a downloaded boolean and media identifier instead of a filesystem path.

The QR signup is still served by the backend. Fresh provider registration checks and no-store responses prevent cached enrollment status. Durable reconciliation markers prevent a removed/restored user or changed line from reviving an old welcome confirmation. New iMessage summaries use three ordered messages: reliability TL;DR, short explanation, report link. Accepted-part receipts prevent automatic replay after a partial or ambiguous send; `accepted` does not prove handset delivery.

Deploy the tested main commit to Render manually, then deploy that same main revision to Vercel. Existing reports keep their saved analysis; submit a new job to exercise the new resolver. Restore a prior Render deployment and Vercel deployment for rollback; no destructive schema migration is needed.

## Verification and limits

Local live verification retrieved the Obama short `3XPuGqqWLtk`, discovered the full Morehouse commencement address `e50Tt9qJRQk`, localized speech at 1102.633 to 1133.767 seconds and saved a completed Tiger report in 22.9 seconds. The observed lexical score was 96.3, not a claim of 96.3% accuracy. The subsequent summary guard explicitly qualifies incomplete coverage.

A blocked-download regression used live Groq and SerpAPI with direct YouTube access deliberately rejected, and persisted `report_job_handover_full_1791704290` in Tiger: `matched`, source `e50Tt9qJRQk`, enclosing caption window 1098 to 1131 seconds, 37.2 seconds. These cue-window endpoints are coarser than the native-caption baseline above. Cold hosted-caption requests hit the former 12-second limit, so the bounded caption timeout was increased to 30 seconds.

Focused tests cover timed matching, negative and ambiguous passages, malformed citations, one-shot repair, partial coverage, source retrieval failures, search errors, no-match reports, idempotency, sanitization, metadata nulls, Tiger persistence and delivery failures. Existing modular pipeline tests remain explicitly scoped to their legacy implementation, alongside the new active-resolver tests.

Public platform retrieval can still be blocked, region-restricted, unavailable or require authentication. No universal retrieval guarantee or 94% accuracy claim is made. The inherited network guard remains enabled; its prohibition on yt-dlp runtime subprocesses can limit bot-challenge handling. Candidate audio fallback is bounded to 90 minutes, chunked within Groq upload limits and slower than captions. Visual edits, deepfakes, speaker identity, earliest-upload provenance and factual truth are not assessed. Actual QR enrollment and all three iMessages arriving on a handset require a live user test after deployment.
