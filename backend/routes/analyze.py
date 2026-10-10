from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.reporting.errors import ErrorCode, QuantroError
from services.reporting.run_analysis import run_analysis

router = APIRouter()

_CLIENT_ERROR_CODES = {ErrorCode.INVALID_URL, ErrorCode.UNSUPPORTED_PLATFORM}


@router.post("/analyze/{job_id}/run")
def run_job(job_id: str) -> JSONResponse:
    try:
        result = run_analysis(job_id)
    except QuantroError as error:
        status = 422 if error.code in _CLIENT_ERROR_CODES else 502
        return JSONResponse(status_code=status, content={"success": False, "result": None, "error": error.to_dict()})
    except NotImplementedError:
        return JSONResponse(
            status_code=501,
            content={"success": False, "result": None, "error": {"message": "run_analysis is not wired up yet"}},
        )

    payload = result.model_dump(mode="json") if isinstance(result, BaseModel) else result
    return JSONResponse(content={"success": True, "result": payload, "error": None})
