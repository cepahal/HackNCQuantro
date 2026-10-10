"""Still images stay usable without inventing a speech timeline."""

from unittest.mock import Mock

import pytest

from backend.services.processing import preprocessing as p
from backend.services.shared.errors import QuantroError, Stage
from backend.services.shared.types import MediaFeatures
from backend.services.specialist.alignment import verify_and_align


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "submitted.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9")
    return {"media_id": "still_001", "local_path": str(path), "mime_type": "image/jpeg"}


def test_jpeg_without_ocr_is_contract_valid_without_ffmpeg_or_provider(image, monkeypatch):
    monkeypatch.setattr(p, "_probe", Mock(side_effect=AssertionError("No video probe")))
    monkeypatch.setattr(p, "client_and_model", Mock(side_effect=AssertionError("No provider")))

    result = p.extract_media_features(image, {"extract_visual_text": False, "max_frames": 12})

    assert MediaFeatures.model_validate(result).model_dump(mode="json") == result
    assert result["duration_seconds"] == 0.0
    assert result["transcript"] == {"status": "unavailable", "processed_ranges": [], "sentences": []}
    assert result["frames"] == [{
        "frame_id": "still_001_frame_0001", "timestamp_seconds": 0.0,
        "image_path": image["local_path"], "image_url": None,
    }]
    assert result["visual_text"] == []
    assert result["warnings"]


def test_jpeg_ocr_returns_linked_frame_even_when_extract_frames_is_false(image, monkeypatch):
    client = Mock()
    monkeypatch.setattr(p, "client_and_model", lambda stage: (client, "test-model"))
    monkeypatch.setattr(p, "_ask", lambda *args: {"text": "The visible headline"})

    result = p.extract_media_features(image, {"extract_frames": False})

    assert result["visual_text"] == [{
        "frame_id": result["frames"][0]["frame_id"], "timestamp_seconds": 0.0,
        "text": "The visible headline",
    }]
    client.close.assert_called_once()


def test_successful_empty_jpeg_ocr_remains_valid(image, monkeypatch):
    client = Mock()
    monkeypatch.setattr(p, "client_and_model", lambda stage: (client, "test-model"))
    monkeypatch.setattr(p, "_ask", lambda *args: {"text": ""})

    result = p.extract_media_features(image)

    assert len(result["frames"]) == 1
    assert result["visual_text"] == []
    assert not any("failed" in warning for warning in result["warnings"])
    client.close.assert_called_once()


def test_jpeg_ocr_provider_failure_is_an_error(image, monkeypatch):
    client = Mock()
    monkeypatch.setattr(p, "client_and_model", lambda stage: (client, "test-model"))
    monkeypatch.setattr(p, "_ask", Mock(side_effect=QuantroError(
        "PROCESSING_FAILED", Stage.EXTRACT_MEDIA_FEATURES, retryable=True
    )))

    with pytest.raises(QuantroError) as error:
        p.extract_media_features(image)

    assert error.value.retryable
    assert error.value.stage is Stage.EXTRACT_MEDIA_FEATURES
    client.close.assert_called_once()


def test_video_ocr_provider_failure_is_an_error(image, monkeypatch, tmp_path):
    from pathlib import Path
    client = Mock()
    folder = tmp_path / "frames"
    folder.mkdir()
    monkeypatch.setattr(p, "_probe", lambda path: (1.0, False, True))
    monkeypatch.setattr(p.tempfile, "mkdtemp", lambda **kwargs: str(folder))
    monkeypatch.setattr(p, "_run", lambda args: Path(args[-1]).write_bytes(b"frame"))
    monkeypatch.setattr(p, "client_and_model", lambda stage: (client, "test-model"))
    monkeypatch.setattr(p, "_ask", Mock(side_effect=QuantroError(
        "PROCESSING_FAILED", Stage.EXTRACT_MEDIA_FEATURES, retryable=True
    )))

    with pytest.raises(QuantroError) as error:
        p.extract_media_features({**image, "mime_type": "video/mp4"}, {"max_frames": 1})

    assert error.value.retryable
    assert error.value.stage is Stage.EXTRACT_MEDIA_FEATURES
    client.close.assert_called_once()


