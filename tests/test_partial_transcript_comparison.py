"""Partial speech interpretations retain immutable citations and bounded verdicts."""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from backend.services.shared.errors import ErrorCode, QuantroError, Stage
from backend.services.shared.types import Comparison, Context, Transcript
from backend.services.specialist import comparison as module


@pytest.fixture
def window_case():
    transcript = {
        "status": "partial",
        "processed_ranges": [{"start_seconds": 3.25, "end_seconds": 11.5}],
        "sentences": [
            {"sentence_id": "clip_a", "start_seconds": 3.25, "end_seconds": 7.875,
             "text": "We would change the policy if the evidence supported it.", "speaker_id": None},
            {"sentence_id": "clip_b", "start_seconds": 8.125, "end_seconds": 11.5,
             "text": "That is a conditional statement.", "speaker_id": None},
        ],
    }
    context = {
        "source_media_id": "remote:youtube:source",
        "windows": [{
            "context_id": "window_001", "match_ids": ["match_001"],
            "requested_start_seconds": 100.0, "requested_end_seconds": 150.0,
            "coverage": "partial",
            "sentences": [
                {"sentence_id": "source_a", "start_seconds": 120.125, "end_seconds": 124.75,
                 "text": "We would change the policy if the evidence supported it.", "speaker_id": None},
                {"sentence_id": "source_b", "start_seconds": 125.0, "end_seconds": 130.625,
                 "text": "The current evidence does not support that change.", "speaker_id": None},
            ],
        }],
        "limitations": ["Only the selected source window was retrieved."],
    }
    response = {"observations": [
        {"explanation": "Both excerpts describe the proposed change conditionally.",
         "evidence": [
             {"media_id": "submitted", "sentence_id": "clip_a"},
             {"media_id": "remote:youtube:source", "sentence_id": "source_a"},
         ]},
        {"explanation": "The source also says the current evidence does not support the change.",
         "evidence": [{"media_id": "remote:youtube:source", "sentence_id": "source_b"}]},
    ]}
    return transcript, context, response


@pytest.fixture
def provider(monkeypatch, window_case):
    client = Mock()
    factory = Mock(return_value=(client, "cheap-test-model"))
    generate = Mock(return_value=deepcopy(window_case[2]))
    monkeypatch.setattr(module, "client_and_model", factory)
    monkeypatch.setattr(module, "generate_json", generate)
    return client, factory, generate


def _compare(case, caption="The speaker recommends changing the policy."):
    return module.compare_transcript_windows("submitted", case[0], case[1], caption)


def _assert_structured(exc):
    assert isinstance(exc, QuantroError)
    assert exc.code is ErrorCode.PROCESSING_FAILED
    assert exc.stage is Stage.COMPARE_CONTEXT_AND_CAPTION
    assert set(exc.to_dict()) == {"code", "stage", "message", "retryable"}


def test_partial_windows_keep_exact_quotes_ids_and_estimated_seconds(window_case, provider):
    before = deepcopy(window_case)
    result = _compare(window_case)
    assert isinstance(result, Comparison)
    assert set(result.model_dump()) == set(Comparison.model_fields)
    assert [finding.finding_id for finding in result.findings] == ["finding_001", "finding_002"]
    evidence = [item.model_dump(mode="json") for finding in result.findings for item in finding.evidence]
    assert evidence == [
        {"media_id": "submitted", "sentence_ids": ["clip_a"], "start_seconds": 3.25,
         "end_seconds": 7.875, "quote": window_case[0]["sentences"][0]["text"]},
        {"media_id": "remote:youtube:source", "sentence_ids": ["source_a"], "start_seconds": 120.125,
         "end_seconds": 124.75, "quote": window_case[1]["windows"][0]["sentences"][0]["text"]},
        {"media_id": "remote:youtube:source", "sentence_ids": ["source_b"], "start_seconds": 125.0,
         "end_seconds": 130.625, "quote": window_case[1]["windows"][0]["sentences"][1]["text"]},
    ]
    assert window_case == before
    assert all(finding.type == "partial_transcript_observation" for finding in result.findings)
    assert "Only the selected source window was retrieved." in result.limitations
    assert any("estimate" in limitation for limitation in result.limitations)
    window_case[0]["sentences"][0]["text"] = "MUTATED AFTER COMPARISON"
    assert result.findings[0].evidence[0].quote == before[0]["sentences"][0]["text"]


@pytest.mark.parametrize("caption,expected", [(None, "not_provided"), ("", "uncertain"), ("Some claim", "uncertain")])
def test_no_definitive_assessment_can_be_supplied_by_model(window_case, provider, caption, expected):
    provider[2].return_value["observations"][0]["explanation"] = "This explanation claims HIGH confidence and absolute accuracy."
    result = _compare(window_case, caption)
    assert result.context_assessment.value == "uncertain"
    assert result.caption_assessment.value == expected
    assert result.assessment_confidence.level.value == "low"
    assert result.assessment_confidence.method == "partial_transcript_context_v1"


