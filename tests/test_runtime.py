"""Media-tool startup checks need no downloads, database, or provider calls."""

import os
from unittest.mock import Mock, call

import pytest
from fastapi.testclient import TestClient

from backend.services.shared import runtime


@pytest.fixture(autouse=True)
def no_configured_directory(monkeypatch):
    monkeypatch.delenv("MEDIA_TOOLS_DIR", raising=False)


def test_existing_tools_are_both_checked_without_changing_path(monkeypatch):
    monkeypatch.setenv("PATH", "existing-path")
    which = Mock(side_effect=["/tools/ffmpeg", "/tools/ffprobe"])
    monkeypatch.setattr(runtime.shutil, "which", which)

    runtime.configure_media_tools()

    assert which.call_args_list == [call("ffmpeg"), call("ffprobe")]
    assert os.environ["PATH"] == "existing-path"


@pytest.mark.parametrize("missing", [("ffmpeg",), ("ffprobe",), ("ffmpeg", "ffprobe")])
def test_missing_tools_fail_with_names_and_operator_action(monkeypatch, missing):
    which = Mock(side_effect=lambda name: None if name in missing else f"/tools/{name}")
    monkeypatch.setattr(runtime.shutil, "which", which)

    with pytest.raises(RuntimeError) as caught:
        runtime.configure_media_tools()

    assert ", ".join(missing) in str(caught.value)
    assert "MEDIA_TOOLS_DIR" in str(caught.value)
    assert "Install FFmpeg" in str(caught.value)
    assert "restart the backend" in str(caught.value)
    assert which.call_args_list == [call("ffmpeg"), call("ffprobe")]


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_invalid_tools_directory_fails_before_lookup(monkeypatch, tmp_path, kind):
    directory = tmp_path / "tools"
    if kind == "file":
        directory.write_text("not a directory")
    monkeypatch.setenv("MEDIA_TOOLS_DIR", str(directory))
    monkeypatch.setenv("PATH", "existing-path")
    which = Mock()
    monkeypatch.setattr(runtime.shutil, "which", which)

    with pytest.raises(RuntimeError, match="MEDIA_TOOLS_DIR must name an existing directory"):
        runtime.configure_media_tools()

    which.assert_not_called()
    assert os.environ["PATH"] == "existing-path"


def test_explicit_directory_is_resolved_prepended_and_not_duplicated(monkeypatch, tmp_path):
    directory = tmp_path / "tools"
    directory.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEDIA_TOOLS_DIR", "tools")
    monkeypatch.setenv("PATH", "existing-path")
    expected = str(directory.resolve()) + os.pathsep + "existing-path"

    def lookup(name):
        assert os.environ["PATH"] == expected
        return str(directory / name)

    which = Mock(side_effect=lookup)
    monkeypatch.setattr(runtime.shutil, "which", which)
    runtime.configure_media_tools()
    runtime.configure_media_tools()

    assert os.environ["PATH"] == expected
    assert which.call_args_list == [call("ffmpeg"), call("ffprobe")] * 2


def test_explicit_directory_makes_both_executables_discoverable(monkeypatch, tmp_path):
    for name in ("ffmpeg", "ffprobe"):
        executable = tmp_path / (name + ".exe" if os.name == "nt" else name)
        executable.write_bytes(b"test lookup only; never executed")
        executable.chmod(0o755)
    monkeypatch.setenv("MEDIA_TOOLS_DIR", str(tmp_path))
    monkeypatch.setenv("PATH", "")

    runtime.configure_media_tools()

    assert runtime.shutil.which("ffmpeg") is not None
    assert runtime.shutil.which("ffprobe") is not None


def test_media_preflight_fails_before_tiger_connection(monkeypatch):
    from backend import main

    preflight = Mock(side_effect=RuntimeError("Required media tools are unavailable: ffmpeg."))
    connect = Mock(side_effect=AssertionError("Database must not be contacted"))
    monkeypatch.setattr(main, "configure_media_tools", preflight)
    monkeypatch.setattr(main.TigerStore, "from_env", connect)

    with pytest.raises(RuntimeError, match="Required media tools are unavailable"), TestClient(main.app):
        pass

    preflight.assert_called_once()
    connect.assert_not_called()
