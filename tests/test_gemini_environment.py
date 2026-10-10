"""Private Gemini configuration takes precedence over tracked placeholders."""

import ssl
from pathlib import Path

import pytest
from google import genai

from backend.services.reporting.errors import Stage
from backend.services.specialist import gemini


def test_private_local_key_overrides_placeholders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "backend").mkdir()
    (tmp_path / ".env").write_text("GEMINI_API_KEY=tracked-placeholder\n")
    (tmp_path / "backend" / ".env").write_text("GEMINI_API_KEY=backend-placeholder\n")
    (tmp_path / ".env.local").write_text("GEMINI_API_KEY=private-test-key\n")
    monkeypatch.setattr(
        gemini, "__file__", str(tmp_path / "backend/services/specialist/gemini.py")
    )
    monkeypatch.setenv("GEMINI_API_KEY", "loaded-placeholder")
    monkeypatch.setenv("GEMINI_MODEL", "test-model")
    monkeypatch.setattr(genai, "Client", lambda **kwargs: kwargs)
    client, model = gemini.client_and_model(Stage.EXTRACT_MEDIA_FEATURES)
    assert client["api_key"] == "private-test-key"
    assert model == "test-model"
    context = client["http_options"]["client_args"]["verify"]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
