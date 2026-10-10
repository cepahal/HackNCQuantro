"""End-to-end smoke test over the real HTTP layer and a real Postgres.

Run against a local Postgres or Tiger Data:
    TIGER_DATABASE_URL=postgresql://localhost:5432/quantro_smoke pytest tests/test_smoke_e2e.py -v

All eight upstream functions are replaced with contract-shaped fixtures, so no
network, model or messaging calls are made. Rows written by the test are deleted
afterwards.
"""

import copy
import json
import os
import uuid

import httpx
import pytest
from sqlalchemy import func, select

import backend.services.pipeline.run_ingest as ingest_module
import backend.services.reporting.run_analysis as analysis_module
from backend.main import app
from backend.routes.common import get_tiger_store
from backend.services.storage.tiger import TigerStore

pytestmark = pytest.mark.skipif(
    not os.getenv("TIGER_DATABASE_URL"), reason="TIGER_DATABASE_URL is not set; smoke test needs a real Postgres"
)

SUBMITTED_URL = "https://www.tiktok.com/@creator/video/1234567890"
SOURCE_URL = "https://www.youtube.com/watch?v=source0001"
SHARED = "Officials confirmed the bridge closure will last through next spring season."

SUBMITTED_POST = {
    "post_id": "post_smoke_submitted",
    "candidate_id": None,
    "post_url": SUBMITTED_URL,
    "platform": "tiktok",
    "title": None,
    "caption": "The bridge is closed forever.",
    "published_at": None,
    "author": {"id": None, "handle": "creator", "display_name": None, "profile_url": None, "bio": None,
               "verified": None, "followers_count": None, "following_count": None, "posts_count": None},
    "engagement": {"views_count": None, "likes_count": None, "comments_count": None, "shares_count": None},
    "media": {"media_id": "media_smoke_clip", "local_path": "/tmp/quantro-post-smoke/media.mp4",
              "mime_type": "video/mp4", "playback_url": None, "duration_seconds": None, "width": None,
              "height": None, "fps": None, "has_audio": None},
    "retrieval": {"retrieved_at": "2026-10-10T17:00:00Z", "provider": "yt-dlp",
                  "metadata_source_urls": [SUBMITTED_URL],
                  "unavailable_fields": ["title", "published_at", "author.followers_count"],
                  "warnings": ["Some optional metadata was unavailable; see unavailable_fields."]},
}

SOURCE_POST = {
    **copy.deepcopy(SUBMITTED_POST),
    "post_id": "post_smoke_source",
    "candidate_id": "cand_smoke_0001",
    "post_url": SOURCE_URL,
    "platform": "youtube",
    "title": "City council briefing",
    "caption": None,
    "media": {"media_id": "media_smoke_source", "local_path": "/tmp/quantro-post-smoke-src/media.mp4",
              "mime_type": "video/mp4", "playback_url": "https://cdn.example/source0001.mp4",
              "duration_seconds": 120.0, "width": 1280, "height": 720, "fps": 30.0, "has_audio": True},
    "retrieval": {"retrieved_at": "2026-10-10T17:01:00Z", "provider": "yt-dlp",
                  "metadata_source_urls": [SOURCE_URL], "unavailable_fields": [], "warnings": []},
}


def _features(media_id, duration, sentences):
    return {
        "media_id": media_id, "duration_seconds": duration, "language": "en",
        "transcript": {
            "status": "complete", "processed_ranges": [{"start_seconds": 0.0, "end_seconds": duration}],
            "sentences": [{"sentence_id": f"{media_id}_sentence_{i:04d}", "start_seconds": start,
                           "end_seconds": end, "text": text, "speaker_id": None}
                          for i, (start, end, text) in enumerate(sentences, start=1)],
        },
        "frames": [{"frame_id": f"{media_id}_frame_0001", "timestamp_seconds": 1.0,
                    "image_path": f"/tmp/quantro_frames_{media_id}/frame_0001.jpg", "image_url": None}],
        "visual_text": [],
        "warnings": ["Gemini transcript timestamps are estimates."],
    }


