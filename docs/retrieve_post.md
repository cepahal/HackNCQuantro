# retrieve_post handoff

This modular stage handles a submitted URL, downloads JPG/MP4, saves the actual media
and Post metadata to Snowflake, then returns the **version 2 Post from HackNC (4).pdf,
pages 3-5**. Pass its complete `media` object directly to the extraction stage.

Run from the repository root after `pip install -r backend/requirements.txt`:

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

TypedDict definitions live in `backend/services/external_apis/post_contract.py`.
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

## Snowflake setup

The checked-in repository initially contained no Snowflake implementation; its
README describes planned Cortex Search. This module adds the required media save
helper using the official Python connector. Snowflake's SQL REST API does not support
file `PUT`; the connector performs the actual stage upload.

Copy Snowflake values from `.env.example` into the ignored root `.env` and fill in
account, user and programmatic access token, plus the warehouse/database/schema
context appropriate to the team's account. Never paste tokens into code or chat.
Run `backend/services/external_apis/snowflake_schema.sql` once in the chosen schema.
Custom stage/table names are supported through the example variables; create those
objects in the schema explicitly if changing the names. This module runs no DDL.

The helper verifies PUT's uploaded status and byte counts, then inserts metadata
and commits. A success Post is returned only after this completes. A failed upload
or metadata insert produces `STORAGE_FAILED`, with no pipeline success. A file that
uploads before an insert fails can remain orphaned on the stage; no unrelated stage
files are removed. `playback_url` stays null because private staged media is not a
public player URL. Snowflake rows include the stage path, media ID, byte count and
SHA-256 digest. The metadata VARIANT preserves the entire v2 Post, including profile
fields and retrieval provenance, unchanged; no additional schema migration is needed
for v2.

## Supported retrieval

Public single-video posts: TikTok, Instagram, X/Twitter (`platform: "twitter"`),
YouTube, Vimeo and Reddit. Also direct public `.mp4`, `.jpg` and `.jpeg` URLs
(`platform: "direct"`). Video extraction requires a downloadable progressive MP4;
private/deleted/login-required/DRM/live/playlist posts and unsupported formats return
errors. Social-platform photo galleries are not supported; submit the direct JPG
URL for an image. Compatibility depends on the upstream extractor and platform
access policies. No retrieval API key or browser cookie is used.

The download subprocess deadline defaults to 120 seconds (`timeout_seconds=`).
Snowflake has separate login/network/query timeouts. The retrieval byte cap defaults
to 100 MB (`max_bytes=`); persisted media remains capped at 100 MB. Direct JPEG streams
enforce the byte cap while writing. Video advertised length and final size are checked;
unknown-length videos can temporarily exceed the accepted cap, so production workers
should use disk quotas.
MP4 validation checks complete container boxes; the next extractor verifies decoding.

The returned local file persists for the next function. Retrieval and extraction must
share a filesystem. The caller removes the file and its now-empty per-call directory
after all extraction/storage consumers finish. Failed calls clean up local artifacts.
Backend `local_path` values must be removed when assembling browser-facing reports;
they are for extraction on the shared backend filesystem, not public media URLs.

## Verification

`python -m pytest tests/test_retrieve_post.py tests/test_retrieval_worker.py tests/test_snowflake_storage.py`

Tests cover exact v2 Post/error contracts, nullable metadata and provenance,
downstream handoff, JPEG/MP4, mandatory storage-before-success, preservation of all
v2 metadata, Snowflake PUT result verification, safe failures, and cleanup.
No live Snowflake save is claimed until credentials and schema are configured.

## Change log

- 2026-10-10: added the retrieval callable, shared v2 types, isolated downloader,
  Snowflake stage/metadata helper and setup SQL, requirements/environment examples,
  tests and handoff documentation. Verified 209 tests, Ruff lint/format, strict mypy
  and dependency consistency. A live public MP4 downloaded 5,510,872 bytes; a mock
  Snowflake connector verified the actual file stream, full Post metadata and commit.
  Live Snowflake upload remains unverified because no token is configured. Supported
  formats and platform restrictions are listed above.