def test_single_bounded_provider_call_uses_ids_instead_of_generated_quotes(window_case, provider):
    _compare(window_case)
    client, factory, generate = provider
    factory.assert_called_once_with(Stage.COMPARE_CONTEXT_AND_CAPTION)
    generate.assert_called_once()
    args, kwargs = generate.call_args
    assert args[:2] == (client, "cheap-test-model")
    assert args[4] is Stage.COMPARE_CONTEXT_AND_CAPTION
    assert kwargs == {"max_attempts": 1}
    payload = json.loads(args[2].split("\n", 1)[1])
    assert payload["submitted_sentences"] == window_case[0]["sentences"]
    assert payload["source_sentences"] == window_case[1]["windows"][0]["sentences"]
    schema = args[3]
    assert set(schema["$defs"]["_WindowCitation"]["properties"]) == {"media_id", "sentence_id"}
    assert set(schema["properties"]) == {"observations"}
    assert "untrusted evidence" in args[2]
    client.close.assert_called_once()


def test_shared_models_are_accepted_without_duration_or_local_path(window_case, provider):
    transcript = Transcript.model_validate(window_case[0])
    context = Context.model_validate(window_case[1])
    before = (transcript.model_dump(), context.model_dump())
    result = module.compare_transcript_windows("submitted", transcript, context, None)
    assert result.findings
    assert (transcript.model_dump(), context.model_dump()) == before
    assert "duration_seconds" not in json.dumps(provider[2].call_args.args[2])
    assert "local_path" not in json.dumps(result.model_dump(mode="json"))


@pytest.mark.parametrize("citation", [
    {"media_id": "unknown", "sentence_id": "source_a"},
    {"media_id": "remote:youtube:source", "sentence_id": "invented"},
    {"media_id": "submitted", "sentence_id": "invented"},
    {"media_id": "remote:youtube:source", "sentence_id": "clip_a"},
])
def test_unknown_or_crossed_citations_are_rejected(window_case, provider, citation):
    provider[2].return_value["observations"][0]["evidence"][1] = citation
    provider[2].return_value["observations"] = provider[2].return_value["observations"][:1]
    with pytest.raises(QuantroError) as error:
        _compare(window_case)
    _assert_structured(error.value)
    provider[0].close.assert_called_once()
    assert provider[2].call_count == 1


@pytest.mark.parametrize("index", [0, 1])
def test_every_observation_requires_source_evidence(window_case, provider, index):
    invalid = provider[2].return_value["observations"][index]
    invalid["evidence"] = [
        {"media_id": "submitted", "sentence_id": "clip_a"},
    ]
    provider[2].return_value["observations"] = [invalid]
    with pytest.raises(QuantroError) as error:
        _compare(window_case)
    _assert_structured(error.value)
    provider[0].close.assert_called_once()


def test_repeated_citation_pairs_are_rejected(window_case, provider):
    evidence = provider[2].return_value["observations"][0]["evidence"]
    evidence.append(deepcopy(evidence[1]))
    provider[2].return_value["observations"] = provider[2].return_value["observations"][:1]
    with pytest.raises(QuantroError) as error:
        _compare(window_case)
    _assert_structured(error.value)
    provider[0].close.assert_called_once()


@pytest.mark.parametrize("raw", [
    None, [], {}, {"observations": []}, {"observations": "invented"},
    {"observations": [{"explanation": "Some claim", "evidence": []}]},
    {"observations": [{"explanation": "", "evidence": [{"media_id": "remote:youtube:source", "sentence_id": "source_a"}]}]},
])
def test_malformed_or_empty_interpretations_are_structured_failures(window_case, provider, raw):
    provider[2].return_value = raw
    with pytest.raises(QuantroError) as error:
        _compare(window_case)
    _assert_structured(error.value)
    provider[0].close.assert_called_once()


@pytest.mark.parametrize("field,value", [
    ("context_assessment", "material_context_missing"),
    ("caption_assessment", "supported_by_context"),
    ("assessment_confidence", {"level": "high"}),
    ("summary", "A definitive verdict"),
])
def test_provider_cannot_add_verdict_fields(window_case, provider, field, value):
    provider[2].return_value[field] = value
    with pytest.raises(QuantroError) as error:
        _compare(window_case)
    _assert_structured(error.value)
    provider[0].close.assert_called_once()


@pytest.mark.parametrize("field,value", [("quote", "Invented quote"), ("start_seconds", 0.0), ("end_seconds", 999.0)])
def test_provider_cannot_override_immutable_citation_details(window_case, provider, field, value):
    provider[2].return_value["observations"][0]["evidence"][1][field] = value
    provider[2].return_value["observations"] = provider[2].return_value["observations"][:1]
    with pytest.raises(QuantroError) as error:
        _compare(window_case)
    _assert_structured(error.value)
    provider[0].close.assert_called_once()


