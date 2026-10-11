"""No-network checks of conservative remote transcript source/context evidence."""

import pytest

from backend.services.reporting import remote_source_analysis as remote
from backend.services.shared.errors import ErrorCode, QuantroError, Stage
from backend.services.shared.types import Comparison, ReportV2, TimeRange

SUBMITTED = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
SOURCE = "https://www.youtube.com/watch?v=TCm9788Tb5g"
SECOND = "https://www.youtube.com/watch?v=abcdefghijk"
THIRD = "https://www.youtube.com/watch?v=lmnopqrstuv"
SPEECH = "If the employment numbers were accurate we would reconsider the public program."
OTHER = "The wildlife sanctuary opened a new enclosure for rescued elephants on Tuesday."
STALE = "Candidate recording downloads, alignment and source-context assessment were not performed in this fallback."


@pytest.fixture(autouse=True)
def _caption_provider_is_explicitly_mocked(monkeypatch):
    monkeypatch.setattr(remote, 'fetch_youtube_transcript', lambda url: None)


def make_report(url=SUBMITTED, text=SPEECH, *, source_context=False):
    suffix = url.split("=")[-1]
    sentences = [{"sentence_id": f"speech_{suffix}", "start_seconds": 5.0,
                  "end_seconds": 12.0, "text": text}]
    if source_context:
        sentences = [
            {"sentence_id": f"before_{suffix}", "start_seconds": 0.0, "end_seconds": 4.0,
             "text": "These numbers have not yet been independently checked."},
            *sentences,
            {"sentence_id": f"after_{suffix}", "start_seconds": 13.0, "end_seconds": 18.0,
             "text": "The decision remains conditional on the independent audit."},
        ]
    return ReportV2.model_validate({
        "job_id": "job_native_sources", "outcome": "no_verified_match",
        "submitted": {
            "post": {"post_id": f"post_{suffix}", "post_url": url, "platform": "youtube",
                     "caption": "The public program will end.", "author": {}, "engagement": {},
                     "media": {"media_id": f"remote:youtube:{suffix}", "mime_type": "video/x-youtube"},
                     "retrieval": {"retrieved_at": "2026-10-10T20:00:00Z",
                                   "provider": "gemini_native_youtube_transcript:gemini-3.1-flash-lite",
                                   "metadata_source_urls": [url],
                                   "warnings": [remote.NO_DISCOVERY_LIMITATION, STALE]}},
            "transcript": {"status": "partial",
                           "processed_ranges": [{"start_seconds": sentence["start_seconds"],
                                                 "end_seconds": sentence["end_seconds"]}
                                                for sentence in sentences],
                           "sentences": sentences},
        },
        "limitations": [remote.NO_DISCOVERY_LIMITATION, STALE,
                        "Transcript text and timestamps are provider estimates."],
        "summary": "No source was verified.",
    })


def row(url=SOURCE, number=1):
    return {"candidate_id": f"candidate_{number}", "url": url,
            "title": "Discovery title", "publisher": "Unverified publisher"}


@pytest.fixture
def calls(monkeypatch):
    calls = {"native": [], "comparison": [], "heartbeat": []}

    def native(job_id, url):
        calls["native"].append((job_id, url))
        return make_report(url, source_context=True)

    def comparison(media_id, transcript, context, caption):
        calls["comparison"].append((media_id, transcript.model_dump(mode="json"),
                                    context.model_dump(mode="json"), caption))
        sentence = context.windows[0].sentences[-1]
        return Comparison.model_validate({
            "context_assessment": "uncertain", "caption_assessment": "uncertain",
            "assessment_confidence": {"level": "low", "method": "partial_transcript_context_v1"},
            "summary": "The available source words describe a conditional decision.",
            "findings": [{"finding_id": "finding_001", "type": "partial_transcript_observation",
                          "explanation": "The supplied source speech makes the decision conditional.",
                          "evidence": [{"media_id": context.source_media_id,
                                        "sentence_ids": [sentence.sentence_id],
                                        "start_seconds": sentence.start_seconds,
                                        "end_seconds": sentence.end_seconds, "quote": sentence.text}]}],
            "limitations": ["Only partial provider-estimated speech was interpreted."],
        })

    monkeypatch.setattr(remote, "build_remote_youtube_report", native)
    monkeypatch.setattr(remote, "compare_transcript_windows", comparison)
    return calls


