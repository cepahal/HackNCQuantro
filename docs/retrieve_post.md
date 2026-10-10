# retrieve_post handoff

This modular stage handles a submitted URL, downloads JPG/MP4, then returns the
**version 2 Post from HackNC (4).pdf, pages 3-5**. Pass its complete `media` object
directly to the extraction stage. Default retrieval requires an accessible media
URL and configured Tiger Data storage. It returns success only after the actual
JPG/MP4 bytes and complete, unchanged Post metadata commit to Tiger Data.

Run from the repository root after `pip install -r backend/requirements.txt`:

```python
from backend.services.external_apis.retrieve_post import retrieve_post

post = retrieve_post(url)
features = extract_media_features(post["media"], options)
```

`url` is the submitted URL string. `extract_media_features` and `options` belong to
the next pipeline stage. For a trigger that returns the shared error envelope:

```python
from backend.services.external_apis.retrieve_post import handle_post_trigger

trigger = {"url": url}
result = handle_post_trigger(trigger["url"])
if "error" in result:
    # {"error": {"code": ..., "stage": "retrieve_post", "message": ..., "retryable": ...}}
    handle_failure(result["error"])
else:
    post = result
    features = extract_media_features(post["media"], options)
```

`handle_failure`, `extract_media_features` and `options` above are the caller's
functions/values. For callers that prefer exceptions, import `retrieve_post` and
`RetrievalError` from the same module; `retrieve_post(url)` raises on failure.
Both entry points are synchronous; in async code call with `asyncio.to_thread`.

Success has exactly this structure; example values below are illustrative:

```json
{
  "post_id": "post_<unique id>",
  "candidate_id": null,
  "post_url": "https://www.tiktok.com/@creator/video/1234567890",
  "platform": "tiktok",
  "title": null,
  "caption": null,
  "published_at": null,
  "author": {
    "id": null,
    "handle": "creator",
    "display_name": null,
    "profile_url": null,
    "bio": null,
    "verified": null,
    "followers_count": null,
    "following_count": null,
    "posts_count": null
  },
  "engagement": {
    "views_count": null,
    "likes_count": null,
    "comments_count": null,
    "shares_count": null
  },
  "media": {
    "media_id": "media_<unique id>",
    "local_path": "<absolute path>/quantro-post-<unique id>/media.mp4",
    "mime_type": "video/mp4",
    "playback_url": null,
    "duration_seconds": null,
    "width": null,
    "height": null,
    "fps": null,
    "has_audio": null
  },
  "retrieval": {
    "retrieved_at": "2026-10-10T17:00:00Z",
    "provider": "yt-dlp",
    "metadata_source_urls": [
      "https://www.tiktok.com/@creator/video/1234567890"
    ],
    "unavailable_fields": [
      "title", "caption", "published_at",
      "author.id", "author.display_name", "author.profile_url", "author.bio",
      "author.verified", "author.followers_count", "author.following_count", "author.posts_count",
      "engagement.views_count", "engagement.likes_count", "engagement.comments_count", "engagement.shares_count",
      "media.duration_seconds", "media.width", "media.height", "media.fps", "media.has_audio"
    ],
    "warnings": ["Some optional metadata was unavailable; see unavailable_fields."]
  }
}
```

Model definitions live in `backend/services/shared/types.py`.
Every key is present. `author` and `engagement` always contain their complete nested
objects, even when every value is null. JPEG uses `media.jpg` and
`mime_type: "image/jpeg"`; the shape stays identical. `candidate_id` is null for a
submitted post. Candidate retrieval is a separate pipeline function.

Available metadata is mapped from the retrieval provider. Unknown caption, profile
statistics, engagement and media properties remain null; an actual count of zero
stays zero. Missing optional metadata does not block retrieval. A title never fills
in for a missing caption. `published_at` describes the post, not the recorded event;
date-only metadata stays null. Dates are ISO 8601 UTC strings; durations use seconds.

