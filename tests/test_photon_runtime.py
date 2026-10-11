"""Hosted Photon lifecycle checks without credentials, providers or child processes."""

import json
import signal
import subprocess
import threading
from unittest.mock import Mock

import pytest

from backend.scripts import start_photon_backend as runtime

SETTINGS = {
    "PORT": "10000",
    "PHOTON_ENABLED": "true",
    "SPECTRUM_PROJECT_ID": "test-project",
    "SPECTRUM_PROJECT_SECRET": "private-test-secret",
    "PHOTON_BRIDGE_TOKEN": "test-token-" + "a" * 32,
}


class Process:
    pid = 12345

    def __init__(self, status=None):
        self.status = status
        self.signals = []
        self.waits = []

    def poll(self):
        return self.status

    def wait(self, timeout):
        self.waits.append(timeout)
        return self.status


@pytest.fixture
def lifecycle(monkeypatch):
    handlers = {}
    signal_calls = []

    def register(kind, handler):
        signal_calls.append((kind, handler))
        handlers[kind] = handler
        return f"old-{kind}"

    def stop(process, *, kill=False):
        if process.poll() is None:
            process.signals.append("kill" if kill else "terminate")
            process.status = -9 if kill else -15

    monkeypatch.setattr(runtime.signal, "signal", register)
    monkeypatch.setattr(runtime, "_signal_child", stop)
    monkeypatch.setattr(runtime, "wait_for_bridge", Mock(return_value=True))
    monkeypatch.setattr(runtime.shutil, "which", lambda name: "/runtime/node")
    return handlers, signal_calls


def test_api_only_mode_execs_existing_server_without_photon_settings(monkeypatch):
    monkeypatch.setattr(runtime.os, "environ", {"PORT": "8000"})
    execute = Mock()
    spawn = Mock(side_effect=AssertionError("API-only mode must not spawn a sidecar"))
    monkeypatch.setattr(runtime.os, "execv", execute)
    monkeypatch.setattr(runtime.os, "chdir", Mock())
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    assert runtime.main() == 0
    execute.assert_called_once_with(runtime.sys.executable, runtime.api_command(8000))
    spawn.assert_not_called()


@pytest.mark.parametrize("missing", ["SPECTRUM_PROJECT_ID", "SPECTRUM_PROJECT_SECRET", "PHOTON_BRIDGE_TOKEN"])
def test_enabled_missing_settings_fail_before_any_process_without_secret_values(missing, monkeypatch, capsys):
    env = {key: value for key, value in SETTINGS.items() if key != missing}
    monkeypatch.setattr(runtime.os, "environ", env)
    spawn = Mock()
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    assert runtime.main() == 1
    message = capsys.readouterr().err
    assert missing in message
    assert all(value not in message for key, value in SETTINGS.items() if "SECRET" in key or "TOKEN" in key)
    spawn.assert_not_called()


def test_actual_api_port_overrides_stale_sidecar_port_without_mutating_input():
    env = {**SETTINGS, "PHOTON_BACKEND_PORT": "8000"}
    result = runtime.photon_environment(env, runtime.api_port(env))
    assert result["PHOTON_BACKEND_PORT"] == "10000"
    assert result["PHOTON_BRIDGE_PORT"] == "8766"
    assert env["PHOTON_BACKEND_PORT"] == "8000"


@pytest.mark.parametrize("changes", [
    {"PORT": "not-a-port"}, {"PORT": "65536"}, {"PORT": "80"},
    {"PHOTON_BRIDGE_PORT": "10000"}, {"PHOTON_BRIDGE_PORT": "0"},
    {"PHOTON_BRIDGE_TOKEN": "credential-that-must-not-appear"},
])
def test_invalid_enabled_configuration_is_rejected_safely(changes):
    env = {**SETTINGS, **changes}
    with pytest.raises(ValueError) as error:
        runtime.photon_environment(env, runtime.api_port(env))
    assert "credential-that-must-not-appear" not in str(error.value)


@pytest.mark.parametrize("body,expected", [
    ({"paths": {path: {method: {}} for path, method in runtime._REQUIRED_ROUTES}}, False),
    ({"paths": {"/analyze/{job_id}/run": {"post": {}}, "/reports/{report_id}": {"get": {}},
                "/ingest/{job_id}": {"post": {}, "get": {}}}}, True),
    ({"paths": {"/analyze/{job_id}/run": {"post": []}}}, False),
    ([], False),
])
def test_readiness_validates_all_existing_routes_over_fixed_loopback(body, expected, monkeypatch):
    response = Mock(status=200)
    response.read.return_value = json.dumps(body).encode()
    connection = Mock()
    connection.__enter__ = Mock(return_value=response)
    connection.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = connection
    create = Mock(return_value=opener)
    monkeypatch.setattr(runtime, "build_opener", create)
    assert runtime.openapi_ready(10000) is expected
    request = opener.open.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:10000/openapi.json"
    assert opener.open.call_args.kwargs["timeout"] == 2
    assert response.read.call_args.args == (512 * 1024 + 1,)
    assert create.call_args.args[0].proxies == {}
    assert isinstance(create.call_args.args[1], runtime._NoRedirect)


def test_no_sidecar_is_started_when_api_exits_before_readiness(lifecycle, monkeypatch):
    api = Process(status=7)
    spawn = Mock(return_value=api)
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    with pytest.raises(RuntimeError, match="API exited"):
        runtime.supervise(10000, SETTINGS)
    assert spawn.call_count == 1
    assert len(api.waits) == 1


