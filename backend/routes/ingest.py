from typing import Annotated

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.routes.common import get_tiger_store, not_found, quantro_error_response
from backend.services.pipeline.run_ingest import run_ingest
from backend.services.reporting.build_report import sanitize_report
from backend.services.reporting.errors import ErrorCode, QuantroError, Stage
from backend.services.storage.tiger import TigerStore

router = APIRouter()

JobId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
TigerDependency = Annotated[TigerStore, Depends(get_tiger_store)]


class IngestRequest(BaseModel):
    url: str = Field(min_length=1, max_length=8192)


@router.post("/ingest/{job_id}")
def ingest(job_id: JobId, body: IngestRequest, store: TigerDependency) -> JSONResponse:
    try:
        result = run_ingest(job_id, body.url, store=store)
    except QuantroError as error:
        return quantro_error_response(error)
    return JSONResponse(content=result.model_dump(mode="json"))


@router.get("/ingest/{job_id}")
def get_ingest(job_id: JobId, store: TigerDependency) -> JSONResponse:
    try:
        job = store.get_job(job_id)
    except Exception as exc:
        raise QuantroError(ErrorCode.STORAGE_FAILED, Stage.EXTRACT_MEDIA_FEATURES, retryable=True) from exc
    if job is None:
        return not_found("Job not found")
    try:
        return JSONResponse(content=sanitize_report(job))
    except QuantroError as error:
        return quantro_error_response(error)
