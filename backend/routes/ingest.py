from typing import Annotated

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.routes.common import get_tiger_store, quantro_error_response
from backend.services.external_apis.retrieve_post import _validate_url
from backend.services.resolution.run import run_ingest
from backend.services.shared.errors import ErrorCode, QuantroError, Stage
from backend.services.storage.tiger import TigerStore

router = APIRouter()

JobId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
TigerDependency = Annotated[TigerStore, Depends(get_tiger_store)]


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=8192)


@router.post("/ingest/{job_id}")
def ingest(job_id: JobId, body: IngestRequest, store: TigerDependency) -> JSONResponse:
    try:
        _validate_url(body.url)
    except QuantroError as error:
        return quantro_error_response(error)
    try:
        created = store.try_create_job(job_id, body.url)
    except Exception:  # noqa: BLE001 - Storage diagnostics must not expose credentials.
        return quantro_error_response(QuantroError(ErrorCode.STORAGE_FAILED, Stage.RETRIEVE_POST, retryable=True))
    if not created:
        conflict = QuantroError(ErrorCode.PROCESSING_FAILED, Stage.RETRIEVE_POST,
                             "This job ID already exists. Use a new job ID for a new ingest.", retryable=False)
        return JSONResponse(status_code=409, content={"error": conflict.public_dict()})
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
        return quantro_error_response(QuantroError(
            ErrorCode.MEDIA_UNAVAILABLE, Stage.RETRIEVE_POST, "Job not found", retryable=False
        ))
    try:
        # Internal ingest inspection retains the exact persisted contract JSON.
        post = job.pop("post_json")
        features = job.pop("features_json")
        return JSONResponse(content={"job": job, "post": post, "features": features})
    except QuantroError as error:
        return quantro_error_response(error)
