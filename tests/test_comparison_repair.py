"""A bounded re-ask fixes formatting without accepting fabricated evidence."""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from google.genai import types

from backend.services.shared.errors import ErrorCode, QuantroError, Stage
from backend.services.specialist import comparison as comparison_module

FIRST = "If those numbers were accurate, I would recommend canceling the program."
SECOND = "But those numbers are not accurate, so that is not my recommendation."
PRIVATE_RESPONSE = "MODEL_RESPONSE_MUST_NOT_BE_ECHOED"


def _generated(data, basis="direct_transcript"):
    return {**data, "caption_evidence_basis": basis}


def _verification(data, relation="contradicts"):
    return {"relation": relation, "evidence": deepcopy(data["findings"][0]["evidence"])}


@pytest.fixture
def comparison_case():
    sentences = [
        {"sentence_id": "source_0", "start_seconds": 0.0, "end_seconds": 8.0,
         "text": FIRST, "speaker_id": None},
        {"sentence_id": "source_1", "start_seconds": 8.0, "end_seconds": 16.0,
         "text": SECOND, "speaker_id": None},
    ]
    clip_sentence = {**sentences[0], "sentence_id": "clip_0"}
    clip = {
        "media_id": "clip", "duration_seconds": 8.0, "language": "en",
        "transcript": {"status": "complete", "processed_ranges": [
            {"start_seconds": 0.0, "end_seconds": 8.0},
        ], "sentences": [clip_sentence]},
        "frames": [], "visual_text": [], "warnings": [],
    }
    context = {
        "source_media_id": "source", "windows": [{
            "context_id": "context_001", "match_ids": ["match_001"],
            "requested_start_seconds": 0.0, "requested_end_seconds": 16.0,
            "coverage": "complete", "sentences": sentences,
        }], "limitations": [],
    }
    valid = {
        "context_assessment": "material_context_missing",
        "caption_assessment": "contradicted_by_context",
        "assessment_confidence": {
            "level": "moderate", "method": "context_evidence_rules_v1",
            "basis": ["The source rejects the recommendation."], "limitations": [],
        },
        "summary": "The clip omits the rejection of the recommendation.",
        "findings": [{
            "finding_id": "finding_001", "type": "omitted_qualification",
            "explanation": "The recommendation is conditional and then rejected.",
            "evidence": [{
                "media_id": "source", "sentence_ids": [sentence["sentence_id"]],
                "start_seconds": sentence["start_seconds"],
                "end_seconds": sentence["end_seconds"], "quote": sentence["text"],
            } for sentence in sentences],
        }],
        "limitations": [],
    }
    invalid = deepcopy(valid)
    invalid["findings"][0]["explanation"] = PRIVATE_RESPONSE
    invalid["findings"][0]["evidence"] = [{
        "media_id": "source", "sentence_ids": ["source_0", "source_1"],
        "start_seconds": 0.0, "end_seconds": 16.0,
        "quote": FIRST + " " + SECOND,
    }]
    return clip, context, valid, invalid


def test_multisentence_quote_stays_rejected_by_local_validator(comparison_case):
    clip, context, _, invalid = comparison_case
    with pytest.raises(ValueError, match="not verbatim"):
        comparison_module._validate_result(invalid, clip, context, "Cancel it")


def test_invalid_quote_gets_one_repair_with_original_evidence(monkeypatch, comparison_case):
    clip, context, valid, invalid = comparison_case
    client = Mock()
    generate = Mock(side_effect=[_generated(invalid), _generated(valid), _verification(valid)])
    monkeypatch.setattr(comparison_module, "client_and_model", lambda stage: (client, "test-model"))
    monkeypatch.setattr(comparison_module, "generate_json", generate)

    result = comparison_module.compare_context_and_caption(clip, context, "Cancel it")

    assert result["context_assessment"] == "material_context_missing"
    assert result["assessment_confidence"]["level"] == "moderate"
    assert result["caption_assessment"] == "contradicted_by_context"
    assert len(result["findings"][0]["evidence"]) == 2
    assert generate.call_count == 3
    first_prompt = generate.call_args_list[0].args[2]
    repair_prompt = generate.call_args_list[1].args[2]
    assert "source_0" in first_prompt and "source_1" in first_prompt
    assert FIRST in repair_prompt and SECOND in repair_prompt
    assert PRIVATE_RESPONSE not in repair_prompt
    assert repair_prompt != first_prompt
    for call in generate.call_args_list[:2]:
        assert call.args[3]["$defs"]["Evidence"]["properties"]["quote"]["enum"] == [FIRST, SECOND]
    client.close.assert_called_once()


