"""Known source offsets extend bounded windows without inventing recording length."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_remote_source_analysis import SECOND, SOURCE, SUBMITTED, make_report, row
from test_remote_youtube_fallback import FakeModels, _walk

from backend.services.reporting import remote_source_analysis as sources
from backend.services.reporting import remote_youtube as remote
from backend.services.reporting import run_analysis as analysis
from backend.services.shared.errors import ErrorCode, QuantroError


@pytest.fixture(autouse=True)
def _caption_provider_is_explicitly_mocked(monkeypatch):
    monkeypatch.setattr(sources, 'fetch_youtube_transcript', lambda url: None)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(remote, "_youtube_metadata", lambda url: {})
    state = SimpleNamespace(models=FakeModels(), close=Mock())
    monkeypatch.setattr(remote, "_client_and_model", lambda: (state, "gemini-3.1-flash-lite"))
    return state


@pytest.mark.parametrize("suffix,expected", [
    ("", 0), ("&t=0", 0), ("&t=125", 125), ("&t=125s", 125),
    ("&t=2m5s", 125), ("&t=1h2m3s", 3723), ("&start=125", 125),
    ("&time_continue=125", 125), ("#t=125", 125), ("#t=2m5s", 125),
    ("&t=125&start=125", 125), ("&t=86400", 86400), ("&si=tracking", 0),
])
def test_only_supported_bounded_timestamp_forms_are_parsed(suffix, expected):
    url = SOURCE + suffix
    assert remote.youtube_window_start(url) == expected
    assert remote.canonical_youtube_url(url) == SOURCE


@pytest.mark.parametrize("suffix", [
    "&t=-1", "&t=1.5", "&t=NaN", "&t=inf", "&t=2mfoo", "&t=1s2m",
    "&t=86401", "&t=", "&t=%20", "&start=1.5", "&t=125&start=126",
    "&t=125&t=126", "&t=125#t=126", "&t=9999999999999999999999999999999999999999",
])
def test_invalid_or_conflicting_timestamp_is_not_guessed(suffix):
    with pytest.raises(ValueError):
        remote.youtube_window_start(SOURCE + suffix)


def test_short_url_timestamp_uses_same_recording_identity():
    assert remote.youtube_window_start("https://youtu.be/TCm9788Tb5g?t=2m5s") == 125
    assert remote.canonical_youtube_url("https://youtu.be/TCm9788Tb5g?t=2m5s") == SOURCE


def test_nonzero_window_requests_absolute_bounds_and_retains_partial_provenance(client):
    client.models.payload = {"sentences": [
        {"start_seconds": 125.0, "end_seconds": 129.25, "text": "The proposal remains conditional on reliable public evidence."},
        {"start_seconds": 130.0, "end_seconds": 135.5, "text": "No final recommendation has been made yet."},
    ]}
    report = remote.build_remote_youtube_report("job_offset", SOURCE, window_start_seconds=125)
    assert [name for name, _ in client.models.calls] == ["count", "generate"]
    counted, generated = [request for _, request in client.models.calls]
    assert counted["contents"] == generated["contents"]
    part = next(item for item in _walk(generated["contents"]) if item.get("file_data"))
    assert part["file_data"]["file_uri"] == SOURCE
    assert part["video_metadata"]["start_offset"] in {"125s", "125.0s", "PT125S", "PT2M5S"}
    assert part["video_metadata"]["end_offset"] in {"185s", "185.0s", "PT185S", "PT3M5S"}
    prompt = ' '.join(item['text'] for item in _walk(generated['contents']) if isinstance(item.get('text'), str))
    assert 'between 125 and 185' in prompt
    assert 'absolute original-video timestamps' in prompt
    assert report.submitted.post.post_url == SOURCE + "&t=125"
    assert SOURCE + "&t=125" in report.submitted.post.retrieval.metadata_source_urls
    assert report.submitted.post.media.duration_seconds is None
    assert report.submitted.transcript.status.value == "partial"
    assert [item.start_seconds for item in report.submitted.transcript.sentences] == [125.0, 130.0]
    assert [(item.start_seconds, item.end_seconds) for item in report.submitted.transcript.processed_ranges] == [
        (125.0, 129.25), (130.0, 135.5),
    ]
    assert "local_path" not in report.model_dump_json()
    assert report.source is report.alignment is report.context is report.comparison is None
    client.close.assert_called_once()


@pytest.mark.parametrize("sentences", [
    [{"start_seconds": 0.0, "end_seconds": 5.0, "text": "Relative timestamps must not be silently shifted."}],
    [{"start_seconds": 124.0, "end_seconds": 130.0, "text": "This starts before the requested window."}],
    [{"start_seconds": 180.0, "end_seconds": 186.0, "text": "This ends after the requested window."}],
])
def test_relative_or_out_of_window_provider_output_is_rejected(client, sentences):
    client.models.payload = {"sentences": sentences}
    with pytest.raises(QuantroError) as caught:
        remote.build_remote_youtube_report("job_invalid_bounds", SOURCE, window_start_seconds=125)
    assert caught.value.code is ErrorCode.TRANSCRIPTION_FAILED
    assert [name for name, _ in client.models.calls] == ["count", "generate"]
    client.close.assert_called_once()


@pytest.mark.parametrize("offset", [-1, 86401, True, 1.5, "125", None])
def test_invalid_explicit_window_fails_before_provider_request(client, offset):
    with pytest.raises(QuantroError):
        remote.build_remote_youtube_report("job_bad_window", SOURCE, window_start_seconds=offset)
    assert client.models.calls == []


def test_same_recording_different_windows_have_distinct_evidence_identifiers(client):
    first = remote.build_remote_youtube_report("job_first", SOURCE)
    client.models.payload["sentences"] = [dict(item, start_seconds=item["start_seconds"] + 125,
                                               end_seconds=item["end_seconds"] + 125)
                                         for item in client.models.payload["sentences"]]
    later = remote.build_remote_youtube_report("job_later", SOURCE, window_start_seconds=125)
    assert first.submitted.post.media.media_id != later.submitted.post.media.media_id
    assert {item.sentence_id for item in first.submitted.transcript.sentences}.isdisjoint(
        item.sentence_id for item in later.submitted.transcript.sentences)
    assert remote.canonical_youtube_url(first.submitted.post.post_url) == remote.canonical_youtube_url(later.submitted.post.post_url)


def test_maximum_offset_remains_one_minute_and_does_not_infer_duration(client):
    client.models.payload = {'sentences': [
        {'start_seconds': 86400.0, 'end_seconds': 86460.0, 'text': 'Observed speech in the final bounded requested window.'},
    ]}
    report = remote.build_remote_youtube_report('job_maximum_offset', SOURCE, window_start_seconds=86400)
    assert report.submitted.transcript.sentences[0].end_seconds == 86460.0
    assert report.submitted.post.media.duration_seconds is None
    assert any('86400 to 86460' in line for line in report.limitations)
    assert [name for name, _ in client.models.calls] == ['count', 'generate']


@pytest.mark.parametrize('url', [
    'https://youtube.com.evil.invalid/watch?v=TCm9788Tb5g&t=125',
    'https://user:password@www.youtube.com/watch?v=TCm9788Tb5g&t=125',
    'https://127.0.0.1/watch?v=TCm9788Tb5g&t=125',
])
def test_timestamp_parser_does_not_accept_untrusted_recording_hosts(url):
    with pytest.raises(QuantroError):
        remote.youtube_window_start(url)


def _shifted_report(url, offset):
    result = make_report(url, source_context=True)
    if offset:
        result.submitted.post.post_url = remote.canonical_youtube_url(url) + f"&t={offset}"
        result.submitted.post.media.media_id += f":window:{offset}"
    for sentence in result.submitted.transcript.sentences:
        sentence.start_seconds += offset
        sentence.end_seconds += offset
    for interval in result.submitted.transcript.processed_ranges:
        interval.start_seconds += offset
        interval.end_seconds += offset
    return result


def test_offset_source_alignment_context_and_two_candidate_budget_remain_absolute(monkeypatch):
    calls = []

    def native(job_id, url, **kwargs):
        offset = kwargs.get("window_start_seconds", 0)
        calls.append((url, kwargs))
        return _shifted_report(url, offset)

    monkeypatch.setattr(sources, "build_remote_youtube_report", native)
    monkeypatch.setattr(sources, "compare_transcript_windows", lambda *args: sources._not_assessed(None))
    candidates = [row("https://youtu.be/jNQXAC9IVRw?t=125"), row(SOURCE + "&t=125"),
                  row("https://youtu.be/TCm9788Tb5g?t=250", 2), row(SECOND + "&start=3720", 3),
                  row("https://www.youtube.com/watch?v=lmnopqrstuv&t=10", 4)]
    result = sources.assess_remote_sources(make_report(), candidates, [], lambda: None)

    assert calls == [(SOURCE, {"window_start_seconds": 125}), (SECOND, {"window_start_seconds": 3720})]
    assert result.outcome.value == "matched"
    assert result.source.post.post_url == SOURCE + "&t=125"
    assert result.source.post.media.duration_seconds is None
    assert result.alignment.matches[0].source_start_seconds == 130.0
    assert result.alignment.matches[0].source_end_seconds == 137.0
    assert result.alignment.match_quality.clip_coverage_ratio is None
    assert result.context.windows[0].requested_start_seconds == 125.0
    assert all(item.start_seconds >= 125 for item in result.context.windows[0].sentences)
    assert result.alignment.edit_analysis.status == "not_assessed"


def test_discovery_preserves_valid_timestamp_without_weakening_self_exclusion(monkeypatch):
    monkeypatch.delenv("SEARCH_FALLBACK_PROVIDER", raising=False)
    monkeypatch.setattr(analysis, "_heartbeat", lambda *args: None)
    monkeypatch.setattr(analysis, "build_search_queries", lambda *args: {"queries": ["spoken clue"], "warnings": []})
    monkeypatch.setattr(analysis, "_discover_sources", lambda *args, **kwargs: [
        row("https://youtu.be/jNQXAC9IVRw?t=125"),
        row("https://youtu.be/TCm9788Tb5g?t=2m5s", 2),
        row(SOURCE + "&start=250", 3), row(SECOND + "&start=3720", 4),
    ])
    assessor = Mock(side_effect=lambda report, *args: report)
    monkeypatch.setattr(analysis, "assess_remote_sources", assessor)
    report = make_report()
    original = deepcopy(report.model_dump(mode="json"))
    result = analysis._discover_remote_sources(report, object(), assess_candidates=True)
    assert [item["url"] for item in assessor.call_args.args[1]] == [SOURCE + "&t=125", SECOND + "&t=3720"]
    assert report.model_dump(mode="json") == original
    assert SUBMITTED + "&t=125" not in result.model_dump_json()


def test_bad_discovery_offset_becomes_explicit_unknown_offset_not_guessed(monkeypatch):
    monkeypatch.delenv("SEARCH_FALLBACK_PROVIDER", raising=False)
    monkeypatch.setattr(analysis, "_heartbeat", lambda *args: None)
    monkeypatch.setattr(analysis, "build_search_queries", lambda *args: {"queries": ["spoken clue"], "warnings": []})
    monkeypatch.setattr(analysis, "_discover_sources", lambda *args, **kwargs: [row(SOURCE + "&t=NaN")])
    assessor = Mock(side_effect=lambda report, *args: report)
    monkeypatch.setattr(analysis, "assess_remote_sources", assessor)
    result = analysis._discover_remote_sources(make_report(), object(), assess_candidates=True)
    assert [item["url"] for item in assessor.call_args.args[1]] == [SOURCE]
    assert any("timestamp" in item.lower() or "offset" in item.lower() for item in result.limitations)
    assert "NaN" not in result.model_dump_json()
