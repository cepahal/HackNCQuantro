"""Bound provider requests while retaining local citations and grounding."""
from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_comparison_repair import _generated, _verification

from backend.services.shared.errors import QuantroError
from backend.services.specialist import comparison as module

pytest_plugins = ["test_comparison_repair"]


@pytest.mark.parametrize("override,expected", [(None, "existing-model"), ("", "existing-model"),
                                               ("  ", "existing-model"),
                                               ("gemini-2.5-flash-lite", "gemini-2.5-flash-lite")])
def test_comparison_only_model_override(monkeypatch, override, expected):
    client = Mock()
    factory = Mock(return_value=(client, "existing-model"))
    monkeypatch.setattr(module, "_gemini_client_and_model", factory)
    monkeypatch.setenv("GEMINI_MODEL", "existing-model")
    if override is None:
        monkeypatch.delenv("GEMINI_COMPARISON_MODEL", raising=False)
    else:
        monkeypatch.setenv("GEMINI_COMPARISON_MODEL", override)
    assert module.client_and_model("compare_context_and_caption") == (client, expected)
    factory.assert_called_once_with("compare_context_and_caption")
    import os
    assert os.environ["GEMINI_MODEL"] == "existing-model"


def invoke(monkeypatch, case, responses, caption="Cancel it"):
    clip, context, _, _ = deepcopy(case)
    client = Mock()
    calls = []

    def generate(*args, **kwargs):
        calls.append((args, kwargs))
        assert kwargs == {"max_attempts": 1}
        assert len(calls) <= 3
        value = responses[len(calls) - 1]
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)

    monkeypatch.setattr(module, "client_and_model", lambda stage: (client, "test-model"))
    monkeypatch.setattr(module, "generate_json", generate)
    result = module.compare_context_and_caption(clip, context, caption)
    client.close.assert_called_once()
    return result, calls


def test_healthy_definitive_caption_has_two_requests(monkeypatch, comparison_case):
    valid = comparison_case[2]
    result, calls = invoke(monkeypatch, comparison_case, [_generated(valid), _verification(valid)])
    assert len(calls) == 2
    assert result["caption_assessment"] == "contradicted_by_context"


def test_one_evidence_repair_plus_grounding_has_three_requests(monkeypatch, comparison_case):
    valid, invalid = comparison_case[2:]
    result, calls = invoke(monkeypatch, comparison_case, [_generated(invalid), _generated(valid), _verification(valid)])
    assert len(calls) == 3
    assert result["assessment_confidence"]["level"] == "moderate"


@pytest.mark.parametrize("caption,basis", [(None, "direct_transcript"), ("Gesture claim", "unobserved_modality")])
def test_null_or_unassessable_caption_has_one_request(monkeypatch, comparison_case, caption, basis):
    result, calls = invoke(monkeypatch, comparison_case, [_generated(comparison_case[2], basis)], caption)
    assert len(calls) == 1
    assert result["caption_assessment"] == ("not_provided" if caption is None else "not_assessed")


def test_empty_source_context_never_creates_client(monkeypatch, comparison_case):
    clip, context, _, _ = deepcopy(comparison_case)
    context["windows"] = []
    factory = Mock(side_effect=AssertionError("No provider call allowed"))
    monkeypatch.setattr(module, "client_and_model", factory)
    result = module.compare_context_and_caption(clip, context, "Cancel it")
    assert result["caption_assessment"] == "not_assessed"
    factory.assert_not_called()


def test_grounding_disagreement_abstains_without_extra_repair(monkeypatch, comparison_case):
    valid = comparison_case[2]
    result, calls = invoke(monkeypatch, comparison_case, [_generated(valid), _verification(valid, "supports")])
    assert len(calls) == 2
    assert result["caption_assessment"] == "not_assessed"
    assert result["findings"] == []


def test_provider_error_does_not_multiply_requests(monkeypatch, comparison_case):
    client = Mock()
    generate = Mock(side_effect=QuantroError("PROCESSING_FAILED", "compare_context_and_caption", "Provider unavailable", True))
    monkeypatch.setattr(module, "client_and_model", lambda stage: (client, "test-model"))
    monkeypatch.setattr(module, "generate_json", generate)
    with pytest.raises(QuantroError):
        module.compare_context_and_caption(*comparison_case[:2], "Cancel it")
    assert generate.call_count == 1
    assert generate.call_args.kwargs == {"max_attempts": 1}
    client.close.assert_called_once()