def test_api_readiness_timeout_is_bounded(monkeypatch):
    monkeypatch.setattr(runtime, "openapi_ready", lambda port: False)
    monkeypatch.setattr(runtime.time, "monotonic", Mock(side_effect=[0, runtime.STARTUP_TIMEOUT_SECONDS + 1]))
    with pytest.raises(RuntimeError, match="startup deadline"):
        runtime.wait_for_api(Process(), 10000, threading.Event())


def test_bridge_readiness_requires_fixed_loopback_health_schema(monkeypatch):
    response = Mock(status=200)
    response.read.return_value = b'{"status":"ok","service":"quantro-photon"}'
    connection = Mock()
    connection.__enter__ = Mock(return_value=response)
    connection.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = connection
    monkeypatch.setattr(runtime, "build_opener", Mock(return_value=opener))
    assert runtime.bridge_ready(8766)
    assert opener.open.call_args.args[0].full_url == "http://127.0.0.1:8766/health"
    assert opener.open.call_args.kwargs["timeout"] == 2
    response.read.return_value = b'{"status":"ok","service":"unrelated-service"}'
    assert not runtime.bridge_ready(8766)


def test_bridge_startup_timeout_is_bounded(monkeypatch):
    monkeypatch.setattr(runtime, "bridge_ready", lambda port: False)
    monkeypatch.setattr(runtime.time, "monotonic", Mock(side_effect=[0, runtime.STARTUP_TIMEOUT_SECONDS + 1]))
    with pytest.raises(RuntimeError, match="Photon bridge.*startup deadline"):
        runtime.wait_for_bridge(Process(), Process(), 8766, threading.Event())


@pytest.mark.parametrize("api_status,photon_status", [(2, None), (None, 2)])
def test_either_child_exit_aborts_bridge_startup(api_status, photon_status, monkeypatch):
    health = Mock(side_effect=AssertionError("Dead processes cannot become ready"))
    monkeypatch.setattr(runtime, "bridge_ready", health)
    with pytest.raises(RuntimeError, match="service exited"):
        runtime.wait_for_bridge(Process(api_status), Process(photon_status), 8766, threading.Event())
    health.assert_not_called()


def test_signal_cancels_bridge_startup_without_probe(monkeypatch):
    stopping = threading.Event()
    stopping.set()
    health = Mock()
    monkeypatch.setattr(runtime, "bridge_ready", health)
    assert runtime.wait_for_bridge(Process(), Process(), 8766, stopping) is False
    health.assert_not_called()


def test_unready_bridge_stops_both_services_and_never_claims_ready(lifecycle, monkeypatch, capsys):
    api, photon = Process(), Process()
    monkeypatch.setattr(runtime.subprocess, "Popen", Mock(side_effect=[api, photon]))
    monkeypatch.setattr(runtime, "wait_for_api", Mock(return_value=True))
    monkeypatch.setattr(runtime, "wait_for_bridge", Mock(side_effect=RuntimeError("Photon bridge startup deadline")))
    with pytest.raises(RuntimeError, match="startup deadline"):
        runtime.supervise(10000, SETTINGS)
    assert api.signals == photon.signals == ["terminate"]
    assert "listener started" not in capsys.readouterr().out


def test_listener_failure_stops_api_and_reaps_both(lifecycle, monkeypatch):
    api, photon = Process(), Process(status=2)
    spawn = Mock(side_effect=[api, photon])
    ready = Mock(return_value=True)
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    monkeypatch.setattr(runtime, "wait_for_api", ready)
    assert runtime.supervise(10000, SETTINGS) == 2
    ready.assert_called_once()
    assert spawn.call_args_list[1].args[0] == ["/runtime/node", "--use-system-ca", "src/index.ts", "--imessage"]
    assert spawn.call_args_list[1].kwargs["cwd"] == runtime.PHOTON_ROOT
    assert api.signals == ["terminate"]
    assert api.waits and photon.waits
    assert lifecycle[1][-2:] == [(signal.SIGINT, f"old-{signal.SIGINT}"), (signal.SIGTERM, f"old-{signal.SIGTERM}")]


def test_sigterm_stops_intake_and_api_with_no_restart(lifecycle, monkeypatch):
    api, photon = Process(), Process()

    def spawn(command, **kwargs):
        if command[0] == "/runtime/node":
            lifecycle[0][signal.SIGTERM](signal.SIGTERM, None)
            return photon
        return api

    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    monkeypatch.setattr(runtime, "wait_for_api", Mock(return_value=True))
    assert runtime.supervise(10000, SETTINGS) == 128 + signal.SIGTERM
    assert photon.signals == api.signals == ["terminate"]
    assert photon.waits and api.waits


def test_node_spawn_failure_still_stops_started_api(lifecycle, monkeypatch):
    api = Process()
    monkeypatch.setattr(runtime.subprocess, "Popen", Mock(side_effect=[api, OSError("spawn failed")]))
    monkeypatch.setattr(runtime, "wait_for_api", Mock(return_value=True))
    with pytest.raises(OSError):
        runtime.supervise(10000, SETTINGS)
    assert api.signals == ["terminate"]
    assert api.waits


def test_unresponsive_children_are_killed_and_reaped(lifecycle):
    api, photon = Process(), Process()
    api.wait = Mock(side_effect=[subprocess.TimeoutExpired("api", 10), 0])
    photon.wait = Mock(side_effect=[subprocess.TimeoutExpired("photon", 10), 0])
    # Simulate children ignoring the graceful signal.
    original = runtime._signal_child

    def ignore_term(process, *, kill=False):
        if kill:
            process.status = None
            original(process, kill=True)

    runtime._signal_child = ignore_term
    try:
        runtime.stop_children([api, photon])
    finally:
        runtime._signal_child = original
    assert api.signals == photon.signals == ["kill"]
    assert api.wait.call_count == photon.wait.call_count == 2
