# Hybrid search verification

Worktree: `Quantro-hybrid`, branch `codex/transcript-hybrid-search`, starting commit `a843b38`, 2026-10-10. The independent verifier changed only `tests/test_hybrid_pipeline.py` and this note.

## Final results

- **Final root full suite after all changes:** `1051 passed, 9 skipped, 45 subtests passed in 67.40s (0:01:07)`. The earlier 1,034-test snapshot preceded search expansion, model selection and batching; it is superseded. The independent verifier did not rerun the final full suite.
- **Independent final check:** all eight pipeline tests passed in 1.60 seconds. Ruff passed for the owned test file after import-order and fixture-registration cleanup; no broad lint suppression was added.
- **Independent affected regressions:** 81 passed in 18.13 seconds across hybrid pipeline/search, source-feature cache storage and existing run-analysis tests. Separate feature-key/pipeline checks: ten passed in 1.74 seconds.
- **Root-reported live smoke:** Tiger and Exa passed with 20 documents and 296 passages; Tiger median search latency was 376 ms and hybrid search took 2.33 seconds. Root reported zero Gemini calls. The independent verifier made no live provider calls or credential reads.
- Diff whitespace checks passed and frontend diff remained empty at independent review snapshots. No P1 issue was found in the reviewed hybrid/cache changes. The verifier made no production changes, commits or pushes.

Root's final additional checks passed: Ruff on all changed Python files, mypy on
six affected typed modules, and read-only backend-to-frontend wire type comparison.
The real backend lifespan booted against existing Tiger configuration and served
OpenAPI with all three required ingest/analyze/report routes (HTTP 200).

The final storage check replayed the same 20 documents with zero duplicates and
zero fabricated times. Batched replay took 10.04 seconds versus the earlier
34.84-second row-at-a-time run; these are two network observations, not a controlled
throughput benchmark. The second Tiger lookup median was 412 ms. The live archive
still contains 296 passages from those 20 modern documents. The older 44-document
research archive is separate and was not imported into Tiger by these checks.

Gemini 2.5 Flash-Lite appeared in the model list but returned HTTP 404 on generation.
3.1 Flash-Lite returned HTTP 200/STOP on a 15-token JSON smoke. One separate
synthetic conditional-recommendation fixture ran through the real comparison in
2.95 seconds using two calls, returned the expected contradicted caption verdict,
and passed local citation/contract checks. Its context-assessment dimension was
not independently labeled; this is not an accuracy benchmark. Total generation
attempts for these checks: four (one 404, one tiny response, two semantic calls).
The previously blocked 64-case external semantic payload was not sent.

The comparison-only model is configured privately; `.env.example` contains its
nonsecret model name. No keys or local recordings are included in the change.
No new frontend code or hosted deployment is part of this branch. Fresh hosted
video acquisition, long-video latency and the general 94-percent target remain
unverified; unit-test counts do not establish model accuracy.

## Independent integration coverage

The eight tests cover:

1. Canonical URL deduplication and submitted-source exclusion before limiting.
2. Web-provider errors remain `SEARCH_FAILED` even with local candidates.
3. Unsafe discovery URLs are discarded while valid candidates remain available.
4. Hybrid search borrows job storage, passes exclusions and restores its context.
5. Actual SQLite archive import/search preserves unavailable timing, deduplicates sources and fabricates no ingest jobs.
6. Local text discovery still requires independent recording extraction/alignment: unrelated retrieved speech returns `no_verified_match` and skips context/caption comparison.
7. A real cache hit skips download/ASR but reruns alignment, updates candidate identity, discloses original retrieval time and preserves the saved snapshot.
8. A cached recording whose resolved provenance identifies the submitted post is rejected before alignment.

Cache review covered immutable identity/content/extraction keys, original-retrieval TTL, model/code/options key separation, matching media IDs, transactional indexing, internal worker-path handling and persisted candidate media playback. Reuse is a prior recording snapshot, not proof of current URL bytes or authenticity.

## Local real-transcript smoke

The actual importer indexed the existing 101,182-character October 22, 2020 debate transcript from `research/quantro-analysis-20261010/experiments/archive/event_00.txt` into temporary SQLite. Exact UTF-8 SHA-256: `9821cf9308807262ac73badeb4c4dd622bc830738f7015b41ecc287b7009f123`. One record inserted; query `Kristen Welker` returned the CPD transcript URL. The temporary database was discarded.

A repeated timing smoke created 58 passages: import 16.672 ms; 100 SQLite searches had median 0.485 ms and maximum 1.427 ms. These are one-document local measurements, not a 100,000-document benchmark or production latency estimate.

## Current limits

- Text matches identify possible sources, not originality, authenticity, physical edits or deepfakes. Downstream alignment remains mandatory.
- Cached snapshots use a one-hour default TTL and floating model aliases. Reports disclose reuse and retrieval time; reuse does not confirm current URL content.
- Independent tests use SQLite and mocked providers. PostgreSQL/provider evidence above is separately attributed to root's live smoke.
- Transcript rights remain unverified. Neither timing smoke establishes large-corpus performance or end-to-end analysis latency.
- Nine full-suite skips remain; passing counts do not imply those paths were exercised.

## Resolved findings

Owners corrected importer bad-URL continuation, unsafe hybrid URL handling, scoped-store test compatibility and GIN-index registration; subsequent focused checks passed. Initial media-tool failures passed after configuring installed FFmpeg. These are resolved, not current release blockers. The verifier made no TLS weakening or SSL environment workaround.
