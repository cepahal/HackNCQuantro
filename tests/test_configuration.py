"""Startup configuration is deterministic and never replaces process settings."""

import os
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from backend.services.shared import config
from backend.services.specialist import gemini


def test_environment_files_load_once_with_explicit_precedence(monkeypatch, tmp_path):
    (tmp_path / ".secrets").mkdir()
    (tmp_path / "backend").mkdir()
    (tmp_path / ".secrets/.env").write_text("QUANTRO_CONFIG_TEST=vault\nQUANTRO_LITERAL_TEST=${PRIVATE_VALUE}\n")
    (tmp_path / ".env.local").write_text("QUANTRO_CONFIG_TEST=local\nQUANTRO_LOCAL_TEST=local\n")
    (tmp_path / ".env").write_text("QUANTRO_CONFIG_TEST=root\nQUANTRO_ROOT_TEST=root\n")
    (tmp_path / "backend/.env").write_text("QUANTRO_CONFIG_TEST=backend\nQUANTRO_BACKEND_TEST=backend\n")
    for name in ("QUANTRO_CONFIG_TEST", "QUANTRO_LITERAL_TEST", "QUANTRO_LOCAL_TEST", "QUANTRO_ROOT_TEST", "QUANTRO_BACKEND_TEST"):
        monkeypatch.delenv(name, raising=False)
    config.load_environment(tmp_path)
    assert os.environ["QUANTRO_CONFIG_TEST"] == "vault"
    assert os.environ["QUANTRO_LITERAL_TEST"] == "${PRIVATE_VALUE}"
    assert os.environ["QUANTRO_LOCAL_TEST"] == "local"
    assert os.environ["QUANTRO_ROOT_TEST"] == "root"
    assert os.environ["QUANTRO_BACKEND_TEST"] == "backend"
    monkeypatch.delenv("QUANTRO_CONFIG_TEST")
    (tmp_path / ".secrets/.env").write_text("QUANTRO_CONFIG_TEST=changed\n")
    config.load_environment(tmp_path)
    assert "QUANTRO_CONFIG_TEST" not in os.environ


@pytest.mark.parametrize("value", ["explicit", ""])
def test_explicit_process_values_even_blank_are_preserved(monkeypatch, tmp_path, value):
    (tmp_path / ".env.local").write_text("QUANTRO_CONFIG_TEST=private-file\n")
    monkeypatch.setenv("QUANTRO_CONFIG_TEST", value)
    config.load_environment(tmp_path)
    assert os.environ["QUANTRO_CONFIG_TEST"] == value


def test_gemini_reuses_central_settings_without_overriding(monkeypatch):
    load = Mock()
    monkeypatch.setattr(gemini, "load_environment", load)
    gemini.load_settings()
    load.assert_called_once_with()


@pytest.fixture
def production_config(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENVIRONMENT", "production")
    for name in ("EXA_API_KEY", "SEARCH_PROVIDER_API_KEY", "SEARCH_GOOGLE_CSE_ID", "GOOGLE_CSE_ID"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("provider", ["", "unknown", "bing"])
def test_startup_rejects_missing_unsupported_or_retired_search(production_config, monkeypatch, provider):
    monkeypatch.setenv("SEARCH_PROVIDER", provider)
    with pytest.raises(RuntimeError, match="supported SEARCH_PROVIDER"):
        config.validate_search_configuration()


@pytest.mark.parametrize("provider,key", [("exa", "EXA_API_KEY"), ("serpapi", "SEARCH_PROVIDER_API_KEY"), ("brave", "SEARCH_PROVIDER_API_KEY")])
def test_startup_checks_selected_provider_credential(production_config, monkeypatch, provider, key):
    monkeypatch.setenv("SEARCH_PROVIDER", provider)
    with pytest.raises(RuntimeError, match="missing its API credential"):
        config.validate_search_configuration()
    monkeypatch.setenv(key, "test-credential")
    config.validate_search_configuration()


def test_google_search_requires_engine_and_corpus_needs_no_web_key(production_config, monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDER", "google_cse")
    monkeypatch.setenv("SEARCH_PROVIDER_API_KEY", "test-credential")
    with pytest.raises(RuntimeError, match="search engine identifier"):
        config.validate_search_configuration()
    monkeypatch.setenv("SEARCH_GOOGLE_CSE_ID", "test-engine")
    config.validate_search_configuration()
    monkeypatch.setenv("SEARCH_PROVIDER", "tiger")
    config.validate_search_configuration()


def test_only_explicit_nonproduction_test_mode_skips_provider_gate(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("SEARCH_PROVIDER", "unknown")
    config.validate_search_configuration()
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(RuntimeError):
        config.validate_search_configuration()


def test_invalid_selected_search_provider_fails_before_database_connection(monkeypatch):
    from backend import main

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SEARCH_PROVIDER", "unsupported")
    monkeypatch.setenv("TIGER_DATABASE_URL", "sqlite://")
    monkeypatch.setattr(main, "configure_media_tools", lambda: None)
    connect = Mock(side_effect=AssertionError("Startup must reject config before contacting storage"))
    monkeypatch.setattr(main.TigerStore, "from_env", connect)
    with pytest.raises(RuntimeError, match="supported SEARCH_PROVIDER"), TestClient(main.app):
        pass
    connect.assert_not_called()


@pytest.mark.parametrize("pool_fails", [False, True])
def test_media_pool_is_lazy_and_closed_with_job_store(monkeypatch, pool_fails):
    from backend import main

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("TIGER_DATABASE_URL", "postgresql://test-only")
    monkeypatch.setattr(main, "configure_media_tools", lambda: None)
    monkeypatch.setattr(main, "validate_search_configuration", lambda: None)
    store, pool = Mock(), Mock()
    monkeypatch.setattr(main.TigerStore, "from_env", lambda: store)
    create_pool = Mock(side_effect=RuntimeError("Invalid media configuration") if pool_fails else None, return_value=pool)
    monkeypatch.setattr(main.TigerDataPostPool, "from_env", create_pool)
    if pool_fails:
        with pytest.raises(RuntimeError, match="Invalid media configuration"), TestClient(main.app):
            pass
        pool.close.assert_not_called()
    else:
        with TestClient(main.app):
            assert main.app.state.media_pool is pool
            pool.acquire.assert_not_called()
        pool.close.assert_called_once()
    store.ensure_schema.assert_called_once()
    store.close.assert_called_once()
    create_pool.assert_called_once()
