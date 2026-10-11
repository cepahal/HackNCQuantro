# Groq resolver integration

The public analyze, ingest and Photon webhook paths use `backend/services/resolution/run.py`. Existing ReportV2 2.1, Tiger tables, report URLs and Photon transport remain compatible. The supplied prototype handles caption/audio-based source discovery and assessment. Analysis reuses the existing native YouTube reader and bounded source locator when a platform challenge blocks caption and audio retrieval.

## Active flow

1. Validate the URL and atomically reserve a Tiger job. Completed jobs return their existing report; other existing IDs cannot be overwritten.
2. Read timed YouTube captions first. When captions are absent, download audio and use Groq `whisper-large-v3-turbo`. Instagram and TikTok require a successful media download. Downloaded media is saved through the existing Tiger media store.
3. Use Groq `openai/gpt-oss-120b` to propose a speaker/event query and a verbatim phrase. Search that query and the cleaned submitted title, interleave and deduplicate, then inspect at most three recordings of at least 180 seconds. An optional `YOUTUBE_API_KEY` uses the official Data API; otherwise yt-dlp search needs no additional key.
4. Compare real timed candidate transcripts using RapidFuzz partial-ratio matching. Scores below 75, generic/tiny phrases, repeated ambiguous passages and missing time bounds cannot verify a match. The score is a heuristic, not measured accuracy or factual reliability.
5. Locate the matched passage using actual caption/ASR segments, retain up to 60 seconds of surrounding speech on either side, and reject repeated short excerpts that supply no surrounding context.
6. Use Groq to explain the clip and compare the submitted caption with source context. Every quotation must occur within one supplied sentence. One bounded repair is allowed after invalid schema/evidence; validation is never relaxed. Incomplete coverage cannot establish that no material context is missing. Search/provider failure remains an error rather than being silently reported as no match.
7. Persist the sanitized report in Tiger, complete the job, then deliver through the existing Photon bridge if a conversation was supplied. A delivery failure does not delete a saved report.

When submitted YouTube retrieval fails specifically because of a platform challenge, analysis requests a bounded native Gemini video window instead. The existing Exa/SerpAPI discovery and timed-caption locator then check possible source recordings, and Groq writes the explanation. This recovery retains partial-transcript provenance, estimated timing and unknown recording duration. It never invents a downloaded file or claims audio, visual or full-recording verification. Private/deleted videos, invalid URLs, missing credentials and unrelated provider failures do not enter this recovery path. If native access also fails, the job remains failed with its structured error.

## Configuration and deployment

Set `GROQ_API_KEY` privately on the backend and `GROQ_LLM_MODEL=openai/gpt-oss-120b`. Keep `SEARCH_PROVIDER=hybrid` with the existing Gemini, Exa and SerpAPI configuration so the native recovery can reuse the established search adapters. Keep the existing Tiger URLs, CA, table prefix, Photon credentials, `REPORT_BASE_URL` and frontend origin. The frontend needs only its existing public backend URL. Never put provider keys in frontend variables or tracked environment files.

The results-page redesign changes only the report components and their scoped styles. Landing, navigation, API types and frontend API wiring are preserved. Results distinguish a matching recording from verified context or factual truth; unknown assessments stay neutral rather than showing a red failure or green assurance.

The QR signup is still served by the backend. Fresh provider registration checks and no-store responses prevent cached enrollment status. Durable reconciliation markers prevent a removed/restored user or changed line from reviving an old welcome confirmation. New iMessage summaries use three ordered messages: reliability TL;DR, short explanation, report link. Accepted-part receipts prevent automatic replay after a partial or ambiguous send; `accepted` does not prove handset delivery.

Deploy the tested main commit to Render manually, then deploy that same main revision to Vercel. Existing reports keep their saved analysis; submit a new job to exercise the new resolver. Restore a prior Render deployment and Vercel deployment for rollback; no destructive schema migration is needed.

## Verification and limits

Local live verification retrieved the Obama short `3XPuGqqWLtk`, discovered the full Morehouse commencement address `e50Tt9qJRQk`, localized speech at 1102.633 to 1133.767 seconds and saved a completed Tiger report in 22.9 seconds. The observed lexical score was 96.3, not a claim of 96.3% accuracy. The subsequent summary guard explicitly qualifies incomplete coverage.

Focused tests cover timed matching, negative and ambiguous passages, malformed citations, one-shot repair, partial coverage, source retrieval failures, search errors, no-match reports, idempotency, sanitization, metadata nulls, Tiger persistence and delivery failures. Existing modular pipeline tests remain explicitly scoped to their legacy implementation, alongside the new active-resolver tests.

Public platform retrieval can still be blocked, region-restricted, unavailable or require authentication. No universal retrieval guarantee or 94% accuracy claim is made. The inherited network guard remains enabled; its prohibition on yt-dlp runtime subprocesses can limit bot-challenge handling. Candidate audio fallback is bounded to 90 minutes, chunked within Groq upload limits and slower than captions. Visual edits, deepfakes, speaker identity, earliest-upload provenance and factual truth are not assessed. Actual QR enrollment and all three iMessages arriving on a handset require a live user test after deployment.
