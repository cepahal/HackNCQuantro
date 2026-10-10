import json

import pytest
from pydantic import ValidationError

from backend.services.external_apis import post_contract
from backend.services.external_apis.retrieve_post import _result
from backend.services.reporting import types as t
from backend.services.reporting.errors import ErrorCode, QuantroError, Stage
from backend.services.specialist import comparison as jason_comparison
from backend.services.specialist.alignment import verify_and_align
from backend.services.specialist.comparison import compare_context_and_caption
from backend.services.specialist.context import select_surrounding_context

EXPECTED_FIELDS = {
    t.Author: {"id", "handle", "display_name", "profile_url", "bio", "verified",
               "followers_count", "following_count", "posts_count"},
    t.Engagement: {"views_count", "likes_count", "comments_count", "shares_count"},
    t.MediaAsset: {"media_id", "local_path", "mime_type", "playback_url", "duration_seconds",
                   "width", "height", "fps", "has_audio"},
    t.Retrieval: {"retrieved_at", "provider", "metadata_source_urls", "unavailable_fields", "warnings"},
    t.Post: {"post_id", "candidate_id", "post_url", "platform", "title", "caption", "author",
             "engagement", "published_at", "media", "retrieval"},
    t.TranscriptSentence: {"sentence_id", "start_seconds", "end_seconds", "text", "speaker_id", "words"},
    t.Transcript: {"status", "processed_ranges", "sentences"},
    t.Frame: {"frame_id", "timestamp_seconds", "image_path", "image_url"},
    t.VisualText: {"frame_id", "timestamp_seconds", "text"},
    t.MediaFeatures: {"media_id", "duration_seconds", "language", "transcript", "frames",
                      "visual_text", "warnings"},
    t.ConfidenceObject: {"level", "method", "basis", "limitations"},
    t.AssessmentConfidence: {"level", "method", "basis", "limitations"},
    t.Match: {"match_id", "clip_start_seconds", "clip_end_seconds", "source_start_seconds",
              "source_end_seconds", "evidence"},
    t.MatchQuality: {"confidence", "matched_clip_seconds", "clip_coverage_ratio", "metrics",
                     "methods_used"},
    t.EditCounts: {"matched_passages", "source_gaps", "unmatched_clip_segments",
                   "confirmed_added_segments", "confirmed_deleted_segments"},
    t.EditAnalysis: {"status", "scope", "minimum_gap_seconds", "source_gaps",
                     "unmatched_clip_ranges", "counts"},
    t.Alignment: {"status", "clip_media_id", "source_media_id", "matches", "match_quality",
                  "edit_analysis", "limitations"},
    t.ContextWindow: {"context_id", "match_ids", "requested_start_seconds",
                      "requested_end_seconds", "coverage", "sentences"},
    t.Context: {"source_media_id", "windows", "limitations"},
    t.Evidence: {"media_id", "sentence_ids", "start_seconds", "end_seconds", "quote"},
    t.Finding: {"finding_id", "type", "explanation", "evidence"},
    t.Comparison: {"context_assessment", "caption_assessment", "assessment_confidence",
                   "summary", "findings", "limitations"},
    t.ReportV2: {"schema_version", "job_id", "outcome", "submitted", "source", "alignment",
                 "context", "comparison", "summary", "limitations"},
}

EXPECTED_ENUMS = {
    t.Outcome: {"matched", "no_verified_match"},
    t.TranscriptStatus: {"complete", "partial", "unavailable"},
    t.ContextAssessment: {"material_context_missing", "no_material_difference_found",
                          "uncertain", "not_assessed"},
    t.CaptionAssessment: {"supported_by_context", "contradicted_by_context", "uncertain",
                          "not_provided", "not_assessed"},
    t.ConfidenceLevel: {"high", "moderate", "low", "not_assessed"},
}


