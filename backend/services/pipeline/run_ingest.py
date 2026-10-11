"""First end-to-end slice: URL -> Post -> MediaFeatures -> Tiger Data.

Runs retrieve_post and extract_media_features, validates both outputs against
the v2 contract, and persists them for the later pipeline stages. Failed submitted
retrievals retain a failed job row so clients can inspect the failing stage.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from ..external_apis.retrieve_post import RetrievalError, _validate_url, retrieve_post
from ..pipeline.artifacts import artifact_scope
from ..processing.preprocessing import extract_media_features
from ..shared.errors import ErrorCode, QuantroError, Stage, normalize_exception
from ..shared.types import (
    IngestResult,
    MediaFeatures,
    Post,
    parse_stage_output,
)
from ..storage.tiger import TigerStore

logger = logging.getLogger(__name__)

EXTRACTION_OPTIONS = {
    "transcript_scope": "full",
    "transcript_ranges": [],
    "extract_frames": True,
    "max_frames": 12,
    "frame_ranges": [],
    "extract_visual_text": True,
}

_PASSTHROUGH_RETRIEVAL_CODES = {
    ErrorCode.INVALID_URL.value,
    ErrorCode.UNSUPPORTED_PLATFORM.value,
    ErrorCode.STORAGE_FAILED.value,
}




def _retrieval_error(exc: Exception) -> QuantroError:
    if isinstance(exc, QuantroError):
        return exc
    if isinstance(exc, RetrievalError) and exc.code in _PASSTHROUGH_RETRIEVAL_CODES:
        return QuantroError(code=exc.code, stage=Stage.RETRIEVE_POST, message=exc.message, retryable=exc.retryable)
    return QuantroError(
        code=ErrorCode.MEDIA_UNAVAILABLE, stage=Stage.RETRIEVE_POST,
        retryable=exc.retryable if isinstance(exc, RetrievalError) else isinstance(exc, (TimeoutError, ConnectionError)),
    )


def _storage_error(stage: Stage) -> QuantroError:
    return QuantroError(code=ErrorCode.STORAGE_FAILED, stage=stage, retryable=True)


@dataclass(frozen=True)
class IngestOutput:
    result: IngestResult
    post: Post
    features: MediaFeatures


@artifact_scope()
def run_ingest(job_id: str, url: str, *, store: TigerStore | None = None,
               _defer_platform_challenge: bool = False) -> IngestResult:
    if store is None:
        owned_store = TigerStore.from_env()
        try:
            return ingest_post_and_features(job_id, url, store=owned_store,
                                            _defer_platform_challenge=_defer_platform_challenge).result
        finally:
            owned_store.close()
    return ingest_post_and_features(job_id, url, store=store,
                                   _defer_platform_challenge=_defer_platform_challenge).result


def ingest_post_and_features(job_id: str, url: str, *, store: TigerStore,
                             _defer_platform_challenge: bool = False) -> IngestOutput:
    """run_ingest, also returning the validated models for the rest of run_analysis."""
    # Reject invalid, private and unsupported URLs before any database write.
    _validate_url(url)
    try:
        store.upsert_job(job_id, url, "running")
    except Exception as exc:
        raise _storage_error(Stage.RETRIEVE_POST) from exc

    try:
        raw_post = retrieve_post(url)
        post = parse_stage_output(Post, raw_post, Stage.RETRIEVE_POST)
    except Exception as exc:
        error = _retrieval_error(exc)
        logger.warning("Pipeline failure code=%s stage=%s type=%s", error.code.value, error.stage.value, type(exc).__name__)
        if (_defer_platform_challenge and isinstance(error, RetrievalError)
                and error.code == ErrorCode.MEDIA_UNAVAILABLE
                and error.reason == "platform_challenge" and _validate_url(url)[1] == "youtube"):
            # The analysis owner is still working; polling must not see a terminal failure.
            raise error from exc
        try:
            store.set_job_status(job_id, "failed", error.code.value, error.stage.value, str(error.public_dict()["message"]), error.retryable)
        except Exception as storage_exc:
            raise _storage_error(Stage.RETRIEVE_POST) from storage_exc
        raise error from exc

    try:
        raw_features = extract_media_features(post.media, dict(EXTRACTION_OPTIONS))
        features = parse_stage_output(MediaFeatures, raw_features, Stage.EXTRACT_MEDIA_FEATURES)
    except Exception as exc:
        error = (
            normalize_exception(Stage.EXTRACT_MEDIA_FEATURES, exc)
            if isinstance(exc, QuantroError)
            else QuantroError(
                code=ErrorCode.TRANSCRIPTION_FAILED,
                stage=Stage.EXTRACT_MEDIA_FEATURES,
                retryable=isinstance(exc, (TimeoutError, ConnectionError)),
            )
        )
        logger.warning("Pipeline failure code=%s stage=%s type=%s", error.code.value, error.stage.value, type(exc).__name__)
        try:
            store.upsert_job(job_id, url, "failed", error.code.value, error.stage.value, str(error.public_dict()["message"]), error.retryable)
        except Exception as storage_exc:
            raise _storage_error(Stage.EXTRACT_MEDIA_FEATURES) from storage_exc
        raise error from exc

    try:
        store.save_post_and_features(
            job_id, post.model_dump(mode="json"), features.model_dump(mode="json")
        )
        store.upsert_job(job_id, url, "ingested")
    except Exception as exc:
        error = _storage_error(Stage.EXTRACT_MEDIA_FEATURES)
        try:
            store.set_job_status(job_id, "failed", error.code.value, error.stage.value, str(error.public_dict()["message"]), error.retryable)
        except Exception as storage_exc:
            raise _storage_error(Stage.EXTRACT_MEDIA_FEATURES) from storage_exc
        raise error from exc

    result = IngestResult(
        job_id=job_id,
        url=url,
        post_id=post.post_id,
        media_id=post.media.media_id,
        transcript_status=features.transcript.status,
        sentence_count=len(features.transcript.sentences),
        frames_count=len(features.frames),
        persisted_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    return IngestOutput(result=result, post=post, features=features)
