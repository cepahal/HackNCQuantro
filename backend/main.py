import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# `uvicorn main:app` from backend/ loads this file as top-level `main`; every module
# imports through `backend.*`, so the repo root must be importable too.
if not __package__:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.services.shared.config import (
    load_environment,
    validate_search_configuration,
)

load_environment()

import inspect
import os
import logging

# Emit only our timing logs; do not enable verbose provider/HTTP logging.
for _timing_module in ("backend.services.reporting.run_analysis", "backend.services.specialist.transcription"):
    _timing_logger = logging.getLogger(_timing_module)
    if not _timing_logger.handlers:
        _handler = logging.StreamHandler()
        _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        _timing_logger.addHandler(_handler)
    _timing_logger.setLevel(logging.INFO)
    _timing_logger.propagate = False
from contextlib import asynccontextmanager
from importlib import import_module
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException

_CONTRACT_FUNCTION_PATHS = {
    "retrieve_post": "backend.services.external_apis.retrieve_post",
    "extract_media_features": "backend.services.processing.preprocessing",
    "build_search_queries": "backend.services.external_apis.build_search_queries",
    "search_candidates": "backend.services.external_apis.search_candidates",
    "retrieve_candidate_media": "backend.services.external_apis.retrieve_candidate_media",
    "verify_and_align": "backend.services.specialist.alignment",
    "select_surrounding_context": "backend.services.specialist.context",
    "compare_context_and_caption": "backend.services.specialist.comparison",
    "build_report": "backend.services.reporting.build_report",
    "save_report_and_complete_job": "backend.services.reporting.save_report",
    "display_report": "backend.services.reporting.display_report",
    "send_summary": "backend.services.reporting.send_summary",
    "run_analysis": "backend.services.reporting.run_analysis",
}


def validate_contract_functions() -> None:
    """Fail before API startup when a required team function is missing."""
    missing: list[str] = []
    for name, module_path in _CONTRACT_FUNCTION_PATHS.items():
        expected = f"{module_path}.{name}"
        try:
            module = import_module(module_path)
        except ImportError:
            missing.append(f"{name}: could not import {expected}")
            continue
        function = getattr(module, name, None)
        if not callable(function):
            missing.append(f"{name}: missing or not callable; expected {expected}")
        else:
            source = inspect.getsourcefile(inspect.unwrap(function))
            if source and any(marker in Path(source).name.lower() for marker in ("stub", "mock")):
                missing.append(f"{name}: stub/mock implementation at {source}; expected {expected}")
    if missing:
        raise RuntimeError("Required contract functions are unavailable: " + "; ".join(missing))


validate_contract_functions()

from backend.routes.analyze import router as analyze_router
from backend.routes.common import (
    log_failure,
    quantro_error_response,
    request_error_response,
    request_stage,
)
from backend.routes.ingest import router as ingest_router
from backend.routes.media import router as media_router
from backend.routes.photon_webhook import router as photon_webhook_router
from backend.routes.request_limits import RequestLimitsMiddleware
from backend.services.external_apis.tigerdata_storage import TigerDataPostPool
from backend.services.shared.errors import ErrorCode, QuantroError
from backend.services.shared.runtime import configure_media_tools
from backend.services.storage.tiger import TigerConfigError, TigerStore

FRONTEND_ORIGIN = (
    os.getenv("FRONTEND_ORIGIN", "").strip()
    or os.getenv("REPORT_BASE_URL", "http://localhost:3000")
).rstrip("/")


def allowed_origins(frontend_origin: str) -> list[str]:
    origins = [frontend_origin]
    parts = urlsplit(frontend_origin)
    twin = {"localhost": "127.0.0.1", "127.0.0.1": "localhost"}.get(parts.hostname or "")
    if twin is not None:
        netloc = f"{twin}:{parts.port}" if parts.port else twin
        origins.append(urlunsplit((parts.scheme, netloc, parts.path, "", "")))
    return origins


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_contract_functions()
    configure_media_tools()
    if not os.getenv("TIGER_DATABASE_URL", "").strip():
        raise TigerConfigError("TIGER_DATABASE_URL is not set. Configure it before API startup.")
    validate_search_configuration()
    store = TigerStore.from_env()
    app.state.tiger_store = store
    app.state.media_pool = None
    try:
        store.ensure_schema()
        modes = {os.getenv("APP_ENV", "").lower(), os.getenv("ENVIRONMENT", "").lower()}
        test_mode = "test" in modes and "production" not in modes
        sqlite_test = test_mode and os.getenv("TIGER_DATABASE_URL", "").startswith("sqlite:")
        if not sqlite_test:
            app.state.media_pool = TigerDataPostPool.from_env()
        yield
    finally:
        try:
            if app.state.media_pool is not None:
                app.state.media_pool.close()
        finally:
            store.close()


app = FastAPI(title="Quantro API", lifespan=lifespan)


@app.exception_handler(QuantroError)
async def handle_quantro_error(request: Request, error: QuantroError):
    return quantro_error_response(error)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, error: RequestValidationError):
    problems = error.errors()
    code = ErrorCode.INVALID_URL if problems and all(tuple(p.get("loc", ())) == ("body", "url") for p in problems) else ErrorCode.PROCESSING_FAILED
    return request_error_response(422, stage=request_stage(request.url.path), code=code)


@app.exception_handler(HTTPException)
async def handle_http_error(request: Request, error: HTTPException):
    return request_error_response(error.status_code, stage=request_stage(request.url.path), headers=error.headers)


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, error: Exception):
    safe = QuantroError(ErrorCode.PROCESSING_FAILED, request_stage(request.url.path))
    log_failure(safe, type(error).__name__)
    return request_error_response(500, stage=safe.stage)


app.add_middleware(RequestLimitsMiddleware)


app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(FRONTEND_ORIGIN),
    allow_methods=["GET", "HEAD", "POST"],
    allow_headers=["Content-Type", "Range", "If-None-Match"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length", "ETag"],
)
app.include_router(analyze_router)
app.include_router(ingest_router)
app.include_router(media_router)
app.include_router(photon_webhook_router)
