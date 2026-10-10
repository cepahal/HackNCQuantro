# Jason's specialist functions — contracts v2, report schema 2.1

The corrected video contracts supplied by Jason take precedence over the older
image workflow in README.md and the outdated owner/model examples in RUNBOOK.md.

## Setup

Use Python 3.11 or newer (the existing reporting package uses datetime.UTC).
Python 3.12 and FFmpeg are installed on Jason's Mac. His ready environment is
`.venv312`; activate it with `source .venv312/bin/activate` from the repo root.
In VS Code select `.venv312/bin/python` as the interpreter.

Install with `python -m pip install -r backend/requirements.txt`.
FFmpeg and ffprobe must be on PATH (`brew install ffmpeg` on Homebrew).
The default Gemini model is `gemini-flash-latest`. Use an audio/image capable
model available to your project, with JSON schema support. Do not use the old
`gemini-1.5-pro` example in RUNBOOK.md.

Set privately in ignored root `.env.local`; it overrides root/backend `.env`
placeholders and any previously loaded environment value. Do not commit keys:

```dotenv
GEMINI_API_KEY=your-private-key
GEMINI_MODEL=gemini-flash-latest
```

The Gemini SDK uses system-trusted root certificates with certificate and hostname
verification enabled. This supports local trusted network certificates without
disabling TLS verification.
SDK construction and request failures are normalized to safe stage errors.
Network timeouts and transient provider errors are retryable; HTTP 402 billing
failures require an account fix and are not marked retryable.

2026-10-10 live recovery: the configured `gemini-flash-latest` alias returned
HTTP 504 for structured JSON requests. `gemini-3.1-flash-lite` passed the same
Comparison schema request with the same key; the ignored local environment now
explicitly selects that model. Set `GEMINI_MODEL=gemini-3.1-flash-lite` privately
to reproduce this working configuration. There is no silent model fallback.
Requests have a 45-second timeout and at most three transient-error attempts,
with 2/4-second backoff and SDK retries disabled. Errors expose safe categories,
HTTP status or an allowlisted finish reason, never provider response bodies.
Comparison permits one re-ask after rejected evidence, using the original input
and fixed correction instructions. All citations still pass the same strict
local checks for verbatim single-sentence quotes, IDs, and exact timestamps.

2026-10-10 configuration handoff: added private local environment loading and
verified system TLS. The configured SDK authenticated successfully using model
listing; 393 tests passed with 2 skips. No API key is committed. Generation and
transcription quality still require a separate approved-media test.

The repository does not include credentials. Missing keys fail explicitly.
Runtime model/network authorization and transcription quality must be verified
with a real approved recording once credentials are configured.

## Integration imports

When running with the repo root on PYTHONPATH:

```python
from backend.services.processing.preprocessing import extract_media_features
from backend.services.specialist.alignment import verify_and_align
from backend.services.specialist.context import select_surrounding_context
from backend.services.specialist.comparison import compare_context_and_caption
```

When launching inside backend as the current API does, omit `backend.`.
Functions 2, 6 and 8 raise the existing `QuantroError`; function 7 preserves its
existing ValueError validation, which the orchestrator should normalize.
All outputs are ordinary dictionaries following the supplied contracts.

## Behavior and boundaries

### Function 2

Accepts MediaAsset plus transcript_scope (full/ranges), transcript_ranges,
extract_frames, max_frames, frame_ranges, extract_visual_text. Empty frame_ranges
samples the full recording; ranged transcription requires explicit valid ranges.
Overlapping processing ranges are merged to avoid duplicate transcription.

FFprobe measures actual file duration and audio/video presence. FFmpeg decodes
mono 16 kHz WAV in bounded 60-second requests. Gemini produces sentence groups;
chunk-local times are validated and offset to the full recording. Incomplete
utterances crossing chunk boundaries remain separate (no invented joins or
speaker identities). Timestamps are model estimates, not forced alignment.
A successful processed silent chunk has no sentences but retains coverage.
A file without audio returns unavailable transcript coverage.
Full success means the whole requested audio was processed, not that the words
or timestamps are guaranteed correct. Full recording coverage is required for
status complete; subset coverage is partial. Any failed transcription request
raises TRANSCRIPTION_FAILED rather than pretending that interval was silent.

