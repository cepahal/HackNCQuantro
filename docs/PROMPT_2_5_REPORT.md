# Prompt 2.5 — execution report (2026-10-10)

## Section 0 — before state
Actual OS: Windows; commands adapted to PowerShell. Before edits:
```
On branch main
Your branch is up to date with 'origin/main'.
nothing to commit, working tree clean
```
Safety branch: integration/kill-stubs-20261010. Main was not merged or modified.
Before-state installed relevant packages: fastapi 0.143.0, httpx 0.28.1,
psycopg/psycopg-binary 3.3.6, pydantic 2.14.0, SQLAlchemy 2.1.4,
yt-dlp 2026.8.19. FFmpeg/ffprobe 9.0.1 present. Tesseract absent.
yt-dlp executable was in .venv/Scripts, absent from inherited PATH.

## Section 1 — full inventory
The prompt's claim that production functions contain canned responses was not confirmed.

| file:line | function | actual behavior | classification | owner | blocks demo E2E? |
|---|---|---|---|---|---|
| backend/services/specialist/gemini.py:23 | client_and_model | Comment describing tracked environment placeholders; actual Gemini API client | D: configuration comment | Jason | funded credits block live comparison |
| backend/services/reporting/send_summary.py:71 | MemoryDeliveryTracker | Real process-local tracker for injected/test deployments; durable Tiger path exists | B: test support | Bhrigu | no |
| backend/services/reporting/save_report.py:44 | MemoryReportStore | Real process-local store for injected/test deployments; durable Tiger path exists | B: test support | Bhrigu | no |
| backend/services/_archive/example_api.py:1 | none | Previously archived empty scaffold | C: already archived | shared | no |

No classification-A canned production stubs found. Worklist addressed actual requested
capability gaps: measured probe metadata, model returns, local speech/OCR, public web search,
startup source-file guard, dependencies and opt-in real-video tests.
No canned IDs, MOCK/STUB environment switches, NotImplementedError, TODO or FIXME found.
The variable clip_sentences in alignment is a real computed list, not a canned ID.
New main.py stub/mock matches are the requested rejection guard itself.

## Sections 2 and 9 — dependencies
Installed official UB-Mannheim Tesseract via winget; installer hash verified:
```
Successfully verified installer hash
Starting package install...
Successfully installed
```
Versions: FFmpeg/ffprobe 9.0.1; Tesseract 5.4.0.20240606;
yt-dlp 2026.08.19; faster-whisper 1.2.1; pytesseract 0.3.13; Pillow 12.3.0.
Pin av 16.1.0: real inference revealed av 19 incompatibility with faster-whisper 1.2.1.
Existing direct Python dependencies pinned in backend/requirements.txt.
OpenCV omitted: no implementation imports it; FFmpeg already extracts actual frames.
Legacy google-generativeai omitted: existing code uses supported google-genai 2.29.0.
No duplicate/unused SDK installed. README contains macOS, Debian and Windows instructions.
```
No broken requirements found.
```

## Sections 3 and 6 — retrieval
Both functions now return canonical Post instances and share _download_and_probe.
Actual saved bytes are probed with bounded FFprobe. Existing guarded worker, download
limits, isolated workspaces, valid JPEG/MP4 checks, TLS checks and Tiger byte persistence remain.
Candidate relationship is saved before success. Unknown statistics remain null.
Actual local MP4 probe test: 0.8 seconds, 160x120, 25 fps, has_audio=False.
Live public hopper JPEG retrieved and persisted to Tiger: 6412 bytes, 128x128, has_audio=False.
Initial requested zoo URL output (before separate-stream fix):
```
{'code': 'MEDIA_UNAVAILABLE', 'stage': 'retrieve_post', 'message': 'The submitted video could not be retrieved.', 'retryable': False}
```
Resolved progressive-only format limitation: guarded worker downloads video/audio streams separately; bounded parent FFmpeg merges local files only. Real zoo retrieval now passed: 19.063583 seconds, 320x240, 15 fps, audio present, persisted Tiger. Full gate output below supersedes initial failure.

## Section 4 — extraction
Default local cached base Whisper CPU/int8, real FFmpeg audio/ranges/frames and Tesseract OCR.
Returns canonical MediaFeatures. Full/range coverage uses actual successful ranges;
failed optional local transcription/OCR produces unavailable/warnings as Prompt 2.5 requests.
Explicit provider=gemini preserves the previous strict Gemini adapter. No paid Gemini calls made.
Actual generated-image OCR output:
```
QUANTRO REAL OCR TEST
OCR PASS
```
Whisper base downloaded and process cache verified. Actual synthesized speech to FFmpeg MP4 to local extraction passed: complete transcript, actual processed range 0–4.568481 seconds, recognized speech, three real JPEG frames. No Gemini or mock used. This is local inference proof, not the required YouTube gate.

