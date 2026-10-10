"""Local adapters preserve measured ranges and explicit unavailable results."""
from types import SimpleNamespace

import pytest

from backend.services.processing import preprocessing as p
from backend.services.shared.types import MediaFeatures


@pytest.fixture
def media(tmp_path, monkeypatch):
    path = tmp_path / "recording.mp4"
    path.write_bytes(b"unit-fixture")
    monkeypatch.setattr(p, "_probe", lambda path: (20.0, True, False))
    monkeypatch.setattr(p, "_run", lambda args: b"")
    monkeypatch.setattr(p, "client_and_model", lambda *args: pytest.fail("Local extraction called Gemini"))
    return {"media_id": "local_unit", "local_path": str(path)}


def test_local_model_return_and_empty_ranges_fallback(media, monkeypatch):
    monkeypatch.setattr(p, "_local_transcript", lambda path: {
        "language": "en", "sentences": [{"start_seconds": 1, "end_seconds": 3, "text": "Actual adapter fixture"}]})
    result = p.extract_media_features(media, {"transcript_scope": "ranges", "transcript_ranges": [], "max_frames": 0})
    assert isinstance(result, MediaFeatures)
    assert result.transcript.status.value == "complete"
    assert result.transcript.processed_ranges[0].end_seconds == 20
    assert result.transcript.sentences[0].speaker_id is None


def test_local_range_timestamps_are_absolute(media, monkeypatch):
    monkeypatch.setattr(p, "_local_transcript", lambda path: {
        "language": "en", "sentences": [{"start_seconds": 1, "end_seconds": 3, "text": "Range speech"}]})
    result = p.extract_media_features(media, {"transcript_scope": "ranges",
        "transcript_ranges": [{"start_seconds": 10, "end_seconds": 15}], "max_frames": 0})
    assert result.transcript.status.value == "partial"
    assert result.transcript.sentences[0].start_seconds == 11
    assert result.transcript.sentences[0].end_seconds == 13


def test_local_failure_is_unavailable_with_warning(media, monkeypatch):
    def fail(path):
        raise RuntimeError("Private provider details")
    monkeypatch.setattr(p, "_local_transcript", fail)
    result = p.extract_media_features(media, {"max_frames": 0})
    assert result.transcript.status.value == "unavailable"
    assert result.transcript.processed_ranges == []
    assert result.transcript.sentences == []
    assert any("transcription unavailable" in warning for warning in result.warnings)
    assert "Private" not in " ".join(result.warnings)


def test_lazy_whisper_segments_are_materialized(monkeypatch, tmp_path):
    model = SimpleNamespace(transcribe=lambda *args, **kwargs: (
        iter([SimpleNamespace(start=0.2, end=1.4, text=" Real words ")]), SimpleNamespace(language="en", duration=2.0)))
    monkeypatch.setattr(p, "_whisper_model", lambda: model)
    result = p._local_transcript(tmp_path / "audio.wav")
    assert result == {"language": "en", "sentences": [{"start_seconds": 0.2, "end_seconds": 1.4, "text": "Real words"}]}


def test_ocr_failure_keeps_frame_without_fabricated_text(monkeypatch):
    monkeypatch.setattr(p, "_local_ocr", lambda path: (_ for _ in ()).throw(RuntimeError("private")))
    result = {"visual_text": [], "warnings": []}
    p._append_local_text(result, {"frame_id": "frame_1", "timestamp_seconds": 2}, None)
    assert result["visual_text"] == []
    assert result["warnings"] == ["Local OCR unavailable for frame frame_1."]


def test_whisper_padding_is_bounded_by_decoded_audio(monkeypatch, tmp_path):
    model = SimpleNamespace(transcribe=lambda *args, **kwargs: (
        iter([SimpleNamespace(start=0.0, end=10.0, text="Actual short speech")]),
        SimpleNamespace(language="en", duration=4.5)))
    monkeypatch.setattr(p, "_whisper_model", lambda: model)
    result = p._local_transcript(tmp_path / "audio.wav")
    assert result["sentences"][0]["end_seconds"] == 4.5
