import json
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.routes.common import get_tiger_store, log_route_error, not_found, quantro_error_response
from backend.services.reporting.build_report import sanitize_report
from backend.services.reporting.run_analysis import run_analysis
from backend.services.reporting.save_report import report_id_for_job
from backend.services.shared.errors import RETRYABLE_CODES, ErrorCode, QuantroError, Stage
from backend.services.shared.types import ReportV2, parse_stage_output
from backend.services.storage.tiger import TigerReportStore, TigerStore

router = APIRouter()

JobId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
ReportId = Annotated[str, Path(pattern=r"^report_[A-Za-z0-9_-]{1,128}$")]
TigerDependency = Annotated[TigerStore, Depends(get_tiger_store)]

# Longer than any realistic analysis (the frontend stops polling at 15 minutes). An
# in-progress job untouched for longer was orphaned, e.g. by a process restart.
STALE_JOB_AFTER = timedelta(minutes=15)
_IN_PROGRESS = {"running", "ingested"}
_STALE_JOB_MESSAGE = "The analysis stopped before it finished. Please submit the link again."


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=8192)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=256)


@router.post("/analyze/{job_id}/run")
def run_job(job_id: JobId, body: AnalyzeRequest, store: TigerDependency) -> JSONResponse:
    try:
        result = run_analysis(job_id, body.url, body.conversation_id, store=store)
    except QuantroError as error:
        log_route_error("Analysis", job_id, error)
        return quantro_error_response(error)
    return JSONResponse(content=result.model_dump(mode="json"))


@router.get("/reports/{report_id}")
def get_report(report_id: ReportId, store: TigerDependency) -> JSONResponse:
    try:
        raw = TigerReportStore(store).get(report_id)
    except Exception as exc:
        raise QuantroError(ErrorCode.STORAGE_FAILED, Stage.DISPLAY_REPORT, retryable=True) from exc
    if raw is None:
        return not_found("Report not found", Stage.DISPLAY_REPORT)
    try:
        report = parse_stage_output(ReportV2, sanitize_report(json.loads(raw)), Stage.DISPLAY_REPORT).model_dump(mode="json")
    except QuantroError as error:
        return quantro_error_response(error)
    except (TypeError, ValueError):
        return quantro_error_response(QuantroError(ErrorCode.PROCESSING_FAILED, Stage.DISPLAY_REPORT))
    return JSONResponse(content=report)


def _is_stale(job: dict) -> bool:
    try:
        updated_at = datetime.fromisoformat(job["updated_at"])
    except (KeyError, TypeError, ValueError):
        return False
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - updated_at > STALE_JOB_AFTER


@router.get("/analyze/{job_id}/status")
def job_status(job_id: JobId, store: TigerDependency) -> JSONResponse:
    """Small public status response for reconnecting after navigation or refresh."""
    try:
        job = store.get_job(job_id)
    except Exception:
        return quantro_error_response(QuantroError(ErrorCode.STORAGE_FAILED, Stage.RUN_ANALYSIS, retryable=True))
    if job is None:
        return not_found("Job not found", Stage.RUN_ANALYSIS)
    status = job["status"]
    error = None
    if status == "failed":
        try:
            code = ErrorCode(job.get("error_code") or ErrorCode.PROCESSING_FAILED)
            error = QuantroError(code, job.get("error_stage") or Stage.RUN_ANALYSIS, retryable=code in RETRYABLE_CODES)
        except ValueError:
            error = QuantroError(ErrorCode.PROCESSING_FAILED, Stage.RUN_ANALYSIS)
    elif status in _IN_PROGRESS and _is_stale(job):
        status = "failed"
        error = QuantroError(ErrorCode.PROCESSING_FAILED, Stage.RUN_ANALYSIS, _STALE_JOB_MESSAGE, retryable=True)
    return JSONResponse(content={
        "job_id": job_id, "status": status,
        "report_id": report_id_for_job(job_id) if status == "completed" else None,
        "error": error.to_dict() if error is not None else None,
    })
