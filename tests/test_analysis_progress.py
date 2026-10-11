import json

from backend.routes.analyze import get_report_progress
from backend.services.resolution.progress import progress_snapshot


def event(stage, status="completed", **data):
    return {"stage": stage, "status": status, "data": data, "elapsed_seconds": 1}


def test_progress_is_based_on_recorded_events():
    p = progress_snapshot("job_1", {"status": "running"}, [])
    assert p["stage"] == "retrieve" and not p["video_ready"]
    p = progress_snapshot(
        "job_1",
        {"status": "running"},
        [
            event("retrieve_post", full_transcript="Real input words"),
            event(
                "build_search_queries",
                unique_phrase="Real phrase",
                speaker_and_event="Possible speaker",
            ),
        ],
    )
    assert p["stage"] == "search" and p["excerpt"] == "Real input words"
    assert p["speaker_clue"] == "Possible speaker"


def test_candidate_does_not_become_selected_source():
    p = progress_snapshot(
        "job_1",
        {"status": "running"},
        [
            event(
                "candidate_transcript",
                "scored",
                video_id="abc",
                title="Candidate",
                match_score=81,
            ),
            event("candidate_evaluation", "eligible"),
        ],
    )
    assert p["source_title"] == "" and p["source_start"] is None
    assert p["candidates"][0]["score"] == 81


def test_failure_and_no_match_are_terminal():
    assert progress_snapshot("job_1", {"status": "failed"}, [])["stage"] == "failed"
    p = progress_snapshot(
        "job_1", {"status": "completed"}, [], {"outcome": "no_verified_match"}
    )
    assert p["stage"] == "no_match" and not p["video_ready"]


def test_saved_report_supplies_replay_without_events():
    p = progress_snapshot(
        "job_1",
        {"status": "completed"},
        [],
        {
            "outcome": "matched",
            "submitted": {
                "post": {"media": {"media_id": "clip"}},
                "transcript": {"sentences": [{"text": "Hello world"}]},
            },
            "source": {"post": {"title": "Original", "media": {"media_id": "source"}}},
            "alignment": {
                "status": "verified",
                "clip_media_id": "clip",
                "source_media_id": "source",
                "match_quality": {"confidence": {"level": "moderate"}},
                "matches": [{"source_start_seconds": 10, "source_end_seconds": 20}],
            },
            "context_selection": {"before": [{"text": "Earlier sentence"}]},
            "context_video": {
                "media_id": "context_example",
                "mime_type": "video/mp4",
                "playback_url": "https://example.com/video",
            },
        },
    )
    assert p["excerpt"] == "Hello world" and p["source_start"] == 10
    assert p["before"] == ["Earlier sentence"] and p["video_ready"]


def test_private_data_not_returned():
    p = progress_snapshot(
        "job_1",
        {"status": "running"},
        [
            event(
                "retrieve_post",
                full_transcript="/Users/person/secret",
                api_key="secret-value",
            )
        ],
    )
    assert "secret-value" not in json.dumps(p) and "/Users/" not in json.dumps(p)


def test_endpoint_works_before_report_exists():
    class Store:
        def get_job(self, _):
            return {"status": "running"}

        def get_diagnostics(self, _):
            return []

        def get_report(self, _):
            raise AssertionError("No report yet")

    response = get_report_progress("report_job_1", Store())
    assert (
        response.status_code == 200 and response.headers["cache-control"] == "no-store"
    )
    assert json.loads(response.body)["stage"] == "retrieve"


def test_export_completed_requires_a_recorded_mp4_asset():
    assert not progress_snapshot(
        "job_1", {"status": "running"}, [event("context_video")]
    )["video_ready"]
    assert not progress_snapshot(
        "job_1",
        {"status": "completed"},
        [],
        {"context_video": {"mime_type": "image/jpeg", "media_id": "image"}},
    )["video_ready"]
    result = progress_snapshot(
        "job_1",
        {"status": "running"},
        [event("context_video", media_id="context_test", mime_type="video/mp4")],
    )
    assert result["video_ready"]


def test_progress_ignores_malformed_optional_rows():
    result = progress_snapshot(
        "job_1",
        {"status": "running"},
        [event("selected_passage", matches={"private": "not a list"})],
        {
            "submitted": {"transcript": {"sentences": None}},
            "alignment": {
                "matches": [
                    {"source_start_seconds": 1, "source_end_seconds": float("inf")}
                ]
            },
            "context_selection": {"before": 4},
        },
    )
    assert result["source_start"] is None and result["excerpt"] == ""


def test_no_match_cannot_show_a_successful_export():
    result = progress_snapshot(
        "job_1",
        {"status": "completed"},
        [event("context_video", media_id="context_test", mime_type="video/mp4")],
        {"outcome": "no_verified_match"},
    )
    assert result["stage"] == "no_match" and not result["video_ready"]


