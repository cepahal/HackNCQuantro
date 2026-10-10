import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent

# The git-ignored .secrets/.env vault wins; dotenv never overrides a key that is already set.
load_dotenv(_REPO_ROOT / ".secrets" / ".env")
load_dotenv(_REPO_ROOT / ".env.local")
load_dotenv()

# `uvicorn main:app` from backend/ loads this file as top-level `main`; every module
# imports through `backend.*`, so the repo root must be importable too.
if not __package__:
    sys.path.insert(0, str(_REPO_ROOT))

import os
from contextlib import asynccontextmanager
from importlib import import_module

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

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
from backend.routes.common import quantro_error_response
from backend.routes.ingest import router as ingest_router
from backend.routes.media import router as media_router
from backend.services.shared.errors import QuantroError
from backend.services.storage.tiger import TigerStore

FRONTEND_ORIGIN = os.getenv("REPORT_BASE_URL", "http://localhost:3000").rstrip("/")


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_contract_functions()
    store = TigerStore.from_env()
    store.ensure_schema()
    app.state.tiger_store = store
    try:
        yield
    finally:
        store.close()


app = FastAPI(title="Quantro API", lifespan=lifespan)


@app.exception_handler(QuantroError)
async def handle_quantro_error(request: Request, error: QuantroError):
    return quantro_error_response(error)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Range"],
)
app.include_router(analyze_router)
app.include_router(ingest_router)
app.include_router(media_router)
