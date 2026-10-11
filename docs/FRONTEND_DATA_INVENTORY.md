# Frontend data wiring and metric inventory

Audited 2026-10-11 against local teammate commit `53ba049` plus the uncommitted integration fixes. This is a source and offline contract audit, not confirmation that the public deployment runs these changes. No commit or deployment was made.

## Input and output wiring

| Frontend entry/control | Backend or local output | Verification / boundary |
| --- | --- | --- |
| Navbar / homepage “Try now” | Opens the shared `InputForm` | UI-only control; no backend call is needed to open or close it |
| Public video URL / Start analysis | `startAnalysis` -> `POST /analyze/{job_id}/run`, body `{url}` | API tests verify a new job, URL body, response identity and structured errors; browser form validates HTTP(S) and forbids userinfo |
| Optional `conversationId` in API client | Same POST with `conversation_id` | Supported by client/server; current website form does not request it |
| Results `?job=` | Awaits in-memory POST; after refresh uses `GET /analyze/{job_id}/status` | Tests cover pending, completed, failed, cancelled, invalid status and bounded timeout |
| Results `?id=`, `?report_id=`, `/results/[reportId]`, `/reports/[report_id]` | `GET /reports/{report_id}` and `GET /reports/{report_id}/display` | API contract tests cover valid reports, missing report, malformed fields and IDs |
| Submitted video | `report.submitted.post.media` -> playback URL or `/media/{media_id}` | Player and media-route tests; original YouTube embed is an explicit fallback |
| Matched recording | `report.source.post.media`, matched outcome and consistent verified alignment | A search candidate is labeled unverified; it is not silently promoted to a match |
| Generated context video | `report.context_video` -> `/media/{media_id}` | Only a verified high/moderate match may display it; no fallback to the original video disguised as an export |
| Diagnostics / retry | `GET /reports/{report_id}/diagnostics` | Same-job validation and safe backend projection; historical missing events stay unavailable |
| Copy link / copy output / download | Clipboard / local `.txt` | Browser actions; no write endpoint needed. Export includes raw public report plus typed data/timeline markers |
| Data markers disclosure | `reportDataMarkers(report, diagnostics)` | New typed projection in `frontend/lib/view-model.ts`, rendered in `DiagnosticPanel` |
| Timeline markers for upcoming bars | `reportTimelineMarkers(report)` | Linked submitted/source passages, assessed gaps, unmatched ranges, context windows; invalid intervals are omitted |
| Navigation, menu, “How it works” | Local links, focus/menu state | UI-only |
| Homepage sample durations/counts/confidence and algorithm animation | Static illustrations | **Not live backend measurements.** Do not use those values as report data |
| Demo link | Configured/default report ID via ordinary report route | The route is wired; this audit does not prove that a particular demo report still exists in production Tiger |
| Old `ResultCard` tabs, passage map and selectors | Local selections over report fields | Component remains in source but is not mounted by the current results page; do not assume its controls are live |
| Phone/QR signup | Backend-served Photon signup, separate from Next.js | Outside the Next.js results form; no registration, phone send, or live Photon change was made by this audit |

Browser-only clipboard behavior and mobile playback were not manually exercised during this audit. Provider availability, platform download access and current cloud environment remain deployment-dependent.

## How to consume the data

```ts
import { reportDataMarkers, reportTimelineMarkers } from "../lib/view-model";

const data = reportDataMarkers(report, diagnostics);
const coverage = data.find(m => m.id === "alignment.match_quality.clip_coverage_ratio");
const timeline = reportTimelineMarkers(report);
```

Each data marker has `id`, `label`, `value`, `unit`, `origin`, `path`, and `range`. Value is a number, label, boolean or null. The path identifies the source field or a documented derivation. `range` is supplied only for a known bounded scale, such as clip coverage `[0,1]` or a lexical score `[0,100]`. An unspecified scale is not automatically suitable for a percentage bar. `origin` distinguishes recorded, derived, model, metadata and diagnostic values. Recorded means saved, not independently verified.

Null means unavailable. A recorded zero remains zero. Empty arrays mean no entries were supplied, not proof that a detector ran. Processed interval lengths merge overlaps. A context window's requested span is not observed transcript completeness. Model confidence stays high/moderate/low/not_assessed. Clip coverage may be formatted as a percentage, but is not accuracy. A lexical score of 90 is not a 90% probability of a correct match. Never combine engagement/popularity with reliability.

## Full metric and evidence inventory