@pytest.mark.parametrize("bad_field,bad_value", [
    ("quote", "A quotation that does not exist."),
    ("sentence_ids", ["invented_source_sentence"]),
    ("start_seconds", 0.25),
    ("media_id", "invented_media"),
])
def test_unrepaired_evidence_fails_after_two_attempts(monkeypatch, comparison_case, bad_field, bad_value):
    clip, context, valid, _ = comparison_case
    invalid = deepcopy(valid)
    invalid["findings"][0]["evidence"][0][bad_field] = bad_value
    client = Mock()
    generate = Mock(return_value=_generated(invalid))
    monkeypatch.setattr(comparison_module, "client_and_model", lambda stage: (client, "test-model"))
    monkeypatch.setattr(comparison_module, "generate_json", generate)

    with pytest.raises(QuantroError) as caught:
        comparison_module.compare_context_and_caption(clip, context, "Cancel it")

    assert caught.value.code is ErrorCode.PROCESSING_FAILED
    assert caught.value.stage is Stage.COMPARE_CONTEXT_AND_CAPTION
    assert generate.call_count == 2
    if isinstance(bad_value, str):
        assert bad_value not in caught.value.message
    client.close.assert_called_once()


def test_provider_failure_does_not_trigger_validation_repair(monkeypatch, comparison_case):
    clip, context, _, _ = comparison_case
    provider_error = QuantroError(
        ErrorCode.PROCESSING_FAILED, Stage.COMPARE_CONTEXT_AND_CAPTION,
        "Gemini request failed (HTTP 503).", retryable=True,
    )
    client = Mock()
    generate = Mock(side_effect=provider_error)
    monkeypatch.setattr(comparison_module, "client_and_model", lambda stage: (client, "test-model"))
    monkeypatch.setattr(comparison_module, "generate_json", generate)

    with pytest.raises(QuantroError) as caught:
        comparison_module.compare_context_and_caption(clip, context, "Cancel it")

    assert caught.value is provider_error
    generate.assert_called_once()
    client.close.assert_called_once()


def test_generation_quote_choices_are_exact_actual_transcript_sentences(monkeypatch, comparison_case):
    clip, context, valid, _ = comparison_case
    submitted_only = "This exact sentence appears only in the submitted transcript."
    clip["transcript"]["sentences"][0]["text"] = submitted_only
    clip["frames"] = [{"frame_id": "frame_001", "timestamp_seconds": 1.0,
                       "image_path": "FRAME_PATH_IS_NOT_QUOTABLE", "image_url": None}]
    clip["visual_text"] = [{"frame_id": "frame_001", "timestamp_seconds": 1.0,
                            "text": "OCR_TEXT_IS_NOT_TRANSCRIPT_EVIDENCE"}]
    context["windows"].append(deepcopy(context["windows"][0]))
    client = Mock()
    generate = Mock(side_effect=[_generated(valid), _verification(valid)])
    monkeypatch.setattr(comparison_module, "client_and_model", lambda stage: (client, "test-model"))
    monkeypatch.setattr(comparison_module, "generate_json", generate)

    comparison_module.compare_context_and_caption(clip, context, "Cancel it")

    schema = generate.call_args_list[0].args[3]
    choices = schema["$defs"]["Evidence"]["properties"]["quote"]["enum"]
    assert choices == [submitted_only, FIRST, SECOND]
    assert FIRST + " " + SECOND not in choices
    assert clip["visual_text"][0]["text"] not in choices
    assert clip["frames"][0]["image_path"] not in choices
    config = types.GenerateContentConfig(response_mime_type="application/json", response_json_schema=schema)
    assert config.response_json_schema == schema


