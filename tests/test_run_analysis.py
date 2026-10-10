import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import backend.services.pipeline.run_ingest as ingest_module
import backend.services.reporting.run_analysis as analysis_module
import backend.services.reporting.send_summary as send_summary_module
from backend.main import app
from backend.routes.common import get_tiger_store
from backend.services.reporting.display_report import display_report
from backend.services.reporting.send_summary import InMemoryDeliveryTracker, SendReceipt
from backend.services.shared.errors import QuantroError
from backend.services.storage.tiger import TigerStore

SHARED = "Officials confirmed the bridge closure will last through next spring season."
CLIP_URL = "https://www.youtube.com/watch?v=clip"


@pytest.fixture
def store(tmp_path):
    tiger = TigerStore(f"sqlite:///{tmp_path / 'tiger.db'}")
    tiger.ensure_schema()
    yield tiger
    tiger.close()


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setenv("REPORT_BASE_URL", "https://quantro.test")
    app.dependency_overrides[get_tiger_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


def _author(followers):
    return {"id": None, "handle": "@someone", "display_name": None, "profile_url": None, "bio": None,
            "verified": None, "followers_count": followers, "following_count": None, "posts_count": None}


def _post(media_id, url, candidate_id=None, followers=None, caption=None):
    return {
        "post_id": f"post_{media_id}", "candidate_id": candidate_id, "post_url": url,
        "platform": "youtube", "title": "Clip", "caption": caption, "author": _author(followers),
        "engagement": {"views_count": 10, "likes_count": None, "comments_count": None, "shares_count": None},
        "published_at": "2026-10-01T12:00:00Z",
        "media": {"media_id": media_id, "local_path": f"/tmp/quantro-{media_id}/media.mp4",
                  "mime_type": "video/mp4", "playback_url": f"https://cdn.example/{media_id}.mp4",
                  "duration_seconds": None, "width": 1280, "height": 720, "fps": 30.0, "has_audio": True},
        "retrieval": {"retrieved_at": "2026-10-10T18:00:00Z", "provider": "yt-dlp",
                      "metadata_source_urls": [url], "unavailable_fields": [], "warnings": []},
    }


def _features(media_id, duration, sentences):
    return {
        "media_id": media_id, "duration_seconds": duration, "language": "en",
        "transcript": {
            "status": "complete", "processed_ranges": [{"start_seconds": 0.0, "end_seconds": duration}],
            "sentences": [{"sentence_id": f"{media_id}_sentence_{i:04d}", "start_seconds": start,
                           "end_seconds": end, "text": text, "speaker_id": None}
                          for i, (start, end, text) in enumerate(sentences, start=1)],
        },
        "frames": [{"frame_id": f"{media_id}_frame_0001", "timestamp_seconds": 1.0,
                    "image_path": f"/tmp/quantro_frames_{media_id}/frame_0001.jpg", "image_url": None}],
        "visual_text": [], "warnings": [],
    }


CLIP_FEATURES = _features("clip_media", 10.0, [(0.5, 9.5, SHARED)])


def _source_features(media_id):
    return _features(media_id, 120.0, [(0.0, 5.0, "Good evening and welcome to the program."),
                                       (40.0, 45.0, SHARED),
                                       (60.0, 65.0, "The repairs are expected to finish by summer.")])


def _candidate(n):
    return {"candidate_id": f"cand_{n}", "url": f"https://www.youtube.com/watch?v=src{n}"}


def _comparison(context, caption):
    sentence = context["windows"][0]["sentences"][-1]
    return {
        "context_assessment": "material_context_missing",
        "caption_assessment": "contradicted_by_context",
        "assessment_confidence": {"level": "moderate", "method": "llm_rubric_v1",
                                  "basis": ["Surrounding context says the closure is temporary."],
                                  "limitations": ["Comparison uses transcript text only."]},
        "summary": "The clip omits that the closure is temporary.",
        "findings": [{"finding_id": "finding_001", "type": "omitted_context",
                      "explanation": "The source says repairs finish by summer.",
                      "evidence": [{"media_id": context["source_media_id"], "sentence_ids": [sentence["sentence_id"]],
                                    "start_seconds": sentence["start_seconds"], "end_seconds": sentence["end_seconds"],
                                    "quote": sentence["text"]}]}],
        "limitations": ["Comparison uses transcript text only."],
    }


class FakeProvider:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    def send(self, conversation_id, text):
        if self.fail:
            raise ConnectionError("bridge down")
        self.sent.append((conversation_id, text))
        return SendReceipt(accepted=True, message_id=f"msg_{len(self.sent)}")


@pytest.fixture
def upstream(monkeypatch):
    state = {
        "candidates": [_candidate(1)],
        "search_error": None,
        "failing_candidates": set(),
        "source_followers": None,
        "provider": FakeProvider(),
        "compare_calls": [],
    }

    def fake_retrieve_post(url):
        return _post("clip_media", url, followers=state.get("clip_followers"), caption="The bridge is closed forever.")

    def fake_extract(media, options):
        media = media.model_dump(mode="json") if hasattr(media, "model_dump") else media
        if media["media_id"] == "clip_media":
            return CLIP_FEATURES
        return _source_features(media["media_id"])

    def fake_search(queries, limit):
        if state["search_error"]:
            raise state["search_error"]
        return {"candidates": state["candidates"], "warnings": state.get("search_warnings", [])}

    def fake_retrieve_candidate(candidate):
        if candidate["candidate_id"] in state["failing_candidates"]:
            raise ConnectionError("candidate download timed out")
        n = candidate["candidate_id"].split("_")[1]
        return _post(f"src_media_{n}", candidate["url"], candidate_id=candidate["candidate_id"],
                     followers=state["source_followers"], caption=state.get("source_caption"))

    def fake_compare(clip_features, context, caption):
        state["compare_calls"].append(caption)
        return _comparison(context, caption)

    monkeypatch.setattr(ingest_module, "retrieve_post", fake_retrieve_post)
    monkeypatch.setattr(ingest_module, "extract_media_features", fake_extract)
    monkeypatch.setattr(analysis_module, "extract_media_features", fake_extract)
    monkeypatch.setattr(analysis_module, "build_search_queries",
                        lambda features, post: {"queries": [SHARED], "warnings": []})
    monkeypatch.setattr(analysis_module, "search_candidates", fake_search)
    monkeypatch.setattr(analysis_module, "retrieve_candidate_media", fake_retrieve_candidate)
    monkeypatch.setattr(analysis_module, "compare_context_and_caption", fake_compare)
    monkeypatch.setattr(send_summary_module, "get_messaging_provider", lambda: state["provider"])
    tracker = InMemoryDeliveryTracker()
    monkeypatch.setattr(send_summary_module, "get_delivery_tracker", lambda: tracker)
    return state


def _run(client, job_id, conversation_id="conv_1"):
    return client.post(f"/analyze/{job_id}/run", json={"url": CLIP_URL, "conversation_id": conversation_id})


def test_matched_end_to_end(client, store, upstream):
    response = _run(client, "job_match")

    assert response.status_code == 200
    assert response.json() == {
        "job_id": "job_match", "outcome": "matched", "report_id": "report_job_match",
        "report_url": "https://quantro.test/reports/report_job_match",
        "delivery_status": "accepted", "delivery_warning": None, "warnings": [],
    }
    report = client.get("/reports/report_job_match").json()
    assert report["schema_version"] == "2.1"
    assert report["outcome"] == "matched"
    assert report["source"]["post"]["candidate_id"] == "cand_1"
    assert report["alignment"]["status"] == "verified"
    assert report["alignment"]["match_quality"]["confidence"]["level"] == "moderate"
    assert report["context"]["windows"][0]["sentences"][-1]["text"].startswith("The repairs")
    assert report["summary"] == "The clip omits that the closure is temporary."
    assert store.get_job("job_match")["status"] == "completed"
    assert upstream["compare_calls"] == [None]
    assert upstream["provider"].sent == [
        ("conv_1", ("The clip omits that the closure is temporary. "
                   "View the evidence: https://quantro.test/reports/report_job_match"))
    ]


def test_empty_search_produces_no_verified_match(client, store, upstream):
    upstream["candidates"] = []

    response = _run(client, "job_empty")

    assert response.status_code == 200
    assert response.json()["outcome"] == "no_verified_match"
    report = client.get("/reports/report_job_empty").json()
    assert report["source"] is None and report["alignment"] is None and report["comparison"] is None
    assert report["limitations"][0] == "No verified source was found for the submitted clip."
    assert upstream["compare_calls"] == []
    assert upstream["provider"].sent[0][1].startswith("No verified source was found for this clip.")


def test_search_failure_is_an_error_not_a_no_match(client, store, upstream):
    upstream["search_error"] = RuntimeError("search provider 503")

    response = _run(client, "job_search")

    assert response.status_code == 500
    assert response.json() == {"error": {"code": "SEARCH_FAILED", "stage": "search_candidates",
                                         "message": "The source search failed.", "retryable": True}}
    assert store.get_report("report_job_search") is None
    job = store.get_job("job_search")
    assert (job["status"], job["error_code"], job["error_stage"]) == ("failed", "SEARCH_FAILED", "search_candidates")
    assert upstream["provider"].sent == []


def test_failing_candidate_is_skipped_and_disclosed(client, store, upstream):
    upstream["candidates"] = [_candidate(1), _candidate(2)]
    upstream["failing_candidates"] = {"cand_1"}

    response = _run(client, "job_skip")

    body = response.json()
    assert response.status_code == 200
    assert body["outcome"] == "matched"
    assert body["warnings"] == ["Candidate cand_1 was skipped: The media could not be retrieved."]
    report = client.get("/reports/report_job_skip").json()
    assert report["source"]["post"]["candidate_id"] == "cand_2"
    assert "Candidate cand_1 was skipped: The media could not be retrieved." in report["limitations"]
    assert "1 of 2 candidate sources could not be assessed; source coverage is limited." in report["limitations"]


def test_delivery_failure_keeps_the_saved_report(client, store, upstream):
    upstream["provider"] = FakeProvider(fail=True)

    response = _run(client, "job_deliver")

    body = response.json()
    assert response.status_code == 200
    assert body["delivery_status"] == "failed"
    assert body["delivery_warning"] == "Summary delivery could not be confirmed. Verify its outcome before another send."
    assert store.get_job("job_deliver")["status"] == "completed"
    stored = client.get("/reports/report_job_deliver")
    assert stored.status_code == 200
    assert stored.json()["outcome"] == "matched"


def test_report_json_never_exposes_local_paths(client, store, upstream):
    _run(client, "job_paths")

    served = client.get("/reports/report_job_paths").text
    persisted = json.dumps(store.get_report("report_job_paths"))
    for text in (served, persisted):
        assert "/tmp/" not in text
        assert "local_path" not in text
        assert "image_path" not in text
    assert "https://cdn.example/src_media_1.mp4" in served


def test_follower_count_does_not_change_confidence(client, store, upstream):
    upstream["source_followers"] = 10_000_000
    upstream["clip_followers"] = 10_000_000
    _run(client, "job_famous")
    upstream["source_followers"] = 0
    upstream["clip_followers"] = 0
    _run(client, "job_unknown")

    famous = client.get("/reports/report_job_famous").json()
    unknown = client.get("/reports/report_job_unknown").json()
    assert famous["source"]["post"]["author"]["followers_count"] == 10_000_000
    assert unknown["source"]["post"]["author"]["followers_count"] == 0
    for key in ("alignment", "comparison", "outcome", "limitations"):
        assert famous[key] == unknown[key]


def test_tied_sources_are_disclosed(client, store, upstream):
    upstream["candidates"] = [_candidate(1), _candidate(2)]

    _run(client, "job_tie")

    report = client.get("/reports/report_job_tie").json()
    assert report["source"]["post"]["candidate_id"] == "cand_1"
    assert "Multiple plausible sources remain." in report["limitations"]
    assert ("Another plausible source was not selected: https://www.youtube.com/watch?v=src2"
            in report["limitations"])


def test_display_report_renders_a_matched_pipeline_report(client, store, upstream):
    _run(client, "job_display")

    model = display_report(client.get("/reports/report_job_display").json())

    assert model.source_match.value == "Moderate"
    assert model.clip_coverage.value == "90%"
    assert model.context_assessment.value == "Moderate"
    assert model.no_source_message is None
    assert model.source_panel.media_playback_url == "https://cdn.example/src_media_1.mp4"
    assert model.poster_info.submitted.followers_count == "Unavailable"
    assert [match["match_id"] for match in model.matches] == ["match_001"]
    assert model.findings[0]["finding_id"] == "finding_001"


def test_display_report_renders_no_verified_match(client, store, upstream):
    upstream["candidates"] = []
    _run(client, "job_display_none")

    model = display_report(client.get("/reports/report_job_display_none").json())

    assert model.no_source_message == "No verified source found"
    assert model.source_match is None and model.source_panel is None
    assert model.limitations[0] == "No verified source was found for the submitted clip."


def test_display_report_rejects_non_report_input():
    with pytest.raises(QuantroError) as caught:
        display_report({"job_id": "x"})
    assert caught.value.stage.value == "display_report"


def test_unknown_report_returns_404(client):
    assert client.get("/reports/report_missing").status_code == 404


def test_submitted_post_is_never_its_own_source(client, store, upstream):
    upstream["candidates"] = [{**_candidate(1), "url": CLIP_URL + "#same-post"}]
    response = _run(client, "job_self", conversation_id=None)
    assert response.status_code == 200
    assert response.json()["outcome"] == "no_verified_match"
    assert "excluded from its own" in response.json()["warnings"][0]
    report = client.get("/reports/report_job_self").json()
    assert report["source"] is None
    assert upstream["compare_calls"] == []


@pytest.mark.parametrize("submitted,candidate", [
    ("https://www.instagram.com/reel/DOtmnryAC-R/?psln=tracking", "https://www.instagram.com/reel/DOtmnryAC-R/"),
    ("https://instagram.com/reel/DOtmnryAC-R/?igsh=tracking", "https://www.instagram.com/p/DOtmnryAC-R/?utm_source=search"),
    ("https://m.instagram.com/p/DOtmnryAC-R/", "https://instagram.com/tv/DOtmnryAC-R/#shared"),
    ("https://instagram.com/reels/DOtmnryAC-R/", "https://www.instagram.com/reel/DOtmnryAC-R"),
    ("https://www.youtube.com/watch?v=jNQXAC9IVRw&si=tracking", "https://youtu.be/jNQXAC9IVRw?t=1"),
    ("https://m.youtube.com/watch?v=jNQXAC9IVRw", "https://youtube.com/shorts/jNQXAC9IVRw?feature=share"),
    ("https://music.youtube.com/watch?v=jNQXAC9IVRw&list=playlist", "https://www.youtube.com/embed/jNQXAC9IVRw"),
    ("https://youtu.be/jNQXAC9IVRw", "https://www.youtube.com/live/jNQXAC9IVRw?si=tracking"),
])
def test_same_platform_post_aliases_are_excluded_before_download(client, store, upstream, monkeypatch, submitted, candidate):
    upstream["candidates"] = [{**_candidate(1), "url": candidate}]
    retrieve = Mock(wraps=analysis_module.retrieve_candidate_media)
    monkeypatch.setattr(analysis_module, "retrieve_candidate_media", retrieve)

    response = client.post("/analyze/job_same_post_alias/run", json={"url": submitted})

    assert response.status_code == 200
    assert response.json()["outcome"] == "no_verified_match"
    assert "excluded from its own" in response.json()["warnings"][0]
    retrieve.assert_not_called()
    assert upstream["compare_calls"] == []
    assert store.get_report("report_job_same_post_alias")["source"] is None


@pytest.mark.parametrize("submitted,candidate", [
    ("https://www.instagram.com/reel/DOtmnryAC-R/?psln=tracking", "https://instagram.com/reel/OtherPost/"),
    ("https://www.youtube.com/watch?v=jNQXAC9IVRw&si=tracking", "https://youtu.be/OtherVideo"),
])
def test_different_platform_post_ids_remain_eligible(client, store, upstream, submitted, candidate):
    upstream["candidates"] = [{**_candidate(1), "url": candidate}]

    response = client.post("/analyze/job_distinct_post/run", json={"url": submitted})

    assert response.status_code == 200
    assert response.json()["outcome"] == "matched"
    assert store.get_report("report_job_distinct_post")["source"]["post"]["post_url"] == candidate


def test_candidate_resolving_to_submitted_post_is_excluded_before_extraction(client, store, upstream, monkeypatch):
    monkeypatch.setattr(analysis_module, "retrieve_candidate_media", lambda candidate: _post(
        "src_media_1", "https://youtu.be/clip?si=tracking", candidate_id=candidate["candidate_id"],
    ))
    extract = Mock(wraps=analysis_module.extract_media_features)
    align = Mock(wraps=analysis_module.verify_and_align)
    monkeypatch.setattr(analysis_module, "extract_media_features", extract)
    monkeypatch.setattr(analysis_module, "verify_and_align", align)

    response = _run(client, "job_redirect_self", conversation_id=None)

    assert response.status_code == 200
    assert response.json()["outcome"] == "no_verified_match"
    assert any("submitted post cannot be its own source" in warning for warning in response.json()["warnings"])
    extract.assert_not_called()
    align.assert_not_called()
    assert upstream["compare_calls"] == []
    assert store.get_report("report_job_redirect_self")["source"] is None


@pytest.mark.parametrize("submitted_url,candidate_url,canonical", [
    ("https://www.instagram.com/share/reel/submitted/", "https://www.instagram.com/share/reel/candidate/",
     "https://www.instagram.com/reel/DOtmnryAC-R/"),
    ("https://vm.tiktok.com/submitted/", "https://vt.tiktok.com/candidate/",
     "https://www.tiktok.com/@someone/video/1234567890"),
])
def test_resolved_metadata_post_identity_blocks_different_share_link_aliases(
    client, store, upstream, monkeypatch, submitted_url, candidate_url, canonical,
):
    upstream["candidates"] = [{**_candidate(1), "url": candidate_url}]

    def submitted(url):
        post = _post("clip_media", url)
        post["retrieval"]["metadata_source_urls"].append(canonical + "?tracking=submitted")
        return post

    def candidate(item):
        post = _post("src_media_1", item["url"], candidate_id=item["candidate_id"])
        post["retrieval"]["metadata_source_urls"].append(canonical + "?tracking=candidate")
        return post

    monkeypatch.setattr(ingest_module, "retrieve_post", submitted)
    monkeypatch.setattr(analysis_module, "retrieve_candidate_media", candidate)
    extract = Mock(wraps=analysis_module.extract_media_features)
    monkeypatch.setattr(analysis_module, "extract_media_features", extract)

    response = client.post("/analyze/job_resolved_share_alias/run", json={"url": submitted_url})

    assert response.status_code == 200
    assert response.json()["outcome"] == "no_verified_match"
    assert any("submitted post cannot be its own source" in warning for warning in response.json()["warnings"])
    extract.assert_not_called()
    assert store.get_report("report_job_resolved_share_alias")["source"] is None


def test_profile_or_malformed_metadata_does_not_make_distinct_posts_the_same(client, store, upstream, monkeypatch):
    original = analysis_module.retrieve_candidate_media

    def candidate(item):
        post = original(item)
        post["retrieval"]["metadata_source_urls"].extend(["https://www.youtube.com/@shared_author", "instagram://[bad"])
        return post

    def submitted(url):
        post = _post("clip_media", url)
        post["retrieval"]["metadata_source_urls"].extend(["https://www.youtube.com/@shared_author", "instagram://[bad"])
        return post

    monkeypatch.setattr(ingest_module, "retrieve_post", submitted)
    monkeypatch.setattr(analysis_module, "retrieve_candidate_media", candidate)

    response = _run(client, "job_shared_profile", conversation_id=None)

    assert response.status_code == 200
    assert response.json()["outcome"] == "matched"
    assert store.get_report("report_job_shared_profile")["source"]["post"]["post_url"] == _candidate(1)["url"]


def test_submitted_resolved_metadata_identity_excludes_canonical_search_candidate_before_download(
    client, store, upstream, monkeypatch,
):
    canonical = "https://www.instagram.com/reel/DOtmnryAC-R/"
    upstream["candidates"] = [{**_candidate(1), "url": canonical}]

    def submitted(url):
        post = _post("clip_media", url)
        post["retrieval"]["metadata_source_urls"].append(canonical)
        return post

    monkeypatch.setattr(ingest_module, "retrieve_post", submitted)
    retrieve = Mock(wraps=analysis_module.retrieve_candidate_media)
    monkeypatch.setattr(analysis_module, "retrieve_candidate_media", retrieve)

    response = client.post("/analyze/job_submitted_canonical/run", json={"url": "https://www.instagram.com/share/reel/original/"})

    assert response.status_code == 200
    assert response.json()["outcome"] == "no_verified_match"
    retrieve.assert_not_called()
    assert store.get_report("report_job_submitted_canonical")["source"] is None


def test_platform_identity_does_not_loosen_job_url_or_generic_media_query_identity():
    tracked = "https://www.instagram.com/reel/DOtmnryAC-R/?psln=tracking"
    clean = "https://www.instagram.com/reel/DOtmnryAC-R/"
    assert analysis_module._post_identity(tracked) == analysis_module._post_identity(clean)
    assert analysis_module._normalized_post_url(tracked) != analysis_module._normalized_post_url(clean)
    assert analysis_module._post_identity("https://cdn.example/video.mp4?clip=1") != analysis_module._post_identity("https://cdn.example/video.mp4?clip=2")
    assert analysis_module._post_identity("https://instagram.com.evil.example/reel/DOtmnryAC-R/") != analysis_module._post_identity(clean)
    assert analysis_module._post_identity("https://www.instagram.com:8080/reel/DOtmnryAC-R/") != analysis_module._post_identity(clean)


def test_cached_self_match_is_rejected_without_mutating_saved_evidence_or_sending(client, store, upstream):
    assert _run(client, "job_valid_seed", conversation_id=None).status_code == 200
    prior = store.get_report("report_job_valid_seed")
    prior["job_id"] = "job_prior_self_match"
    prior["source"]["post"]["post_url"] = "https://youtu.be/clip?si=tracking"
    store.upsert_job("job_prior_self_match", CLIP_URL, "completed")
    store.save_report("report_job_prior_self_match", "job_prior_self_match", prior)

    response = _run(client, "job_prior_self_match")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "PROCESSING_FAILED"
    assert response.json()["error"]["stage"] == "build_report"
    assert "own source" in response.json()["error"]["message"]
    assert store.get_report("report_job_prior_self_match") == prior
    assert store.get_job("job_prior_self_match")["status"] == "completed"
    assert upstream["provider"].sent == []
    assert len(upstream["compare_calls"]) == 1


@pytest.mark.parametrize("submitted_url,source_url,canonical", [
    ("https://www.instagram.com/reel/DOtmnryAC-R/?psln=tracking", "https://instagram.com/p/DOtmnryAC-R/", None),
    ("https://www.youtube.com/watch?v=clip&si=tracking", "https://youtu.be/clip?t=1", None),
    ("https://vm.tiktok.com/submitted/", "https://vt.tiktok.com/candidate/",
     "https://www.tiktok.com/@someone/video/1234567890"),
])
def test_report_boundaries_reject_self_matches_without_rewriting_saved_evidence(
    client, store, upstream, submitted_url, source_url, canonical,
):
    from backend.services.reporting.build_report import build_report
    from backend.services.shared.types import ReportV2

    assert _run(client, "job_boundary_seed", conversation_id=None).status_code == 200
    prior = store.get_report("report_job_boundary_seed")
    submitted = _post("clip_media", submitted_url)
    source = _post("src_media_1", source_url, candidate_id="cand_1")
    if canonical:
        submitted["retrieval"]["metadata_source_urls"].append(canonical + "?tracking=submitted")
        source["retrieval"]["metadata_source_urls"].append(canonical + "?tracking=candidate")

    with pytest.raises(QuantroError) as caught:
        build_report(
            "job_boundary_build", submitted, CLIP_FEATURES, source, _source_features("src_media_1"),
            prior["alignment"], prior["context"], prior["comparison"],
        )
    assert caught.value.code.value == "PROCESSING_FAILED"
    assert caught.value.stage.value == "build_report"
    assert store.get_report("report_job_boundary_build") is None

    prior["job_id"] = "job_boundary_legacy"
    for panel, post in (("submitted", submitted), ("source", source)):
        prior[panel]["post"]["post_url"] = post["post_url"]
        prior[panel]["post"]["retrieval"] = post["retrieval"]
    store.upsert_job("job_boundary_legacy", submitted_url, "completed")
    store.save_report("report_job_boundary_legacy", "job_boundary_legacy", prior)

    for endpoint in ("/reports/report_job_boundary_legacy", "/reports/report_job_boundary_legacy/display"):
        response = client.get(endpoint)
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "PROCESSING_FAILED"
        assert response.json()["error"]["stage"] == "display_report"
        assert "own source" in response.json()["error"]["message"]
        assert source_url not in response.text
    for value in (prior, ReportV2.model_validate(prior)):
        with pytest.raises(QuantroError) as caught:
            display_report(value)
        assert caught.value.stage.value == "display_report"
    assert store.get_report("report_job_boundary_legacy") == prior
    assert store.get_job("job_boundary_legacy")["status"] == "completed"
    assert upstream["provider"].sent == []


def test_first_saved_self_match_is_not_delivered_after_new_valid_analysis(client, store, upstream):
    assert _run(client, "job_reread_seed", conversation_id=None).status_code == 200
    prior = store.get_report("report_job_reread_seed")
    prior["job_id"] = "job_reread_legacy"
    prior["source"]["post"]["post_url"] = "https://youtu.be/clip?si=tracking"
    store.upsert_job("job_reread_legacy", CLIP_URL, "running")
    store.save_report("report_job_reread_legacy", "job_reread_legacy", prior)

    response = _run(client, "job_reread_legacy")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "PROCESSING_FAILED"
    assert response.json()["error"]["stage"] == "save_report_and_complete_job"
    assert store.get_report("report_job_reread_legacy") == prior
    assert upstream["provider"].sent == []


def test_nested_confidence_limitations_reach_report(client, upstream, monkeypatch):
    original = analysis_module.compare_context_and_caption

    def compare(*args):
        result = original(*args)
        result["assessment_confidence"]["limitations"].append("Unique confidence limitation.")
        return result

    monkeypatch.setattr(analysis_module, "compare_context_and_caption", compare)
    assert _run(client, "job_confidence_limits").status_code == 200
    report = client.get("/reports/report_job_confidence_limits").json()
    assert report["limitations"].count("Unique confidence limitation.") == 1


def test_report_build_and_save_accept_contract_dictionaries(store):
    from backend.services.reporting.build_report import build_report
    from backend.services.reporting.save_report import save_report_and_complete_job
    from backend.services.storage.tiger import TigerJobStore, TigerReportStore

    report = build_report("job_dicts", _post("clip_media", CLIP_URL), CLIP_FEATURES, None, None, None, None, None)
    store.upsert_job("job_dicts", CLIP_URL, "running")
    result = save_report_and_complete_job(
        "job_dicts", report.model_dump(mode="json"),
        report_store=TigerReportStore(store), job_store=TigerJobStore(store),
    )
    assert result.status == "completed"
    assert store.get_report(result.report_id) == report.model_dump(mode="json")


def test_completed_job_reuses_saved_report_without_new_analysis(client, store, upstream):
    first = _run(client, "job_repeat").json()
    upstream["search_error"] = RuntimeError("Search must not run on a completed job.")
    second = _run(client, "job_repeat")
    assert second.status_code == 200
    assert second.json() == first
    assert len(upstream["compare_calls"]) == 1
    assert len(upstream["provider"].sent) == 1
    assert len(store.get_job_reports("job_repeat")) == 1


def test_completed_job_cannot_be_reused_for_a_different_url(client, store, upstream):
    assert _run(client, "job_url_identity").status_code == 200
    response = client.post("/analyze/job_url_identity/run", json={"url": "https://example.com/another.mp4"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROCESSING_FAILED"
    assert store.get_job("job_url_identity")["status"] == "completed"


def test_report_get_storage_failure_has_safe_error_envelope(client, store, monkeypatch):
    def fail(report_id):
        raise RuntimeError("private connection detail")

    monkeypatch.setattr(store, "get_report", fail)
    response = client.get("/reports/report_job_broken")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "STORAGE_FAILED"
    assert "private" not in response.text


@pytest.mark.parametrize("retryable", [False, True])
def test_search_provider_failure_is_always_retryable(client, store, upstream, retryable):
    original = QuantroError("SEARCH_FAILED", "search_candidates", retryable=retryable)
    upstream["search_error"] = original
    response = _run(client, "job_typed_search")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "SEARCH_FAILED"
    assert response.json()["error"]["stage"] == "search_candidates"
    assert response.json()["error"]["retryable"] is True
    assert store.get_job("job_typed_search")["status"] == "failed"
    assert store.get_report("report_job_typed_search") is None


def test_first_saved_report_controls_delivery_and_result(client, store, upstream):
    from backend.services.reporting.build_report import build_report

    assert _run(client, "job_first_save", conversation_id=None).status_code == 200
    canonical = store.get_report("report_job_first_save")
    losing_report = build_report("job_first_save", _post("clip_media", CLIP_URL), CLIP_FEATURES, None, None, None, None, None)
    provider = FakeProvider()
    result = analysis_module._complete_and_deliver(
        "job_first_save", losing_report, "conv_concurrent", store,
        provider, InMemoryDeliveryTracker(), [],
    )
    assert result.outcome.value == canonical["outcome"] == "matched"
    assert provider.sent == [(
        "conv_concurrent",
        f"{canonical['summary']} View the evidence: {result.report_url}",
    )]
    assert store.get_report("report_job_first_save") == canonical


def test_status_reconnects_to_completed_report(client, store, upstream):
    assert _run(client, "job_reconnect").status_code == 200
    response = client.get("/analyze/job_reconnect/status")
    assert response.json() == {"job_id": "job_reconnect", "status": "completed",
                               "report_id": "report_job_reconnect", "error": None}


def test_status_only_returns_safe_job_fields(client, store):
    store.upsert_job("job_running", CLIP_URL, "running")
    assert client.get("/analyze/job_running/status").json() == {
        "job_id": "job_running", "status": "running", "report_id": None, "error": None}
    store.set_job_status("job_running", "failed", "SEARCH_FAILED", "search_candidates")
    response = client.get("/analyze/job_running/status")
    assert response.json()["error"]["code"] == "SEARCH_FAILED"
    assert "url" not in response.json()
    assert client.get("/analyze/job_missing/status").status_code == 404
def test_comparison_uses_selected_source_caption(client, upstream):
    upstream["source_caption"] = "Actual source caption."
    assert _run(client, "job_source_caption", conversation_id=None).status_code == 200
    assert upstream["compare_calls"] == ["Actual source caption."]


def test_untyped_delivery_failure_after_save_is_normalized(client, store, upstream, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private provider detail")
    monkeypatch.setattr(analysis_module, "send_summary", fail)
    response = _run(client, "job_untyped_delivery")
    assert response.status_code == 200
    assert response.json()["delivery_status"] == "failed"
    assert "private" not in response.text
    assert client.get("/reports/report_job_untyped_delivery").status_code == 200
    assert store.get_job("job_untyped_delivery")["status"] == "completed"


def test_query_warnings_forwarded_by_search_are_not_duplicated(client, upstream, monkeypatch):
    warning = "Available evidence is incomplete."
    monkeypatch.setattr(analysis_module, "build_search_queries",
                        lambda features, post: {"queries": [SHARED], "warnings": [warning]})
    upstream["search_warnings"] = [warning]
    response = _run(client, "job_dedup_warnings", conversation_id=None)
    assert response.status_code == 200
    assert response.json()["warnings"].count(warning) == 1


def test_display_endpoint_uses_canonical_backend_display_model(client, upstream):
    assert _run(client, "job_display_api", conversation_id=None).status_code == 200
    report = client.get("/reports/report_job_display_api").json()
    response = client.get("/reports/report_job_display_api/display")
    assert response.status_code == 200
    assert response.json() == display_report(report).model_dump(mode="json")
    assert response.json()["source_match"]["label"] == "Source-match confidence"
    assert response.json()["clip_coverage"]["label"] == "Submitted clip aligned"
    assert response.json()["context_assessment"]["label"] == "Context-assessment confidence"
    assert "local_path" not in response.text and "/tmp/" not in response.text


@pytest.mark.parametrize("status", ["running", "ingested", "failed"])
def test_every_job_url_is_immutable(client, store, upstream, status):
    store.upsert_job("job_immutable", CLIP_URL, status)
    before = store.get_job("job_immutable")
    response = client.post("/analyze/job_immutable/run", json={"url": "https://example.com/other.mp4"})
    assert response.status_code == 409
    assert store.get_job("job_immutable") == before
    assert upstream["compare_calls"] == []


def test_status_preserves_retryability_and_safe_message(client, store, upstream):
    upstream["search_error"] = ConnectionError("private provider detail")
    response = _run(client, "job_status_retry", conversation_id=None)
    assert response.status_code == 500
    status = client.get("/analyze/job_status_retry/status").json()
    assert status["error"] == response.json()["error"]
    assert status["error"]["retryable"] is True
    assert "private" not in json.dumps(status)


def test_status_expires_stalled_job_but_never_completed_job(client, store):
    from datetime import UTC, datetime, timedelta
    for job_id, status in [("job_stale", "running"), ("job_done", "completed")]:
        store.upsert_job(job_id, CLIP_URL, status)
        with store._engine.begin() as connection:
            connection.execute(store.jobs.update().where(store.jobs.c.id == job_id).values(
                updated_at=datetime.now(UTC) - timedelta(hours=2)))
    response = client.get("/analyze/job_stale/status").json()
    assert response["status"] == "failed"
    assert response["error"]["retryable"] is True
    assert response["error"]["stage"] == "run_analysis"
    assert client.get("/analyze/job_done/status").json()["status"] == "completed"


def test_recent_worker_heartbeat_prevents_stale_expiry(store):
    from datetime import UTC, datetime, timedelta
    store.upsert_job("job_heartbeat", CLIP_URL, "running")
    old_cutoff = datetime.now(UTC) - timedelta(minutes=15)
    store.touch_job("job_heartbeat")
    assert not store.expire_stale_job("job_heartbeat", old_cutoff)
    assert store.get_job("job_heartbeat")["status"] == "running"


def test_analyze_extra_fields_are_rejected_without_rows(client, store):
    response = client.post("/analyze/job_extra/run", json={"url": CLIP_URL, "unexpected": "private"})
    assert response.status_code == 422
    assert "private" not in response.text
    assert store.get_job("job_extra") is None


@pytest.mark.parametrize("model_return", [False, True])
def test_candidate_analysis_skips_unused_visual_calls(upstream, monkeypatch, model_return):
    original = analysis_module.extract_media_features
    options_seen = []
    def extract(media, options):
        options_seen.append(options)
        value = original(media, options)
        return analysis_module.MediaFeatures.model_validate(value) if model_return else value
    monkeypatch.setattr(analysis_module, 'extract_media_features', extract)
    assessed = analysis_module._assess_candidate(_candidate(1), analysis_module.MediaFeatures.model_validate(CLIP_FEATURES), {CLIP_URL})
    assert any("visual extraction was skipped" in warning for warning in assessed.features.warnings)
    assert assessed.alignment.status == 'verified'
    assert options_seen[0]['transcript_scope'] == 'full'
    assert options_seen[0]['extract_frames'] is False
    assert options_seen[0]['extract_visual_text'] is False
    assert options_seen[0]['max_frames'] == 0
    assert ingest_module.EXTRACTION_OPTIONS['extract_visual_text'] is True
