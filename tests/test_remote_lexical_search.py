"""Optional lexical discovery preserves quotes, privacy and primary results."""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

import backend.services.reporting.run_analysis as analysis
from backend.services.shared.errors import ErrorCode, QuantroError, Stage
from backend.services.shared.types import ReportV2

SUBMITTED = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
PRIMARY = "https://www.youtube.com/watch?v=TCm9788Tb5g"
LEXICAL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
OTHER = "https://www.youtube.com/watch?v=YE7VzlLtp-4"
QUOTE = '"We would not cancel the program if the figures were accurate"'


def _candidate(url, number=1):
    return {"candidate_id": f"candidate_{number}", "url": url,
            "title": None, "publisher": None, "published_at": None, "snippet": None,
            "discovered_by_queries": [QUOTE]}


@pytest.fixture
def report():
    return ReportV2.model_validate({
        "job_id": "job_lexical_search", "outcome": "no_verified_match",
        "submitted": {
            "post": {
                "post_id": "submitted", "post_url": SUBMITTED, "platform": "youtube",
                "author": {}, "engagement": {},
                "media": {"media_id": "remote:youtube:submitted", "mime_type": "video/x-youtube"},
                "retrieval": {"retrieved_at": "2026-10-10T20:00:00Z", "provider": "test_remote",
                              "metadata_source_urls": [SUBMITTED], "warnings": [analysis.NO_DISCOVERY_LIMITATION]},
            },
            "transcript": {"status": "partial", "processed_ranges": [{"start_seconds": 0.0, "end_seconds": 6.0}],
                           "sentences": [{"sentence_id": "clip_1", "start_seconds": 0.0, "end_seconds": 6.0,
                                          "text": "We would not cancel the program if the figures were accurate.", "speaker_id": None}]},
        },
        "limitations": [analysis.NO_DISCOVERY_LIMITATION], "summary": "Partial transcript only.",
    })


@pytest.fixture
def search_mocks(monkeypatch):
    for name in ("SEARCH_FALLBACK_PROVIDER", "SEARCH_FALLBACK_API_KEY", "SEARCH_PROVIDER_API_KEY",
                 "SEARCH_GOOGLE_CSE_ID", "GOOGLE_CSE_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SEARCH_PROVIDER", "hybrid")
    monkeypatch.setattr(analysis, "_heartbeat", lambda *args: None)
    monkeypatch.setattr(analysis, "build_search_queries", lambda *args: {"queries": [QUOTE], "warnings": []})
    primary = Mock(return_value=[_candidate(PRIMARY)])
    lexical = Mock(return_value={"candidates": [_candidate(LEXICAL)], "warnings": []})
    monkeypatch.setattr(analysis, "_discover_sources", primary)
    monkeypatch.setattr(analysis, "_web_search", lexical)

    def no_unrelated_work(*args, **kwargs):
        raise AssertionError("Discovery must not invoke AI, download, alignment or source assessment.")

    for name in ("build_remote_youtube_report", "retrieve_candidate_media", "extract_media_features",
                 "verify_and_align", "select_surrounding_context", "compare_context_and_caption", "assess_remote_sources"):
        monkeypatch.setattr(analysis, name, no_unrelated_work)
    return primary, lexical


def _links(result):
    return [item.removeprefix("Unverified source candidate: ") for item in result.limitations
            if item.startswith("Unverified source candidate: ")]


def _assert_unverified(result):
    assert result.outcome.value == "no_verified_match"
    assert result.source is result.alignment is result.context is result.comparison is None
    assert result.submitted.post.media.duration_seconds is None
    assert "local_path" not in result.model_dump_json()


