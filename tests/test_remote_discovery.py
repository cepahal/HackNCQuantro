"""Remote speech can discover possible sources without fabricating verification."""

import copy
from types import SimpleNamespace

import pytest

import backend.services.reporting.run_analysis as analysis
from backend.services.shared.errors import ErrorCode, QuantroError, Stage
from backend.services.shared.types import ReportV2

URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
SOURCE = "https://www.youtube.com/watch?v=TCm9788Tb5g"
OTHER_SOURCE = "https://archive.org/details/public-political-speech"


@pytest.fixture
def report():
    return ReportV2.model_validate({
        "job_id": "job_remote_discovery",
        "outcome": "no_verified_match",
        "submitted": {
            "post": {
                "post_id": "remote_post", "post_url": URL, "platform": "youtube",
                "author": {}, "engagement": {},
                "media": {"media_id": "remote:youtube:example", "mime_type": "video/x-youtube"},
                "retrieval": {
                    "retrieved_at": "2026-10-10T20:00:00Z",
                    "provider": "gemini_native_youtube_transcript:gemini-3.1-flash-lite",
                    "metadata_source_urls": [URL, "https://youtu.be/jNQXAC9IVRw"],
                    "warnings": ["Transcript and timestamps are provider estimates.",
                                 analysis.NO_DISCOVERY_LIMITATION],
                },
            },
            "transcript": {
                "status": "partial",
                "processed_ranges": [{"start_seconds": 0.0, "end_seconds": 5.0}],
                "sentences": [{
                    "sentence_id": "remote_speech_1", "start_seconds": 0.0,
                    "end_seconds": 5.0,
                    "text": "If the numbers were accurate, we would reconsider the public program.",
                }],
            },
        },
        "limitations": ["Remote transcript estimates; no downloaded recording is available.",
                        analysis.NO_DISCOVERY_LIMITATION],
        "summary": "Limited transcript result; no source was verified.",
    })


@pytest.fixture(autouse=True)
def forbid_additional_media_or_ai_calls(monkeypatch):
    monkeypatch.delenv('SEARCH_FALLBACK_PROVIDER', raising=False)
    def forbidden(*args, **kwargs):
        raise AssertionError("Remote discovery must not download, transcribe or verify media.")

    for name in ("build_remote_youtube_report", "retrieve_candidate_media", "extract_media_features",
                 "verify_and_align", "select_surrounding_context", "compare_context_and_caption"):
        monkeypatch.setattr(analysis, name, forbidden)
    # This unit boundary borrows storage; heartbeat persistence is covered by
    # the pipeline/Tiger tests rather than requiring a database for discovery.
    monkeypatch.setattr(analysis, "_heartbeat", lambda *args: None)


def candidate(url, number=1):
    return {"candidate_id": f"candidate_{number}", "url": url,
            "title": None, "publisher": None, "published_at": None, "snippet": None,
            "discovered_by_queries": ["quoted speech"]}


def assert_unverified(result):
    assert isinstance(result, ReportV2)
    assert result.outcome == "no_verified_match"
    assert result.source is result.alignment is result.context is result.comparison is None
    assert result.submitted.post.media.duration_seconds is None
    assert result.submitted.post.media.playback_url is None
    assert result.submitted.frames == result.submitted.visual_text == []
    assert "local_path" not in result.model_dump_json()


def test_discovery_uses_unchanged_speech_and_no_invented_duration(report, monkeypatch):
    store = SimpleNamespace(name="borrowed_store")
    clues = [f'"distinctive spoken clue number {number}"' for number in range(4)]
    report.submitted.transcript.sentences = [report.submitted.transcript.sentences[0].model_copy(update={
        "sentence_id": f"remote_speech_{number}", "start_seconds": float(number),
        "end_seconds": float(number + 1), "text": clue[1:-1],
    }) for number, clue in enumerate(clues)]
    original = report.model_dump(mode="json")
    calls = []

    def build(features, post):
        calls.append(("build", copy.deepcopy(features), copy.deepcopy(post)))
        return {"queries": clues, "warnings": ["The transcript is partial."]}

    def discover(queries, borrowed_store, excluded, warnings, limit, **kwargs):
        calls.append(("discover", queries.model_dump(mode="json"), borrowed_store, set(excluded), limit))
        warnings.append("Discovery candidates have not been verified.")
        return [candidate(SOURCE)]

    monkeypatch.setattr(analysis, "build_search_queries", build)
    monkeypatch.setattr(analysis, "_discover_sources", discover)
    result = analysis._discover_remote_sources(report, store)

    assert [call[0] for call in calls] == ["build", "discover"]
    projected = calls[0][1]
    assert set(projected) == {"media_id", "transcript", "visual_text"}
    assert projected["transcript"] == original["submitted"]["transcript"]
    assert projected["visual_text"] == []
    assert projected["media_id"] == original["submitted"]["post"]["media"]["media_id"]
    assert calls[0][2] == original["submitted"]["post"]
    assert calls[1][1]["queries"] == [clues[3], clues[0], clues[2]]
    assert calls[1][2] is store
    assert calls[1][3] == {"youtube:jNQXAC9IVRw"}
    assert calls[1][4] == 3
    assert result.submitted.transcript.model_dump(mode="json") == original["submitted"]["transcript"]
    assert any(SOURCE in item for item in result.limitations)
    assert any("partial" in item.lower() for item in result.limitations)
    assert "unverified" in " ".join(result.limitations).lower()
    assert analysis.NO_DISCOVERY_LIMITATION not in result.limitations
    assert analysis.NO_DISCOVERY_LIMITATION not in result.submitted.post.retrieval.warnings
    assert report.model_dump(mode="json") == original
    assert_unverified(result)


