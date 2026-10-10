import json
import os
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.routes.common import get_tiger_store, not_found, quantro_error_response
from backend.services.reporting.build_report import sanitize_report
from backend.services.reporting.display_report import display_report
from backend.services.reporting.run_analysis import run_analysis
from backend.services.reporting.save_report import report_id_for_job
from backend.services.shared.errors import RETRYABLE_CODES, ErrorCode, QuantroError, Stage
from backend.services.shared.post_identity import ensure_distinct_source
from backend.services.shared.types import ReportV2, parse_stage_output
from backend.services.storage.tiger import TigerReportStore, TigerStore

router = APIRouter()

JobId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
ReportId = Annotated[str, Path(pattern=r"^report_[A-Za-z0-9_-]{1,128}$")]
TigerDependency = Annotated[TigerStore, Depends(get_tiger_store)]


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=8192)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=256)


@router.post("/analyze/{job_id}/run")
def run_job(job_id: JobId, body: AnalyzeRequest, store: TigerDependency) -> JSONResponse:
    try:
        result = run_analysis(job_id, body.url, body.conversation_id, store=store)
    except QuantroError as error:
        return quantro_error_response(error)
    return JSONResponse(content=result.model_dump(mode="json"))


@router.get("/reports/{report_id}")
def get_report(report_id: ReportId, store: TigerDependency) -> JSONResponse:
    try:
        raw = TigerReportStore(store).get(report_id)
    except Exception as exc:
        raise QuantroError(ErrorCode.STORAGE_FAILED, Stage.DISPLAY_REPORT, retryable=True) from exc
    if raw is None:
        return not_found("Report not found")
    try:
        report = parse_stage_output(ReportV2, sanitize_report(json.loads(raw)), Stage.DISPLAY_REPORT)
        ensure_distinct_source(report.submitted.post, report.source.post if report.source else None, Stage.DISPLAY_REPORT)
    except QuantroError as error:
        return quantro_error_response(error)
    except (TypeError, ValueError):
        return quantro_error_response(QuantroError(ErrorCode.PROCESSING_FAILED, Stage.DISPLAY_REPORT))
    return JSONResponse(content=report.model_dump(mode="json"))


@router.get("/analyze/{job_id}/status")
def job_status(job_id: JobId, store: TigerDependency) -> JSONResponse:
    """Small public status response for reconnecting after navigation or refresh."""
    try:
        job = store.get_job(job_id)
        if job is not None and job["status"] in ("running", "ingested"):
            timeout = int(os.getenv("ANALYSIS_STALE_SECONDS", "900"))
            if not 60 <= timeout <= 86400:
                timeout = 900
            cutoff = datetime.now(UTC) - timedelta(seconds=timeout)
            if store.expire_stale_job(job_id, cutoff):
                job = store.get_job(job_id)
    except Exception:  # noqa: BLE001 - Keep storage diagnostics private.
        return quantro_error_response(QuantroError(ErrorCode.STORAGE_FAILED, Stage.RUN_ANALYSIS, retryable=True))
    if job is None:
        return not_found("Job not found", stage=Stage.RUN_ANALYSIS)
    status = job["status"]
    error = None
    if status == "failed":
        try:
            error = QuantroError(job.get("error_code") or ErrorCode.PROCESSING_FAILED,
                                job.get("error_stage") or Stage.RUN_ANALYSIS,
                                message=job.get("error_message"), retryable=(bool(job["error_retryable"]) if job.get("error_retryable") is not None else job.get("error_code") in RETRYABLE_CODES))
        except ValueError:
            error = QuantroError(ErrorCode.PROCESSING_FAILED, Stage.RUN_ANALYSIS)
    return JSONResponse(content={
        "job_id": job_id, "status": status,
        "report_id": report_id_for_job(job_id) if status == "completed" else None,
        "error": error.public_dict() if error is not None else None,
    })


@router.get("/reports/{report_id}/display")
def get_display_model(report_id: ReportId, store: TigerDependency) -> JSONResponse:
    response = get_report(report_id, store)
    if response.status_code != 200:
        return response
    try:
        model = display_report(json.loads(bytes(response.body)))
        return JSONResponse(content=model.model_dump(mode="json"))
    except QuantroError as error:
        return quantro_error_response(error)
