import pytest

from backend.services.reporting.display_report import (
    CONFIDENCE_NOTE,
    CONTEXT_UNAVAILABLE_MESSAGE,
    NO_SOURCE_MESSAGE,
    build_display_model,
)
from backend.services.shared.errors import ErrorCode, QuantroError, Stage


def _confidence(level):
    return {"level": level, "method": "test", "basis": ["shared phrase"], "limitations": ["short clip"]}


def _matched_kwargs(**overrides):
    kwargs = dict(
        outcome="matched",
        submitted_post={"post_id": "p_sub", "author": {"followers_count": 1200, "following_count": 80, "posts_count": 45}},
        submitted_transcript={"status": "complete", "sentences": [{"sentence_id": "s1", "text": "hello"}]},
        submitted_playback_url=None,
        source_post={"post_id": "p_src", "author": {"followers_count": None, "following_count": None, "posts_count": None}},
        source_transcript={"status": "partial", "sentences": []},
        source_playback_url="https://cdn.example.com/src.mp4",
        match_confidence=_confidence("moderate"),
        clip_coverage_ratio=1.0,
        matched_clip_seconds=12.5,
        assessment_confidence=_confidence("low"),
        matches=[{"match_id": "m1"}],
        edit_analysis={"counts": {"cuts": 2, "inserted_segments": None}},
        findings=[{"finding_id": "f1"}],
        limitations=["alignment gap"],
    )
    kwargs.update(overrides)
    return kwargs


def _no_match_kwargs():
    return _matched_kwargs(
        outcome="no_verified_match",
        source_post=None,
        source_transcript=None,
        source_playback_url=None,
        match_confidence=None,
        clip_coverage_ratio=None,
        matched_clip_seconds=None,
        assessment_confidence=None,
        matches=[],
        edit_analysis=None,
        findings=[],
        limitations=["No verified source was found."],
    )


def test_matched_report_labels():
    model = build_display_model(**_matched_kwargs())

    assert model.source_match.label == "Source-match confidence"
    assert model.source_match.value == "Moderate"
    assert model.clip_coverage.label == "Excerpt aligned"
    assert model.clip_coverage.value == "100%"
    assert model.clip_coverage.description == "12.5 seconds of the submitted excerpt aligned to the selected source."
    assert model.context_assessment.label == "Context-assessment confidence"
    assert model.context_assessment.value == "Low"
    assert model.confidence_note == CONFIDENCE_NOTE
    assert model.no_source_message is None
    assert model.context_unavailable_message is None
    assert model.source_panel.media_playback_url == "https://cdn.example.com/src.mp4"
    assert model.submitted_panel.media_playback_url is None


def test_no_verified_match_hides_confidence_blocks():
    model = build_display_model(**_no_match_kwargs())

    assert model.source_match is None
    assert model.clip_coverage is None
    assert model.context_assessment is None
    assert model.source_panel is None
    assert model.no_source_message == NO_SOURCE_MESSAGE
    assert model.submitted_panel.transcript["sentences"][0]["sentence_id"] == "s1"
    assert model.limitations == ["No verified source was found."]


@pytest.mark.parametrize(
    "overrides",
    [{"assessment_confidence": _confidence("not_assessed")}, {"assessment_confidence": None, "findings": []}],
)
def test_context_not_assessed(overrides):
    model = build_display_model(**_matched_kwargs(**overrides))

    assert model.context_assessment.value == "Not assessed"
    assert model.context_unavailable_message == CONTEXT_UNAVAILABLE_MESSAGE


def test_null_poster_counts_display_unavailable_not_zero():
    model = build_display_model(**_matched_kwargs())

    source = model.poster_info.source.model_dump()
    assert source == {"followers_count": "Unavailable", "following_count": "Unavailable", "posts_count": "Unavailable"}
    assert 0 not in source.values()
    assert model.poster_info.submitted.followers_count == 1200


def test_null_edit_counts_display_not_assessed_not_zero():
    model = build_display_model(**_matched_kwargs())

    assert model.edit_analysis["counts"] == {"cuts": 2, "inserted_segments": "Not assessed"}


def test_follower_count_does_not_change_confidence():
    big = _matched_kwargs(
        submitted_post={"post_id": "p_sub", "author": {"followers_count": 10_000_000}},
    )
    assert build_display_model(**big).source_match == build_display_model(**_matched_kwargs()).source_match


@pytest.mark.parametrize("kwargs", [_matched_kwargs(), _no_match_kwargs()])
def test_never_claims_accuracy_or_numeric_confidence(kwargs):
    model = build_display_model(**kwargs)
    payload = model.model_dump_json().lower()

    assert "100% accurate" not in payload
    for block in (model.source_match, model.context_assessment):
        if block is not None:
            assert "%" not in block.value


def test_rejects_out_of_range_clip_ratio():
    with pytest.raises(QuantroError) as caught:
        build_display_model(**_matched_kwargs(clip_coverage_ratio=1.5))

    assert caught.value.code is ErrorCode.PROCESSING_FAILED
    assert caught.value.stage is Stage.DISPLAY_REPORT


def test_rejects_unknown_confidence_level():
    with pytest.raises(QuantroError):
        build_display_model(**_matched_kwargs(match_confidence=_confidence("85%")))
