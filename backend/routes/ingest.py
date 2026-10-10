from typing import Annotated

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.routes.common import get_tiger_store, not_found, quantro_error_response
from backend.services.pipeline.run_ingest import run_ingest
from backend.services.reporting.errors import QuantroError
from backend.services.storage.tiger import TigerStore

router = APIRouter()

JobId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,128}$")]


class IngestRequest(BaseModel):
    url: str = Field(min_length=1, max_length=8192)


@router.post("/ingest/{job_id}")
def ingest(job_id: JobId, body: IngestRequest, store: TigerStore = Depends(get_tiger_store)) -> JSONResponse:
    try:
        result = run_ingest(job_id, body.url, store=store)
    except QuantroError as error:
        return quantro_error_response(error)
    return JSONResponse(content=result.model_dump(mode="json"))


@router.get("/ingest/{job_id}")
def get_ingest(job_id: JobId, store: TigerStore = Depends(get_tiger_store)) -> JSONResponse:
    job = store.get_job(job_id)
    if job is None:
        return not_found("Job not found")
    return JSONResponse(content=job)
