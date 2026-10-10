"""First end-to-end slice: URL -> Post -> MediaFeatures -> Tiger Data.

Runs retrieve_post and extract_media_features, validates both outputs against
the v2 contract, and persists them for the later pipeline stages. Job rows are
only written once a post was retrieved, so an unreadable URL leaves no trace.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel

from ..external_apis.retrieve_post import RetrievalError, retrieve_post
from ..processing.preprocessing import extract_media_features
from ..reporting.errors import ErrorCode, QuantroError, Stage
from ..reporting.types import MediaFeatures, Post, TranscriptStatus, parse_stage_output
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


class IngestResult(BaseModel):
    job_id: str
    url: str
    post_id: str
    media_id: str
    transcript_status: TranscriptStatus
    sentence_count: int
    frames_count: int
    persisted_at: str


def _retrieval_error(exc: Exception) -> QuantroError:
    if isinstance(exc, RetrievalError) and exc.code in _PASSTHROUGH_RETRIEVAL_CODES:
        return QuantroError(code=exc.code, stage=Stage.RETRIEVE_POST, message=exc.message)
    return QuantroError(code=ErrorCode.MEDIA_UNAVAILABLE, stage=Stage.RETRIEVE_POST)


def _storage_error(stage: Stage) -> QuantroError:
    return QuantroError(code=ErrorCode.STORAGE_FAILED, stage=stage, retryable=True)


@dataclass(frozen=True)
class IngestOutput:
    result: IngestResult
    post: Post
    features: MediaFeatures


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
        raw_post = retrieve_post(url)
    except Exception as exc:
        raise _retrieval_error(exc) from exc
    post = parse_stage_output(Post, raw_post, Stage.RETRIEVE_POST)

    try:
        store.upsert_job(job_id, url, "running")
    except Exception as exc:
        raise _storage_error(Stage.RETRIEVE_POST) from exc

    try:
        raw_features = extract_media_features(post.media.model_dump(mode="json"), dict(EXTRACTION_OPTIONS))
        features = parse_stage_output(MediaFeatures, raw_features, Stage.EXTRACT_MEDIA_FEATURES)
    except Exception as exc:
        error = QuantroError(
            code=ErrorCode.TRANSCRIPTION_FAILED, stage=Stage.EXTRACT_MEDIA_FEATURES, retryable=True
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
        raise _storage_error(Stage.EXTRACT_MEDIA_FEATURES) from exc

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