def assess(report, candidates, calls, warnings=None):
    return remote.assess_remote_sources(report, candidates, [] if warnings is None else warnings,
                                        lambda: calls["heartbeat"].append(True))


def test_exact_distinctive_source_match_has_partial_context_without_fake_duration(calls):
    report = make_report()
    original = report.model_dump(mode="json")
    source_original = make_report(SOURCE, source_context=True).submitted.transcript.model_dump(mode="json")
    result = assess(report, [row()], calls)

    assert result.outcome == "matched"
    assert result.source.post.post_url == SOURCE
    assert result.source.post.candidate_id == "candidate_1"
    assert result.source.post.title == "Discovery title"
    assert result.source.post.author.display_name is None
    assert "not independently verified" in " ".join(result.source.post.retrieval.warnings)
    assert result.source.post.media.duration_seconds is None
    assert result.submitted.post.media.duration_seconds is None
    assert result.source.post.media.playback_url is None
    assert result.alignment.status == "verified"
    assert result.alignment.match_quality.confidence.level == "moderate"
    assert result.alignment.match_quality.matched_clip_seconds == 7.0
    assert result.alignment.match_quality.clip_coverage_ratio is None
    assert result.alignment.edit_analysis.status == "not_assessed"
    assert result.alignment.edit_analysis.counts.confirmed_deleted_segments is None
    assert result.context.windows[0].coverage == "partial"
    assert result.context.windows[0].requested_start_seconds == 0.0
    assert result.context.windows[0].requested_end_seconds == 18.0
    assert [item.model_dump(mode="json") for item in result.context.windows[0].sentences] == source_original["sentences"]
    assert result.comparison.findings[0].evidence[0].quote == source_original["sentences"][-1]["text"]
    assert len(calls["native"]) == len(calls["comparison"]) == 1
    assert calls["comparison"][0][3] == report.submitted.post.caption
    assert "earliest original" in result.summary
    assert remote.NO_DISCOVERY_LIMITATION not in result.limitations
    assert STALE not in result.limitations
    assert STALE not in result.submitted.post.retrieval.warnings
    assert remote.NO_DISCOVERY_LIMITATION not in result.source.post.retrieval.warnings
    assert "local_path" not in result.model_dump_json()
    assert report.model_dump(mode="json") == original


def test_search_title_alone_never_verifies_a_source(calls, monkeypatch):
    monkeypatch.setattr(remote, "build_remote_youtube_report", lambda job_id, url: make_report(url, OTHER))
    candidate = row()
    candidate["title"] = SPEECH
    result = assess(make_report(), [candidate], calls)

    assert result.outcome == "no_verified_match"
    assert result.source is result.alignment is result.context is result.comparison is None
    assert calls["comparison"] == []
    assert "no distinctive unique speech match" in result.summary


def test_aliases_self_and_unsafe_urls_are_excluded_before_two_call_budget(calls):
    rows = [row("https://youtu.be/jNQXAC9IVRw"), row("http://127.0.0.1/video"),
            row("https://www.youtube.com/watch?v=too_short"), row(SOURCE),
            row("https://youtu.be/TCm9788Tb5g", 2), row(SECOND, 3), row(THIRD, 4)]
    result = assess(make_report(), rows, calls)

    assert [url for _, url in calls["native"]] == [SOURCE, SECOND]
    assert len(calls["comparison"]) == 1
    assert result.source.post.post_url == SOURCE  # Canonical URL tie-break.
    assert any("Multiple plausible sources remain" in item for item in result.limitations)
    assert "127.0.0.1" not in result.model_dump_json()


