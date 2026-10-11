"""Offline canonical snapshot reuse when current caption providers fail."""
import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from backend.services.pipeline.feature_cache import extraction_signature
from backend.services.pipeline.run_ingest import EXTRACTION_OPTIONS
from backend.services.resolution import candidates
from backend.services.resolution.alignment import build_alignment
from backend.services.shared.types import MediaFeatures
from backend.services.storage.tiger import TigerStore

URL = "https://www.youtube.com/watch?v=AAAAAAAAAAA"
TEXT = "The observatory measured hydrogen emissions from distant galaxies before the expedition returned with unprecedented photographic evidence."


def snapshot(age=0):
    stamp = (datetime.now(UTC) - timedelta(seconds=age)).isoformat()
    return {"post_json": {"post_id": "source", "post_url": URL, "platform": "youtube",
            "author": {}, "engagement": {}, "media": {"media_id": "source", "local_path": "/missing.mp4", "mime_type": "video/mp4"},
            "retrieval": {"retrieved_at": stamp, "provider": "fixture"}},
            "features_json": {"media_id": "source", "duration_seconds": 300,
            "transcript": {"status": "complete", "sentences": [{"sentence_id": "s1", "text": TEXT,
                "start_seconds": 120, "end_seconds": 130}]}},
            "media_sha256": "a" * 64, "retrieved_at": stamp}


@pytest.mark.asyncio
async def test_real_sqlite_snapshot_preserves_temporal_alignment(tmp_path, monkeypatch):
    store = TigerStore(f"sqlite:///{tmp_path / 'source.db'}")
    store.ensure_schema()
    data = snapshot()
    options = {**EXTRACTION_OPTIONS, "extract_frames": False, "extract_visual_text": False, "max_frames": 0}
    store.cache_source_features(data["post_json"], data["features_json"], extraction_signature(options), data["media_sha256"])
    async def blocked(*args, **kwargs):
        raise AssertionError("No caption or audio request is allowed")
    monkeypatch.setattr(candidates, "_candidate_captions", blocked)
    monkeypatch.setattr(candidates, "_whisper_workaround", blocked)
    try:
        result = await candidates._score_candidate(candidates.Candidate("AAAAAAAAAAA", "Recording", 300),
            candidates.normalize_text(TEXT), caption_result=None, store=store)
        assert result["match_score"] == 100
        timed = result["transcript_result"]
        assert timed["source"] == "cached_source_snapshot"
        assert timed["current_url_bytes_verified"] is False
        assert timed["segments"][0]["start"] == 120
        source = MediaFeatures.model_validate(data["features_json"])
        clip_data = copy.deepcopy(data["features_json"])
        clip_data["media_id"] = "clip"
        clip_data["duration_seconds"] = 10
        clip_data["transcript"]["sentences"][0].update(start_seconds=0, end_seconds=10)
        alignment = build_alignment(MediaFeatures.model_validate(clip_data), source, result["match_score"])
        assert alignment.status == "verified"
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["stale", "future", "identity", "media", "hash", "untimed", "storage"])
async def test_invalid_snapshot_falls_through_to_audio(monkeypatch, fault):
    data = snapshot(3700 if fault == "stale" else -60 if fault == "future" else 0)
    if fault == "identity": data["post_json"]["post_url"] = "https://youtu.be/BBBBBBBBBBB"
    if fault == "media": data["features_json"]["media_id"] = "other"
    if fault == "hash": data["media_sha256"] = "invalid"
    if fault == "untimed": data["features_json"]["transcript"]["sentences"][0]["end_seconds"] = 120
    def lookup(*args):
        if fault == "storage": raise RuntimeError("unavailable")
        return data
    calls = []
    async def unavailable(*args):
        calls.append(True)
    monkeypatch.setattr(candidates, "_whisper_workaround", unavailable)
    result = await candidates._score_candidate(candidates.Candidate("AAAAAAAAAAA", "Recording", 300),
        candidates.normalize_text(TEXT), caption_result=None, store=SimpleNamespace(get_cached_source=lookup))
    assert result is None and calls == [True]