@pytest.mark.parametrize("model", list(EXPECTED_FIELDS), ids=lambda m: m.__name__)
def test_model_has_exact_contract_fields(model):
    actual = set(model.model_fields)
    expected = EXPECTED_FIELDS[model]
    assert not expected - actual, f"{model.__name__} is missing {sorted(expected - actual)}"
    assert not actual - expected, f"{model.__name__} has extra {sorted(actual - expected)}"


@pytest.mark.parametrize("enum", list(EXPECTED_ENUMS), ids=lambda e: e.__name__)
def test_enum_has_exact_contract_values(enum):
    assert {member.value for member in enum} == EXPECTED_ENUMS[enum]


@pytest.mark.parametrize(
    "ours, theirs",
    [
        (t.Author, post_contract.Author),
        (t.Engagement, post_contract.Engagement),
        (t.MediaAsset, post_contract.MediaAsset),
        (t.Retrieval, post_contract.Retrieval),
        (t.Post, post_contract.Post),
    ],
    ids=lambda m: m.__name__,
)
def test_post_models_match_retrieval_owner_contract(ours, theirs):
    assert ours is theirs
    assert set(ours.model_fields) == set(theirs.model_fields)


@pytest.mark.parametrize(
    "ours, theirs",
    [
        (t.Comparison, jason_comparison.Comparison),
        (t.Finding, jason_comparison.Finding),
        (t.Evidence, jason_comparison.Evidence),
        (t.AssessmentConfidence, jason_comparison.Confidence),
    ],
    ids=lambda m: m.__name__,
)
def test_comparison_models_match_specialist_contract(ours, theirs):
    assert set(ours.model_fields) == set(theirs.model_fields)


def test_report_schema_version_is_fixed():
    assert t.ReportV2.model_fields["schema_version"].default == "2.1"
    with pytest.raises(ValidationError):
        t.ReportV2.model_validate({**_minimal_report(), "schema_version": "2.0"})


def test_report_media_rejects_local_paths():
    report = _minimal_report()
    report["submitted"]["post"]["media"]["local_path"] = "/tmp/x.mp4"
    with pytest.raises(ValidationError):
        t.ReportV2.model_validate(report)

    report = _minimal_report()
    report["submitted"]["frames"] = [{"frame_id": "f1", "timestamp_seconds": 1.0, "image_url": None,
                                      "image_path": "/tmp/f1.jpg"}]
    with pytest.raises(ValidationError):
        t.ReportV2.model_validate(report)


def test_unknown_values_stay_null_and_keys_are_never_omitted():
    author = t.Author.model_validate({"followers_count": None})
    dumped = author.model_dump()
    assert set(dumped) == EXPECTED_FIELDS[t.Author]
    assert dumped["followers_count"] is None
    assert dumped["following_count"] is None


def test_counts_reject_booleans_and_negatives():
    for bad in (True, -1, 1.5):
        with pytest.raises(ValidationError):
            t.Author.model_validate({"followers_count": bad})


def test_unassessed_edit_counts_can_all_be_null():
    counts = t.EditCounts.model_validate({name: None for name in EXPECTED_FIELDS[t.EditCounts]})
    assert all(value is None for value in counts.model_dump().values())


@pytest.mark.parametrize("value", ["2024-05-01T12:00:00+02:00", "2024-05-01T12:00:00", "May 1 2024"])
def test_timestamps_must_be_iso_8601_utc(value):
    with pytest.raises(ValidationError):
        t.Retrieval.model_validate(_retrieval(retrieved_at=value))


def test_parse_stage_output_raises_quantro_error():
    with pytest.raises(QuantroError) as caught:
        t.parse_stage_output(t.Alignment, {"status": "verified"}, "verify_and_align")

    assert caught.value.code is ErrorCode.PROCESSING_FAILED
    assert caught.value.stage is Stage.VERIFY_AND_ALIGN


def test_real_retrieve_post_output_matches_contract(tmp_path):
    (tmp_path / "media.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9")
    info = {"_type": "image", "title": "A post", "uploader": "someone", "timestamp": 1714564800}

    post = _result("https://example.com/a.jpg", "direct", tmp_path, json.dumps(info), 1_000_000)

    parsed = t.parse_stage_output(t.Post, post, "retrieve_post")
    assert parsed.model_dump(mode="json") == post
    assert parsed.author.followers_count is None