def test_quote_choices_are_fresh_and_do_not_change_shared_comparison_schema(monkeypatch, comparison_case):
    clip, context, valid, _ = comparison_case
    canonical_schema = comparison_module.Comparison.model_json_schema()
    client = Mock()
    generate = Mock(side_effect=[_generated(valid), _verification(valid), _generated(valid), _verification(valid)])
    monkeypatch.setattr(comparison_module, "client_and_model", lambda stage: (client, "test-model"))
    monkeypatch.setattr(comparison_module, "generate_json", generate)
    first_only = "The first request has a unique submitted sentence."
    second_only = "The second request has a different submitted sentence."
    clip["transcript"]["sentences"][0]["text"] = first_only
    comparison_module.compare_context_and_caption(clip, context, "Cancel it")
    clip["transcript"]["sentences"][0]["text"] = second_only
    comparison_module.compare_context_and_caption(clip, context, "Cancel it")

    first_schema, second_schema = [call.args[3] for call in generate.call_args_list[::2]]
    assert first_schema is not second_schema
    assert first_schema["$defs"]["Evidence"]["properties"]["quote"]["enum"] == [first_only, FIRST, SECOND]
    assert second_schema["$defs"]["Evidence"]["properties"]["quote"]["enum"] == [second_only, FIRST, SECOND]
    assert comparison_module.Comparison.model_json_schema() == canonical_schema
    assert "enum" not in canonical_schema["$defs"]["Evidence"]["properties"]["quote"]
    assert "caption_evidence_basis" not in canonical_schema["properties"]


@pytest.mark.parametrize("basis", [
    "absence_only", "unobserved_modality", "insufficient_evidence", None,
    "unrecognized_basis", True, ["direct_transcript"],
])
def test_non_direct_or_invalid_caption_basis_fails_closed(monkeypatch, comparison_case, basis):
    clip, context, valid, _ = comparison_case
    client = Mock()
    generate = Mock(return_value=_generated(valid, basis))
    monkeypatch.setattr(comparison_module, "client_and_model", lambda stage: (client, "test-model"))
    monkeypatch.setattr(comparison_module, "generate_json", generate)

    result = comparison_module.compare_context_and_caption(clip, context, "A visual event occurred.")

    assert result["caption_assessment"] == "not_assessed"
    assert result["context_assessment"] == "not_assessed"
    assert result["assessment_confidence"]["level"] == "not_assessed"
    assert result["assessment_confidence"]["basis"] == []
    assert result["findings"] == []
    assert "contradict" not in result["summary"].lower()
    assert set(result) == set(comparison_module.Comparison.model_fields)
    assert "caption_evidence_basis" not in result
    generate.assert_called_once()


def test_missing_caption_basis_fails_closed(monkeypatch, comparison_case):
    clip, context, valid, _ = comparison_case
    monkeypatch.setattr(comparison_module, "client_and_model", lambda stage: (Mock(), "test-model"))
    monkeypatch.setattr(comparison_module, "generate_json", Mock(return_value=valid))

    result = comparison_module.compare_context_and_caption(clip, context, "A visual event occurred.")

    assert result["caption_assessment"] == "not_assessed"
    assert result["findings"] == []


def test_null_caption_preserves_spoken_context_assessment(monkeypatch, comparison_case):
    clip, context, valid, _ = comparison_case
    monkeypatch.setattr(comparison_module, "client_and_model", lambda stage: (Mock(), "test-model"))
    monkeypatch.setattr(comparison_module, "generate_json", Mock(return_value=_generated(valid, "insufficient_evidence")))

    result = comparison_module.compare_context_and_caption(clip, context, None)

    assert result["caption_assessment"] == "not_provided"
    assert result["context_assessment"] == "material_context_missing"
    assert result["assessment_confidence"]["level"] == "moderate"
    assert result["findings"]


