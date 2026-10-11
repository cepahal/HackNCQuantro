"""Run the API, optionally supervising its existing private Spectrum sidecar."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PHOTON_ROOT = BACKEND_ROOT / "services" / "photon"
STARTUP_TIMEOUT_SECONDS = 90.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0
_REQUIRED_ROUTES = (
    ("/analyze/{job_id}/run", "post"),
    ("/reports/{report_id}", "get"),
    ("/ingest/{job_id}", "post"),
    ("/ingest/{job_id}", "get"),
)


def api_port(env: Mapping[str, str]) -> int:
    value = env.get("PORT", "80")
    if not re.fullmatch(r"[0-9]{1,5}", value) or not 1 <= int(value) <= 65_535:
        raise ValueError("PORT must be an integer between 1 and 65535.")
    return int(value)


def photon_environment(env: Mapping[str, str], port: int) -> dict[str, str]:
    """Validate private settings without copying their values into diagnostics."""
    missing = [name for name in ("SPECTRUM_PROJECT_ID", "SPECTRUM_PROJECT_SECRET", "PHOTON_BRIDGE_TOKEN")
               if not env.get(name, "").strip()]
    if missing:
        raise ValueError("Photon requires private environment settings: " + ", ".join(missing) + ".")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", env["PHOTON_BRIDGE_TOKEN"].strip()):
        raise ValueError("PHOTON_BRIDGE_TOKEN must contain 32-128 letters, numbers, underscores or hyphens.")
    if port < 1_024:
        raise ValueError("Photon requires PORT between 1024 and 65535.")
    bridge = env.get("PHOTON_BRIDGE_PORT", "8766").strip() or "8766"
    if not re.fullmatch(r"[0-9]{1,5}", bridge) or not 1_024 <= int(bridge) <= 65_535 or int(bridge) == port:
        raise ValueError("PHOTON_BRIDGE_PORT must be 1024-65535 and differ from PORT.")
    child_env = dict(env)
    child_env["PHOTON_BRIDGE_TOKEN"] = env["PHOTON_BRIDGE_TOKEN"].strip()
    child_env["PHOTON_BACKEND_PORT"] = str(port)
    child_env["PHOTON_BRIDGE_PORT"] = bridge
    return child_env


def api_command(port: int) -> list[str]:
    return [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", str(port)]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def openapi_ready(port: int) -> bool:
    """Use the same four-route readiness contract as the Spectrum dispatcher."""
    request = Request(f"http://127.0.0.1:{port}/openapi.json", method="GET")
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=2) as response:
            if response.status != 200:
                return False
            raw = response.read(512 * 1_024 + 1)
        if len(raw) > 512 * 1_024:
            return False
        body = json.loads(raw)
        paths = body.get("paths") if isinstance(body, dict) else None
        return isinstance(paths, dict) and all(
            isinstance(paths.get(path), dict) and isinstance(paths[path].get(method), dict)
            for path, method in _REQUIRED_ROUTES
        )
    except (OSError, ValueError, TypeError):
        return False


def wait_for_api(process: subprocess.Popen, port: int, stopping: threading.Event) -> bool:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while not stopping.is_set():
        if process.poll() is not None:
            raise RuntimeError("The API exited before Photon could start.")
        if openapi_ready(port):
            return True
        if time.monotonic() >= deadline:
            raise RuntimeError("The API did not become ready before the Photon startup deadline.")
        stopping.wait(0.25)
    return False


def bridge_ready(port: int) -> bool:
    """Check the private bridge process, without sending or claiming delivery."""
    request = Request(f"http://127.0.0.1:{port}/health", method="GET")
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=2) as response:
            if response.status != 200:
                return False
            raw = response.read(16 * 1_024 + 1)
        if len(raw) > 16 * 1_024:
            return False
        body = json.loads(raw)
        return isinstance(body, dict) and body.get("status") == "ok" and body.get("service") == "quantro-photon"
    except (OSError, ValueError, TypeError):
        return False


def wait_for_bridge(api: subprocess.Popen, photon: subprocess.Popen, port: int, stopping: threading.Event) -> bool:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while not stopping.is_set():
        if api.poll() is not None or photon.poll() is not None:
            raise RuntimeError("A Quantro service exited before the Photon bridge became ready.")
        if bridge_ready(port):
            return True
        if time.monotonic() >= deadline:
            raise RuntimeError("The Photon bridge did not become ready before its startup deadline.")
        stopping.wait(0.25)
    return False


def _signal_child(process: subprocess.Popen, *, kill: bool = False) -> None:
    if process.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            if kill:
                process.kill()
            else:
                process.terminate()
        else:
            os.killpg(process.pid, signal.SIGKILL if kill else signal.SIGTERM)
    except ProcessLookupError:
        pass


def stop_children(processes: list[subprocess.Popen]) -> None:
    """Stop intake before the API, then reap both; bound graceful shutdown."""
    children = list(reversed(processes))
    for process in children:
        _signal_child(process)
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS
    for process in children:
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _signal_child(process, kill=True)
            process.wait(timeout=5)


def supervise(port: int, child_env: dict[str, str]) -> int:
    node = shutil.which("node")
    if node is None or not (PHOTON_ROOT / "src" / "index.ts").is_file():
        raise RuntimeError("Photon requires Node 24 and the existing Spectrum listener files.")
    stopping = threading.Event()
    received_signal = 0

    def request_stop(signum, _frame):
        nonlocal received_signal
        received_signal = signum
        stopping.set()

    previous = {kind: signal.signal(kind, request_stop) for kind in (signal.SIGINT, signal.SIGTERM)}
    children: list[subprocess.Popen] = []
    try:
        api = subprocess.Popen(api_command(port), cwd=BACKEND_ROOT, env=child_env, start_new_session=os.name == "posix")
        children.append(api)
        if not wait_for_api(api, port, stopping):
            return 128 + received_signal
        if stopping.is_set():
            return 128 + received_signal
        photon = subprocess.Popen([node, "--use-system-ca", "src/index.ts", "--imessage"],
                                  cwd=PHOTON_ROOT, env=child_env, start_new_session=os.name == "posix")
        children.append(photon)
        if not wait_for_bridge(api, photon, int(child_env.get("PHOTON_BRIDGE_PORT", "8766")), stopping):
            return 128 + received_signal
        if stopping.is_set():
            return 128 + received_signal
        print("Quantro API and private Photon listener started.", flush=True)
        while not stopping.is_set():
            for label, process in (("API", api), ("Photon listener", photon)):
                status = process.poll()
                if status is not None:
                    print(f"{label} exited; stopping both services.", file=sys.stderr, flush=True)
                    return status if 0 < status <= 255 else 1
            stopping.wait(0.25)
        return 128 + received_signal
    finally:
        try:
            stop_children(children)
        finally:
            for kind, handler in previous.items():
                signal.signal(kind, handler)


def main() -> int:
    try:
        port = api_port(os.environ)
        if os.environ.get("PHOTON_ENABLED", "").strip().lower() != "true":
            os.chdir(BACKEND_ROOT)
            os.execv(sys.executable, api_command(port))
            return 0
        child_env = photon_environment(os.environ, port)
        return supervise(port, child_env)
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    except OSError:
        print("A required Quantro runtime process could not start.", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