def test_candidate_failure_retains_warning_and_second_verified_source(calls, monkeypatch):
    def native(job_id, url):
        calls["native"].append((job_id, url))
        if url == SOURCE:
            raise RuntimeError("provider secret=do-not-display")
        return make_report(url, source_context=True)

    monkeypatch.setattr(remote, "build_remote_youtube_report", native)
    warnings = []
    result = assess(make_report(), [row(SOURCE), row(SECOND, 2), row(THIRD, 3)], calls, warnings)

    assert result.outcome == "matched"
    assert result.source.post.post_url == SECOND
    assert len(calls["native"]) == 2
    assert any("unknown content was not treated as a mismatch" in item for item in warnings)
    assert "do-not-display" not in result.model_dump_json()


def test_identical_matching_copy_is_only_an_unverified_lead(calls, monkeypatch):
    monkeypatch.setattr(remote, "build_remote_youtube_report", lambda job_id, url: make_report(url))
    result = assess(make_report(), [row()], calls)

    assert result.outcome == "no_verified_match"
    assert result.source is result.alignment is result.context is result.comparison is None
    assert calls["comparison"] == []
    assert "matching speech was found" in result.summary
    assert any(SOURCE in item and "duplicate clip" in item for item in result.limitations)
    assert result.submitted.post.media.duration_seconds is None


@pytest.mark.parametrize("text", [
    "Thanks!", SPEECH,
    "Thank you everyone for watching this video today.",
    "Please subscribe to our channel for more interesting daily videos.",
])
def test_copy_with_repeated_speech_or_outro_is_not_a_contextual_source(calls, monkeypatch, text):
    source = make_report(SOURCE)
    additional = source.submitted.transcript.sentences[0].model_copy(update={
        "sentence_id": "outro", "start_seconds": 13.0, "end_seconds": 18.0, "text": text,
    })
    source.submitted.transcript.sentences.append(additional)
    source.submitted.transcript.processed_ranges.append(TimeRange(start_seconds=13.0, end_seconds=18.0))
    monkeypatch.setattr(remote, "build_remote_youtube_report", lambda *args: source)
    result = assess(make_report(), [row()], calls)

    assert result.outcome == "no_verified_match"
    assert result.source is result.context is result.comparison is None
    assert calls["comparison"] == []


def test_short_corrective_qualifier_is_sufficient_observed_context(calls, monkeypatch):
    source = make_report(SOURCE, source_context=True)
    source.submitted.transcript.sentences = source.submitted.transcript.sentences[1:]
    source.submitted.transcript.sentences[-1].text = "Those figures are not accurate."
    source.submitted.transcript.processed_ranges = source.submitted.transcript.processed_ranges[1:]
    monkeypatch.setattr(remote, "build_remote_youtube_report", lambda *args: source)
    result = assess(make_report(), [row()], calls)

    assert result.outcome == "matched"
    assert result.context.windows[0].sentences[-1].text == "Those figures are not accurate."
    assert len(calls["comparison"]) == 1
    assert result.comparison.context_assessment == "uncertain"
    assert result.comparison.assessment_confidence.level == "low"


def test_duplicate_first_candidate_does_not_prevent_second_richer_source(calls, monkeypatch):
    def native(job_id, url):
        calls["native"].append((job_id, url))
        return make_report(url, source_context=url != SOURCE)

    monkeypatch.setattr(remote, "build_remote_youtube_report", native)
    result = assess(make_report(), [row(SOURCE), row(SECOND, 2), row(THIRD, 3)], calls)

    assert result.outcome == "matched"
    assert result.source.post.post_url == SECOND
    assert [url for _, url in calls["native"]] == [SOURCE, SECOND]
    assert len(calls["comparison"]) == 1
    assert any(SOURCE in item and "duplicate clip" in item for item in result.limitations)


def test_novel_speech_outside_observed_context_padding_does_not_qualify(calls, monkeypatch):
    source = make_report(SOURCE)
    additional = source.submitted.transcript.sentences[0].model_copy(update={
        "sentence_id": "distant_context", "start_seconds": 50.0, "end_seconds": 58.0,
        "text": "The independent audit has since found substantial errors in these statistics.",
    })
    source.submitted.transcript.sentences.append(additional)
    source.submitted.transcript.processed_ranges.append(TimeRange(start_seconds=50.0, end_seconds=58.0))
    monkeypatch.setattr(remote, "build_remote_youtube_report", lambda *args: source)
    result = assess(make_report(), [row()], calls)

    assert result.outcome == "no_verified_match"
    assert calls["comparison"] == []


