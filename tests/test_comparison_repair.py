"""A bounded re-ask fixes formatting without accepting fabricated evidence."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from google.genai import types

from backend.services.shared.errors import ErrorCode, QuantroError, Stage
from backend.services.specialist import comparison as comparison_module

FIRST = "If those numbers were accurate, I would recommend canceling the program."
SECOND = "But those numbers are not accurate, so that is not my recommendation."
PRIVATE_RESPONSE = "MODEL_RESPONSE_MUST_NOT_BE_ECHOED"


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
    generate = Mock(side_effect=[invalid, valid])
    monkeypatch.setattr(comparison_module, "client_and_model", lambda stage: (client, "test-model"))
    monkeypatch.setattr(comparison_module, "generate_json", generate)

    result = comparison_module.compare_context_and_caption(clip, context, "Cancel it")

    assert result["context_assessment"] == "material_context_missing"
    assert result["assessment_confidence"]["level"] == "moderate"
    assert len(result["findings"][0]["evidence"]) == 2
    assert generate.call_count == 2
    first_prompt = generate.call_args_list[0].args[2]
    repair_prompt = generate.call_args_list[1].args[2]
    assert "source_0" in first_prompt and "source_1" in first_prompt
    assert FIRST in repair_prompt and SECOND in repair_prompt
    assert PRIVATE_RESPONSE not in repair_prompt
    assert repair_prompt != first_prompt
    for call in generate.call_args_list:
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
    generate = Mock(return_value=invalid)
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
    generate = Mock(return_value=valid)
    monkeypatch.setattr(comparison_module, "client_and_model", lambda stage: (client, "test-model"))
    monkeypatch.setattr(comparison_module, "generate_json", generate)

    comparison_module.compare_context_and_caption(clip, context, "Cancel it")

    schema = generate.call_args.args[3]
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
    generate = Mock(return_value=valid)
    monkeypatch.setattr(comparison_module, "client_and_model", lambda stage: (client, "test-model"))
    monkeypatch.setattr(comparison_module, "generate_json", generate)
    first_only = "The first request has a unique submitted sentence."
    second_only = "The second request has a different submitted sentence."
    clip["transcript"]["sentences"][0]["text"] = first_only
    comparison_module.compare_context_and_caption(clip, context, "Cancel it")
    clip["transcript"]["sentences"][0]["text"] = second_only
    comparison_module.compare_context_and_caption(clip, context, "Cancel it")

    first_schema, second_schema = [call.args[3] for call in generate.call_args_list]
    assert first_schema is not second_schema
    assert first_schema["$defs"]["Evidence"]["properties"]["quote"]["enum"] == [first_only, FIRST, SECOND]
    assert second_schema["$defs"]["Evidence"]["properties"]["quote"]["enum"] == [second_only, FIRST, SECOND]
    assert comparison_module.Comparison.model_json_schema() == canonical_schema
    assert "enum" not in canonical_schema["$defs"]["Evidence"]["properties"]["quote"]