Frames are sampled at interval midpoints and saved as JPEGs. Timestamps are
requested positions with frame-level uncertainty. OCR may need frames even when
extract_frames is false, so those backing frames are returned for evidence IDs.
Frame sampling failures become warnings; they are not silently claimed as
successful processing. When visible-text extraction is requested, provider or
response-validation failures are errors. Set `extract_visual_text=false` to skip
OCR explicitly. Generated frame directories persist for downstream
matching; the job owner must remove them after analysis. Temporary WAVs are
removed automatically. No arbitrary recording-duration cap is imposed; the
orchestrator should impose its job time/cost limits before extraction.

JPEGs retain the same feature field structure. They have duration 0, unavailable
speech, and at most one frame at timestamp 0, with optional visible-text extraction.
These timestamps describe a still image, not a recording timeline. Transcript
alignment returns not_assessed for images and recordings without usable speech;
this implementation does not verify image matches.

### Function 6

A deterministic, conservative transcript baseline: case/punctuation-normalized
exact word-sequence matching across sentence boundaries. An accepted passage
must contain fully matched clip sentences, at least eight words, at least four
unique non-stopwords, and a unique location in the candidate transcript. These
are documented heuristic thresholds, not calibrated probabilities. Short or
approximate matches can be missed intentionally. Repeated passages are ambiguous.

No visual/audio corroboration is claimed; accepted matches are moderate.
Unprocessed input returns not_assessed. Processed but unverified input returns
no_verified_match, empty matches, and null coverage metrics. Accepted coverage
uses the union of clip intervals. Enclosing source sentence boundaries retain
transcription precision; word-level timestamps are never interpolated.

Edit analysis describes observed gaps only. When both transcripts are complete,
status is assessed; otherwise it is not_assessed. Source gaps of at least one
second use start_seconds, end_seconds, before_match_id and after_match_id.
Unmatched clip intervals use start_seconds/end_seconds. These list item shapes
fill in details not specified in the sample contract. Confirm them with the UI
owner. Confirmed insertion/deletion counts always remain null: a gap is not proof
of an edit. Reordered passages receive a limitation. Even a verified recording
is not necessarily the original upload.

### Function 7

Existing behavior is preserved. It additionally validates processed intervals
and propagates extraction warnings (including timestamp precision) to context
limitations, so function 8 cannot lose those caveats.

### Function 8

Uses transcript/context/caption evidence, never credentials, file paths, or
frames. Model output must pass a strict schema, actual sentence-ID lookup,
verbatim quote validation, exact cited-sentence timestamp checks, and source
evidence requirements. Each quote must occur within one cited sentence.
Provider failures are errors, not uncertain/no-match outcomes. Missing usable
speech/context returns not_assessed without a provider call. Null caption yields
not_provided. Incomplete coverage or low confidence forces uncertain. Transcript
only confidence is capped at moderate; no audio/recording verification is claimed.
Evidence-reference validation does not establish semantic correctness; inspect
live outputs during demo validation.

## Verification

From repo root:

```bash
source .venv312/bin/activate
python -m pytest tests/test_specialist.py tests/test_context.py -q
python -m pytest tests -q
```

The tests use fake Gemini responses and a real generated FFmpeg video. They test
contracts, range offsets, silent/unavailable media, ambiguous source matches,
negation changes, gaps, citation fabrication, uncertainty, and SDK configuration.
They do not make paid API calls, send messages, or upload user media.

Bhrigu's run_analysis is still a stub, and retrieval/search/frontend work is
outside Jason's scope. Import these four functions into that orchestrator; call
functions 7 and 8 only after a verified candidate is selected.
