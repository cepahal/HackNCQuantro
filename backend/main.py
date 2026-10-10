import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent

# The git-ignored .secrets/.env vault wins; dotenv never overrides a key that is already set.
load_dotenv(_REPO_ROOT / ".secrets" / ".env")
load_dotenv()

# `uvicorn main:app` from backend/ loads this file as top-level `main`; every module
# imports through `backend.*`, so the repo root must be importable too.
if not __package__:
    sys.path.insert(0, str(_REPO_ROOT))

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.routes.analyze import router as analyze_router
from backend.routes.ingest import router as ingest_router
from backend.services.storage.tiger import TigerStore

FRONTEND_ORIGIN = os.getenv("REPORT_BASE_URL", "http://localhost:3000").rstrip("/")


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = TigerStore.from_env()
    store.ensure_schema()
    app.state.tiger_store = store
    try:
        yield
    finally:
        store.close()


app = FastAPI(title="Quantro API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
app.include_router(analyze_router)
app.include_router(ingest_router)
