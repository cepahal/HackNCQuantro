# Quantro provider benchmark

This isolated runner calls the existing function 2 and function 3. It does not
change production providers, credentials, the frontend, or the Tiger Data corpus.
It compares local Whisper and configured Gemini on identical audio, then feeds
the same saved queries to Exa and SerpApi. Search adapters are benchmark-only.

## Run from the repository root

Use the existing Python environment and FFmpeg:

```sh
.venv312/bin/python -m benchmarks.run prepare
.venv312/bin/python -m benchmarks.run transcribe
.venv312/bin/python -m benchmarks.run search
.venv312/bin/python -m benchmarks.run report
```

Files go under ignored `.data/benchmark`; `results/REPORT.md` summarizes outcomes.
Each completed case is saved immediately. Existing result files are reused,
including failures, to prevent accidental repeat API charges. For a deliberate
fresh trial, set `--out .data/benchmark/trial-2` on transcribe/search/report.
To retry only a failed/missing-key case, remove that case's result JSON first.
Never remove successful results just to regenerate the report.

`--providers whisper` runs transcription without Gemini. Search accepts
`--providers exa serpapi`. `--query-provider whisper` is the default and uses
Whisper queries for BOTH search providers; select gemini to evaluate its queries
in a separate experiment. Do not mix query sources when ranking providers.

Privately configure in ignored root `.env.local`:

```dotenv
EXA_API_KEY=your_key
SERPAPI_API_KEY=your_key
```

Whisper uses the already configured local model and needs no key. Gemini uses
existing GEMINI_API_KEY/GEMINI_MODEL. Account creation/verification happens in
each provider dashboard. Missing keys are recorded as blocked, not false misses.
No keys, provider exception messages, billing URLs, or media bytes are committed.

## Budget and protocol

The pilot has nine variants of three NASA assets: original audio, restricted
300–3400 Hz bandwidth, and reproducible added pink noise (seed 42). The variant
named `compressed` is bandwidth filtering, not a codec-compression benchmark.
The noise amplitude is fixed, not calibrated to an SNR. The downloaded files are
small public archive clips; they are NOT full original speeches or verified
earliest uploads. Downloads/extraction and model loading can take time.

Whisper/Gemini each transcribe the same nine WAV files. Audio hashes are recorded
so comparisons can verify identical bytes. No reference text, source name, title,
URL, or speaker identity is supplied to either model or the generated queries.
Production extraction may split audio into 60-second chunks. Gemini API cost is
unknown in this report (not zero); check billing. Whisper has zero transcription
API charges but still consumes local compute. First-run model load affects timing.

Each search provider receives at most two identical transcript-derived queries per
case: at most 18 requests/provider. Each query contributes its first five results.
Round-robin rank fusion deduplicates URLs; no incomparable provider scores are
combined. The run keeps top-five URLs, per-query ranks, timing, request counts,
and Exa's reported cost when available. SerpApi cost remains unknown. No retries
or upgrades are automatic. The search runner does not download arbitrary results.

API contracts: [Exa search](https://exa.ai/docs/reference/search),
[SerpApi Google search](https://serpapi.com/search-api).
Free allowances/pricing must be checked on provider dashboards before larger runs.

## What can be concluded

**This pilot is not a trustworthy accuracy leaderboard yet.** NASA's published
transcripts have not been manually reconciled to every audible word. One includes
an unknown `??` span, and editorial wording may differ. The output therefore calls
its edit-distance rate a *published-reference difference*, not gold transcription
WER. Never replace the reference with one model's output and then grade against it.
Word timestamp accuracy requires independently annotated word boundaries; this
runner does not claim to measure it.

The two Kennedy assets overlap in speech content, so both share the development
group. The Apollo asset is reserved as a held-out pilot group; variants from the
same recording must never be split between development and evaluation. Three
famous English archive assets cannot establish general social-media accuracy.

Exact target URL top-1/top-5 counts are only a lower bound. Alternate copies of
the same recording require human review; a page discussing the speech is not
necessarily a recording match. HTTP/credential failures are distinct from
successful empty results. A ranking is never proof of the earliest original.

Before choosing a provider for production:

1. Expand to at least ten independent recordings, with several speakers and
   languages, interviews, noise, short excerpts, and known edits.
2. Human-review verbatim references and timing; preserve disfluencies. Annotate
   known clip/source intervals and gaps separately from transcript wording.
3. Review top-five candidates without knowing which provider produced them.
   Record equivalent recording, article-only, wrong source, inaccessible source,
   or uncertain. Keep the judgment evidence and URLs.
4. Retrieve accessible candidates using the production downloader, extract their
   features, and run function 6. Record correct-source selection, false positives,
   timestamp error, and abstention. The current stored-corpus search is not a
   substitute for measuring public-web retrieval.
5. Repeat on untouched source groups and compare paired latency and actual costs.

The existing `tests/test_word_alignment.py` separately measures the known six-second
cut and continuous clip. It does not measure web discovery. Full frontend/database
integration is not required to run these component evaluations.