CLIP_FEATURES = _features("media_smoke_clip", 10.0, [(0.5, 9.5, SHARED)])
SOURCE_FEATURES = _features("media_smoke_source", 120.0, [
    (0.0, 5.0, "Good evening and welcome to the council briefing."),
    (40.0, 45.0, SHARED),
    (60.0, 65.0, "The repairs are expected to finish by summer."),
])

QUERIES = {"queries": ["bridge closure will last through next spring season"], "warnings": []}
CANDIDATES = {
    "candidates": [{"candidate_id": "cand_smoke_0001", "url": SOURCE_URL, "title": "City council briefing",
                    "publisher": "City Council", "published_at": "2026-10-01T00:00:00Z",
                    "snippet": "Bridge closure update", "discovered_by_queries": QUERIES["queries"]}],
    "warnings": [],
}

ALIGNMENT_CONFIDENCE = {
    "level": "moderate", "method": "evidence_rules_v1",
    "basis": ["Distinctive submitted speech aligns to a unique source passage."],
    "limitations": ["Visual and audio matching were not performed."],
}
ALIGNMENT = {
    "status": "verified",
    "clip_media_id": "media_smoke_clip",
    "source_media_id": "media_smoke_source",
    "matches": [{
        "match_id": "match_001", "clip_start_seconds": 0.5, "clip_end_seconds": 9.5,
        "source_start_seconds": 40.0, "source_end_seconds": 45.0,
        "evidence": [{"method": "transcript_alignment", "clip_refs": ["media_smoke_clip_sentence_0001"],
                      "source_refs": ["media_smoke_source_sentence_0002"],
                      "description": "A distinctive word sequence matches exactly."}],
    }],
    "match_quality": {"confidence": ALIGNMENT_CONFIDENCE, "matched_clip_seconds": 9.0,
                      "clip_coverage_ratio": 0.9, "metrics": [], "methods_used": ["transcript_alignment"]},
    "edit_analysis": {
        "status": "assessed", "scope": "between_matched_passages", "minimum_gap_seconds": 1.0,
        "source_gaps": [],
        "unmatched_clip_ranges": [{"start_seconds": 0.0, "end_seconds": 0.5},
                                  {"start_seconds": 9.5, "end_seconds": 10.0}],
        "counts": {"matched_passages": 1, "source_gaps": 0, "unmatched_clip_segments": 2,
                   "confirmed_added_segments": None, "confirmed_deleted_segments": None},
    },
    "limitations": ["Only single-passage alignment was evaluated."],
}
CONTEXT = {
    "source_media_id": "media_smoke_source",
    "windows": [{"context_id": "context_001", "match_ids": ["match_001"], "requested_start_seconds": 10.0,
                 "requested_end_seconds": 75.0, "coverage": "complete",
                 "sentences": SOURCE_FEATURES["transcript"]["sentences"][1:]}],
    "limitations": ["Gemini transcript timestamps are estimates."],
}
COMPARISON_CONFIDENCE = {
    "level": "moderate", "method": "llm_rubric_v1",
    "basis": ["The source states the closure ends when repairs finish."],
    "limitations": ["Confidence is capped at moderate for transcript-only evidence."],
}
COMPARISON = {
    "context_assessment": "material_context_missing",
    "caption_assessment": "contradicted_by_context",
    "assessment_confidence": COMPARISON_CONFIDENCE,
    "summary": "The clip omits that the closure is temporary.",
    "findings": [{"finding_id": "finding_001", "type": "omitted_qualification",
                  "explanation": "The source says repairs finish by summer.",
                  "evidence": [{"media_id": "media_smoke_source",
                                "sentence_ids": ["media_smoke_source_sentence_0003"],
                                "start_seconds": 60.0, "end_seconds": 65.0,
                                "quote": "The repairs are expected to finish by summer."}]}],
    "limitations": ["Confidence is capped at moderate for transcript-only evidence."],
}


@pytest.fixture
def store():
    tiger_store = TigerStore.from_env()
    tiger_store.ensure_schema()
    yield tiger_store
    tiger_store.close()