@pytest.mark.parametrize("response", [{}, {"text": 5}])
def test_jpeg_ocr_invalid_output_is_a_safe_error(image, monkeypatch, response):
    client = Mock()
    monkeypatch.setattr(p, "client_and_model", lambda stage: (client, "test-model"))
    monkeypatch.setattr(p, "_ask", lambda *args: response)

    with pytest.raises(QuantroError) as error:
        p.extract_media_features(image)

    assert error.value.code.value == "PROCESSING_FAILED"
    assert error.value.stage is Stage.EXTRACT_MEDIA_FEATURES
    client.close.assert_called_once()


@pytest.mark.parametrize("options", [
    {"extract_frames": False, "extract_visual_text": False},
    {"max_frames": 0},
])
def test_jpeg_disabled_extraction_does_not_call_provider(image, monkeypatch, options):
    monkeypatch.setattr(p, "client_and_model", Mock(side_effect=AssertionError("No provider")))

    result = p.extract_media_features(image, options)

    assert result["frames"] == []
    assert result["visual_text"] == []


def test_jpeg_cannot_be_verified_by_transcript_alignment(image):
    features = p.extract_media_features(image, {"extract_visual_text": False})
    candidate = {**features, "media_id": "source_still"}

    result = verify_and_align(features, candidate)

    assert result["status"] == "not_assessed"
    assert result["matches"] == []
    assert result["match_quality"]["matched_clip_seconds"] is None
    assert result["match_quality"]["clip_coverage_ratio"] is None
    assert all(value is None for value in result["edit_analysis"]["counts"].values())


def test_processed_silence_is_not_assessed_without_speech_or_other_matching():
    clip = {
        "media_id": "silent_clip", "duration_seconds": 10.0,
        "transcript": {
            "status": "complete", "processed_ranges": [{"start_seconds": 0.0, "end_seconds": 10.0}],
            "sentences": [],
        }, "warnings": [],
    }

    result = verify_and_align(clip, {**clip, "media_id": "silent_source"})

    assert result["status"] == "not_assessed"
    assert result["match_quality"]["confidence"]["level"] == "not_assessed"
    assert result["match_quality"]["methods_used"] == []
    assert all(value is None for value in result["edit_analysis"]["counts"].values())


@pytest.mark.parametrize("options", [
    {"transcript_scope": "ranges", "transcript_ranges": []},
    {"frame_ranges": [{"start_seconds": 0, "end_seconds": 1}]},
    {"max_frames": True},
    {"extract_visual_text": "yes"},
])
def test_invalid_still_image_options_have_normalized_errors(image, options):
    with pytest.raises(QuantroError) as error:
        p.extract_media_features(image, options)

    assert error.value.code.value == "PROCESSING_FAILED"
    assert error.value.stage is Stage.EXTRACT_MEDIA_FEATURES


@pytest.mark.parametrize("media,options", [
    (None, None), ({}, None), ({"media_id": ""}, None),
    ({"media_id": "x", "local_path": None}, None),
    ({"media_id": "x", "local_path": "private-path"}, []),
])
def test_malformed_extraction_inputs_have_safe_normalized_errors(media, options):
    with pytest.raises(QuantroError) as error:
        p.extract_media_features(media, options)

    assert error.value.code.value == "PROCESSING_FAILED"
    assert "private-path" not in error.value.message


def test_invalid_probe_metadata_does_not_leak_key_error(image, monkeypatch):
    monkeypatch.setattr(p, "_probe", Mock(side_effect=KeyError("duration")))
    video = {**image, "mime_type": "video/mp4"}

    with pytest.raises(QuantroError) as error:
        p.extract_media_features(video)

    assert error.value.stage is Stage.EXTRACT_MEDIA_FEATURES
    assert error.value.code.value == "PROCESSING_FAILED"


def test_non_jpeg_file_is_not_returned_as_an_image(image):
    from pathlib import Path
    Path(image["local_path"]).write_bytes(b"not an image")

    with pytest.raises(QuantroError) as error:
        p.extract_media_features(image, {"extract_visual_text": False})

    assert error.value.code.value == "PROCESSING_FAILED"