@pytest.mark.parametrize("provider", ["serpapi", "brave", "google_cse"])
def test_explicit_provider_runs_one_quote_query_and_prioritizes_lexical_leads(report, search_mocks, monkeypatch, provider):
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", f" {provider.upper()} ")
    monkeypatch.setenv("SEARCH_FALLBACK_API_KEY", "test-private-fallback-key")
    monkeypatch.setenv("SEARCH_PROVIDER_API_KEY", "test-private-primary-key")
    monkeypatch.setenv("SEARCH_GOOGLE_CSE_ID", "test-engine")
    before = report.model_dump(mode="json")
    result = analysis._discover_remote_sources(report, object())

    primary, lexical = search_mocks
    primary.assert_called_once()
    lexical.assert_called_once()
    args = lexical.call_args.args
    assert args[0] == [QUOTE + " site:youtube.com/watch"]
    assert args[1:3] == (5, [])
    assert args[3]["SEARCH_PROVIDER"] == provider
    assert args[3]["SEARCH_PROVIDER_API_KEY"] == "test-private-fallback-key"
    assert args[3]["SEARCH_GOOGLE_CSE_ID"] == "test-engine"
    assert _links(result) == [LEXICAL, PRIMARY]
    assert any("lexical" in item for item in result.limitations)
    assert report.model_dump(mode="json") == before
    _assert_unverified(result)


@pytest.mark.parametrize("provider", [None, "", " ", "exa", "hybrid", "bing", "unsupported"])
def test_disabled_or_unrecognized_fallback_adds_no_provider_request(report, search_mocks, monkeypatch, provider):
    if provider is not None:
        monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", provider)
    monkeypatch.setenv("SEARCH_PROVIDER_API_KEY", "unused-existing-key")
    result = analysis._discover_remote_sources(report, object())
    search_mocks[1].assert_not_called()
    assert _links(result) == [PRIMARY]
    assert not any("lexical" in item for item in result.limitations)
    _assert_unverified(result)


@pytest.mark.parametrize("dedicated", [None, "", "test-dedicated-key"])
def test_key_selection_uses_only_explicit_dedicated_or_existing_search_key(report, search_mocks, monkeypatch, dedicated):
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "serpapi")
    monkeypatch.setenv("SEARCH_PROVIDER_API_KEY", "test-existing-search-key")
    if dedicated is not None:
        monkeypatch.setenv("SEARCH_FALLBACK_API_KEY", dedicated)
    analysis._discover_remote_sources(report, object())
    settings = search_mocks[1].call_args.args[3]
    assert settings["SEARCH_PROVIDER_API_KEY"] == (dedicated or "test-existing-search-key")


def test_no_distinctive_quote_skips_primary_and_lexical_search(report, search_mocks, monkeypatch):
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "serpapi")
    monkeypatch.setenv("SEARCH_FALLBACK_API_KEY", "test-key")
    monkeypatch.setattr(analysis, "build_search_queries", lambda *args: {"queries": [], "warnings": []})
    report.submitted.transcript.sentences[0].text = "Hello and thank you."
    result = analysis._discover_remote_sources(report, object())
    search_mocks[0].assert_not_called()
    search_mocks[1].assert_not_called()
    assert _links(result) == []
    assert any("skipped" in item for item in result.limitations)
    _assert_unverified(result)


def test_lexical_failure_preserves_successful_primary_search_without_diagnostics(report, search_mocks, monkeypatch, caplog, capsys):
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "serpapi")
    monkeypatch.setenv("SEARCH_FALLBACK_API_KEY", "test-private-fallback-key")
    search_mocks[1].side_effect = RuntimeError("PRIVATE_DIAGNOSTIC https://serpapi.com/?api_key=test-private-fallback-key")
    result = analysis._discover_remote_sources(report, object())
    assert _links(result) == [PRIMARY]
    assert any("lexical search was unavailable" in item for item in result.limitations)
    serialized = result.model_dump_json() + caplog.text + capsys.readouterr().out
    assert "test-private-fallback-key" not in serialized
    assert "PRIVATE_DIAGNOSTIC" not in serialized
    assert "api_key=" not in serialized
    _assert_unverified(result)


@pytest.mark.parametrize("raw", [None, [], {}, {"candidates": "invalid", "warnings": []},
                                  {"candidates": [{"candidate_id": "missing_url"}], "warnings": []}])