@pytest.mark.parametrize("basis", ["unobserved_modality", "direct_transcript"])
def test_audience_gesture_caption_cannot_be_contradicted_by_missing_transcript_detail(monkeypatch, comparison_case, basis):
    clip, context, valid, _ = comparison_case
    first = "Obviously, I didn't know Charlie Kirk. I was generally aware of some of his ideas."
    second = "I think those ideas were wrong. But that doesn't negate the fact that what happened was a tragedy and that I mourned for him and his family."
    clip["duration_seconds"] = 10.0
    clip["transcript"]["processed_ranges"][0]["end_seconds"] = 10.0
    clip["transcript"]["sentences"][0].update(text=first, start_seconds=0.0, end_seconds=10.0)
    context["windows"][0]["requested_end_seconds"] = 22.0
    for sentence, text, start, end in zip(context["windows"][0]["sentences"], (first, second), (0.0, 10.0), (10.0, 22.0), strict=True):
        sentence.update(text=text, start_seconds=start, end_seconds=end)
    evidence = valid["findings"][0]["evidence"]
    for item, sentence in zip(evidence, context["windows"][0]["sentences"], strict=True):
        item.update(quote=sentence["text"], start_seconds=sentence["start_seconds"], end_seconds=sentence["end_seconds"])
    valid["context_assessment"] = "no_material_difference_found"
    valid["caption_assessment"] = "contradicted_by_context"
    valid["findings"][0]["explanation"] = "The caption claims a stop gesture and applause, but the transcript contains no information about physical actions."
    valid["limitations"] = ["The caption's non-verbal claims cannot be verified or refuted by the transcript alone."]
    responses = [_generated(valid, basis)]
    if basis == "direct_transcript":
        responses.append({"relation": "not_assessable", "evidence": []})
    generate = Mock(side_effect=responses)
    monkeypatch.setattr(comparison_module, "client_and_model", lambda stage: (Mock(), "test-model"))
    monkeypatch.setattr(comparison_module, "generate_json", generate)

    result = comparison_module.compare_context_and_caption(clip, context, "The audience applauded and the speaker made a stop gesture.")

    assert result["caption_assessment"] == "not_assessed"
    assert result["assessment_confidence"]["level"] == "not_assessed"
    assert result["findings"] == []
    assert generate.call_count == len(responses)
    prompt = generate.call_args_list[0].args[2]
    assert "A transcript not mentioning an event is not evidence that the event did not happen." in prompt
    schema = generate.call_args_list[0].args[3]
    assert "caption_evidence_basis" in schema["required"]
    assert schema["properties"]["caption_evidence_basis"]["enum"] == [
        "direct_transcript", "absence_only", "unobserved_modality", "insufficient_evidence",
    ]


@pytest.mark.parametrize("assessment,relation,caption", [
    ("contradicted_by_context", "contradicts", "The speaker recommends canceling the program."),
    ("supported_by_context", "supports", "The speaker rejects canceling the program."),
])
def test_definitive_caption_requires_agreeing_source_grounding(monkeypatch, comparison_case, assessment, relation, caption):
    clip, context, valid, _ = comparison_case
    valid["caption_assessment"] = assessment
    generate = Mock(side_effect=[_generated(valid), _verification(valid, relation)])
    monkeypatch.setattr(comparison_module, "client_and_model", lambda stage: (Mock(), "test-model"))
    monkeypatch.setattr(comparison_module, "generate_json", generate)

    result = comparison_module.compare_context_and_caption(clip, context, caption)

    assert result["caption_assessment"] == assessment
    assert result["assessment_confidence"]["level"] == "moderate"
    assert result["findings"][0]["evidence"] == valid["findings"][0]["evidence"]
    assert set(result) == set(comparison_module.Comparison.model_fields)
    assert generate.call_count == 2
    assert generate.call_args_list[1].kwargs == {"max_attempts": 1}


def test_secondary_prompt_contains_only_original_sentences_and_caption(monkeypatch, comparison_case):
    clip, context, valid, _ = comparison_case
    valid["summary"] = "PRIMARY_SUMMARY_MUST_NOT_ANCHOR_VERIFIER"
    valid["findings"][0]["explanation"] = "PRIMARY_EXPLANATION_MUST_NOT_ANCHOR_VERIFIER"
    generate = Mock(side_effect=[_generated(valid), _verification(valid)])
    monkeypatch.setattr(comparison_module, "client_and_model", lambda stage: (Mock(), "test-model"))
    monkeypatch.setattr(comparison_module, "generate_json", generate)
    caption = "The speaker recommends canceling the program."

    comparison_module.compare_context_and_caption(clip, context, caption)

    prompt = generate.call_args_list[1].args[2]
    payload = json.loads(prompt.split("\n", 1)[1])
    assert payload == {
        "caption": caption, "clip_media_id": clip["media_id"],
        "clip_sentences": clip["transcript"]["sentences"],
        "source_media_id": context["source_media_id"],
        "source_sentences": context["windows"][0]["sentences"],
    }
    assert valid["summary"] not in prompt
    assert valid["findings"][0]["explanation"] not in prompt
    assert "caption_evidence_basis" not in prompt
    assert "contradicted_by_context" not in prompt
    schema = generate.call_args_list[1].args[3]
    assert schema["$defs"]["Evidence"]["properties"]["quote"]["enum"] == [FIRST, SECOND]
    assert schema["properties"]["relation"]["enum"] == ["supports", "contradicts", "not_assessable"]
    assert types.GenerateContentConfig(response_json_schema=schema).response_json_schema == schema
    assert comparison_module.Comparison.model_json_schema()["properties"].keys() == valid.keys()


