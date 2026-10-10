"""First end-to-end slice: URL -> Post -> MediaFeatures -> Tiger Data.

Runs retrieve_post and extract_media_features, validates both outputs against
the v2 contract, and persists them for the later pipeline stages. Rejected URLs and
job ID conflicts fail before any job row is written; failed retrievals of accepted
URLs retain a failed job row so clients can inspect the failing stage.
"""

from ..pipeline.artifacts import artifact_scope
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..external_apis.retrieve_post import RetrievalError, _validate_url, retrieve_post
from ..processing.preprocessing import extract_media_features
from ..shared.errors import ErrorCode, JobConflictError, QuantroError, Stage, normalize_exception
from ..shared.types import (
    IngestResult,
    MediaFeatures,
    Post,
    parse_stage_output,
)
from ..storage.tiger import TigerStore

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


def _normalized_post_url(value: str) -> str:
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower()
        port = url.port
        if port is not None and (url.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            host = f"{host}:{port}"
        return urlunsplit((url.scheme.lower(), host, url.path.rstrip("/") or "/", urlencode(sorted(parse_qsl(url.query))), ""))
    except (TypeError, ValueError):
        return value


def check_job_identity(existing_job: dict | None, url: str) -> None:
    """A job ID belongs to the first URL submitted with it."""
    if existing_job is not None and _normalized_post_url(existing_job["url"]) != _normalized_post_url(url):
        raise JobConflictError("This job ID already belongs to a different submitted URL.")


@dataclass(frozen=True)
class IngestOutput:
    result: IngestResult
    post: Post
    features: MediaFeatures


@artifact_scope()
def run_ingest(job_id: str, url: str, *, store: TigerStore | None = None) -> IngestResult:
    if store is None:
        owned_store = TigerStore.from_env()
        try:
            return ingest_post_and_features(job_id, url, store=owned_store).result
        finally:
            owned_store.close()
    return ingest_post_and_features(job_id, url, store=store).result


def ingest_post_and_features(job_id: str, url: str, *, store: TigerStore) -> IngestOutput:
    """run_ingest, also returning the validated models for the rest of run_analysis."""
    try:
        _validate_url(url)
    except Exception as exc:
        raise _retrieval_error(exc) from exc
    try:
        existing_job = store.get_job(job_id)
    except Exception as exc:
        raise _storage_error(Stage.RETRIEVE_POST) from exc
    check_job_identity(existing_job, url)
    if existing_job is not None and existing_job["status"] == "completed":
        raise JobConflictError("This job has already completed; submit the link with a new job ID.")
    try:
        store.upsert_job(job_id, url, "running")
    except Exception as exc:
        raise _storage_error(Stage.RETRIEVE_POST) from exc

    try:
        raw_post = retrieve_post(url)
        post = parse_stage_output(Post, raw_post, Stage.RETRIEVE_POST)
    except Exception as exc:
        error = _retrieval_error(exc)
        try:
            store.set_job_status(job_id, "failed", error.code.value, error.stage.value)
        except Exception as storage_exc:
            raise _storage_error(Stage.RETRIEVE_POST) from storage_exc
        raise error from exc

    try:
        raw_features = extract_media_features(post.media.model_dump(mode="json"), dict(EXTRACTION_OPTIONS))
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
        try:
            store.upsert_job(job_id, url, "failed", error.code.value, error.stage.value)
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
            store.set_job_status(job_id, "failed", error.code.value, error.stage.value)
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