Paths below are in `GET /reports/{report_id}` unless stated otherwise. `submitted` and `source` have the same shape; `source` can be null. Every saved public field remains available in the raw diagnostic JSON even if it is not a numeric marker.

### Recording and transcript data, separately for submitted and source

| Data | Path / derivation | Unit / use |
| --- | --- | --- |
| Media ID, type, playback URL | `{side}.post.media.{media_id,mime_type,playback_url}` | Playback and marker targeting |
| Recording duration | `{side}.post.media.duration_seconds` | Seconds, nullable; not replaced with last caption timestamp |
| Width, height, frame rate | `{side}.post.media.{width,height,fps}` | Pixels; frames/second; nullable |
| Audio present | `{side}.post.media.has_audio` | Boolean/null |
| Transcript status | `{side}.transcript.status` | Complete/partial/unavailable |
| Processed ranges | `{side}.transcript.processed_ranges[].{start_seconds,end_seconds}` | Seconds; provider coverage windows |
| Processed interval total | `{side}.transcript.processed_seconds` marker | Derived union of processed ranges, seconds; not measured voiced duration |
| Timed sentence count | `{side}.transcript.sentences.length` | Count |
| Sentence ID, text, start/end, speaker tag | `{side}.transcript.sentences[].{sentence_id,text,start_seconds,end_seconds,speaker_id}` | Text and seconds; speaker tag is not verified identity |
| Timed word count | Sum of `sentences[].words.length` | Count of supplied word-time entries, not API tokens; zero means no entries supplied |
| Individual timed words | `{side}.transcript.sentences[].words[].{text,start_seconds,end_seconds}` | Optional text and seconds |
| Frame count, IDs, timestamps, public image URLs | `{side}.frames.length`, `.frames[].{frame_id,timestamp_seconds,image_url}` | Count and seconds; image URL may be null |
| Visual/OCR text count and entries | `{side}.visual_text.length`, `.visual_text[].{frame_id,timestamp_seconds,text}` | Count, seconds and text |

Current Groq resolver explicitly reports that frame extraction and visual forensics did not run. Its empty frame/OCR arrays are not a deepfake or edit-detection result. The public schema supports these fields for other implementations.

### Metadata and engagement

| Data | Path |
| --- | --- |
| Post ID, candidate ID, original URL, platform | `{side}.post.{post_id,candidate_id,post_url,platform}` |
| Title, caption, publication timestamp | `{side}.post.{title,caption,published_at}` |
| Account ID, handle, display name, profile URL, biography, verification badge | `{side}.post.author.{id,handle,display_name,profile_url,bio,verified}` |
| Followers, following, post count | `{side}.post.author.{followers_count,following_count,posts_count}` |
| Views, likes, comments, shares | `{side}.post.engagement.{views_count,likes_count,comments_count,shares_count}` |
| Retrieval provider, retrieval time, metadata references | `{side}.post.retrieval.{provider,retrieved_at,metadata_source_urls}` |
| Missing metadata fields and retrieval warnings | `{side}.post.retrieval.{unavailable_fields,warnings}` |

All counts above are nullable. The active resolver leaves several optional social metrics unavailable. Schema support does not imply that each platform returned them. Publication and retrieval times are ISO 8601 UTC strings.

### Matching and alignment

| Data | Path / derivation | Unit / interpretation |
| --- | --- | --- |
| Outcome and alignment status | `outcome`, `alignment.status` | Labels, not truth verdicts |
| Clip/source media identity | `alignment.{clip_media_id,source_media_id}` | Links each passage to its video |
| Match IDs, clip/source start/end | `alignment.matches[].{match_id,clip_start_seconds,clip_end_seconds,source_start_seconds,source_end_seconds}` | Seconds; linked timeline markers |
| Match method, references, explanation | `alignment.matches[].evidence[].{method,clip_refs,source_refs,description}` | Evidence/provenance |
| Source-match confidence and method | `alignment.match_quality.confidence.{level,method}` | Categorical level |
| Confidence basis and limits | `alignment.match_quality.confidence.{basis,limitations}` | Text |
| Matched submitted duration | `alignment.match_quality.matched_clip_seconds` | Seconds/null |
| Submitted clip coverage | `alignment.match_quality.clip_coverage_ratio` | 0..1/null; the percentage-friendly coverage bar |
| Algorithm metrics | `alignment.match_quality.metrics[].{name,value,unit}` | Dynamic metric names/units; active resolver uses `transcript_partial_ratio`, unit `score_0_100` |
| Methods used | `alignment.match_quality.methods_used` | Labels |
| Alignment limitations | `alignment.limitations` | Text |

