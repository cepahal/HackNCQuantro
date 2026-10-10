"""Gemini failures stay actionable, bounded, and free of provider secrets."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from backend.services.shared.errors import ErrorCode, QuantroError, Stage
from backend.services.specialist import gemini

STAGE = Stage.COMPARE_CONTEXT_AND_CAPTION
PRIVATE = "private-api-key provider-body https://private.example/?key=hidden"


class ProviderFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(PRIVATE)


def response(text='{"summary":"ok"}', reason="STOP", candidates=True):
    return SimpleNamespace(
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(value=reason))]
        if candidates else [],
        text=text,
    )


@pytest.fixture
def sleep(monkeypatch):
    sleeping = Mock()
    monkeypatch.setattr(gemini.time, "sleep", sleeping)
    return sleeping


def generate(client):
    return gemini.generate_json(
        client, "test-model", "untrusted evidence", {"type": "object"}, STAGE,
    )


def test_success_preserves_structured_output_request(sleep):
    client = Mock()
    client.models.generate_content.return_value = response()

    assert generate(client) == {"summary": "ok"}

    client.models.generate_content.assert_called_once()
    kwargs = client.models.generate_content.call_args.kwargs
    assert kwargs["model"] == "test-model"
    assert kwargs["contents"] == "untrusted evidence"
    assert kwargs["config"]["response_mime_type"] == "application/json"
    assert kwargs["config"]["response_json_schema"] == {"type": "object"}
    assert kwargs["config"]["max_output_tokens"] == 8192
    sleep.assert_not_called()


def test_transient_failure_recovers_before_returning(sleep):
    client = Mock()
    client.models.generate_content.side_effect = [ProviderFailure(503), response()]

    assert generate(client) == {"summary": "ok"}
    assert client.models.generate_content.call_count == 2
    sleep.assert_called_once_with(2)


def test_single_attempt_override_never_retries_transient_failure(sleep):
    client = Mock()
    client.models.generate_content.side_effect = ProviderFailure(503)

    with pytest.raises(QuantroError, match="HTTP 503"):
        gemini.generate_json(client, "test-model", "evidence", {}, STAGE, max_attempts=1)

    client.models.generate_content.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.parametrize("attempts", [0, 4, True, 1.5, "1"])
def test_attempt_override_cannot_disable_or_expand_bound(sleep, attempts):
    client = Mock()

    with pytest.raises(ValueError, match="integer from 1 to 3"):
        gemini.generate_json(client, "test-model", "evidence", {}, STAGE, max_attempts=attempts)

    client.models.generate_content.assert_not_called()
    sleep.assert_not_called()


@pytest.mark.parametrize("failure,diagnostic", [
    (ProviderFailure(429), "HTTP 429"),
    (ProviderFailure(500), "HTTP 500"),
    (ProviderFailure(502), "HTTP 502"),
    (ProviderFailure(503), "HTTP 503"),
    (ProviderFailure(504), "HTTP 504"),
    (httpx.ReadTimeout(PRIVATE), "timed out"),
    (httpx.ConnectError(PRIVATE), "connection"),
])
def test_transient_errors_stop_after_three_safe_attempts(sleep, caplog, failure, diagnostic):
    client = Mock()
    client.models.generate_content.side_effect = failure

    with pytest.raises(QuantroError) as caught:
        generate(client)

    error = caught.value
    assert error.code is ErrorCode.PROCESSING_FAILED
    assert error.stage is STAGE
    assert error.retryable is True
    assert diagnostic in error.message
    assert client.models.generate_content.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [2, 4]
    assert PRIVATE not in json.dumps(error.to_dict())
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404])
def test_permanent_provider_failure_is_not_retried(sleep, caplog, status):
    client = Mock()
    client.models.generate_content.side_effect = ProviderFailure(status)

    with pytest.raises(QuantroError) as caught:
        generate(client)

    assert caught.value.message == f"Gemini request failed (HTTP {status})."
    assert caught.value.retryable is False
    assert client.models.generate_content.call_count == 1
    sleep.assert_not_called()
    assert PRIVATE not in str(caught.value)
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("bad_response,diagnostic", [
    (response(candidates=False), "candidate"),
    (response(text=""), "empty"),
    (response(text=None), "empty"),
    (response(text="  \n"), "empty"),
    (response(reason="MAX_TOKENS"), "MAX_TOKENS"),
    (response(reason="SAFETY"), "SAFETY"),
    (response(text=PRIVATE), "JSON"),
    (response(reason=PRIVATE), "UNKNOWN"),
])
def test_invalid_responses_are_diagnosed_without_echoing_content(sleep, caplog, bad_response, diagnostic):
    client = Mock()
    client.models.generate_content.return_value = bad_response

    with pytest.raises(QuantroError) as caught:
        generate(client)

    assert diagnostic.lower() in caught.value.message.lower()
    assert caught.value.retryable is False
    assert caught.value.stage is STAGE
    assert client.models.generate_content.call_count == 1
    sleep.assert_not_called()
    assert PRIVATE not in json.dumps(caught.value.to_dict())
    assert PRIVATE not in caplog.text


def test_callers_custom_error_code_survives_adapter_failure(sleep):
    client = Mock()
    client.models.generate_content.side_effect = ProviderFailure(403)

    with pytest.raises(QuantroError) as caught:
        gemini.generate_json(
            client, "test-model", "evidence", {},
            Stage.EXTRACT_MEDIA_FEATURES, code=ErrorCode.TRANSCRIPTION_FAILED,
        )

    assert caught.value.code is ErrorCode.TRANSCRIPTION_FAILED
    assert caught.value.stage is Stage.EXTRACT_MEDIA_FEATURES


@pytest.mark.parametrize("status", [PRIVATE, True, 999])
def test_untrusted_exception_status_is_never_echoed(sleep, caplog, status):
    client = Mock()
    client.models.generate_content.side_effect = ProviderFailure(status)

    with pytest.raises(QuantroError) as caught:
        generate(client)

    assert caught.value.message == "Gemini request failed."
    assert caught.value.retryable is False
    assert client.models.generate_content.call_count == 1
    sleep.assert_not_called()
    assert PRIVATE not in caplog.text
