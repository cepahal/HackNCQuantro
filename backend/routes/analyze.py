import json
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.routes.common import get_tiger_store, not_found, quantro_error_response
from backend.services.reporting.build_report import sanitize_report
from backend.services.reporting.errors import QuantroError
from backend.services.reporting.run_analysis import run_analysis
from backend.services.storage.tiger import TigerReportStore, TigerStore

router = APIRouter()

JobId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
ReportId = Annotated[str, Path(pattern=r"^report_[A-Za-z0-9_-]{1,128}$")]


class AnalyzeRequest(BaseModel):
    url: str = Field(min_length=1, max_length=8192)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=256)


@router.post("/analyze/{job_id}/run")
def run_job(job_id: JobId, body: AnalyzeRequest, store: TigerStore = Depends(get_tiger_store)) -> JSONResponse:
    try:
        result = run_analysis(job_id, body.url, body.conversation_id, store=store)
    except QuantroError as error:
        return quantro_error_response(error)
    return JSONResponse(content=result.model_dump(mode="json"))


@router.get("/reports/{report_id}")
def get_report(report_id: ReportId, store: TigerStore = Depends(get_tiger_store)) -> JSONResponse:
    raw = TigerReportStore(store).get(report_id)
    if raw is None:
        return not_found("Report not found")
    try:
        report = sanitize_report(json.loads(raw))
    except QuantroError as error:
        return quantro_error_response(error)
    return JSONResponse(content=report)