### Edits and source gaps

| Data | Path / derivation | Interpretation |
| --- | --- | --- |
| Edit assessment status and scope | `alignment.edit_analysis.{status,scope}` | Must check before drawing edit bars |
| Minimum gap threshold | `alignment.edit_analysis.minimum_gap_seconds` | Seconds |
| Source gaps and adjacent matches | `alignment.edit_analysis.source_gaps[].{start_seconds,end_seconds,before_match_id,after_match_id}` | Approximate timeline gaps |
| Unmatched submitted ranges | `alignment.edit_analysis.unmatched_clip_ranges[].{start_seconds,end_seconds}` | Seconds |
| Counts | `alignment.edit_analysis.counts.{matched_passages,source_gaps,unmatched_clip_segments,confirmed_added_segments,confirmed_deleted_segments}` | Nullable counts; unavailable if not assessed, except matched passage count |
| Total gap / unmatched interval | `source_gap_seconds`, `unmatched_clip_seconds` derived markers | Union duration in seconds, only when edits assessed |
| Legacy passage span / out-of-order flag | `derive(report).{passageSpan,outOfOrder}` | Existing local helper; out-of-order means non-chronological or overlapping passages, not proof of malicious editing |
| Legacy fraction left out / matched source total | `derive(report).{leftOutPct,sourceMatchedSeconds,gapSeconds}` | Existing approximation; do not present as forensic certainty or a new confidence percent |

Current single-passage resolver sets edit analysis to `not_assessed`, so confirmed additions/deletions and gap-duration charts normally remain unavailable. Generated context video is a viewing aid, not proof of a splice or deepfake.

### Context, interpretation and quoted evidence

| Data | Path / derivation |
| --- | --- |
| Context source ID, window count | `context.source_media_id`, `context.windows.length` |
| Window IDs, linked matches, requested start/end | `context.windows[].{context_id,match_ids,requested_start_seconds,requested_end_seconds}` |
| Window coverage labels | `context.windows[].coverage` (complete/partial/unavailable) |
| Context sentences | `context.windows[].sentences[]`, same transcript sentence shape |
| Requested context interval total | `context.requested_seconds` derived marker, overlap merged |
| Context limits | `context.limitations` |
| Context assessment | `comparison.context_assessment` (material_context_missing/no_material_difference_found/uncertain/not_assessed) |
| Caption assessment | `comparison.caption_assessment` (supported_by_context/contradicted_by_context/uncertain/not_provided/not_assessed) |
| Assessment confidence | `comparison.assessment_confidence.{level,method,basis,limitations}` |
| Analysis prose | `summary`, `comparison.summary` |
| Finding count, type, explanation | `comparison.findings.length`, `.findings[].{finding_id,type,explanation}` |
| Evidence count | Sum of `comparison.findings[].evidence.length` |
| Evidence media ID, sentence refs, times, quote | `comparison.findings[].evidence[].{media_id,sentence_ids,start_seconds,end_seconds,quote}` |
| Comparison limits and all report limits | `comparison.limitations`, `limitations`, derived `limitations.length` |
| Display labels | `GET /reports/{id}/display`: `source_match`, `clip_coverage`, `context_assessment`, `confidence_note`, panel data, findings and limits |

### Generated context export

| Data | Path |
| --- | --- |
| Export ID, URL, duration, dimensions, FPS, audio/type | `context_video.{media_id,playback_url,duration_seconds,width,height,fps,has_audio,mime_type}` |
| Source-relative cut boundaries | `context_selection.{start_seconds,end_seconds}` |
| Selection strategy / before-after method | `context_selection.{strategy,before_method,after_method}` |
| Selected preceding/following sentences | `context_selection.{before,after}[].{text,start,end}` |
| Timing qualification | `context_selection.timing_note` |

These optional fields may be absent/null on older reports or failed exports. The export preserves continuous source footage and adds timed labels; it does not remove source gaps. Do not confuse source-relative selection times with zero-based generated-video time.

### Discovery, diagnostics, job and delivery data

Source: `GET /reports/{id}/diagnostics`, `events[]`. Event `data` has a safe allowlist, not arbitrary provider response bodies.

