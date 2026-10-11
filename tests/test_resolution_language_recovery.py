"""Offline regressions for language failures without weakening source evidence."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from backend.services.resolution import analysis
from backend.services.shared.errors import ErrorCode, QuantroError, Stage

PRIVATE = "gsk_private-key https://private.example/?token=secret"
PROMPT = "Supplied speech: We should protect the public interest."
VALUE = {"takeaways": "The speaker discusses public interest.", "meaning": "A policy argument."}


def groq_response(text=None):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=json.dumps(VALUE) if text is None else text),
    )])


def gemini_response(text=None, reason="STOP"):
    return SimpleNamespace(
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(value=reason))],
        text=json.dumps(VALUE) if text is None else text,
    )


@pytest.fixture
def providers(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-only-offline")
    monkeypatch.setenv("GEMINI_COMPARISON_MODEL", "")
    groq = Mock()
    groq.__enter__ = Mock(return_value=groq)
    groq.__exit__ = Mock(return_value=False)
    groq.chat.completions.create.return_value = groq_response()
    factory = Mock(return_value=groq)
    monkeypatch.setattr(analysis, "Groq", factory)
    # Avoid creating real TLS/HTTP resources while exercising provider ownership.
    monkeypatch.setattr(analysis.httpx, "Client", Mock())
    monkeypatch.setattr(analysis.ssl, "create_default_context", Mock())
    gemini = Mock()
    gemini.models.generate_content.return_value = gemini_response()
    fallback = Mock(return_value=(gemini, "configured-gemini-model"))
    monkeypatch.setattr(analysis, "client_and_model", fallback)
    events = Mock()
    monkeypatch.setattr(analysis, "record", events)
    return groq, factory, gemini, fallback, events


def test_valid_groq_response_preserves_original_provider(providers):
    groq, factory, gemini, fallback, events = providers
    assert analysis._json_response(PROMPT) == VALUE
    request = groq.chat.completions.create.call_args.kwargs
    assert request["messages"][1]["content"] == PROMPT
    assert factory.call_args.kwargs["max_retries"] == 0
    groq.__exit__.assert_called_once()
    fallback.assert_not_called()
    gemini.models.generate_content.assert_not_called()
    events.assert_called_once_with("language_assessment", "completed", {"provider": "groq"})


@pytest.mark.parametrize("status,reason", [
    (401, "authentication_failed"), (403, "authentication_failed"),
    (429, "rate_or_quota_limit"), (503, "provider_unavailable"),
])
def test_groq_status_recovers_using_same_evidence(providers, status, reason, caplog):
    groq, _, gemini, fallback, events = providers
    error = RuntimeError(PRIVATE)
    error.status_code = status
    groq.chat.completions.create.side_effect = error
    assert analysis._json_response(PROMPT) == VALUE
    fallback.assert_called_once_with(Stage.COMPARE_CONTEXT_AND_CAPTION)
    request = gemini.models.generate_content.call_args.kwargs
    assert request["contents"] == PROMPT
    assert request["config"]["response_mime_type"] == "application/json"
    assert request["config"]["temperature"] == 0
    gemini.close.assert_called_once()
    groq.__exit__.assert_called_once()
    assert events.call_args_list[0].args[2] == {
        "provider": "groq", "reason": reason, "status": status,
    }
    assert PRIVATE not in caplog.text
    assert PRIVATE not in repr(events.call_args_list)


@pytest.mark.parametrize("error", [
    TimeoutError(PRIVATE), ConnectionError(PRIVATE), httpx.ConnectError(PRIVATE),
])
def test_network_error_recovers_without_leaking_provider_error(providers, error, caplog):
    groq, _, gemini, _, events = providers
    groq.chat.completions.create.side_effect = error
    assert analysis._json_response(PROMPT) == VALUE
    assert events.call_args_list[0].args[2]["reason"] == "network_failed"
    assert PRIVATE not in caplog.text
    gemini.close.assert_called_once()


@pytest.mark.parametrize("invalid", ["not JSON", "[]", "null", ""])
def test_invalid_groq_json_repairs_once_then_recovers(providers, invalid):
    groq, _, gemini, _, events = providers
    groq.chat.completions.create.return_value = groq_response(invalid)
    assert analysis._json_response(PROMPT) == VALUE
    assert groq.chat.completions.create.call_count == 2
    assert all(PROMPT in call.kwargs["messages"][1]["content"]
               for call in groq.chat.completions.create.call_args_list)
    assert events.call_args_list[0].args[2]["reason"] == "invalid_response"
    gemini.close.assert_called_once()


def test_valid_groq_json_repair_does_not_invoke_fallback(providers):
    groq, _, _, fallback, _ = providers
    groq.chat.completions.create.side_effect = [groq_response("bad JSON"), groq_response()]
    assert analysis._json_response(PROMPT) == VALUE
    assert groq.chat.completions.create.call_count == 2
    fallback.assert_not_called()


def test_missing_groq_key_uses_existing_gemini_adapter(providers, monkeypatch):
    _, factory, gemini, fallback, events = providers
    monkeypatch.delenv("GROQ_API_KEY")
    monkeypatch.setenv("GEMINI_COMPARISON_MODEL", " scoped-gemini-model ")
    assert analysis._json_response(PROMPT) == VALUE
    factory.assert_not_called()
    fallback.assert_called_once_with(Stage.COMPARE_CONTEXT_AND_CAPTION)
    assert gemini.models.generate_content.call_args.kwargs["model"] == "scoped-gemini-model"
    assert events.call_args_list[0].args[2]["reason"] == "not_configured"
    gemini.close.assert_called_once()


def test_gemini_invalid_json_has_one_bounded_repair(providers, monkeypatch):
    _, _, gemini, _, _ = providers
    monkeypatch.delenv("GROQ_API_KEY")
    gemini.models.generate_content.side_effect = [gemini_response("bad JSON"), gemini_response()]
    assert analysis._json_response(PROMPT) == VALUE
    assert gemini.models.generate_content.call_count == 2
    assert all(PROMPT in call.kwargs["contents"]
               for call in gemini.models.generate_content.call_args_list)
    gemini.close.assert_called_once()


@pytest.mark.parametrize("responses", [
    [gemini_response("[]"), gemini_response("[]")],
    [gemini_response(reason="MAX_TOKENS")],
    [gemini_response(reason="SAFETY")],
    [SimpleNamespace(candidates=[], text="{}")],
    [TimeoutError(PRIVATE)],
])
def test_both_failed_providers_return_safe_typed_error(providers, responses, caplog):
    groq, _, gemini, _, events = providers
    groq.chat.completions.create.side_effect = RuntimeError(PRIVATE)
    gemini.models.generate_content.side_effect = responses
    with pytest.raises(QuantroError) as caught:
        analysis._json_response(PROMPT)
    assert caught.value.code is ErrorCode.PROCESSING_FAILED
    assert caught.value.stage is Stage.COMPARE_CONTEXT_AND_CAPTION
    assert caught.value.retryable is True
    assert PRIVATE not in json.dumps(caught.value.public_dict())
    assert PRIVATE not in caplog.text
    assert PRIVATE not in repr(events.call_args_list)
    assert gemini.models.generate_content.call_count <= 2
    gemini.close.assert_called_once()


def test_both_keys_missing_remains_a_typed_optional_failure(providers, monkeypatch):
    _, factory, _, fallback, _ = providers
    monkeypatch.delenv("GROQ_API_KEY")
    fallback.side_effect = QuantroError(
        ErrorCode.PROCESSING_FAILED, Stage.COMPARE_CONTEXT_AND_CAPTION,
        "GEMINI_API_KEY is not configured.",
    )
    with pytest.raises(QuantroError) as caught:
        analysis._json_response(PROMPT)
    factory.assert_not_called()
    assert caught.value.stage is Stage.COMPARE_CONTEXT_AND_CAPTION
    assert "GEMINI_API_KEY" not in caught.value.message


def features(*texts):
    return SimpleNamespace(transcript=SimpleNamespace(
        sentences=[SimpleNamespace(text=text) for text in texts],
    ))


def test_extractive_context_uses_quotes_without_semantic_verdict():
    result = analysis._extractive_context_summary(
        features("We should protect the public interest."),
        {"s1": SimpleNamespace(text="We must consider the cost as well.")},
    )
    assert '"We should protect the public interest."' in result
    assert '"We must consider the cost as well."' in result
    assert "not a conclusion" in result
    assert "authenticity were not checked" in result


@pytest.mark.parametrize("unsafe", [
    "/tmp/private", "/home/private", "file://private", "api_key=private",
    "<script>private</script>", "bearer private", "gsk_private", r"C:\private\file",
])
def test_extractive_quotes_filter_private_and_unsafe_markers(unsafe):
    result = analysis._extractive_clip_summary(features(unsafe, "Ordinary supplied speech."))
    assert unsafe not in result
    assert "Ordinary supplied speech." in result
    assert "A longer source recording was not confirmed" in result


def test_extractive_summary_with_no_speech_stays_honest():
    result = analysis._extractive_clip_summary(features())
    assert "No usable speech" in result
    assert "automated explanation is unavailable" in result
