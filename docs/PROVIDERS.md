# Website backend: Exa and Whisper

The website and both Photon intake paths call the same backend orchestrator.
Provider keys and Whisper run on that backend, never in the browser. No frontend
API fields, report URLs, Photon routing, or shared function outputs changed.

## Configuration

Set privately on the backend host (or root `.env.local` for local development):

```dotenv
SEARCH_PROVIDER=exa
EXA_API_KEY=your_private_key
TRANSCRIPTION_PROVIDER=whisper
WHISPER_MODEL=small.en
GEMINI_API_KEY=your_existing_private_key
GEMINI_MODEL=your_available_model
```

For the supplied Docker image, use its baked-in `WHISPER_MODEL=/opt/quantro/whisper`
(default Docker ENV), not the local `small.en` example or a developer's Mac path.
`small.en` is English-only; build with WHISPER_BUILD_MODEL=small for multilingual
speech. The build downloads the model, making missing network access at first
transcription less likely. Model builds require network access and additional
image space. Docker build can explicitly opt out with INSTALL_WHISPER=0 if the
host instead uses TRANSCRIPTION_PROVIDER=gemini.

Local setup still requires FFmpeg and both requirements files:

```sh
python -m pip install -r backend/requirements.txt -r backend/requirements-whisper.txt
```

Render's blueprint now selects Exa/Whisper and requests EXA_API_KEY. Existing
services may need those environment settings applied in their dashboard and a
redeploy. The free plan remains unchanged: available RAM must be checked before
using Whisper there, especially alongside the API and concurrent jobs. Budget
roughly 2 GB or more for initial deployment testing; this is a starting point,
not a measured production requirement or a claim that the free instance works.
No paid plan was selected by this change.

Bhrigu still configures REPORT_BASE_URL to Grant's actual frontend origin and
NEXT_PUBLIC_API_BASE_URL to the backend origin. Keep Tiger and Photon credentials
on the backend. Exa does not replace Tiger's media/job/report storage. Backend
startup still requires Tiger access and its schemas. No teammate's remote/local
workspace or deployed service is automatically updated by committing this code.

## Function connections

- Function 2 already supports local Whisper, including optional word timing.
  Deployment now installs that dependency/model and selects it consistently.
  Gemini remains in use for requested frame OCR and function 8.
- Function 4 selects Exa for SEARCH_PROVIDER=exa. SEARCH_PROVIDER=tiger retains
  the previous stored-corpus mode; unset remains tiger for legacy compatibility.
  There is no silent fallback if Exa credentials or calls fail.
- Exa makes at most four requests, with at most ten results/request. Three use
  the first mixed speech/OCR/title clues; one repeats the leading clue using
  the [documented includeDomains filter](https://exa.ai/docs/reference/search)
  for YouTube, Vimeo and Internet Archive. General search remains unrestricted.
  Results merge by round-robin rank and prefer recording-shaped URLs before
  limiting, with exact URL deduplication and matched-query provenance.
  It returns the existing CandidateList contract; optional metadata stays null
  when not available. A search result is never a verified recording.
- Some failed queries produce an explicit limited-coverage warning. Complete
  provider failure raises SEARCH_FAILED. Authentication/quota failures stop
  further calls. Error text does not include provider diagnostics or keys.
- Function 4 now accepts public webpage candidates. Function 5 allows the existing
  yt-dlp worker to try their embedded media. Submitted URL policy stays unchanged.
  DNS/redirect/network checks, time and size limits, MP4 validation, no-cookie
  policy and disabled external downloaders remain in force.
- This is not a universal page crawler or stream converter. Article-only pages,
  login requirements, blocked downloads and unsupported stream formats remain
  candidate failures. The orchestrator skips them and discloses limited coverage;
  it does not fabricate a source match.

## Validation on 2026-10-10

653 Python tests passed, 1 skipped, 45 subtests passed, including the new provider
selector, ranking/provenance, safe errors, partial failure, and webpage boundary
checks. Actual local function 2 selected Whisper without patching and returned a
complete transcript with word timestamps. Actual production function 4 selected
Exa without provider injection and returned five real candidate pages. A live
JFK Library candidate attempt returned MEDIA_UNAVAILABLE through function 5;
no full candidate download/alignment success is claimed for that page.

Docker is unavailable on this development machine, so image build, hosted memory
use, hosted Tiger access and full deployed analysis were not validated here.
The user reports a successful Photon video-to-website round trip on the team's
setup; that is separate from verification of this new deployed search path.


### Integration repair validation

A fresh local small.en Whisper run on the existing compressed Kennedy sample
returned 3 sentences and 72 timed words in 3.80 seconds, with a maximum resident
set of 1,027,145,728 bytes (about 980 MiB). This is one macOS CPU measurement,
not a Linux production sizing benchmark. Allow additional memory for the API,
frame processing, database client, and simultaneous requests. Hosted capacity
must be validated before relying on the current free-plan blueprint.