def test_real_specialist_outputs_match_contract():
    clip, source = _clip_features(), _source_features()
    t.parse_stage_output(t.MediaFeatures, clip, "extract_media_features")
    t.parse_stage_output(t.MediaFeatures, source, "extract_media_features")

    alignment = verify_and_align(clip, source)
    parsed_alignment = t.parse_stage_output(t.Alignment, alignment, "verify_and_align")
    assert parsed_alignment.status == "verified"
    assert parsed_alignment.model_dump(mode="json") == alignment

    context = select_surrounding_context(alignment, source)
    assert t.parse_stage_output(t.Context, context, "select_surrounding_context").model_dump(mode="json") == context

    silent_clip = {**clip, "media_id": "media_silent",
                   "transcript": {"status": "unavailable", "processed_ranges": [], "sentences": []}}
    comparison = compare_context_and_caption(silent_clip, context, None)
    parsed_comparison = t.parse_stage_output(t.Comparison, comparison, "compare_context_and_caption")
    assert parsed_comparison.model_dump(mode="json") == comparison


def test_unverified_alignment_matches_contract():
    clip = _clip_features(text="Completely different words appear here without any shared passage at all")

    alignment = verify_and_align(clip, _source_features())

    parsed = t.parse_stage_output(t.Alignment, alignment, "verify_and_align")
    assert parsed.status == "no_verified_match"
    assert parsed.match_quality.clip_coverage_ratio is None
    assert parsed.edit_analysis.counts.source_gaps is None


SHARED = "Officials confirmed the bridge closure will last through next spring season"


def _sentence(media_id, index, start, end, text):
    return {"sentence_id": f"{media_id}_sentence_{index:04d}", "start_seconds": start,
            "end_seconds": end, "text": text, "speaker_id": None}


def _clip_features(text=SHARED):
    return {
        "media_id": "media_clip", "duration_seconds": 10.0, "language": "en",
        "transcript": {"status": "complete", "processed_ranges": [{"start_seconds": 0.0, "end_seconds": 10.0}],
                       "sentences": [_sentence("media_clip", 1, 0.0, 10.0, text)]},
        "frames": [{"frame_id": "media_clip_frame_0001", "timestamp_seconds": 5.0,
                    "image_path": "frames/frame_0001.jpg", "image_url": None}],
        "visual_text": [], "warnings": [],
    }


def _source_features():
    return {
        "media_id": "media_source", "duration_seconds": 60.0, "language": "en",
        "transcript": {"status": "complete", "processed_ranges": [{"start_seconds": 0.0, "end_seconds": 60.0}],
                       "sentences": [
                           _sentence("media_source", 1, 0.0, 10.0, "Welcome everyone to the evening briefing tonight"),
                           _sentence("media_source", 2, 20.0, 30.0, SHARED),
                           _sentence("media_source", 3, 40.0, 50.0, "However the closure depends on final funding approval"),
                       ]},
        "frames": [], "visual_text": [], "warnings": [],
    }


def _retrieval(**overrides):
    return {"retrieved_at": "2026-10-10T17:00:00Z", "provider": "yt-dlp", "metadata_source_urls": [],
            "unavailable_fields": [], "warnings": [], **overrides}


def _minimal_report():
    post = {
        "post_id": "post_1", "candidate_id": None, "post_url": "https://example.com/p", "platform": "direct",
        "title": None, "caption": None, "author": {}, "engagement": {}, "published_at": None,
        "media": {"media_id": "media_1", "mime_type": "video/mp4"}, "retrieval": _retrieval(),
    }
    return {
        "job_id": "job_1", "outcome": "no_verified_match",
        "submitted": {"post": post, "transcript": {"status": "unavailable", "processed_ranges": [], "sentences": []},
                      "frames": [], "visual_text": []},
        "limitations": ["No verified source was found."],
    }
