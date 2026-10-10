import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# `uvicorn main:app` from backend/ loads this file as top-level `main`; every module
# imports through `backend.*`, so the repo root must be importable too.
if not __package__:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.services.shared.settings import load_environment

load_environment()

import logging
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
from starlette.exceptions import HTTPException as StarletteHTTPException

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
    if missing:
        raise RuntimeError("Required contract functions are unavailable: " + "; ".join(missing))


validate_contract_functions()

from backend.routes.analyze import router as analyze_router
from backend.routes.common import error_response, quantro_error_response, stage_for_path
from backend.routes.ingest import router as ingest_router
from backend.routes.media import close_media_store
from backend.routes.media import router as media_router
from backend.routes.photon_webhook import router as photon_webhook_router
from backend.routes.request_guard import RequestGuard
from backend.services.external_apis.search_candidates import check_search_settings
from backend.services.shared.errors import ErrorCode, QuantroError
from backend.services.storage.tiger import TigerStore

logger = logging.getLogger(__name__)

FRONTEND_ORIGIN = os.getenv("REPORT_BASE_URL", "http://localhost:3000").rstrip("/")
_LOOPBACK_TWINS = {"localhost": "127.0.0.1", "127.0.0.1": "localhost"}


def allowed_origins(frontend_origin: str) -> list[str]:
    """The configured origin, plus its localhost/127.0.0.1 twin for local development."""
    origins = [frontend_origin]
    parts = urlsplit(frontend_origin)
    twin = _LOOPBACK_TWINS.get(parts.hostname or "")
    if twin is not None:
        netloc = f"{twin}:{parts.port}" if parts.port else twin
        origins.append(urlunsplit((parts.scheme, netloc, parts.path, "", "")))
    return origins


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_contract_functions()
    try:
        check_search_settings()
    except QuantroError:
        logger.warning("Source search is misconfigured; analyses will fail until it is fixed (see the error above).")
    store = TigerStore.from_env()
    store.ensure_schema()
    app.state.tiger_store = store
    try:
        yield
    finally:
        store.close()
        close_media_store()


app = FastAPI(title="Quantro API", lifespan=lifespan)


@app.exception_handler(QuantroError)
async def handle_quantro_error(request: Request, error: QuantroError):
    return quantro_error_response(error)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, error: RequestValidationError):
    problems = error.errors()
    logger.info("Rejected %s %s: %s", request.method, request.url.path, sorted({str(p.get("type")) for p in problems}))
    if problems and all(tuple(p.get("loc", ())) == ("body", "url") for p in problems):
        return error_response(422, ErrorCode.INVALID_URL, stage_for_path(request.url.path))
    return error_response(422, ErrorCode.PROCESSING_FAILED, stage_for_path(request.url.path), "The request was invalid.")


@app.exception_handler(StarletteHTTPException)
async def handle_http_error(request: Request, error: StarletteHTTPException):
    message = error.detail if isinstance(error.detail, str) else "The request could not be completed."
    return error_response(
        error.status_code, ErrorCode.PROCESSING_FAILED, stage_for_path(request.url.path), message, headers=error.headers
    )


app.add_middleware(RequestGuard)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(FRONTEND_ORIGIN),
    allow_methods=["GET", "HEAD", "POST"],
    allow_headers=["Content-Type", "Range"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length", "ETag"],
)
app.include_router(analyze_router)
app.include_router(ingest_router)
app.include_router(media_router)
app.include_router(photon_webhook_router)
