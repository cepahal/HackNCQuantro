"""Partial absolute timestamps must not masquerade as excerpt duration."""

import pytest
import test_resolution_pipeline as pipeline

from backend.services.resolution import run
from backend.services.shared.types import TranscriptStatus


@pytest.fixture
def store(tmp_path):
    yield from pipeline.store.__wrapped__(tmp_path)


@pytest.mark.parametrize("recording_duration,expected", [(None, "matched"), (312.0, "no_verified_match")])
def test_late_window_preserves_source_eligibility_without_claiming_full_duration(
    store, monkeypatch, recording_duration, expected,
):
    pipeline.wired.__wrapped__(monkeypatch)
    monkeypatch.setenv("CONTEXT_VIDEO_ENABLED", "false")
    original = run.resolve_media.side_effect

    async def resolve(url, **kwargs):
        post, features = await original(url, **kwargs)
        if not kwargs.get("candidate_id"):
            post.media.duration_seconds = recording_duration
            features.duration_seconds = 312.0
            features.transcript.status = TranscriptStatus.PARTIAL
            sentence = features.transcript.sentences[0]
            sentence.start_seconds = 300.0
            sentence.end_seconds = 312.0
            features.transcript.processed_ranges[0].start_seconds = 300.0
            features.transcript.processed_ranges[0].end_seconds = 312.0
        return post, features

    monkeypatch.setattr(run.resolve_media, "side_effect", resolve)
    result = run.run_analysis("job_partial_late", pipeline.CLIP, store=store)
    assert result.outcome.value == expected
    report = store.get_report(result.report_id)
    if expected == "matched":
        assert report["submitted"]["post"]["media"]["duration_seconds"] is None
        assert report["submitted"]["transcript"]["status"] == "partial"
        assert report["alignment"]["matches"][0]["clip_start_seconds"] == 300.0
        assert report["alignment"]["matches"][0]["source_start_seconds"] == 100.0
    else:
        assert report["source"] is None