@pytest.fixture
def job_id(store):
    job = f"smoke_{uuid.uuid4().hex[:12]}"
    yield job
    with store._engine.begin() as connection:
        connection.execute(store.reports.delete().where(store.reports.c.job_id == job))
        connection.execute(store.posts.delete().where(store.posts.c.job_id == job))
        connection.execute(store.jobs.delete().where(store.jobs.c.id == job))


@pytest.fixture
def upstream(monkeypatch, store):
    monkeypatch.setenv("REPORT_STORE", "tiger")
    monkeypatch.setenv("REPORT_BASE_URL", "https://quantro.test")
    features_by_media = {"media_smoke_clip": CLIP_FEATURES, "media_smoke_source": SOURCE_FEATURES}

    def extract(media, options=None):
        return copy.deepcopy(features_by_media[media["media_id"]])

    fakes = {
        (ingest_module, "retrieve_post"): lambda url: copy.deepcopy(SUBMITTED_POST),
        (ingest_module, "extract_media_features"): extract,
        (analysis_module, "extract_media_features"): extract,
        (analysis_module, "build_search_queries"): lambda features, post: copy.deepcopy(QUERIES),
        (analysis_module, "search_candidates"): lambda queries, limit: copy.deepcopy(CANDIDATES),
        (analysis_module, "retrieve_candidate_media"): lambda candidate: copy.deepcopy(SOURCE_POST),
        (analysis_module, "verify_and_align"): lambda clip, source: copy.deepcopy(ALIGNMENT),
        (analysis_module, "select_surrounding_context"): lambda alignment, features, options: copy.deepcopy(CONTEXT),
        (analysis_module, "compare_context_and_caption"): lambda clip, context, caption: copy.deepcopy(COMPARISON),
    }
    for (module, name), fake in fakes.items():
        monkeypatch.setattr(module, name, fake)
    app.dependency_overrides[get_tiger_store] = lambda: store
    yield
    app.dependency_overrides.clear()


def _count(store, table, column, value):
    with store._engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(table).where(column == value)).scalar_one()


@pytest.mark.asyncio
async def test_ingest_analyze_report_round_trip(store, job_id, upstream):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://smoke.test") as client:
        ingest = await client.post(f"/ingest/{job_id}", json={"url": SUBMITTED_URL})
        assert ingest.status_code == 200, ingest.text
        ingest_body = ingest.json()
        assert set(ingest_body) == {"job_id", "url", "post_id", "media_id", "transcript_status",
                                    "sentence_count", "frames_count", "persisted_at"}
        assert ingest_body["media_id"] == "media_smoke_clip"
        assert _count(store, store.jobs, store.jobs.c.id, job_id) == 1

        run = await client.post(f"/analyze/{job_id}/run", json={"url": SUBMITTED_URL})
        assert run.status_code == 200, run.text
        result = run.json()
        assert result["outcome"] == "matched"
        assert result["report_id"] == f"report_{job_id}"
        assert result["delivery_status"] == "skipped"

        report_response = await client.get(f"/reports/{result['report_id']}")
        assert report_response.status_code == 200
        assert "/tmp/" not in report_response.text
        report = report_response.json()
        assert report["schema_version"] == "2.1"
        assert report["submitted"]["transcript"] == CLIP_FEATURES["transcript"]
        assert report["source"]["transcript"] == SOURCE_FEATURES["transcript"]
        assert report["alignment"]["match_quality"]["confidence"] == ALIGNMENT_CONFIDENCE
        assert report["comparison"]["assessment_confidence"] == COMPARISON_CONFIDENCE
        assert "/tmp/" not in json.dumps(store.get_report(result["report_id"]))

        rerun = await client.post(f"/analyze/{job_id}/run", json={"url": SUBMITTED_URL})
        assert rerun.status_code == 200, rerun.text
        assert rerun.json()["report_id"] == result["report_id"]

    assert _count(store, store.reports, store.reports.c.job_id, job_id) == 1
    assert _count(store, store.jobs, store.jobs.c.id, job_id) == 1
    assert _count(store, store.posts, store.posts.c.job_id, job_id) == 1
    assert store.get_job(job_id)["status"] == "completed"