@pytest.mark.parametrize("failure", [
    QuantroError(ErrorCode.PROCESSING_FAILED, Stage.COMPARE_CONTEXT_AND_CAPTION, "Provider unavailable", True),
    TimeoutError("PRIVATE signed URL https://example.com/?api_key=PRIVATE_VALUE"),
    RuntimeError("PRIVATE signed URL https://example.com/?api_key=PRIVATE_VALUE"),
])
def test_provider_failure_closes_client_and_does_not_echo_private_errors(window_case, provider, failure):
    provider[2].side_effect = failure
    with pytest.raises(QuantroError) as error:
        _compare(window_case)
    _assert_structured(error.value)
    assert "PRIVATE" not in error.value.message
    assert "api_key" not in error.value.message
    assert error.value.retryable is isinstance(failure, (QuantroError, TimeoutError))
    provider[0].close.assert_called_once()
    assert provider[2].call_count == 1


@pytest.mark.parametrize("invalid", ["empty_clip", "empty_source", "duplicate_clip", "same_media", "invalid_caption"])
def test_invalid_inputs_fail_before_creating_provider_client(window_case, provider, invalid):
    media_id, caption = "submitted", None
    if invalid == "empty_clip":
        window_case[0]["sentences"] = []
    elif invalid == "empty_source":
        window_case[1]["windows"] = []
    elif invalid == "duplicate_clip":
        window_case[0]["sentences"].append(deepcopy(window_case[0]["sentences"][0]))
    elif invalid == "same_media":
        media_id = window_case[1]["source_media_id"]
    else:
        caption = 123
    with pytest.raises(QuantroError) as error:
        module.compare_transcript_windows(media_id, window_case[0], window_case[1], caption)
    _assert_structured(error.value)
    provider[1].assert_not_called()
    provider[2].assert_not_called()


@pytest.mark.parametrize("invalid", [
    "unavailable", "reversed", "zero_length", "outside_processed", "bad_processed",
    "blank_id", "blank_text", "overlap",
])
def test_invalid_submitted_speech_never_reaches_provider(window_case, provider, invalid):
    transcript = window_case[0]
    sentence = transcript["sentences"][0]
    if invalid == "unavailable":
        transcript["status"] = "unavailable"
    elif invalid == "reversed":
        sentence["end_seconds"] = 2.0
    elif invalid == "zero_length":
        sentence["end_seconds"] = sentence["start_seconds"]
    elif invalid == "outside_processed":
        sentence["start_seconds"] = 1.0
    elif invalid == "bad_processed":
        transcript["processed_ranges"][0]["end_seconds"] = 2.0
    elif invalid == "blank_id":
        sentence["sentence_id"] = " "
    elif invalid == "blank_text":
        sentence["text"] = " "
    else:
        transcript["sentences"][1]["start_seconds"] = 7.0
    with pytest.raises(QuantroError) as error:
        _compare(window_case)
    _assert_structured(error.value)
    provider[1].assert_not_called()
    provider[2].assert_not_called()


def test_whitespace_only_explanation_is_not_a_successful_analysis(window_case, provider):
    provider[2].return_value["observations"][0]["explanation"] = " \n\t "
    provider[2].return_value["observations"] = provider[2].return_value["observations"][:1]
    with pytest.raises(QuantroError) as error:
        _compare(window_case)
    _assert_structured(error.value)
    provider[0].close.assert_called_once()


def test_one_bad_observation_does_not_discard_independently_cited_valid_analysis(window_case, provider):
    invalid = provider[2].return_value["observations"][1]
    invalid["explanation"] = "DISCARDED_PRIVATE_MODEL_TEXT with an unsupported allegation."
    invalid["evidence"] = [{"media_id": "submitted", "sentence_id": "clip_b"}]
    result = _compare(window_case)

    assert len(result.findings) == 1
    assert result.findings[0].explanation == window_case[2]["observations"][0]["explanation"]
    assert [(item.media_id, item.sentence_ids) for item in result.findings[0].evidence] == [
        ("submitted", ["clip_a"]), ("remote:youtube:source", ["source_a"]),
    ]
    assert result.findings[0].evidence[1].quote == window_case[1]["windows"][0]["sentences"][0]["text"]
    assert result.context_assessment.value == "uncertain"
    assert result.assessment_confidence.level.value == "low"
    assert "DISCARDED_PRIVATE_MODEL_TEXT" not in result.model_dump_json()
    assert any("observation" in item.lower() and ("discard" in item.lower() or "invalid" in item.lower())
               for item in result.limitations)
    provider[0].close.assert_called_once()
    assert provider[2].call_count == 1