def test_endpoint_missing_job_is_not_cached():
    class Store:
        def get_job(self, _):
            return None

    response = get_report_progress("report_job_1", Store())
    assert (
        response.status_code == 404 and response.headers["cache-control"] == "no-store"
    )


def test_endpoint_storage_failure_is_safe_and_not_cached():
    class Store:
        def get_job(self, _):
            raise RuntimeError("api_key=private-value /tmp/database")

    response = get_report_progress("report_job_1", Store())
    assert (
        response.status_code == 500 and response.headers["cache-control"] == "no-store"
    )
    assert (
        "private-value" not in response.body.decode()
        and "/tmp/" not in response.body.decode()
    )


def test_selected_passage_capture_exposes_only_recorded_selection():
    p = progress_snapshot(
        "job_1",
        {"status": "running"},
        [
            event(
                "selected_passage",
                title="Long recording",
                matches=[{"source_start_seconds": 42.0, "source_end_seconds": 51.0}],
            )
        ],
    )
    assert p["stage"] == "align" and p["source_title"] == "Long recording"
    assert (p["source_start"], p["source_end"]) == (42.0, 51.0) and not p["video_ready"]


def test_completed_progress_rejects_report_from_another_job(monkeypatch):
    from types import SimpleNamespace as N

    from backend.routes import analyze

    class Store:
        def get_job(self, _):
            return {"status": "completed"}

        def get_diagnostics(self, _):
            return []

        def get_report(self, _):
            return {}

    monkeypatch.setattr(
        analyze,
        "parse_stage_output",
        lambda *args: N(job_id="job_other", submitted=N(post=None), source=None),
    )
    response = get_report_progress("report_job_1", Store())
    assert (
        response.status_code == 404 and response.headers["cache-control"] == "no-store"
    )


def test_completed_job_without_saved_report_is_not_successful():
    result = progress_snapshot("job_missing", {"status": "completed"}, [])
    assert result["stage"] == "failed" and not result["video_ready"]


def test_export_failure_does_not_claim_report_is_saved():
    result = progress_snapshot(
        "job_running", {"status": "running"}, [event("context_video", "unavailable")]
    )
    assert "report is available" not in result["message"].lower()


def test_diagnostic_report_cannot_override_saved_no_match():
    result = progress_snapshot(
        "job_1",
        {"status": "completed"},
        [
            event("build_report", report={"outcome": "matched"}),
            event("sentence_context", before=[{"text": "Stale source context"}]),
        ],
        {"outcome": "no_verified_match"},
    )
    assert result["stage"] == "no_match"
    assert result["before"] == [] and not result["video_ready"]


def test_diagnostic_report_is_not_proof_of_durable_completion():
    result = progress_snapshot(
        "job_1",
        {"status": "completed"},
        [
            event("build_report", report={"outcome": "no_verified_match"}),
        ],
    )
    assert result["stage"] == "failed" and not result["video_ready"]


def test_unverified_matched_report_is_not_successful():
    for alignment in (None, {}, {"status": "verified", "matches": []}):
        result = progress_snapshot(
            "job_1",
            {"status": "completed"},
            [],
            {
                "outcome": "matched",
                "alignment": alignment,
                "context_video": {"media_id": "context_test", "mime_type": "video/mp4"},
            },
        )
        assert result["stage"] == "failed" and not result["video_ready"]


def test_matched_completion_requires_distinct_source_and_confident_alignment():
    from copy import deepcopy

    report = {
        "outcome": "matched",
        "submitted": {"post": {"media": {"media_id": "clip"}}},
        "source": {"post": {"media": {"media_id": "source"}}},
        "alignment": {
            "status": "verified",
            "clip_media_id": "clip",
            "source_media_id": "source",
            "match_quality": {"confidence": {"level": "high"}},
            "matches": [{"source_start_seconds": 10, "source_end_seconds": 20}],
        },
    }
    assert (
        progress_snapshot("job_1", {"status": "completed"}, [], report)["stage"]
        == "complete"
    )
    for defect in (
        "same_source",
        "low_confidence",
        "wrong_source",
        "unverified",
        "invalid_time",
    ):
        invalid = deepcopy(report)
        if defect == "same_source":
            invalid["source"]["post"]["media"]["media_id"] = "clip"
            invalid["alignment"]["source_media_id"] = "clip"
        elif defect == "low_confidence":
            invalid["alignment"]["match_quality"]["confidence"]["level"] = "low"
        elif defect == "wrong_source":
            invalid["alignment"]["source_media_id"] = "other"
        elif defect == "unverified":
            invalid["alignment"]["status"] = "not_assessed"
        else:
            invalid["alignment"]["matches"][0]["source_end_seconds"] = float("inf")
        assert (
            progress_snapshot("job_1", {"status": "completed"}, [], invalid)["stage"]
            == "failed"
        ), defect
