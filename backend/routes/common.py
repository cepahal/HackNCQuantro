from fastapi import Request
from fastapi.responses import JSONResponse

from backend.services.shared.errors import ErrorCode, QuantroError
from backend.services.storage.tiger import TigerStore

_STATUS_BY_CODE = {
    ErrorCode.INVALID_URL: 400,
    ErrorCode.UNSUPPORTED_PLATFORM: 400,
    ErrorCode.MEDIA_UNAVAILABLE: 404,
    ErrorCode.TRANSCRIPTION_FAILED: 502,
    ErrorCode.STORAGE_FAILED: 500,
}


def get_tiger_store(request: Request) -> TigerStore:
    return request.app.state.tiger_store


def quantro_error_response(error: QuantroError) -> JSONResponse:
    return JSONResponse(status_code=_STATUS_BY_CODE.get(error.code, 500), content={"error": error.to_dict()})


def not_found(message: str) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": {"message": message}})
