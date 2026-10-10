"""Gemini reads process settings; local env files only fill keys the process lacks."""

import logging
import ssl
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from google import genai

from backend.services.shared import settings
from backend.services.shared.errors import QuantroError, Stage
from backend.services.specialist import gemini


def test_process_key_wins_over_local_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env.local").write_text("GEMINI_API_KEY=file-key\nGEMINI_MODEL=file-model\n")
    monkeypatch.setenv("GEMINI_API_KEY", "process-key")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    settings.load_env_files(tmp_path)
    monkeypatch.setattr(genai, "Client", lambda **kwargs: kwargs)
    client, model = gemini.client_and_model(Stage.EXTRACT_MEDIA_FEATURES)
    assert client["api_key"] == "process-key"
    assert model == "file-model"
    context = client["http_options"]["client_args"]["verify"]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_env_files_are_not_reread_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    settings.load_environment()
    reads = []
    monkeypatch.setattr(settings, "load_env_files", lambda *args, **kwargs: reads.append(args))
    monkeypatch.setenv("GEMINI_API_KEY", "process-key")
    monkeypatch.setattr(genai, "Client", lambda **kwargs: kwargs)
    gemini.client_and_model(Stage.EXTRACT_MEDIA_FEATURES)
    gemini.client_and_model(Stage.EXTRACT_MEDIA_FEATURES)
    assert reads == []


def test_missing_key_is_logged_but_not_named_to_clients(monkeypatch, caplog) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "")
    with caplog.at_level(logging.ERROR), pytest.raises(QuantroError) as error:
        gemini.client_and_model(Stage.COMPARE_CONTEXT_AND_CAPTION)
    assert "GEMINI_API_KEY" not in error.value.message
    assert "GEMINI_API_KEY" in caplog.text


class ProviderFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__("private provider details")


@pytest.mark.parametrize("failure,retryable", [
    (httpx.ConnectError("private connection details"), True),
    (httpx.ReadTimeout("private timeout details"), True),
    (ProviderFailure(429), True),
    (ProviderFailure(402), False),
    (ValueError("private constructor details"), False),
])
def test_constructor_failures_are_safe_stage_errors(monkeypatch, tmp_path, failure, retryable):
    (tmp_path / ".env.local").write_text("GEMINI_API_KEY=private-test-key\n")
    monkeypatch.setenv("GEMINI_API_KEY", "loaded-test-key")
    monkeypatch.setattr(gemini, "__file__", str(tmp_path / "backend/services/specialist/gemini.py"))
    monkeypatch.setattr(genai, "Client", Mock(side_effect=failure))

    with pytest.raises(QuantroError) as error:
        gemini.client_and_model(Stage.EXTRACT_MEDIA_FEATURES)

    assert error.value.stage is Stage.EXTRACT_MEDIA_FEATURES
    assert error.value.code.value == "PROCESSING_FAILED"
    assert error.value.retryable is retryable
    assert "private" not in error.value.message


@pytest.mark.parametrize("failure,retryable", [
    (httpx.ConnectError("private connection details"), True),
    (httpx.ReadTimeout("private timeout details"), True),
    (ProviderFailure(429), True),
    (ProviderFailure(402), False),
    (ProviderFailure(400), False),
])
def test_generation_transport_and_payment_errors_preserve_retryability(failure, retryable):
    client = Mock()
    client.models.generate_content.side_effect = failure

    with pytest.raises(QuantroError) as error:
        gemini.generate_json(client, "test-model", "test content", {}, Stage.COMPARE_CONTEXT_AND_CAPTION)

    assert error.value.stage is Stage.COMPARE_CONTEXT_AND_CAPTION
    assert error.value.retryable is retryable
    assert "private" not in error.value.message
