import hashlib

from backend.services.pipeline.feature_cache import extraction_signature, media_sha256


def test_signature_tracks_model_and_options_but_excludes_secrets(monkeypatch):
    monkeypatch.setenv("WHISPER_MODEL", "small")
    first = extraction_signature({"extract_frames": False})
    monkeypatch.setenv("GEMINI_API_KEY", "private-test-value")
    assert extraction_signature({"extract_frames": False}) == first
    monkeypatch.setenv("WHISPER_MODEL", "base")
    assert extraction_signature({"extract_frames": False}) != first
    monkeypatch.setenv("WHISPER_MODEL", "small")
    assert extraction_signature({"extract_frames": True}) != first


def test_media_digest_is_file_content_not_path(tmp_path):
    first, second = tmp_path / "first.mp4", tmp_path / "other.mp4"
    first.write_bytes(b"public-media-bytes")
    second.write_bytes(first.read_bytes())
    assert (
        media_sha256(str(first))
        == media_sha256(str(second))
        == hashlib.sha256(first.read_bytes()).hexdigest()
    )
