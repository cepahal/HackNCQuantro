# Live search pilot — 2026-10-10

Exa and SerpApi keys both authenticated successfully. Each received the same saved Whisper queries, at most two queries per case. No production provider configuration changed.

| Provider | Completed cases | Requests attempted | Exact target URL top-5 | Cases with a downloader-accepted URL | Median completed-case seconds |
|---|---:|---:|---:|---:|---:|
| exa | 9/9 | 18 | 0/9 | 0/9 | 2.400 |
| serpapi | 5/9 | 16 | 0/5 | 2/5 | 2.153 |

## Interpretation

Exact target URL scoring checks only the original NASA MOV/AVI asset URLs; it misses alternate copies and recording landing pages. Zero exact hits is not proof that no useful source was found. Candidate relevance has not been comprehensively adjudicated, so there is no accuracy winner. The three assets overlap in content and use famous English archival speech.

Queries repeat across variants, and providers may cache results. Single-run latency is descriptive only. Failed cases are excluded from completed-case latency but shown in completion counts. Provider error classes are recorded without credential-bearing diagnostics.

The current production downloader validates supported post domains or direct MP4/JPEG URLs. Most search results are general webpages, so they cannot be passed directly to function 5. Passing its URL gate does not demonstrate successful download or matching media. No candidate bytes were downloaded and no candidate alignment was run in this search pilot.

Spot check: both providers found the [JFK Library congressional speech page](https://www.jfklibrary.org/learn/about-jfk/historic-speeches/address-to-joint-session-of-congress-may-25-1961). The library describes an embedded nine-minute excerpt, not the full address, and says no downloadable file is available. This is useful discovery evidence, not a completed retrieval/verification result.

## Recommendation

Keep local Whisper for transcription: the previous paired pilot did not establish an accuracy winner, and local word timestamps already support alignment without transcription API charges. Keep Gemini for context comparison and optional OCR.

Exa completed 9/9 cases; SerpApi completed 5/9 with four ReadTimeout failures. Prefer Exa for the next integration trial on observed completion reliability, not proven matching accuracy. Before selecting a production winner, support discovery of playable media from candidate pages (within the existing retrieval safeguards), then measure successful downloads and verified matches on independent recordings. These benchmark adapters are not wired into the production stored-corpus search.

## Failures

- serpapi / commit_clean: error (ReadTimeout).
- serpapi / commit_compressed: error (ReadTimeout).
- serpapi / smallstep_clean: error (ReadTimeout).
- serpapi / smallstep_compressed: error (ReadTimeout).

Exa reported total estimated search cost: $0.126. SerpApi dollar cost is unknown; consult the account usage dashboard. No paid plan was enabled.

## Query-quality finding

The clean Apollo case generated an exact quoted query containing the ASR wording "The lamb footbeds are". Exa returned several unrelated pages alongside relevant lunar-history pages. Improve query robustness and phrase selection in a separate paired experiment; do not alter this baseline or silently inject known source titles.
