"""Offline vertical slice: blocked YouTube input to persisted source report."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from youtube_transcript_api import RequestBlocked

from backend.services.reporting import remote_youtube
from backend.services.resolution import (
    candidates,
    context_video,
    download,
    media,
    run,
    sentence_context,
    transcribe,
)
from backend.services.shared.errors import ErrorCode, QuantroError, Stage
from backend.services.storage.tiger import TigerStore

CLIP_ID = "R0ukW4AzcSs"
SOURCE_ID = "xm-eaneh31E"
CLIP_URL = f"https://www.youtube.com/shorts/{CLIP_ID}"
WORDS = "Nobody is going to give you anything that you have not earned through your own hard work."


@pytest.mark.parametrize("audio_error", ["auth_required", "download_failed"])
@pytest.mark.parametrize("correct_source", [True, False])
def test_blocked_input_preserves_actual_matching_and_saved_report(monkeypatch, tmp_path, audio_error, correct_source):
    monkeypatch.setenv("SEARCH_PROVIDER", "youtube")
    monkeypatch.setenv("GROQ_API_KEY", "offline-test-unused")
    monkeypatch.setenv("RESOLUTION_DOWNLOAD_VIDEO", "true")
    monkeypatch.setenv("CONTEXT_VIDEO_ENABLED", "true")
    monkeypatch.setenv("REPORT_BASE_URL", "https://quantro-ten.vercel.app")
    monkeypatch.setattr(transcribe, "_CAPTION_CACHE_DIR", tmp_path / "captions")

    source_words = WORDS if correct_source else (
        "The observatory measured hydrogen emissions from distant galaxies before "
        "the expedition returned with unprecedented photographic evidence."
    )
    source_captions = {
        "title": "Full commencement address", "source": "youtube_captions", "duration": 300.0,
        "transcript": "Graduates face responsibility. " + source_words + " Others deserve a fair chance.",
        "segments": [
            {"text": "Graduates now face the challenge of building their own future.", "start": 80.0, "end": 95.0},
            {"text": source_words, "start": 100.0, "end": 112.0},
            {"text": "We must also make sure that others have a fair chance.", "start": 115.0, "end": 130.0},
        ],
    }

    def native_captions(video_id):
        if video_id == CLIP_ID:
            raise RequestBlocked(video_id)
        assert video_id == SOURCE_ID
        return source_captions

    monkeypatch.setattr(transcribe, "_fetch_youtube_captions_uncached", native_captions)
    monkeypatch.setattr(transcribe, "_fetch_hosted_captions", lambda *args, **kwargs: None)
    monkeypatch.setattr(media, "download_short_media", AsyncMock(side_effect=download.MediaDownloadError(
        audio_error, "Offline native recording unavailable", CLIP_URL, "youtube")))
    audio_download = AsyncMock(side_effect=download.MediaDownloadError(
        audio_error, "Offline audio unavailable", CLIP_URL, "youtube"))
    monkeypatch.setattr(transcribe, "download_short_media", audio_download)

    native_reader = []
    def gemini_response(job_id, url, **kwargs):
        native_reader.append((job_id, url, kwargs))
        return SimpleNamespace(submitted=SimpleNamespace(
            post=SimpleNamespace(title="Commencement excerpt"),
            transcript=SimpleNamespace(sentences=[SimpleNamespace(text=WORDS, start_seconds=0.0, end_seconds=12.0)])))
    monkeypatch.setattr(remote_youtube, "build_remote_youtube_report", gemini_response)
    monkeypatch.setattr(candidates, "extract_search_metadata", AsyncMock(return_value={
        "speaker_and_event": "commencement", "unique_phrase": "nobody is going to give you anything that",
        "query": "commencement speech", "title_query": "commencement excerpt"}))
    monkeypatch.setattr(candidates, "_search", AsyncMock(return_value=[
        candidates.Candidate(SOURCE_ID, "Full commencement address", 300.0)]))
    monkeypatch.setattr(sentence_context, "model_counts", lambda *args: None)
    context_download = AsyncMock(side_effect=download.MediaDownloadError(
        "auth_required", "Offline source recording unavailable", f"https://www.youtube.com/watch?v={SOURCE_ID}", "youtube"))
    monkeypatch.setattr(context_video, "download_short_media", context_download)
    monkeypatch.setattr(context_video.TigerDataPostStore, "from_env", lambda: SimpleNamespace(
        read_media=lambda media_id: None, close=lambda: None))

    def language_unavailable(*args):
        raise QuantroError(ErrorCode.PROCESSING_FAILED, Stage.COMPARE_CONTEXT_AND_CAPTION)
    monkeypatch.setattr(run, "compare_clip", language_unavailable)
    monkeypatch.setattr(run, "interpret_clip", lambda *args: "The excerpt encourages graduates to earn their success.")

    store = TigerStore(f"sqlite:///{tmp_path / 'pipeline.db'}")
    store.ensure_schema()
    try:
        result = run.run_analysis("job_fallback_e2e", CLIP_URL, store=store)
        saved = store.get_report(result.report_id)
        assert saved is not None and saved["context_video"] is None
        assert len(native_reader) == 1
        assert native_reader[0][1] == CLIP_URL
        assert audio_download.await_count == 1
        assert saved["submitted"]["transcript"]["status"] == "partial"
        assert saved["submitted"]["transcript"]["sentences"][0]["text"] == WORDS
        assert saved["submitted"]["post"]["media"]["duration_seconds"] is None
        if correct_source:
            assert result.outcome.value == "matched"
            assert saved["source"]["post"]["post_url"].endswith(SOURCE_ID)
            assert saved["alignment"]["status"] == "verified"
            assert saved["alignment"]["matches"][0]["source_start_seconds"] == 100.0
            assert saved["comparison"]["context_assessment"] == "not_assessed"
            assert any("could not be exported" in item for item in saved["limitations"])
            assert context_download.await_count == 1
        else:
            assert result.outcome.value == "no_verified_match"
            assert saved["source"] is None and saved["alignment"] is None
            context_download.assert_not_awaited()
        assert run.run_analysis("job_fallback_e2e", CLIP_URL, store=store) == result
        assert len(native_reader) == 1
    finally:
        store.close()
