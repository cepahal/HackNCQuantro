# Prompt 4 verification

Real request used an accessible public JPEG, preserving the user's pause on paid Gemini calls.
This proves the real no-match/save/GET/delivery-failure flow, not live matched Gemini comparison.
Live matched comparison remains blocked by unfunded Gemini; no model response fabricated.

## Actual POST output

POST localhost:8000/analyze/job_real_001/run with public Pillow hopper.jpg and conversation_id=conv_test:

```json
{"job_id":"job_real_001","report_id":"report_job_real_001","report_url":"http://localhost:3000/reports/report_job_real_001","outcome":"no_verified_match","delivery_status":"failed","delivery_warning":"Could not confirm Photon delivery. Verify the outcome before requesting another send.","warnings":["Transcript coverage is incomplete; queries use only available evidence.","No distinctive searchable text was available.","Transcript coverage is incomplete; queries use only available evidence.","No distinctive searchable text was available.","No search queries were available.","Could not confirm Photon delivery. Verify the outcome before requesting another send."]}
```

## Actual GET output (first 50 pretty-printed lines)

```json
{
  "schema_version": "2.1",
  "job_id": "job_real_001",
  "outcome": "no_verified_match",
  "submitted": {
    "post": {
      "post_id": "post_e7859aa5e12c4b6c862bbbd7bca83120",
      "candidate_id": null,
      "post_url": "https://raw.githubusercontent.com/python-pillow/Pillow/main/Tests/images/hopper.jpg",
      "platform": "direct",
      "title": null,
      "caption": null,
      "author": {
        "id": null,
        "handle": null,
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
      "published_at": null,
      "media": {
        "media_id": "media_d6a00962a0664a8ba43e3512f95ccdd4",
        "mime_type": "image/jpeg",
        "playback_url": null,
        "duration_seconds": null,
        "width": 128,
        "height": 128,
        "fps": null,
        "has_audio": false
      },
      "retrieval": {
        "retrieved_at": "2026-10-10T20:03:00.608339Z",
        "provider": "direct-http",
        "metadata_source_urls": [
          "https://raw.githubusercontent.com/python-pillow/Pillow/main/Tests/images/hopper.jpg"
        ],
        "unavailable_fields": [
          "title",
          "caption",
          "published_at",
```

## Actual SQL output

psql is unavailable locally; executed the exact requested SELECT using SQLAlchemy/psycopg against live Tiger Data.

```text
report_id | job_id | created_at
report_job_real_001 | job_real_001 | 2026-10-10 20:03:03.675023+00:00
report_live_jpeg_22868a08f9 | live_jpeg_22868a08f9 | 2026-10-10 19:12:05.730624+00:00
```

GET returned HTTP 200; saved report has no /tmp/ or local_path. The initial POST included duplicate upstream warnings; final code deduplicates these, verified by regression tests.

Prompt 4 explicitly passes source_post.caption to comparison; this differs from the earlier submitted-caption flow and is implemented as requested.

Final verification: pytest tests/test_run_analysis.py -v => 24 passed; related orchestration/save/smoke suite => 39 passed before the final added warning-dedup regression. Ruff, git diff --check and secret scan passed.
