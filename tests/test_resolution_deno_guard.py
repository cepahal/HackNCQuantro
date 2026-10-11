"""The resolver permits one sandboxed JS runtime, without relaxing URL guards."""
import pytest

from backend.services.external_apis._retrieval_worker import UnsafeDestinationError
from backend.services.resolution import download


@pytest.fixture
def deno(monkeypatch, tmp_path):
    binary = tmp_path / "deno"
    binary.write_text("test runtime")
    monkeypatch.setattr(download, "_find_tool", lambda name: str(binary) if name == "deno" else None)
    return str(binary)


def test_exact_deno_solver_is_allowed_with_sanitized_environment(deno):
    environment = {"PATH": "/usr/bin", "LC_CTYPE": "C.UTF-8", "HTTP_PROXY": ""}
    command = [deno, "run", *sorted(download._DENO_FLAGS), "-"]
    download._resolver_audit("subprocess.Popen", (deno, command, None, environment))
    download._resolver_audit("subprocess.Popen", (deno, [deno, "--version"], None, {}))


@pytest.mark.parametrize("key,value", [("GROQ_API_KEY", "private"), ("NODE_OPTIONS", "private"), ("HTTP_PROXY", "http://localhost")])
def test_deno_child_environment_is_validated_before_execution(deno, key, value):
    command = [deno, "run", *sorted(download._DENO_FLAGS), "-"]
    with pytest.raises(UnsafeDestinationError):
        download._resolver_audit("subprocess.Popen", (deno, command, None, {key: value}))


@pytest.mark.parametrize("extra", ["--allow-net", "--allow-all", "--allow-read", "--allow-env", "--allow-run", "--unsafely-ignore-certificate-errors", "script.js"])
def test_extra_permissions_and_arguments_are_denied(deno, extra):
    command = [deno, "run", *sorted(download._DENO_FLAGS), extra, "-"]
    with pytest.raises(UnsafeDestinationError):
        download._resolver_audit("subprocess.Popen", (deno, command, None, {}))


def test_missing_guard_flags_and_other_executables_are_denied(deno):
    command = [deno, "run", *sorted(download._DENO_FLAGS - {"--no-remote"}), "-"]
    with pytest.raises(UnsafeDestinationError):
        download._resolver_audit("subprocess.Popen", (deno, command, None, {}))
    with pytest.raises(UnsafeDestinationError):
        download._resolver_audit("subprocess.Popen", ("node", ["node", "--version"], None, {}))


def test_public_network_guard_still_rejects_private_addresses():
    with pytest.raises(UnsafeDestinationError):
        download._resolver_audit("socket.connect", (None, ("127.0.0.1", 80)))


def test_worker_environment_drops_secrets_and_proxy_settings():
    assert download._child_environment({"PATH": "bin", "HTTPS_PROXY": "proxy", "GEMINI_API_KEY": "secret"}) == {"PATH": "bin"}


@pytest.mark.skipif(download.os.name != "nt", reason="Windows audit command-line representation")
def test_windows_canonical_command_line_is_validated(deno):
    command = [deno, "run", *sorted(download._DENO_FLAGS), "-"]
    download._resolver_audit("subprocess.Popen", (None, download.subprocess.list2cmdline(command), None, {}))
    with pytest.raises(UnsafeDestinationError):
        download._resolver_audit("subprocess.Popen", (None, download.subprocess.list2cmdline(command + ["--allow-net"]), None, {}))
