import logging
from collections.abc import Mapping

from fastapi import Request
from fastapi.responses import JSONResponse

from backend.services.shared.errors import ErrorCode, JobConflictError, QuantroError, Stage
from backend.services.storage.tiger import TigerStore

logger = logging.getLogger(__name__)

_STATUS_BY_CODE = {
    ErrorCode.INVALID_URL: 400,
    ErrorCode.UNSUPPORTED_PLATFORM: 400,
    ErrorCode.MEDIA_UNAVAILABLE: 404,
    ErrorCode.TRANSCRIPTION_FAILED: 502,
    ErrorCode.STORAGE_FAILED: 500,
}


def get_tiger_store(request: Request) -> TigerStore:
    return request.app.state.tiger_store


def _envelope(status_code: int, error: QuantroError, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": error.to_dict()}, headers=headers)


def quantro_error_response(error: QuantroError) -> JSONResponse:
    status_code = JobConflictError.http_status if isinstance(error, JobConflictError) else _STATUS_BY_CODE.get(error.code, 500)
    return _envelope(status_code, error)


def error_response(
    status_code: int,
    code: ErrorCode,
    stage: Stage,
    message: str | None = None,
    *,
    retryable: bool = False,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Every non-2xx body has the same {"error": {code, stage, message, retryable}} shape."""
    return _envelope(status_code, QuantroError(code, stage, message, retryable), headers)


def not_found(message: str, stage: Stage, code: ErrorCode = ErrorCode.PROCESSING_FAILED) -> JSONResponse:
    return error_response(404, code, stage, message)


def stage_for_path(path: str) -> Stage:
    if path.startswith(("/reports/", "/media/")):
        return Stage.DISPLAY_REPORT
    if path.startswith("/ingest/"):
        return Stage.RETRIEVE_POST
    return Stage.RUN_ANALYSIS


def log_route_error(action: str, job_id: str, error: QuantroError) -> None:
    """Server-side record of a failure the client only sees as a redacted envelope."""
    cause = error.__cause__ or error.__context__
    logger.warning(
        "%s %s failed: code=%s stage=%s retryable=%s cause=%s",
        action, job_id, error.code.value, error.stage.value, error.retryable,
        type(cause).__name__ if cause is not None else "none",
    )
    # Underlying exception text can include provider hosts or URLs; keep it out of default logs.
    logger.debug("%s %s failure traceback", action, job_id, exc_info=error)