def test_malformed_lexical_response_is_a_warning_not_loss_of_primary_candidates(report, search_mocks, monkeypatch, raw):
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "brave")
    monkeypatch.setenv("SEARCH_FALLBACK_API_KEY", "test-key")
    search_mocks[1].return_value = raw
    result = analysis._discover_remote_sources(report, object())
    assert _links(result) == [PRIMARY]
    assert any("lexical search was unavailable" in item for item in result.limitations)
    _assert_unverified(result)


def test_safe_filter_deduplicates_self_aliases_and_unsafe_urls_before_limit(report, search_mocks, monkeypatch):
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "serpapi")
    monkeypatch.setenv("SEARCH_FALLBACK_API_KEY", "test-key")
    search_mocks[1].return_value = {"candidates": [
        _candidate("https://youtu.be/jNQXAC9IVRw", 0),
        _candidate("http://127.0.0.1/internal", 1),
        _candidate("https://user:PRIVATE_PASSWORD@example.com/speech", 2),
        _candidate(LEXICAL, 3), _candidate("https://youtu.be/aqz-KE-bpKQ", 4),
        _candidate(OTHER, 5),
    ], "warnings": []}
    result = analysis._discover_remote_sources(report, object())
    assert _links(result) == [LEXICAL, OTHER, PRIMARY]
    serialized = result.model_dump_json()
    assert "127.0.0.1" not in serialized
    assert "PRIVATE_PASSWORD" not in serialized
    assert "https://youtu.be/aqz-KE-bpKQ" not in serialized
    assert any("unsafe" in item for item in result.limitations)
    _assert_unverified(result)


def test_empty_lexical_success_keeps_primary_and_does_not_claim_verified_source(report, search_mocks, monkeypatch):
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "serpapi")
    monkeypatch.setenv("SEARCH_FALLBACK_API_KEY", "test-key")
    search_mocks[1].return_value = {"candidates": [], "warnings": ["Lexical coverage is limited."]}
    result = analysis._discover_remote_sources(report, object())
    assert _links(result) == [PRIMARY]
    assert "Lexical coverage is limited." in result.limitations
    assert "unverified" in result.summary
    _assert_unverified(result)


def test_primary_failure_remains_an_error_and_does_not_start_optional_search(report, search_mocks, monkeypatch):
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "serpapi")
    monkeypatch.setenv("SEARCH_FALLBACK_API_KEY", "test-key")
    failure = QuantroError(ErrorCode.SEARCH_FAILED, Stage.SEARCH_CANDIDATES, retryable=True)
    search_mocks[0].side_effect = failure
    with pytest.raises(QuantroError) as error:
        analysis._discover_remote_sources(report, object())
    assert error.value is failure
    search_mocks[1].assert_not_called()


def test_real_query_builder_preserves_negation_and_conditional_without_ai(report, search_mocks, monkeypatch):
    from backend.services.external_apis.build_search_queries import build_search_queries

    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "serpapi")
    monkeypatch.setenv("SEARCH_FALLBACK_API_KEY", "test-key")
    monkeypatch.setattr(analysis, "build_search_queries", build_search_queries)
    original = deepcopy(report.submitted.transcript.model_dump(mode="json"))
    result = analysis._discover_remote_sources(report, object())
    assert search_mocks[1].call_args.args[0] == [QUOTE + " site:youtube.com/watch"]
    assert result.submitted.transcript.model_dump(mode="json") == original
    assert "not cancel" in search_mocks[1].call_args.args[0][0]
    assert "if the figures" in search_mocks[1].call_args.args[0][0]


def test_whole_quoted_speech_is_not_sliced_in_middle_of_a_token(report, search_mocks, monkeypatch):
    long_quote = '"' + " ".join("distinctive" * 4 + str(index) for index in range(11)) + '"'
    assert 470 < len(long_quote) <= 512
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "serpapi")
    monkeypatch.setenv("SEARCH_FALLBACK_API_KEY", "test-key")
    monkeypatch.setattr(analysis, "build_search_queries", lambda *args: {"queries": [long_quote], "warnings": []})
    report.submitted.transcript.sentences[0].text = long_quote[1:-1]
    result = analysis._discover_remote_sources(report, object())
    assert search_mocks[1].call_args.args[0] == [long_quote + " site:youtube.com/watch"]
    _assert_unverified(result)


