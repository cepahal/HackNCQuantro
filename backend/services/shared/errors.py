"""Error normalization for the Quantro analysis pipeline.

Every stage of run_analysis reports failures as a QuantroError so the job record,
the report page, and the iMessage reply all see the same shape. Stages that know
the precise cause raise QuantroError directly; anything else is converted by
normalize_exception using a per-stage default code.
"""

from enum import Enum


class ErrorCode(str, Enum):
    INVALID_URL = "INVALID_URL"
    UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"
    MEDIA_UNAVAILABLE = "MEDIA_UNAVAILABLE"
    TRANSCRIPTION_FAILED = "TRANSCRIPTION_FAILED"
    SEARCH_FAILED = "SEARCH_FAILED"
    PROCESSING_FAILED = "PROCESSING_FAILED"
    STORAGE_FAILED = "STORAGE_FAILED"
    MESSAGE_SEND_FAILED = "MESSAGE_SEND_FAILED"


class Stage(str, Enum):
    RETRIEVE_POST = "retrieve_post"
    EXTRACT_MEDIA_FEATURES = "extract_media_features"
    BUILD_SEARCH_QUERIES = "build_search_queries"
    SEARCH_CANDIDATES = "search_candidates"
    RETRIEVE_CANDIDATE_MEDIA = "retrieve_candidate_media"
    VERIFY_AND_ALIGN = "verify_and_align"
    SELECT_SURROUNDING_CONTEXT = "select_surrounding_context"
    COMPARE_CONTEXT_AND_CAPTION = "compare_context_and_caption"
    BUILD_REPORT = "build_report"
    SAVE_REPORT_AND_COMPLETE_JOB = "save_report_and_complete_job"
    DISPLAY_REPORT = "display_report"
    SEND_SUMMARY = "send_summary"
    RUN_ANALYSIS = "run_analysis"


# User-facing text only; raw exception messages can contain URLs, tokens, or stack details.
_SAFE_MESSAGE_BY_CODE: dict[ErrorCode, str] = {
    ErrorCode.INVALID_URL: "The link could not be read as a valid URL.",
    ErrorCode.UNSUPPORTED_PLATFORM: "Posts from this platform are not supported yet.",
    ErrorCode.MEDIA_UNAVAILABLE: "The media could not be retrieved.",
    ErrorCode.TRANSCRIPTION_FAILED: "The audio could not be transcribed.",
    ErrorCode.SEARCH_FAILED: "The source search failed.",
    ErrorCode.PROCESSING_FAILED: "The analysis could not be completed.",
    ErrorCode.STORAGE_FAILED: "The report could not be saved.",
    ErrorCode.MESSAGE_SEND_FAILED: "The summary message could not be sent.",
}


class QuantroError(Exception):
    def __init__(
        self,
        code: ErrorCode | str,
        stage: Stage | str,
        message: str | None = None,
        retryable: bool = False,
    ) -> None:
        self.code = ErrorCode(code)
        self.stage = Stage(stage)
        self.message = (
            message if message is not None else _SAFE_MESSAGE_BY_CODE[self.code]
        )
        self.retryable = retryable
        super().__init__(self.message)

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "code": self.code.value,
            "stage": self.stage.value,
            "message": self.message,
            "retryable": self.retryable,
        }


class JobConflictError(QuantroError):
    """A job ID reused for a different URL, or re-ingested after it completed; routes answer 409."""

    http_status = 409

    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.PROCESSING_FAILED, Stage.RETRIEVE_POST, message)


# Codes whose raise sites default to retryable=True (transient infrastructure failures).
# Persisted jobs keep only the code, so status responses derive retryability from it.
RETRYABLE_CODES = frozenset({ErrorCode.STORAGE_FAILED, ErrorCode.SEARCH_FAILED, ErrorCode.MESSAGE_SEND_FAILED})


_DEFAULT_CODE_BY_STAGE: dict[Stage, ErrorCode] = {
    Stage.RETRIEVE_POST: ErrorCode.MEDIA_UNAVAILABLE,
    Stage.EXTRACT_MEDIA_FEATURES: ErrorCode.PROCESSING_FAILED,
    Stage.BUILD_SEARCH_QUERIES: ErrorCode.PROCESSING_FAILED,
    Stage.SEARCH_CANDIDATES: ErrorCode.SEARCH_FAILED,
    Stage.RETRIEVE_CANDIDATE_MEDIA: ErrorCode.MEDIA_UNAVAILABLE,
    Stage.VERIFY_AND_ALIGN: ErrorCode.PROCESSING_FAILED,
    Stage.SELECT_SURROUNDING_CONTEXT: ErrorCode.PROCESSING_FAILED,
    Stage.COMPARE_CONTEXT_AND_CAPTION: ErrorCode.PROCESSING_FAILED,
    Stage.BUILD_REPORT: ErrorCode.PROCESSING_FAILED,
    Stage.SAVE_REPORT_AND_COMPLETE_JOB: ErrorCode.STORAGE_FAILED,
    Stage.DISPLAY_REPORT: ErrorCode.PROCESSING_FAILED,
    Stage.SEND_SUMMARY: ErrorCode.MESSAGE_SEND_FAILED,
    Stage.RUN_ANALYSIS: ErrorCode.PROCESSING_FAILED,
}

_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError)


def normalize_exception(stage: Stage | str, exc: BaseException) -> QuantroError:
    if isinstance(exc, QuantroError):
        return exc

    stage = Stage(stage)
    return QuantroError(
        code=_DEFAULT_CODE_BY_STAGE[stage],
        stage=stage,
        retryable=isinstance(exc, _RETRYABLE_EXCEPTIONS),
    )


__all__ = ["RETRYABLE_CODES", "ErrorCode", "JobConflictError", "QuantroError", "Stage", "normalize_exception"]