| Data | Location / meaning |
| --- | --- |
| Capture status, job ID, event count, terminal output | `capture_status`, `job_id`, `events.length`, `terminal_output` |
| Stage, status, elapsed seconds | `events[].{stage,status,elapsed_seconds}` |
| Timing scope | New optional `events[].timing_kind`: stage_duration or job_elapsed. Old events have unknown scope. Never sum nested/parallel durations for total latency |
| Query text, extracted speaker/event clue, distinctive phrase, title query | `events[].data.search_metadata`, query-building/search-query event data |
| Search result counts | `events[].data.{returned_count,kept_count,candidate_count}` |
| Candidate IDs, URLs, titles, duration hints | `events[].data.candidates[]`, `.leads[]`, candidate evaluation data |
| Candidate lexical score, decision threshold | `events[].data.{match_score,threshold}`; unit score_0_100, not probability |
| Candidate transcript and provider source | `events[].data.transcript_result.{transcript,segments,source,duration,coverage,duration_known}` when supplied |
| Downloaded flag, full retained transcript, provider and media snapshot | Retrieval event `data.{downloaded,full_transcript,transcript_source,post,transcript,frames,visual_text,warnings}` |
| Selection decision and rejection reason | Selection/evaluation `data.{selected,url,reason}`; candidate_number where present |
| Saved report/outcome | Build/save/completion event `data.{report,report_id,outcome}` |
| Error | Failure event `data.{code,stage,message,retryable}`; safe public error text |
| Job status, report ID, error | `GET /analyze/{job_id}/status`; job timestamps are stored internally but this status response does not expose them |
| Analysis response delivery data | POST response `AnalysisResult.{delivery_status,delivery_warning,warnings,report_url,report_id,job_id,outcome}`. Delivery acceptance is not proof the phone received/read it |
| Ingest counts | Optional client `ingest()` -> `POST /ingest/{job_id}`: transcript_status, sentence_count, frames_count, persisted_at, IDs. Current website form calls analyze directly |

Candidate scores and duration hints may appear only in diagnostics, not in the final winning-source report. Older captures cannot be reconstructed. Private paths, credentials and raw provider bodies are intentionally excluded. Diagnostics are loaded once on report display or explicit retry; no new streaming/polling feed was added.

## Provider-specific availability and missing metrics

The live code path is selected by imports, not by the existence of keys: `backend/routes/analyze.py` and Photon import `services/resolution/run.py`. That resolver uses Groq LLM analysis and Groq Whisper fallback, caption retrieval and configured search. Other Gemini modules remain in the repository; they are not automatically called by the active resolver.

| Provider | Available from the current saved data | Not currently recorded / available |
| --- | --- | --- |
| Groq LLM | Interpretation, context/caption assessment, categorical assessment confidence, evidence; query clues; context-selection strategy | Input/output/cached/reasoning token usage, per-call cost, remaining balance, exact per-request model/ID, per-call HTTP status/retries. Groq SDK responses are reduced to validated analysis data |
| Groq Whisper | Provider label `groq_whisper`, transcript, provider segment times, duration/coverage metadata; stage timing | ASR confidence probabilities, word error rate, no-speech/logprob metrics, billing/remaining audio quota. Raw verbose response fields are not retained wholesale |
| Gemini | Legacy paths have transcript estimates and context/caption comparisons under the shared schema if those paths are run | Active resolver does not emit Gemini-specific results; no Gemini usageMetadata, token cost, credits, grounding count, safety rating, or probability-of-truth metric is exposed |
| YouTube captions / hosted captions | Caption text, intervals, provenance and coverage limitations; may avoid Whisper calls | Independent word-timing accuracy or confirmed speaker identity |
| SerpAPI / YouTube search / other discovery | Queries, result counts, candidate links/titles/duration hints and lexical scoring | Remaining account credits and invoices; exact request spending or comprehensive-web recall |
| Tiger Data | Persisted jobs/reports, media lookup and safe job timestamps | Public database connection details, raw storage credentials, query latency or storage-use billing |
| Photon | Delivery status in analysis response/events, warnings | Actual handset receipt/read confirmation, unrestricted public-number authorization, API credits |

Not implemented: calibrated misinformation/truth probability, deceptive intent, deepfake probability, validated speaker identification, emotion/sentiment score, frame/audio fingerprint similarity in the active resolver, or playback-speed scalar. These must remain unavailable rather than be inferred from transcript matching. Account quota must be checked in the provider dashboard or implemented separately; a generic MEDIA_UNAVAILABLE error is not evidence that LLM credits ran out.

## Verification and scope

The audit uses existing frontend API tests, backend route/shape tests, media serving and safe diagnostic projection tests. Added tests cover null vs zero, merged interval durations, stale no-match fields, typed scales, timeline links, unknown historic timing, same-job diagnostics, context-export public fields and sensitive-data exclusion. No new dependency, provider API call, schema migration, database write, phone send or deployment is required for the projection.
