import logging
from collections.abc import Mapping

from fastapi import Request
from fastapi.responses import JSONResponse

from backend.services.shared.errors import ErrorCode, QuantroError, Stage
from backend.services.storage.tiger import TigerStore

_STATUS_BY_CODE = {
    ErrorCode.INVALID_URL: 400,
    ErrorCode.UNSUPPORTED_PLATFORM: 400,
    ErrorCode.MEDIA_UNAVAILABLE: 404,
    ErrorCode.TRANSCRIPTION_FAILED: 502,
    ErrorCode.STORAGE_FAILED: 500,
}
logger = logging.getLogger(__name__)


def request_stage(path: str) -> Stage:
    if path.startswith("/ingest/"):
        return Stage.RETRIEVE_POST
    if path.startswith(("/reports/", "/media/")):
        return Stage.DISPLAY_REPORT
    return Stage.RUN_ANALYSIS


def log_failure(error: QuantroError, exception_type: str | None = None) -> None:
    unexpected = exception_type is not None
    cause = error.__cause__ or error.__context__
    exception_type = exception_type or getattr(error, "exception_type", type(error).__name__)
    logger.log(logging.ERROR if unexpected else logging.WARNING, "Request failed code=%s stage=%s exception_type=%s cause=%s", error.code.value, error.stage.value, exception_type, type(cause).__name__ if cause else "none")


def request_error_response(status_code: int, *, stage: Stage = Stage.RUN_ANALYSIS,
                           message: str | None = None, code: ErrorCode = ErrorCode.PROCESSING_FAILED,
                           headers: Mapping[str, str] | None = None) -> JSONResponse:
    safe_messages = {
        400: "The request is invalid.", 401: "Request authentication failed.",
        403: "This request is not permitted.", 404: "The requested resource was not found.",
        405: "This request method is not supported.", 409: "The request conflicts with an existing job.",
        413: "The request body is too large.", 416: "The requested media range is unavailable.",
        422: "The request input is invalid.",
    }
    error = QuantroError(code, stage, message or safe_messages.get(status_code))
    log_failure(error)
    return JSONResponse(status_code=status_code, content={"error": error.public_dict()}, headers=headers)


def get_tiger_store(request: Request) -> TigerStore:
    return request.app.state.tiger_store


def quantro_error_response(error: QuantroError) -> JSONResponse:
    status = 409 if getattr(error, "status_code", None) == 409 else _STATUS_BY_CODE.get(error.code, 500)
    log_failure(error)
    return JSONResponse(status_code=status, content={"error": error.public_dict()})


def not_found(message: str, *, stage: Stage = Stage.DISPLAY_REPORT) -> JSONResponse:
    return request_error_response(404, stage=stage, message=message)