def test_appended_title_and_caption_cannot_replace_spoken_payoff_query(report, search_mocks, monkeypatch):
    from backend.services.external_apis.build_search_queries import build_search_queries

    payoff = "We must not mistake a conditional recommendation for an unconditional promise"
    report.submitted.post.title = "Bush Texas Education Address"
    report.submitted.post.caption = "The speaker guaranteed the policy will be canceled tomorrow."
    report.submitted.transcript.sentences.append(report.submitted.transcript.sentences[0].model_copy(update={
        "sentence_id": "clip_2", "start_seconds": 6.0, "end_seconds": 12.0, "text": payoff + ".",
    }))
    report.submitted.transcript.processed_ranges[0].end_seconds = 12.0
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "serpapi")
    monkeypatch.setenv("SEARCH_FALLBACK_API_KEY", "test-key")
    monkeypatch.setattr(analysis, "build_search_queries", build_search_queries)
    before = report.model_dump(mode="json")

    result = analysis._discover_remote_sources(report, object())

    assert search_mocks[0].call_args.args[0].queries == ['"' + payoff + '"', QUOTE]
    assert search_mocks[1].call_args.args[0] == [
        'Bush Texas Education Address "' + payoff + '" site:youtube.com/watch',
    ]
    assert "guaranteed" not in search_mocks[1].call_args.args[0][0]
    assert report.model_dump(mode="json") == before
    assert result.submitted.transcript == report.submitted.transcript
    _assert_unverified(result)


def test_distinctive_spoken_payoff_after_four_segments_keeps_same_search_budget(report, search_mocks, monkeypatch):
    from backend.services.external_apis.build_search_queries import build_search_queries

    texts = [f"Specific spoken subject number {number} preserves distinct context" for number in range(1, 5)]
    texts.append("This final qualification materially changes the preceding recommendation about public spending")
    template = report.submitted.transcript.sentences[0]
    for number, text in enumerate(texts, 1):
        report.submitted.transcript.sentences.append(template.model_copy(update={
            "sentence_id": f"clip_{number + 1}", "start_seconds": float(number * 6),
            "end_seconds": float((number + 1) * 6), "text": text + ".",
        }))
    report.submitted.transcript.processed_ranges[0].end_seconds = 36.0
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "serpapi")
    monkeypatch.setenv("SEARCH_FALLBACK_API_KEY", "test-key")
    monkeypatch.setattr(analysis, "build_search_queries", build_search_queries)

    analysis._discover_remote_sources(report, object())

    assert search_mocks[0].call_args.args[0].queries == ['"' + texts[-1] + '"', QUOTE, '"' + texts[2] + '"']
    search_mocks[0].assert_called_once()
    search_mocks[1].assert_called_once()
    assert search_mocks[1].call_args.args[0] == ['"' + texts[-1] + '" site:youtube.com/watch']


def test_missing_fallback_key_never_creates_an_http_client(report, search_mocks, monkeypatch):
    from backend.services.external_apis.search_candidates import _web_search

    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "serpapi")
    monkeypatch.setattr(analysis, "_web_search", _web_search)
    http_client = Mock(side_effect=AssertionError("Unconfigured providers must not issue an HTTP request."))
    monkeypatch.setattr("backend.services.external_apis.search_candidates.httpx.Client", http_client)
    result = analysis._discover_remote_sources(report, object())
    http_client.assert_not_called()
    assert _links(result) == [PRIMARY]
    assert any("lexical search was unavailable" in item for item in result.limitations)
    _assert_unverified(result)


def test_credentials_are_absent_from_report_and_logs_on_success(report, search_mocks, monkeypatch, caplog, capsys):
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "serpapi")
    monkeypatch.setenv("SEARCH_FALLBACK_API_KEY", "test-private-fallback-key")
    monkeypatch.setenv("SEARCH_PROVIDER_API_KEY", "test-private-primary-key")
    result = analysis._discover_remote_sources(report, object())
    output = result.model_dump_json() + caplog.text + capsys.readouterr().out
    assert "test-private-fallback-key" not in output
    assert "test-private-primary-key" not in output
    assert "SEARCH_FALLBACK_API_KEY" not in output
    assert "SEARCH_PROVIDER_API_KEY" not in output
    assert json.loads(result.model_dump_json())["outcome"] == "no_verified_match"