`retrieval.provider` is `yt-dlp` for video and `direct-http` for direct JPEG downloads.
`metadata_source_urls` deduplicates the submitted URL and safe canonical post URL.
A profile URL may appear in `author.profile_url`; an unfetched profile page is never
listed as a metadata source. `unavailable_fields` names null optional metadata leaves
with dotted paths. It excludes the intentionally null `candidate_id` and private
`media.playback_url`. Missing metadata produces a generic warning; fully available
metadata uses `warnings: []`. These fields describe available retrieval evidence,
not the truth of a caption or the reliability of an account.

## Required Tiger Data persistence

`retrieve_post(url)` creates its default `TigerDataPostStore`, saves the actual
downloaded media bytes and complete Post, and closes its own connection. Persistence
is mandatory for a successful default call. The unchanged Post is returned only
after the database confirms the inserted post ID and the transaction commits.

Before the first default call:

1. Install `backend/requirements.txt` from the repository root.
2. Apply `backend/services/external_apis/tigerdata_schema.sql` once in the target
   Tiger Data/PostgreSQL database using a migration role. Grant the backend role
   `USAGE` on `quantro_retrieval` and `INSERT`/`SELECT` on `quantro_retrieval.posts`.
   The runtime creates no schema or table.
3. Set the server-only connection settings in ignored root `.env.local`:

   ```dotenv
   TIGERDATA_DSN=postgresql://<user>:<password>@<host>:<port>/<database>
   TIGERDATA_SSL_ROOT_CERT=system
   ```

The existing root `.env` is tracked. Keep credentials in `.env.local` or the process
environment; an ignore rule does not remove a file already tracked by Git. Preserve
teammates' existing settings. Settings precedence, highest first: process
environment, `.secrets/.env`, `.env.local`, root `.env`, `backend/.env`. Files are
loaded once per process and only fill keys the process does not already set. `DATABASE_URL` is a
fallback only when `TIGERDATA_DSN` is absent; an explicitly blank primary value fails
configuration rather than selecting a different database.

The DSN must identify one explicit TCP host, without `service`, `hostaddr`, or
multiple hosts. A specified port must be valid; it defaults to PostgreSQL's 5432
when omitted. The connection
forces certificate and hostname verification (`sslmode=verify-full`), a 10-second
connection timeout, and a 120-second statement timeout. System root certificates
are the default; set `TIGERDATA_SSL_ROOT_CERT` to a trusted CA file when needed.

`quantro_retrieval.posts` stores the file in `media_bytes BYTEA`, the unchanged v2
Post in `metadata JSONB`, plus post/media IDs, MIME type, byte count, SHA-256 digest
and creation timestamp. One bound `INSERT ... RETURNING post_id` and commit persist
bytes and metadata atomically. A failed insert or commit rolls back and produces
`STORAGE_FAILED`; retrieval cleans up its local media. This storage path requires
no S3 bucket, public media URL, Snowflake stage, or separate upload transaction.
`media.playback_url` remains null.

For tests or a caller-managed connection, pass a store implementing
`save_post(post)` through `storage=store`. The caller owns and closes an injected
store. An injected database connection must be dedicated to this store, idle on
entry, and have autocommit disabled: `save_post` commits or rolls back its
transaction. Injection preserves the same Post contract and failure behavior; the
production default remains Tiger Data. The unused Snowflake helper was removed
(see `docs/CLEANUP_LOG.md`).

## Supported retrieval

Public single-video posts: TikTok, Instagram, X/Twitter (`platform: "twitter"`),
YouTube, Vimeo and Reddit. Also direct public `.mp4`, `.jpg` and `.jpeg` URLs
(`platform: "direct"`). Video extraction requires a downloadable progressive MP4;
private/deleted/login-required/DRM/live/playlist posts and unsupported formats return
errors. Social-platform photo galleries are not supported; submit the direct JPG
URL for an image. Compatibility depends on the upstream extractor and platform
access policies. The current downloader uses no browser cookie and does not require
`RETRIEVAL_PROVIDER_API_KEY`; it uses yt-dlp for video and direct HTTP for JPEG.