def test_changed_number_is_not_corrected_into_an_exact_match(calls, monkeypatch):
    submitted = "If all 140 employment figures were accurate we would reconsider this public program."
    candidate = submitted.replace("140", "1400")
    monkeypatch.setattr(remote, "build_remote_youtube_report", lambda job_id, url: make_report(url, candidate))
    result = assess(make_report(text=submitted), [row()], calls)

    assert result.outcome == "no_verified_match"
    assert calls["comparison"] == []


def test_repeated_source_passage_is_not_accepted_as_unique(calls, monkeypatch):
    source = make_report(SOURCE)
    duplicate = source.submitted.transcript.sentences[0].model_copy(update={
        "sentence_id": "repeated_speech", "start_seconds": 20.0, "end_seconds": 27.0,
    })
    source.submitted.transcript.sentences.append(duplicate)
    source.submitted.transcript.processed_ranges.append(TimeRange(start_seconds=20.0, end_seconds=27.0))
    source = ReportV2.model_validate(source.model_dump(mode="json"))
    monkeypatch.setattr(remote, "build_remote_youtube_report", lambda *args: source)
    result = assess(make_report(), [row()], calls)

    assert result.outcome == "no_verified_match"
    assert calls["comparison"] == []


def test_semantic_failure_retains_verified_source_without_false_verdict(calls, monkeypatch):
    def comparison(*args):
        raise QuantroError(ErrorCode.PROCESSING_FAILED, Stage.COMPARE_CONTEXT_AND_CAPTION,
                          "private provider details must remain hidden")

    monkeypatch.setattr(remote, "compare_transcript_windows", comparison)
    result = assess(make_report(), [row()], calls)

    assert result.outcome == "matched"
    assert result.source is not None
    assert result.comparison.context_assessment == "not_assessed"
    assert result.comparison.assessment_confidence.level == "not_assessed"
    assert result.comparison.findings == []
    assert any("observed speech match was retained" in item for item in result.limitations)
    assert "private provider details" not in result.model_dump_json()


def test_heartbeat_failure_is_not_swallowed_as_unknown_candidate(calls):
    def heartbeat():
        raise QuantroError(ErrorCode.STORAGE_FAILED, Stage.SAVE_REPORT_AND_COMPLETE_JOB)

    with pytest.raises(QuantroError) as error:
        remote.assess_remote_sources(make_report(), [row()], [], heartbeat)
    assert error.value.code == ErrorCode.STORAGE_FAILED
    assert calls["native"] == []


def test_returned_self_or_different_recording_evidence_is_rejected(calls, monkeypatch):
    monkeypatch.setattr(remote, "build_remote_youtube_report", lambda *args: make_report())
    result = assess(make_report(), [row()], calls)

    assert result.outcome == "no_verified_match"
    assert calls["comparison"] == []
    assert any("could not be assessed" in item for item in result.limitations)


def test_empty_candidate_list_keeps_null_source_and_no_provider_calls(calls):
    result = assess(make_report(), [], calls)

    assert result.outcome == "no_verified_match"
    assert result.source is result.alignment is result.context is result.comparison is None
    assert calls["native"] == calls["comparison"] == calls["heartbeat"] == []
    assert "0 source candidate windows" in result.summary


def test_discovery_warnings_cannot_restore_stale_unassessed_stage(calls):
    report = make_report()
    selected = f"Unverified source candidate: {SOURCE}"
    report.limitations.append(selected)
    report.submitted.post.retrieval.warnings.append(selected)
    warnings = [STALE, remote.NO_DISCOVERY_LIMITATION, selected, "Search used partial estimated text."]

    result = assess(report, [row()], calls, warnings)

    assert result.outcome == "matched"
    assert STALE not in result.limitations
    assert remote.NO_DISCOVERY_LIMITATION not in result.limitations
    assert selected not in result.limitations
    assert selected not in result.submitted.post.retrieval.warnings
    assert STALE not in warnings and selected not in warnings
    assert "Search used partial estimated text." in result.limitations