@pytest.mark.parametrize("assess", [False, True])
def test_unsupported_primary_leads_cannot_hide_later_youtube_assessment(report, search_mocks, monkeypatch, assess):
    articles = [f"https://example.com/public-speech/{number}" for number in range(3)]
    search_mocks[0].return_value = [*[_candidate(url, number) for number, url in enumerate(articles)],
                                   _candidate("https://youtu.be/aqz-KE-bpKQ", 3), _candidate(OTHER, 4)]
    assessor = Mock(side_effect=lambda result, *args: result)
    monkeypatch.setattr(analysis, "assess_remote_sources", assessor)

    result = analysis._discover_remote_sources(report, object(), assess_candidates=assess)

    assert search_mocks[0].call_args.args[4] == (5 if assess else 3)
    search_mocks[0].assert_called_once()
    search_mocks[1].assert_not_called()
    if assess:
        assessor.assert_called_once()
        assert [item["url"] for item in assessor.call_args.args[1]] == [LEXICAL, OTHER]
        assert _links(result) == [LEXICAL, OTHER]
        assert any("outside the supported YouTube" in item for item in result.limitations)
    else:
        assessor.assert_not_called()
        assert _links(result) == articles
    _assert_unverified(result)


def test_active_assessment_uses_one_fuller_context_query_preserving_spoken_words(report, search_mocks, monkeypatch):
    monkeypatch.setenv('SEARCH_FALLBACK_PROVIDER', 'serpapi')
    monkeypatch.setenv('SEARCH_FALLBACK_API_KEY', 'test-key')
    report.submitted.post.title = 'Bush Texas Education Address'
    report.submitted.post.caption = 'Texas school funding discussion.'
    report.submitted.transcript.sentences.append(report.submitted.transcript.sentences[0].model_copy(update={
        'sentence_id': 'payoff', 'start_seconds': 6.0, 'end_seconds': 8.0,
        'text': 'Now watch this drive.',
    }))
    assessor = Mock(side_effect=lambda result, *args: result)
    monkeypatch.setattr(analysis, 'assess_remote_sources', assessor)
    before = report.model_dump(mode='json')

    result = analysis._discover_remote_sources(report, object(), assess_candidates=True)

    search_mocks[0].assert_called_once()
    search_mocks[1].assert_called_once()
    query = search_mocks[1].call_args.args[0]
    assert query == ['Bush Texas Education Address school funding discussion ' + QUOTE
                     + ' site:youtube.com/watch']
    assert any('unverified search clues' in item for item in result.limitations)
    assert len(query) == 1 and len(query[0]) <= 512
    assert report.model_dump(mode='json') == before
    assessor.assert_called_once()
    assert len(assessor.call_args.args[1]) <= analysis.SEARCH_LIMIT
    _assert_unverified(result)


def test_context_query_drops_only_optional_clues_before_a_long_exact_quote():
    quote = '"' + ' '.join('distinctive' * 4 + str(index) for index in range(11)) + '"'
    assert analysis._remote_context_query([quote], 'Unverified title clue') == quote + ' site:youtube.com/watch'
    assert analysis._remote_context_query([], 'Unverified title') is None