def test_discovery_filters_aliases_self_and_unsafe_urls_before_candidate_count(report, monkeypatch):
    monkeypatch.setattr(analysis, "build_search_queries", lambda *args: {
        "queries": ['"quoted speech"'], "warnings": [],
    })
    rows = [candidate(SOURCE), candidate("https://youtu.be/TCm9788Tb5g", 2),
            candidate("https://youtu.be/jNQXAC9IVRw", 3), candidate("http://127.0.0.1/private", 4),
            candidate(OTHER_SOURCE, 5)]
    monkeypatch.setattr(analysis, "_discover_sources", lambda *args, **kwargs: rows)

    result = analysis._discover_remote_sources(report, object())

    candidate_notes = [item for item in result.limitations if SOURCE in item or OTHER_SOURCE in item]
    assert len(candidate_notes) == 2
    assert sum(SOURCE in item for item in candidate_notes) == 1
    assert sum(OTHER_SOURCE in item for item in candidate_notes) == 1
    assert all("127.0.0.1" not in item for item in result.limitations)
    assert all("https://youtu.be/TCm9788Tb5g" not in item for item in result.limitations)
    assert all("https://youtu.be/jNQXAC9IVRw" not in item for item in result.limitations)
    assert "2" in result.summary
    assert "unverified" in result.summary.lower()
    assert_unverified(result)


def test_candidate_display_is_bounded_even_if_provider_returns_excess_rows(report, monkeypatch):
    monkeypatch.setattr(analysis, "build_search_queries", lambda *args: {
        "queries": ['"quoted speech"'], "warnings": [],
    })
    urls = [f"https://example.com/public-speech/{number}" for number in range(8)]
    monkeypatch.setattr(analysis, "_discover_sources", lambda *args, **kwargs: [
        candidate(url, number) for number, url in enumerate(urls)
    ])

    result = analysis._discover_remote_sources(report, object())

    notes = " ".join(result.limitations)
    assert sum(url in notes for url in urls) == 3
    assert "3" in result.summary
    assert_unverified(result)


def test_nondistinctive_speech_skips_search_with_explicit_limitation(report, monkeypatch):
    report.submitted.transcript.sentences[0].text = "Hello and thank you."
    monkeypatch.setattr(analysis, "build_search_queries", lambda *args: {
        "queries": [], "warnings": ["No distinctive searchable text was available."],
    })

    def no_search(*args, **kwargs):
        raise AssertionError("Empty clues must not invoke a search provider.")

    monkeypatch.setattr(analysis, "_discover_sources", no_search)
    result = analysis._discover_remote_sources(report, object())

    assert any("distinctive" in item.lower() for item in result.limitations)
    assert any("skipped" in item.lower() and "discovery" in item.lower() for item in result.limitations)
    assert_unverified(result)


def test_empty_successful_search_preserves_partial_evidence_without_claiming_match(report, monkeypatch):
    monkeypatch.setattr(analysis, "build_search_queries", lambda *args: {
        "queries": ['"quoted speech"'], "warnings": [],
    })
    monkeypatch.setattr(analysis, "_discover_sources", lambda *args, **kwargs: [])

    result = analysis._discover_remote_sources(report, object())

    assert result.submitted.transcript == report.submitted.transcript
    assert any("candidate" in item.lower() for item in result.limitations)
    assert_unverified(result)


def test_search_failure_propagates_instead_of_becoming_false_no_match(report, monkeypatch):
    monkeypatch.setattr(analysis, "build_search_queries", lambda *args: {
        "queries": ['"quoted speech"'], "warnings": [],
    })
    error = QuantroError(ErrorCode.SEARCH_FAILED, Stage.SEARCH_CANDIDATES,
                         "The source search could not be completed.", retryable=True)

    def failed(*args, **kwargs):
        raise error

    monkeypatch.setattr(analysis, "_discover_sources", failed)
    with pytest.raises(QuantroError) as caught:
        analysis._discover_remote_sources(report, object())

    assert caught.value is error
    assert caught.value.code == ErrorCode.SEARCH_FAILED
    assert caught.value.stage == Stage.SEARCH_CANDIDATES
    assert caught.value.retryable is True


def test_real_query_builder_preserves_conditional_and_negated_words(report, monkeypatch):
    seen = []

    def discover(queries, *args, **kwargs):
        seen.extend(queries.queries)
        return []

    sentence = report.submitted.transcript.sentences[0]
    sentence.text = "If the figures were accurate, we would not cancel the public program."
    original = sentence.text
    monkeypatch.setattr(analysis, "_discover_sources", discover)

    result = analysis._discover_remote_sources(report, object())

    assert seen and len(seen) <= 3
    assert any("If the figures were accurate" in query for query in seen)
    assert any("not cancel" in query for query in seen)
    assert result.submitted.transcript.sentences[0].text == original
    assert_unverified(result)
