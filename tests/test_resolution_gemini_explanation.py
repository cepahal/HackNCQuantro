"""Gemini explanations preserve strict JSON, bounded calls and safe failures."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.services.resolution import analysis
from backend.services.shared.errors import ErrorCode, QuantroError, Stage

STAGE = Stage.COMPARE_CONTEXT_AND_CAPTION
PRIVATE = "private-provider-key https://private.example/?token=secret"


def response(text='{"takeaways":"He orders coffee.","meaning":"An everyday request."}', reason="STOP"):
    return SimpleNamespace(
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(value=reason))],
        text=text,
    )


def wire(monkeypatch, responses):
    client = Mock()
    client.models.generate_content.side_effect = responses
    factory = Mock(return_value=(client, "configured-gemini-model"))
    monkeypatch.setattr(analysis, "client_and_model", factory)
    monkeypatch.setenv("GEMINI_COMPARISON_MODEL", "")
    return client, factory


@pytest.mark.parametrize("override,expected", [
    (None, "configured-gemini-model"),
    ("", "configured-gemini-model"),
    ("  ", "configured-gemini-model"),
    (" gemini-2.5-flash-lite ", "gemini-2.5-flash-lite"),
])
def test_explanation_uses_gemini_with_scoped_model_override(monkeypatch, override, expected):
    client, factory = wire(monkeypatch, [response()])
    if override is None:
        monkeypatch.delenv("GEMINI_COMPARISON_MODEL")
    else:
        monkeypatch.setenv("GEMINI_COMPARISON_MODEL", override)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_LLM_MODEL", "must-not-select-this-model")
    prompt = "Explain only this untrusted transcript: I would like a coffee."

    result = analysis._json_response(prompt)

    assert result == {"takeaways": "He orders coffee.", "meaning": "An everyday request."}
    factory.assert_called_once_with(STAGE)
    client.models.generate_content.assert_called_once()
    request = client.models.generate_content.call_args.kwargs
    assert request["model"] == expected
    assert prompt in request["contents"]
    assert request["config"]["response_mime_type"] == "application/json"
    assert request["config"]["temperature"] == 0
    assert request["config"]["max_output_tokens"] == 8192
    client.close.assert_called_once()


def test_invalid_json_is_repaired_once_without_changing_evidence(monkeypatch):
    client, factory = wire(monkeypatch, [response("not JSON"), response('{"summary":"Observed speech only."}')])
    prompt = "Actual transcript evidence must remain present on repair."

    assert analysis._json_response(prompt) == {"summary": "Observed speech only."}
    assert client.models.generate_content.call_count == 2
    assert all(prompt in call.kwargs["contents"]
               for call in client.models.generate_content.call_args_list)
    factory.assert_called_once()
    client.close.assert_called_once()


@pytest.mark.parametrize("text", ["not JSON", "[]", '"a plain string"', "null", ""])
def test_unusable_json_never_exceeds_two_requests(monkeypatch, text):
    client, _ = wire(monkeypatch, [response(text), response(text)])
    with pytest.raises(QuantroError) as caught:
        analysis._json_response("Supplied speech")
    assert caught.value.code is ErrorCode.PROCESSING_FAILED
    assert caught.value.stage is STAGE
    assert 1 <= client.models.generate_content.call_count <= 2
    client.close.assert_called_once()


@pytest.mark.parametrize("reason", ["SAFETY", "MAX_TOKENS", "RECITATION"])
def test_noncompleted_response_is_not_presented_as_an_explanation(monkeypatch, reason):
    client, _ = wire(monkeypatch, [response(reason=reason)])
    with pytest.raises(QuantroError) as caught:
        analysis._json_response("Supplied speech")
    assert caught.value.code is ErrorCode.PROCESSING_FAILED
    assert caught.value.stage is STAGE
    client.models.generate_content.assert_called_once()
    client.close.assert_called_once()


def test_typed_provider_failure_is_preserved_and_client_closed(monkeypatch):
    error = QuantroError(ErrorCode.PROCESSING_FAILED, STAGE, "Gemini request failed (HTTP 503).", True)
    client, _ = wire(monkeypatch, [error])
    with pytest.raises(QuantroError) as caught:
        analysis._json_response("Supplied speech")
    assert caught.value is error
    assert caught.value.retryable is True
    client.models.generate_content.assert_called_once()
    client.close.assert_called_once()


def test_unexpected_provider_failure_does_not_leak_private_diagnostics(monkeypatch, caplog):
    client, _ = wire(monkeypatch, [RuntimeError(PRIVATE)])
    with pytest.raises(QuantroError) as caught:
        analysis._json_response("Supplied speech")
    assert caught.value.code is ErrorCode.PROCESSING_FAILED
    assert caught.value.stage is STAGE
    assert PRIVATE not in json.dumps(caught.value.public_dict())
    assert PRIVATE not in caplog.text
    client.models.generate_content.assert_called_once()
    client.close.assert_called_once()


def test_unconfigured_gemini_does_not_fall_back_to_groq(monkeypatch):
    error = QuantroError(ErrorCode.PROCESSING_FAILED, STAGE, "The analysis provider is not configured.")
    factory = Mock(side_effect=error)
    monkeypatch.setattr(analysis, "client_and_model", factory)
    monkeypatch.setenv("GROQ_API_KEY", "test-only-not-a-live-key")
    with pytest.raises(QuantroError) as caught:
        analysis._json_response("Supplied speech")
    assert caught.value is error
    factory.assert_called_once_with(STAGE)
