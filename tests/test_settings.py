"""Local env files fill only unset settings, in one documented precedence order."""

import os
from pathlib import Path

import pytest

from backend.services.shared import settings

KEYS = ("QUANTRO_TEST_VAULT", "QUANTRO_TEST_LOCAL", "QUANTRO_TEST_ROOT", "QUANTRO_TEST_BACKEND", "QUANTRO_TEST_PROCESS")


def test_env_file_precedence_and_process_environment_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".secrets").mkdir()
    (tmp_path / "backend").mkdir()
    every_key = "".join(f"{key}={{}}\n" for key in KEYS)
    (tmp_path / ".secrets" / ".env").write_text("QUANTRO_TEST_VAULT=vault\nQUANTRO_TEST_PROCESS=vault\n")
    (tmp_path / ".env.local").write_text(every_key.format("local", "local", "local", "local", "local"))
    (tmp_path / ".env").write_text(every_key.format("root", "root", "root", "root", "root"))
    (tmp_path / "backend" / ".env").write_text(every_key.format(*["backend"] * 5))
    monkeypatch.setenv("QUANTRO_TEST_PROCESS", "process")

    settings.load_env_files(tmp_path)

    assert [os.environ[key] for key in KEYS] == ["vault", "local", "local", "local", "process"]


def test_later_files_fill_keys_missing_from_earlier_ones(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "backend").mkdir()
    (tmp_path / ".env").write_text("QUANTRO_TEST_ROOT=root\n")
    (tmp_path / "backend" / ".env").write_text("QUANTRO_TEST_ROOT=backend\nQUANTRO_TEST_BACKEND=backend\n")

    settings.load_env_files(tmp_path)

    assert os.environ["QUANTRO_TEST_ROOT"] == "root"
    assert os.environ["QUANTRO_TEST_BACKEND"] == "backend"