@pytest.mark.parametrize("invalid", [
    "missing", "malformed", "unknown_relation", "extra_field", "disagreement",
    "not_assessable", "empty_evidence", "clip_evidence", "unknown_id", "invented_quote",
    "joined_quote", "excerpt_quote", "wrong_time", "repeated_id",
])
def test_invalid_or_ungrounded_secondary_check_abstains_once(monkeypatch, comparison_case, invalid):
    clip, context, valid, _ = comparison_case
    secondary = _verification(valid)
    if invalid == "missing":
        secondary = {}
    elif invalid == "malformed":
        secondary = [PRIVATE_RESPONSE]
    elif invalid == "unknown_relation":
        secondary["relation"] = "definitely_true"
    elif invalid == "extra_field":
        secondary["explanation"] = PRIVATE_RESPONSE
    elif invalid == "disagreement":
        secondary["relation"] = "supports"
    elif invalid == "not_assessable":
        secondary = {"relation": "not_assessable", "evidence": []}
    elif invalid == "empty_evidence":
        secondary["evidence"] = []
    elif invalid == "clip_evidence":
        secondary["evidence"][0].update(media_id="clip", sentence_ids=["clip_0"])
    elif invalid == "unknown_id":
        secondary["evidence"][0]["sentence_ids"] = ["source_invented"]
    elif invalid == "invented_quote":
        secondary["evidence"][0]["quote"] = PRIVATE_RESPONSE
    elif invalid == "joined_quote":
        secondary["evidence"][0].update(quote=FIRST + " " + SECOND, sentence_ids=["source_0", "source_1"], end_seconds=16.0)
    elif invalid == "excerpt_quote":
        secondary["evidence"][0]["quote"] = "recommend canceling the program"
    elif invalid == "wrong_time":
        secondary["evidence"][0]["start_seconds"] = 0.1
    elif invalid == "repeated_id":
        secondary["evidence"][0]["sentence_ids"] = ["source_0", "source_0"]
    generate = Mock(side_effect=[_generated(valid), secondary])
    monkeypatch.setattr(comparison_module, "client_and_model", lambda stage: (Mock(), "test-model"))
    monkeypatch.setattr(comparison_module, "generate_json", generate)

    result = comparison_module.compare_context_and_caption(clip, context, "The speaker recommends canceling the program.")

    assert result["context_assessment"] == "not_assessed"
    assert result["caption_assessment"] == "not_assessed"
    assert result["assessment_confidence"]["level"] == "not_assessed"
    assert result["assessment_confidence"]["basis"] == []
    assert result["findings"] == []
    assert any("separate transcript grounding check" in limit for limit in result["limitations"])
    assert PRIVATE_RESPONSE not in json.dumps(result)
    assert set(result) == set(comparison_module.Comparison.model_fields)
    assert generate.call_count == 2
    assert generate.call_args_list[1].kwargs == {"max_attempts": 1}


@pytest.mark.parametrize("error", [
    QuantroError(ErrorCode.PROCESSING_FAILED, Stage.COMPARE_CONTEXT_AND_CAPTION,
                 "Gemini request failed (HTTP 503).", retryable=True),
    RuntimeError(PRIVATE_RESPONSE),
])
def test_secondary_provider_failure_keeps_reportable_abstention(monkeypatch, comparison_case, error):
    clip, context, valid, _ = comparison_case
    client = Mock()
    generate = Mock(side_effect=[_generated(valid), error])
    monkeypatch.setattr(comparison_module, "client_and_model", lambda stage: (client, "test-model"))
    monkeypatch.setattr(comparison_module, "generate_json", generate)

    result = comparison_module.compare_context_and_caption(clip, context, "The speaker recommends canceling the program.")

    assert result["caption_assessment"] == "not_assessed"
    assert result["assessment_confidence"]["level"] == "not_assessed"
    assert result["findings"] == []
    assert PRIVATE_RESPONSE not in json.dumps(result)
    assert generate.call_count == 2
    assert generate.call_args_list[1].kwargs == {"max_attempts": 1}
    client.close.assert_called_once()