def test_broader_recording_ranks_ahead_of_short_lexical_reposts_before_cutoff(report, search_mocks, monkeypatch):
    monkeypatch.setenv('SEARCH_FALLBACK_PROVIDER', 'serpapi')
    full = dict(_candidate(OTHER, 9), title='Full remarks and questions', snippet='Duration: 18:42')
    search_mocks[0].return_value = [full]
    search_mocks[1].return_value = {'candidates': [
        dict(_candidate(LEXICAL, 1), title='Viral clip', snippet='Duration: 23 seconds'),
        dict(_candidate(PRIMARY, 2), title='Best highlights'),
        _candidate('https://youtu.be/jNQXAC9IVRw', 3),
        dict(_candidate('https://youtu.be/YE7VzlLtp-4', 4), title='Full remarks'),
    ], 'warnings': []}
    assessor = Mock(side_effect=lambda result, *args: result)
    monkeypatch.setattr(analysis, 'assess_remote_sources', assessor)

    result = analysis._discover_remote_sources(report, object(), assess_candidates=True)

    rows = assessor.call_args.args[1]
    assert [item['url'] for item in rows] == [OTHER, LEXICAL, PRIMARY]
    assert len({item['url'] for item in rows}) == 3
    assert _links(result) == [OTHER, LEXICAL, PRIMARY]
    assert all('duration_seconds' not in item for item in rows)
    assert any('unverified fuller-recording metadata' in item for item in result.limitations)
    assert result.submitted.post.media.duration_seconds is None
    search_mocks[1].assert_called_once()
    _assert_unverified(result)


@pytest.mark.parametrize(('metadata', 'expected'), [
    ({'title': 'Full interview'}, 0),
    ({'title': 'Extended restaurant visit'}, 1),
    ({'title': 'Full visit'}, 0),
    ({'title': 'Original footage'}, 0),
    ({'snippet': 'Duration: 1:02:03'}, 0),
    ({'snippet': 'Length: 2 minutes'}, 0),
    ({'snippet': 'Duration: 23 seconds'}, 2),
    ({'url': 'https://www.youtube.com/shorts/aqz-KE-bpKQ'}, 2),
    ({'title': 'A speech in 2026 at 12:30'}, 1),
    ({'snippet': 'Duration: 1:99:99'}, 1),
    ({'snippet': 'Duration: 99999 hours'}, 1),
])
def test_ranking_uses_only_explicit_bounded_unverified_metadata(metadata, expected):
    assert analysis._remote_context_priority(metadata)[0] == expected


def test_food_order_query_keeps_tail_scene_clues_and_caption_as_unverified_locators(report, search_mocks, monkeypatch):
    monkeypatch.setenv('SEARCH_FALLBACK_PROVIDER', 'serpapi')
    monkeypatch.setenv('SEARCH_FALLBACK_API_KEY', 'test-key')
    report.submitted.post.title = 'President Donald Trump stops to talk with supporters during a campaign visit at Chick-fil-A Atlanta'
    report.submitted.post.caption = 'Vine City restaurant visit April 2024'
    report.submitted.transcript.sentences[0].text = 'We are going to order thirty milkshakes for everyone here.'
    monkeypatch.setattr(analysis, 'assess_remote_sources', lambda result, *args: result)
    before = report.model_dump(mode='json')

    result = analysis._discover_remote_sources(report, object(), assess_candidates=True)

    search_mocks[0].assert_called_once()
    search_mocks[1].assert_called_once()
    query = search_mocks[1].call_args.args[0][0]
    assert '"We are going to order thirty milkshakes for everyone here"' in query
    assert 'Chick' in query and 'Atlanta' in query
    assert 'Vine' in query and 'City' in query
    assert ' OR ' not in query and '"full video"' not in query
    assert len(query) <= 512
    assert report.model_dump(mode='json') == before
    assert any('unverified search clues' in item for item in result.limitations)
    _assert_unverified(result)


