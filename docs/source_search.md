# Source-search handoff: functions 3, 4 and 5

Update 2026-10-10: `SEARCH_PROVIDER=exa` now selects production web search.
See [provider deployment](PROVIDERS.md) for keys, bounded calls and candidate-page
retrieval. The corpus instructions below apply to `SEARCH_PROVIDER=tiger` only.

These callables implement the team's v2 query/candidate/Post contracts. All are
synchronous; use a worker thread when calling from async code.

```python
from backend.services.external_apis.build_search_queries import build_search_queries
from backend.services.external_apis.search_candidates import search_candidates
from backend.services.external_apis.retrieve_candidate_media import retrieve_candidate_media

queries = build_search_queries(submitted_features, submitted_post)
results = search_candidates(queries, limit=5)
for candidate in results["candidates"]:
    source_post = retrieve_candidate_media(candidate)
    # Pass source_post["media"] to extract_media_features.
```

When launching from `backend/`, omit the `backend.` import prefix, as with the
existing specialist modules. `QuantroError.to_dict()` returns the shared error
fields (`code`, `stage`, `message`, `retryable`); the caller wraps them in `error`.

## Setup and corpus

Install `backend/requirements.txt`. Apply or reapply
`backend/services/external_apis/tigerdata_schema.sql` once: it now adds an
idempotent GIN full-text index to the existing `quantro_retrieval.posts` table.
Grant the backend role `USAGE` on the schema and `SELECT`/`INSERT` on the table.
Runtime performs no DDL. Connection TLS and transaction ownership follow
[retrieve_post](retrieve_post.md).

Set `TIGERDATA_DSN` privately in root `.env.local` or the process environment.
`TIGERDATA_SSL_ROOT_CERT` defaults to `system`. Settings load in increasing
precedence: `backend/.env`, root `.env`, root `.env.local`, process environment.
Keep credentials out of tracked files. A present blank `TIGERDATA_DSN` remains
an explicit configuration failure rather than silently selecting another DB.

The legacy/default-unset search provider is **Tiger Data's stored Post corpus**, searching
retrieved titles, captions/descriptions and author display names/handles. Populate
it with known accessible source recordings by calling `retrieve_post(source_url)`.
No separate corpus table, Snowflake account or web-search API key is required.
This does not search the public web or index transcripts automatically. If the
corpus has no matching descriptions, an empty result is the correct outcome.
The submitted post or a repost can appear in the corpus; search establishes no
original-source claim. The orchestrator filters the submitted URL before source
selection. Alignment and context analysis still decide evidence.

## Exact outputs and behavior

- `build_search_queries(features, post)` returns only `queries: list[str]` and
  `warnings: list[str]`. Features must identify the same `media_id` as the Post.
  It uses available spoken phrases, visible text, title and an explicitly
  unverified caption clue. It preserves input objects, removes duplicate queries,
  bounds queries to eight and 512 characters, and requires no model credentials.
  Missing distinctive clues produce an empty query array with a warning.
- `search_candidates(queries, limit)` accepts the query array or complete function
  3 output. It returns only `candidates` and `warnings`. Each candidate contains
  exactly `candidate_id`, `url`, nullable `title`, `publisher`, `published_at`,
  `snippet`, and `discovered_by_queries`. Candidate IDs are stable for the URL;
  provenance lists only queries that actually matched. SQL deduplicates source
  URLs before the limit, ranks matches deterministically, and binds all parameters.
  The accepted limit is 1–50 and at most 20 nonempty queries of 512 characters.
  Successful no-match searches return `candidates: []`; configuration, SQL or
  invalid provider results raise `SEARCH_FAILED`, never fabricated empty success.
- `retrieve_candidate_media(candidate)` requires one complete candidate. It uses
  the existing public JPG/MP4 downloader, returns the unchanged full Post shape
  with fresh post/media IDs, and populates `candidate_id` **before** storing bytes
  and metadata in one transaction. Thus returned and persisted Posts are equal.
  Search snippets never replace retrieved captions. Failed retrieval/storage
  removes this call's local files and raises an error at `retrieve_candidate_media`.

For tests or a future search provider, `search_candidates(..., provider=provider)`
accepts `find_posts(queries, limit)` returning `(Post metadata, matched queries)`
rows. Injected providers remain caller-owned. Candidate retrieval also accepts
the existing `storage`, `output_dir`, `timeout_seconds`, and `max_bytes` options.
Dedicated idle connections are required: saving commits its transaction; searching
ends its read transaction by rollback. Successful local media remains available
for downstream extraction; the job owner removes it after all consumers finish.

## Verification and change log

- 2026-10-10: implemented the three modular stages, shared search contracts,
  existing-table full-text search/index, backend environment fallback, and
  contract/failure tests. Preserved the downloader and shared Post structure.
- Unit tests cover query provenance/bounds, malformed inputs, empty results,
  provider failures, SQL binding/transaction cleanup, and real downloader-boundary
  JPG/MP4 files saved with linked candidate metadata before success.
- A local PostgreSQL engine (PGlite, installed only under ignored `.venv`) checked
  the actual migration twice, GIN creation, BYTEA/JSONB insertion/readback, phrase
  search, duplicate URLs before limit, provenance, empty results and bound SQL.
  This is SQL verification, not a live Tiger Data authentication test.
- Live Tiger Data access remains unverified because no DSN is configured. The
  separate specialist extractor's JPEG/video-duration limitation is unchanged.
- Final verification: 392 tests passed, 2 skipped; Ruff lint/format, strict mypy
  and dependency checks passed. Root and backend launch imports both passed.

## Live Tiger Data verification — 2026-10-10

Verified query generation and the actual candidate search SQL against the team's
Tiger Cloud service. A synthetic search fixture was inserted in a transaction
and rolled back, leaving no invented source metadata in the corpus. The returned
candidate was passed to the real downloader; its new Post and JPG bytes committed
with the same candidate_id and exact returned JSON. Real JPG and MP4 saves were
also read back and compared byte-for-byte and by SHA-256. Three public sample
media Posts remain as integration verification records; their local paths refer
to the machine that downloaded them. Search still covers stored Post metadata,
not the public web or automatically indexed transcripts.

Private settings are in ignored root `.env.local`. Windows certificate trust was
configured with the existing `TIGERDATA_SSL_ROOT_CERT` option pointing at a local
PEM of trusted system CAs; certificate and hostname verification remain enabled.
Those retrieval checks did not verify transcription or S3 hosting. The orchestrator
and frontend are now connected; see [integration](INTEGRATION.md) and
[provider deployment](PROVIDERS.md) for current verification and provider gates.
