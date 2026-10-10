"""Canonical Whisper adapters preserve measured ranges and fail explicitly."""
from types import SimpleNamespace

import pytest

from backend.services.processing import preprocessing as p
from backend.services.shared.errors import QuantroError
from backend.services.shared.types import MediaFeatures
from backend.services.specialist import transcription as t


@pytest.fixture
def media(tmp_path, monkeypatch):
    path = tmp_path / "recording.mp4"
    path.write_bytes(b"unit-fixture")
    monkeypatch.setattr(p, "_probe", lambda path: (20.0, True, False))
    monkeypatch.setattr(p, "_run", lambda args: b"")
    monkeypatch.setattr(t, "provider", lambda: "whisper")
    monkeypatch.setattr(p, "client_and_model", lambda *args: pytest.fail("Local speech called Gemini"))
    return {"media_id": "local_unit", "local_path": str(path)}


def test_local_full_recording_validates_shared_model(media, monkeypatch):
    monkeypatch.setattr(t, "transcribe", lambda path: {
        "language": "en", "sentences": [{"start_seconds": 1, "end_seconds": 3, "text": "Actual adapter fixture"}]})
    result = p.extract_media_features(media, {"transcript_scope": "full", "transcript_ranges": [], "max_frames": 0})
    validated = MediaFeatures.model_validate(result)
    assert validated.transcript.status.value == "complete"
    assert validated.transcript.processed_ranges[0].end_seconds == 20
    assert validated.transcript.sentences[0].speaker_id is None


def test_ranged_transcription_requires_explicit_ranges(media):
    with pytest.raises(QuantroError) as caught:
        p.extract_media_features(media, {"transcript_scope": "ranges", "transcript_ranges": [], "max_frames": 0})
    assert caught.value.code.value == "PROCESSING_FAILED"


def test_local_range_timestamps_are_absolute(media, monkeypatch):
    monkeypatch.setattr(t, "transcribe", lambda path: {
        "language": "en", "sentences": [{"start_seconds": 1, "end_seconds": 3, "text": "Range speech"}]})
    result = p.extract_media_features(media, {"transcript_scope": "ranges",
        "transcript_ranges": [{"start_seconds": 10, "end_seconds": 15}], "max_frames": 0})
    assert result["transcript"]["status"] == "partial"
    assert result["transcript"]["sentences"][0]["start_seconds"] == 11
    assert result["transcript"]["sentences"][0]["end_seconds"] == 13


def test_local_failure_raises_without_exposing_provider_details(media, monkeypatch):
    def fail(path):
        raise RuntimeError("Private provider details")
    monkeypatch.setattr(t, "transcribe", fail)
    with pytest.raises(QuantroError) as caught:
        p.extract_media_features(media, {"max_frames": 0})
    assert caught.value.code.value == "TRANSCRIPTION_FAILED"
    assert "Private" not in str(caught.value)


def test_lazy_whisper_segments_are_materialized(monkeypatch, tmp_path):
    model = SimpleNamespace(transcribe=lambda *args, **kwargs: (
        iter([SimpleNamespace(start=0.2, end=1.4, text=" Real words ")]), SimpleNamespace(language="en", duration=2.0)))
    monkeypatch.setattr(t, "_model", lambda name: model)
    (tmp_path / "audio.wav").write_bytes(b"lazy segment audio fixture")
    result = t.transcribe(tmp_path / "audio.wav")
    assert result == {"language": "en", "sentences": [
        {"start_seconds": 0.2, "end_seconds": 1.4, "text": "Real words", "words": []}]}


def test_whisper_padding_is_bounded_by_decoded_audio(monkeypatch, tmp_path):
    word = SimpleNamespace(start=0.0, end=10.0, word="Actual short speech")
    model = SimpleNamespace(transcribe=lambda *args, **kwargs: (
        iter([SimpleNamespace(start=0.0, end=10.0, text="Actual short speech", words=[word]),
              SimpleNamespace(start=5.0, end=10.0, text="Padded segment", words=[])]),
        SimpleNamespace(language="en", duration=4.5)))
    monkeypatch.setattr(t, "_model", lambda name: model)
    (tmp_path / "audio.wav").write_bytes(b"bounded padding audio fixture")
    result = t.transcribe(tmp_path / "audio.wav")
    assert len(result["sentences"]) == 1
    assert result["sentences"][0]["end_seconds"] == 4.5
    assert result["sentences"][0]["words"][0]["end_seconds"] == 4.5