def test_short_numeric_utterance_and_joint_primary_locator_survive_lexical_failure(report, search_mocks, monkeypatch):
    monkeypatch.setenv('SEARCH_FALLBACK_PROVIDER', 'serpapi')
    monkeypatch.setenv('SEARCH_FALLBACK_API_KEY', 'test-key')
    report.submitted.post.title = 'Donald Trump visits Chick-fil-A in Atlanta'
    report.submitted.post.caption = 'Vine City restaurant visit'
    report.submitted.transcript.sentences[0].text = "I'm going to take care of the customers."
    report.submitted.transcript.sentences.append(report.submitted.transcript.sentences[0].model_copy(update={
        'sentence_id': 'numeric_order', 'start_seconds': 6.0, 'end_seconds': 7.0, 'text': '30 milkshakes.',
    }))
    report.submitted.transcript.processed_ranges[0].end_seconds = 7.0
    search_mocks[1].side_effect = QuantroError(ErrorCode.SEARCH_FAILED, Stage.SEARCH_CANDIDATES, retryable=True)
    monkeypatch.setattr(analysis, 'assess_remote_sources', lambda result, *args: result)
    before = report.model_dump(mode='json')

    result = analysis._discover_remote_sources(report, object(), assess_candidates=True)

    primary_queries = search_mocks[0].call_args.args[0].queries
    assert primary_queries[0] == '"30 milkshakes"'
    assert len(primary_queries) <= analysis.REMOTE_SEARCH_QUERY_LIMIT
    assert '"30 milkshakes"' in primary_queries[-1]
    assert 'Trump' in primary_queries[-1] and 'Chick' in primary_queries[-1]
    assert 'Vine' in primary_queries[-1] and 'City' in primary_queries[-1]
    assert ' OR ' not in primary_queries[-1]
    assert '"30 milkshakes"' in search_mocks[1].call_args.args[0][0]
    search_mocks[0].assert_called_once()
    search_mocks[1].assert_called_once()
    assert _links(result) == [PRIMARY]
    assert report.model_dump(mode='json') == before
    _assert_unverified(result)


@pytest.mark.parametrize(('title', 'texts', 'expected'), [
    ("Obama: 'We're building Iron Man'", [
        'Today I am joined by researchers who invent some of the most advanced metals on the planet.',
        "Basically, I'm here to announce that we're building Iron Man.",
        "We've been this has been a secret project we've been working on for a long time.",
        'Thank you very much.',
    ], '"Basically I\'m here to announce that we\'re building Iron Man"'),
    ('Former U.S. president Donald Trump orders 30 milkshakes at Atlanta Chick-fil-A', [
        'So can I have 30 milkshakes?', "I'm going to take care of the customers.",
        "You're making a lot of money?", 'Thank you very much.',
    ], '"So can I have 30 milkshakes"'),
])
def test_live_failure_distinctive_speech_drives_first_primary_query(report, search_mocks, monkeypatch, title, texts, expected):
    report.submitted.post.title = title
    template = report.submitted.transcript.sentences[0]
    report.submitted.transcript.sentences = [template.model_copy(update={
        'sentence_id': f'observed_{index}', 'text': text,
        'start_seconds': float(index * 6), 'end_seconds': float(index * 6 + 5),
    }) for index, text in enumerate(texts)]
    monkeypatch.setenv('SEARCH_FALLBACK_PROVIDER', 'serpapi')
    monkeypatch.setenv('SEARCH_FALLBACK_API_KEY', 'test-key')
    monkeypatch.setattr(analysis, 'assess_remote_sources', lambda result, *args: result)
    before = report.submitted.transcript.model_dump(mode='json')
    analysis._discover_remote_sources(report, object(), assess_candidates=True)
    primary = search_mocks[0].call_args.args[0].queries
    assert primary[0] == expected
    assert len(primary) <= 3 and all('Thank you' not in query for query in primary)
    lexical = search_mocks[1].call_args.args[0][0]
    assert expected in lexical and ' OR ' not in lexical
    assert report.submitted.transcript.model_dump(mode='json') == before
    search_mocks[0].assert_called_once()
    search_mocks[1].assert_called_once()


def test_related_event_lead_precedes_unrelated_full_recording_without_filtering_everything():
    related = {'title': 'Obama remarks on manufacturing innovation Iron Man', 'snippet': 'Full remarks'}
    unrelated = {'title': 'Cuphead secret project full video', 'snippet': 'Duration: 20 minutes'}
    rows = [unrelated, related]
    rows.sort(key=lambda row: analysis._remote_relevance_priority(
        row, "Obama: We're building Iron Man", None, '"we\'re building Iron Man"'))
    assert rows == [related, unrelated]
    assert analysis._remote_context_quote(['"Thank you very much"'], None, None) is None
