"""Private Gemini configuration takes precedence over tracked placeholders."""

import ssl
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from google import genai

from backend.services.shared.config import load_environment
from backend.services.shared.errors import QuantroError, Stage
from backend.services.specialist import gemini


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    monkeypatch.setattr(gemini.time, "sleep", Mock())


def test_explicit_process_key_is_not_overridden_by_local_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "backend").mkdir()
    (tmp_path / ".env").write_text("GEMINI_API_KEY=tracked-placeholder\n")
    (tmp_path / "backend" / ".env").write_text("GEMINI_API_KEY=backend-placeholder\n")
    (tmp_path / ".env.local").write_text("GEMINI_API_KEY=private-test-key\n")
    monkeypatch.setattr(gemini, "load_settings", lambda: load_environment(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "loaded-placeholder")
    monkeypatch.setenv("GEMINI_MODEL", "test-model")
    monkeypatch.setattr(genai, "Client", lambda **kwargs: kwargs)
    client, model = gemini.client_and_model(Stage.EXTRACT_MEDIA_FEATURES)
    assert client["api_key"] == "loaded-placeholder"
    assert model == "test-model"
    context = client["http_options"]["client_args"]["verify"]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert client["http_options"]["retry_options"]["attempts"] == 1
    assert client["http_options"]["timeout"] == 45000


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
    monkeypatch.setattr(gemini, "load_settings", lambda: load_environment(tmp_path))
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
