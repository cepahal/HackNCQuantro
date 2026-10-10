"""Pydantic models mirroring the Quantro v2 contract (report schema 2.1).

Shared by every reporting stage of run_analysis. Upstream stages keep returning
plain dicts; parse_stage_output enforces these shapes at the reporting boundary.
Field sets are pinned to the teammates' declared contracts by
tests/test_types_contract.py.

Shared rules: seconds are numbers, timestamps are ISO 8601 UTC, unknown values
are null (never omitted, never 0), empty collections are [].

# reconciled with v2 contract on 2026-10-10
"""

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeFloat,
    StrictBool,
    ValidationError,
)

from .errors import ErrorCode, QuantroError, Stage


def _utc_iso_8601(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("timestamp must be ISO 8601 in UTC")
    return value


UtcTimestamp = Annotated[str, AfterValidator(_utc_iso_8601)]
Count = Annotated[int, Field(ge=0, strict=True)]
Seconds = NonNegativeFloat


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ConfidenceLevel(str, Enum):
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    NOT_ASSESSED = "not_assessed"


class Outcome(str, Enum):
    MATCHED = "matched"
    NO_VERIFIED_MATCH = "no_verified_match"


class TranscriptStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class ContextAssessment(str, Enum):
    MATERIAL_CONTEXT_MISSING = "material_context_missing"
    NO_MATERIAL_DIFFERENCE_FOUND = "no_material_difference_found"
    UNCERTAIN = "uncertain"
    NOT_ASSESSED = "not_assessed"


class CaptionAssessment(str, Enum):
    SUPPORTED_BY_CONTEXT = "supported_by_context"
    CONTRADICTED_BY_CONTEXT = "contradicted_by_context"
    UNCERTAIN = "uncertain"
    NOT_PROVIDED = "not_provided"
    NOT_ASSESSED = "not_assessed"


# --- retrieve_post / retrieve_candidate_media -------------------------------


class Author(ContractModel):
    id: str | None = None
    handle: str | None = None
    display_name: str | None = None
    profile_url: str | None = None
    bio: str | None = None
    verified: StrictBool | None = None
    followers_count: Count | None = None
    following_count: Count | None = None
    posts_count: Count | None = None


class Engagement(ContractModel):
    views_count: Count | None = None
    likes_count: Count | None = None
    comments_count: Count | None = None
    shares_count: Count | None = None


class PublicMediaAsset(ContractModel):
    """MediaAsset as it may appear in a saved report: no worker filesystem path."""

    media_id: str
    mime_type: str
    playback_url: str | None = None
    duration_seconds: Seconds | None = None
    width: Count | None = None
    height: Count | None = None
    fps: NonNegativeFloat | None = None
    has_audio: StrictBool | None = None


class MediaAsset(PublicMediaAsset):
    local_path: str


class Retrieval(ContractModel):
    retrieved_at: UtcTimestamp
    provider: str
    metadata_source_urls: list[str]
    unavailable_fields: list[str]
    warnings: list[str]


class PublicPost(ContractModel):
    post_id: str
    candidate_id: str | None = None
    post_url: str
    platform: str
    title: str | None = None
    caption: str | None = None
    author: Author
    engagement: Engagement
    published_at: UtcTimestamp | None = None
    media: PublicMediaAsset
    retrieval: Retrieval


class Post(PublicPost):
    media: MediaAsset


# --- extract_media_features --------------------------------------------------


class TimeRange(ContractModel):
    start_seconds: Seconds
    end_seconds: Seconds


class TranscriptSentence(ContractModel):
    sentence_id: str
    start_seconds: Seconds
    end_seconds: Seconds
    text: str
    speaker_id: str | None = None


class Transcript(ContractModel):
    status: TranscriptStatus
    processed_ranges: list[TimeRange]
    sentences: list[TranscriptSentence]


class PublicFrame(ContractModel):
    """Frame as it may appear in a saved report: no worker filesystem path."""

    frame_id: str
    timestamp_seconds: Seconds
    image_url: str | None = None


class Frame(PublicFrame):
    image_path: str


class VisualText(ContractModel):
    frame_id: str
    timestamp_seconds: Seconds
    text: str


class MediaFeatures(ContractModel):
    media_id: str
    duration_seconds: Seconds
    language: str | None = None
    transcript: Transcript
    frames: list[Frame]
    visual_text: list[VisualText]
    warnings: list[str]


# --- verify_and_align --------------------------------------------------------


class ConfidenceObject(ContractModel):
    level: ConfidenceLevel
    method: str
    basis: list[str]
    limitations: list[str]


class MatchEvidence(ContractModel):
    method: str
    clip_refs: list[str]
    source_refs: list[str]
    description: str


class Match(ContractModel):
    match_id: str
    clip_start_seconds: Seconds
    clip_end_seconds: Seconds
    source_start_seconds: Seconds
    source_end_seconds: Seconds
    evidence: list[MatchEvidence]


class MatchQuality(ContractModel):
    confidence: ConfidenceObject
    matched_clip_seconds: Seconds | None = None
    clip_coverage_ratio: Annotated[float, Field(ge=0, le=1)] | None = None
    metrics: list[dict[str, Any]]
    methods_used: list[str]


class SourceGap(ContractModel):
    start_seconds: Seconds
    end_seconds: Seconds
    before_match_id: str
    after_match_id: str


class EditCounts(ContractModel):
    matched_passages: Count
    source_gaps: Count | None = None
    unmatched_clip_segments: Count | None = None
    confirmed_added_segments: Count | None = None
    confirmed_deleted_segments: Count | None = None


class EditAnalysis(ContractModel):
    status: Literal["assessed", "not_assessed"]
    scope: str
    minimum_gap_seconds: Seconds
    source_gaps: list[SourceGap]
    unmatched_clip_ranges: list[TimeRange]
    counts: EditCounts


class Alignment(ContractModel):
    status: Literal["verified", "no_verified_match", "not_assessed"]
    clip_media_id: str
    source_media_id: str
    matches: list[Match]
    match_quality: MatchQuality
    edit_analysis: EditAnalysis
    limitations: list[str]


# --- select_surrounding_context ----------------------------------------------


class ContextWindow(ContractModel):
    context_id: str
    match_ids: list[str]
    requested_start_seconds: Seconds
    requested_end_seconds: Seconds
    coverage: Literal["complete", "partial", "unavailable"]
    sentences: list[TranscriptSentence]


class Context(ContractModel):
    source_media_id: str
    windows: list[ContextWindow]
    limitations: list[str]


# --- compare_context_and_caption ---------------------------------------------

AssessmentConfidence = ConfidenceObject


class Evidence(ContractModel):
    media_id: str
    sentence_ids: list[str]
    start_seconds: Seconds
    end_seconds: Seconds
    quote: str


class Finding(ContractModel):
    finding_id: str
    type: str
    explanation: str
    evidence: list[Evidence]


class Comparison(ContractModel):
    context_assessment: ContextAssessment
    caption_assessment: CaptionAssessment
    assessment_confidence: AssessmentConfidence
    summary: str
    findings: list[Finding]
    limitations: list[str]


# --- report ------------------------------------------------------------------


class ReportMedia(ContractModel):
    post: PublicPost
    transcript: Transcript
    frames: list[PublicFrame]
    visual_text: list[VisualText]


class ReportV2(ContractModel):
    schema_version: Literal["2.1"] = "2.1"
    job_id: str
    outcome: Outcome
    submitted: ReportMedia
    source: ReportMedia | None = None
    alignment: Alignment | None = None
    context: Context | None = None
    comparison: Comparison | None = None
    summary: str | None = None
    limitations: list[str]


class JobCompletionResult(BaseModel):
    job_id: str
    status: Literal["completed"]
    report_id: str
    report_url: str
    completed_at: str


class DeliveryResult(BaseModel):
    conversation_id: str
    message_id: str
    status: Literal["accepted"]


ModelT = TypeVar("ModelT", bound=BaseModel)


def parse_stage_output(model: type[ModelT], data: Any, stage: Stage | str) -> ModelT:
    """Validate an upstream stage's dict output against the contract model."""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise QuantroError(
            code=ErrorCode.PROCESSING_FAILED,
            stage=stage,
            message=f"{Stage(stage).value} returned data that does not match the v2 contract",
        ) from exc