The download subprocess deadline defaults to 120 seconds (`timeout_seconds=`).
Tiger Data has separate connection/query timeouts; the download deadline does not
cover database persistence. The retrieval byte cap defaults to 100 MB
(`max_bytes=`), and Tiger Data storage accepts files up to 100 MB. Direct JPEG
streams enforce the byte cap while writing.
Video advertised length and final size are checked;
unknown-length videos can temporarily exceed the accepted cap, so production workers
should use disk quotas.
MP4 validation checks complete container boxes; the next extractor verifies decoding.

The returned local file persists for the next function. Retrieval and extraction must
share a filesystem. The caller removes the file and its now-empty per-call directory
after all extraction/storage consumers finish. Failed calls clean up local artifacts.
Backend `local_path` values must be removed when assembling browser-facing reports;
they are for extraction on the shared backend filesystem, not public media URLs.

## Verification

`python -m pytest tests/test_retrieve_post.py tests/test_retrieval_worker.py tests/test_tigerdata_storage.py`

Tests cover exact v2 Post/error contracts, nullable metadata and provenance,
downstream handoff, JPEG/MP4, default persistence and injected stores, preservation
of all v2 metadata, Tiger Data bytes/JSON transaction bindings and rollback, TLS/DSN
settings, safe failures, and cleanup.
Live Tiger Data authentication, schema setup and JPG/MP4 persistence were verified
on 2026-10-10; see the latest verification entry below.

## Change log

- 2026-10-10: added the retrieval callable, shared v2 types, isolated downloader,
  Snowflake stage/metadata helper and setup SQL, requirements/environment examples,
  tests and handoff documentation. Verified 209 tests, Ruff lint/format, strict mypy
  and dependency consistency. A live public MP4 downloaded 5,510,872 bytes; a mock
  Snowflake connector verified the actual file stream, full Post metadata and commit.
  At that verification, live Snowflake upload was unverified because no token was
  configured. Supported formats and platform restrictions are listed above.
- 2026-10-10: added compatibility with the team's `SNOWFLAKE_PASSWORD` setting while
  retaining optional PAT authentication and token precedence. Existing root `.env`
  settings remain intact. Verified 225 tests, Ruff lint/format, strict mypy and
  dependency consistency after the authentication change. Live authentication and
  persistence remain unverified because no credentials have been provided.
- 2026-10-10: made `retrieve_post(url)` standalone by default: download and return the
  complete v2 Post directly. Snowflake persistence runs only through an explicitly
  supplied `storage` object, which the caller owns and closes. Verified 228 tests,
  Ruff lint/format and strict mypy. Default calls downloaded a real 5,510,872-byte
  MP4 and a real 6,412-byte JPG (128x128), returned JSON-native Posts and retained
  usable local files without loading Snowflake. No credentials or Snowflake setup
  are required for standalone retrieval.
- 2026-10-10: superseded the prior standalone default with mandatory Tiger Data
  persistence. Default calls store actual JPG/MP4 bytes and the complete unchanged
  v2 Post in `quantro_retrieval.posts` within one verified insert/commit transaction,
  then return the existing extraction contract. Added explicit one-time migration
  and `.env.local` setup instructions, environment precedence, TLS requirements,
  and storage failure behavior. The root `.env` is tracked, so secrets belong in
  ignored `.env.local` or the process environment. No live cloud persistence or
  S3 integration is claimed; the user will configure connection settings later.

- 2026-10-10: configured the existing Tiger Cloud service using its downloaded
  credentials in ignored `.env.local`. Applied the idempotent retrieval migration
  alongside existing public job/post/report tables without changing those tables.
  Verified TLS hostname/certificate validation, real JPG (6,412 bytes) and MP4
  (5,510,872 bytes) downloads, committed BYTEA readback, SHA-256 and full Post JSON
  equality. Verified candidate download with fresh IDs and persisted candidate_id.
  On this Windows machine, libpq's `system` trust failed; exporting Python's
  trusted system CA certificates to an ignored PEM and setting the existing
  `TIGERDATA_SSL_ROOT_CERT` option fixed it with `verify-full` still enforced.
  The local PEM path is machine-specific and must not be copied to deployments.
