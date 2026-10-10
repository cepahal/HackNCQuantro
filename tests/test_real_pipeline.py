"""Opt-in live video gate; no synthetic provider or fixture media."""
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from backend.services.external_apis.build_search_queries import build_search_queries
from backend.services.external_apis.retrieve_candidate_media import (
    retrieve_candidate_media,
)
from backend.services.external_apis.retrieve_post import retrieve_post
from backend.services.external_apis.search_candidates import search_candidates
from backend.services.processing.preprocessing import extract_media_features
from backend.services.specialist.alignment import verify_and_align

pytestmark = pytest.mark.skipif(os.getenv("NETWORK") != "1", reason="requires network")
URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
OPTIONS = {"transcript_scope": "full", "transcript_ranges": [], "extract_frames": True,
           "max_frames": 3, "frame_ranges": [], "extract_visual_text": True}

@pytest.fixture(scope="module")
def live_post():
    load_dotenv(Path(__file__).resolve().parents[1] / ".env.local", override=False)
    return retrieve_post(URL)

@pytest.fixture(scope="module")
def live_features(live_post):
    return extract_media_features(live_post.media, OPTIONS)

@pytest.fixture(scope="module")
def live_candidates(live_features, live_post):
    queries = build_search_queries(live_features, live_post)
    print("PASS build_search_queries", queries["queries"])
    return search_candidates(queries, 3)

def test_retrieve_post_real_video(live_post):
    media = live_post.media
    assert Path(media.local_path).is_file()
    assert Path(media.local_path).stat().st_size > 10000
    assert 15 < media.duration_seconds < 25
    assert media.width > 0 and media.height > 0
    print("local_path:", media.local_path)
    print("exists:", Path(media.local_path).exists(), "size:", Path(media.local_path).stat().st_size)
    print("fps:", media.fps, "has_audio:", media.has_audio, "title:", live_post.title)
    print("unavailable_fields:", live_post.retrieval.unavailable_fields)
    print("PASS retrieve_post", media.duration_seconds, media.width, media.height)

def test_extract_media_features_real_video(live_features):
    assert live_features.transcript.status in ("complete", "partial")
    assert live_features.transcript.sentences
    assert len(live_features.frames) == 3
    assert all(Path(frame.image_path).is_file() for frame in live_features.frames)
    text = " ".join(sentence.text for sentence in live_features.transcript.sentences).lower()
    assert "elephant" in text
    for sentence in live_features.transcript.sentences:
        print(sentence.start_seconds, sentence.end_seconds, repr(sentence.text))
    for frame in live_features.frames:
        print("frame:", frame.image_path, Path(frame.image_path).exists())
    print("visual_text:", len(live_features.visual_text), "warnings:", live_features.warnings)
    print("PASS extract_media_features", len(live_features.transcript.sentences), len(live_features.frames))

def test_search_candidates_real(live_candidates):
    assert live_candidates["candidates"], "Successful web discovery must return a source for this demo."
    assert all(candidate["url"].startswith("https://") for candidate in live_candidates["candidates"])
    for candidate in live_candidates["candidates"]:
        print(candidate["url"], "|", candidate["title"])
    print("warnings:", live_candidates["warnings"])
    print("PASS search_candidates", len(live_candidates["candidates"]))

def test_verify_and_align_real(live_features, live_candidates):
    candidate = next((item for item in live_candidates["candidates"] if "youtube.com/watch" in item["url"]), None)
    assert candidate is not None, "Search must discover a downloadable source video."
    post = retrieve_candidate_media(candidate)
    assert Path(post.media.local_path).is_file()
    features = extract_media_features(post.media, OPTIONS)
    alignment = verify_and_align(live_features, features)
    assert alignment["status"] in ("verified", "no_verified_match", "not_assessed")
    assert alignment["status"] != "not_assessed", "Source transcription must be available."
    print("PASS verify_and_align", alignment["status"], alignment["match_quality"]["confidence"]["level"])
    print("ALL PASS")