## Section 5 — search
Public web adapters: SerpAPI, Brave, Google CSE. Explicit tigerdata selects actual stored corpus.
Fixed provider hosts, bounded requests, safe result URLs, UUID identities, merged provenance,
null dates, dedupe and result cap. Total failures raise QuantroError; partial failures warn.
Bing Search API is retired; configuration raises a clear error instead of pretending it works.
Private SerpAPI credentials are stored only in ignored .env.local. No key values in this report.
Initial live test exposed a local TLS trust-store issue. Fixed with system trusted CA context; verification stays enabled. Actual requested search returned 3 real URLs and no warnings. Exact output: [PROMPT_2_5_SEARCH_OUTPUT.txt](PROMPT_2_5_SEARCH_OUTPUT.txt).

## Section 7 — alignment/context/comparison
Existing alignment uses normalized real transcript token sequences and SequenceMatcher;
no hard-coded contract-example text. Context uses actual source sentences and coverage ranges.
Comparison uses real context and evidence-validated Gemini output; funded credits still required.
Existing transcript-only moderate confidence cap retained: coverage alone is not independent
recording verification. No mock matcher or fake Gemini interpretation added.

## Section 8 — startup and orphan stubs
reporting/_stubs.py and backend mock_*.py/fake_*.py were already absent; none created.
Orchestrator imports real modules directly, no silent stub fallback found.
Startup now rejects callable functions whose actual source filename contains stub/mock.
Regression test confirms rejection and lists the offending function.

## Section 10 — hard gate
PASSED after ranking correction: real YouTube retrieval, real Whisper transcription, three real JPEG frames, real SerpAPI search, candidate retrieval, verified alignment with moderate confidence. ALL PASS printed; 4 live tests passed in 23.12 seconds.
No Prompt 3, main merge, or claims of live matched-source completion.
Twitter/X not attempted: no verified public example supplied; optional per prompt.

## Section 11 — automated real pipeline tests
Created tests/test_real_pipeline.py: four opt-in real-network tests, module-scoped media
fixture avoids repeated downloads during a run; all are skipped in normal CI.
Exact NETWORK=1 pytest output: [PROMPT_2_5_NETWORK_TEST_OUTPUT.txt](PROMPT_2_5_NETWORK_TEST_OUTPUT.txt).
Latest output supersedes the initial four setup errors from actual YouTube retrieval. No mocks substituted.
Exact full offline suite output: [PROMPT_2_5_UNIT_TEST_OUTPUT.txt](PROMPT_2_5_UNIT_TEST_OUTPUT.txt).

## Section 12 — verification and remaining gates
```
boot ok
All checks passed!
```
Ruff verified changed production modules and new focused tests. git diff --check passed.
Missing live proof beyond this slice: funded Gemini comparison; real iMessage delivery remains
an existing integration acceptance gate. These are external availability/account gates,
not canned implementations. Main remains untouched on the requested safety branch.
The real-video slice passed; Gemini comparison remains separately paused. No Prompt 3 or main merge.

### Complete changed-file inventory
```
 M .env.example
 M README.md
 M backend/main.py
 M backend/requirements.txt
 M backend/services/external_apis/retrieve_candidate_media.py
 M backend/services/external_apis/retrieve_post.py
 M backend/services/external_apis/search_candidates.py
 M backend/services/processing/preprocessing.py
 M backend/services/shared/types.py
 M docs/architecture.md
 M tests/test_contract_shapes.py
 M tests/test_retrieve_post.py
 M tests/test_source_search.py
 M tests/test_specialist.py
 M tests/test_specialist_images.py
?? docs/PROMPT_2_5_NETWORK_TEST_OUTPUT.txt
?? docs/PROMPT_2_5_UNIT_TEST_OUTPUT.txt
?? tests/test_local_features.py
?? tests/test_real_pipeline.py
?? tests/test_retrieval_probe.py
?? tests/test_web_search.py
```

## Final unit verification

626 passed, 5 skipped, 45 subtests passed. Four skips are explicit NETWORK gating; one existing Windows symlink permission skip.

## Exact final outputs

### PROMPT_2_5_NETWORK_TEST_OUTPUT.txt

