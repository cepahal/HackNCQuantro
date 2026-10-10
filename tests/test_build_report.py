import pytest

from backend.services.reporting.build_report import merge_limitations, sanitize_report
from backend.services.reporting.errors import ErrorCode, QuantroError, Stage, normalize_exception


def test_sanitize_removes_sensitive_keys_at_every_depth():
    report = {
        "local_path": "frames/0001.jpg",
        "submitted": {
            "post": {"playback_url": None, "token": "abc", "API_KEY": "k"},
            "frames": [{"frame_id": "f1", "image_path": "frames/f1.jpg"}],
        },
        "credentials": {"user": "x"},
    }

    cleaned = sanitize_report(report)

    assert cleaned == {
        "submitted": {
            "post": {"playback_url": None},
            "frames": [{"frame_id": "f1"}],
        },
    }
    assert "local_path" in report


def test_sanitize_preserves_playback_url():
    report = {"post": {"playback_url": "https://cdn.example.com/v.mp4"}}
    assert sanitize_report(report) == report


@pytest.mark.parametrize("value", ["/tmp/quantro/clip.mp4", "file:///Users/me/clip.mp4"])
def test_sanitize_rejects_local_file_references(value):
    with pytest.raises(QuantroError) as caught:
        sanitize_report({"post": {"caption": "ok", "notes": [value]}})

    assert caught.value.code is ErrorCode.PROCESSING_FAILED
    assert caught.value.stage is Stage.BUILD_REPORT
    assert value not in caught.value.message


def test_merge_limitations_preserves_order_and_dedupes():
    merged = merge_limitations(
        ["alignment gap", "low audio"],
        ["low audio", "caption unclear"],
        None,
        ["rate limited"],
    )
    assert merged == ["alignment gap", "low audio", "caption unclear", "rate limited"]


def test_normalize_exception_passes_through_quantro_error():
    original = QuantroError(code="INVALID_URL", stage="retrieve_post", message="Bad link")
    assert normalize_exception("build_report", original) is original


def test_normalize_exception_uses_stage_default_and_hides_raw_message():
    error = normalize_exception("search_candidates", TimeoutError("token=secret"))
    assert error.to_dict() == {
        "code": "SEARCH_FAILED",
        "stage": "search_candidates",
        "message": "The source search failed.",
        "retryable": True,
    }