```text
============================= test session starts =============================
platform win32 -- Python 3.12.14, pytest-9.1.1, pluggy-1.6.0 -- C:\Users\mogal\Documents\ChatGPT\HackNCQuantro\.venv\Scripts\python.exe
rootdir: C:\Users\mogal\Documents\ChatGPT\HackNCQuantro
configfile: pytest.ini
plugins: anyio-4.15.1, platformdirs-4.12.4, asyncio-1.4.0, mock-3.16.0
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=function, asyncio_default_test_loop_scope=function
collecting ... collected 4 items

tests/test_real_pipeline.py::test_retrieve_post_real_video local_path: C:\Users\mogal\AppData\Local\Temp\quantro\quantro-post-1bcxuua2\media.mp4
exists: True size: 533931
fps: 15.0 has_audio: True title: Me at the zoo
unavailable_fields: ['author.bio', 'author.following_count', 'author.posts_count', 'engagement.shares_count']
PASS retrieve_post 19.063583 320 240
PASSED
tests/test_real_pipeline.py::test_extract_media_features_real_video 0.18 4.18 'All right, so here we are, one of the elephants.'
4.18 14.18 "The cool thing about these guys is that they have really, really, really long punks, and that's cool."
14.18 19.0635625 "And that's pretty much all there is to say."
frame: C:\Users\mogal\AppData\Local\Temp\quantro_frames_zsqn_pok\frame_0001.jpg True
frame: C:\Users\mogal\AppData\Local\Temp\quantro_frames_zsqn_pok\frame_0002.jpg True
frame: C:\Users\mogal\AppData\Local\Temp\quantro_frames_zsqn_pok\frame_0003.jpg True
visual_text: 0 warnings: ['Whisper segment timestamps are speech-model estimates; speaker identities are not assessed.', 'Frame timestamps are requested sample positions and may differ by a frame from decoded timestamps.']
PASS extract_media_features 3 3
PASSED
tests/test_real_pipeline.py::test_search_candidates_real PASS build_search_queries ['"All right so here we are one of the elephants"', '"The cool thing about these guys is that they have really really"', '"And that\'s pretty much all there is to say"', '"Me at the zoo"', '"Microplastics are accumulating in human brains at an alarming rate https www"']
https://www.youtube.com/watch?v=044O0mMUXgk&xstg=CAMSBhUDzO3xHw%3D%3D | Me at the zoo | Jawed Karim | First Youtube Video | 2005
https://open.spotify.com/track/7AN2BmbMRsh0eCVVjY19oJ | The Elephant Bowling Team - song and lyrics by SteveSongs
https://www.facebook.com/EvanCarmichaelcom/posts/did-you-know-youtubes-very-first-video-was-only-18-seconds-longin-2005-a-guy-nam/1590775962410721/ | Did you know YouTube's very first video was only 18 ...
warnings: ['The submitted caption is an unverified search clue, not established fact.']
PASS search_candidates 3
PASSED
tests/test_real_pipeline.py::test_verify_and_align_real PASS verify_and_align verified moderate
ALL PASS
PASSED

============================= 4 passed in 23.12s ==============================

```

### PROMPT_2_5_SEARCH_OUTPUT.txt

```text
count: 3
https://en.wikipedia.org/wiki/Me_at_the_zoo | Me at the zoo
https://www.youtube.com/@jawed/videos | jawed
https://simple.wikipedia.org/wiki/Me_at_the_zoo | Me at the zoo
warnings: []

```

### PROMPT_2_5_UNIT_TEST_OUTPUT.txt

```text
........................................................................ [ 11%]
............................................................ssss................................... [ 27%]
........................................................................ [ 38%]
........................................................................ [ 49%]
........................................................................ [ 61%]
........................................................................ [ 72%]
........................................................................ [ 84%]
................s....................................................... [ 95%]
............................                                             [100%]
626 passed, 5 skipped, 45 subtests passed in 26.58s

```

After final search-ranking edit: 56 focused contract/web tests passed; Ruff passed; secret scan of all 23 changed/untracked nonignored files passed. Private .env.local remains ignored.

### Final file inventory
```text
 M .env.example
 M README.md
 M backend/main.py
 M backend/requirements.txt
 M backend/services/external_apis/retrieve_candidate_media.py
 M backend/services/external_apis/retrieve_post.py
 M backend/services/external_apis/search_candidates.py
 M backend/services/processing/preprocessing.py
 M backend/services/shared/types.py
 M docs/architecture.md
 M tests/test_contract_shapes.py
 M tests/test_retrieve_post.py
 M tests/test_source_search.py
 M tests/test_specialist.py
 M tests/test_specialist_images.py
?? docs/PROMPT_2_5_NETWORK_TEST_OUTPUT.txt
?? docs/PROMPT_2_5_REPORT.md
?? docs/PROMPT_2_5_SEARCH_OUTPUT.txt
?? docs/PROMPT_2_5_UNIT_TEST_OUTPUT.txt
?? tests/test_local_features.py
?? tests/test_real_pipeline.py
?? tests/test_retrieval_probe.py
?? tests/test_web_search.py
```
