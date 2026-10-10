"""Pydantic models mirroring the Quantro v2 contract (report schema 2.1).

Canonical shared models for every stage of run_analysis. Upstream stages keep returning
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
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    ValidationError,
)

from .errors import ErrorCode, QuantroError, Stage


def _utc_iso_8601(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("timestamp must be ISO 8601 in UTC")
    return value


UtcTimestamp = Annotated[StrictStr, AfterValidator(_utc_iso_8601)]
Count = Annotated[int, Field(ge=0, strict=True)]
Seconds = Annotated[float, Field(ge=0, strict=True)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def _string_value(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a JSON string.")  # noqa: TRY004 - Pydantic wraps ValueError as a schema failure.
    return value


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
    fps: Seconds | None = None
    has_audio: StrictBool | None = None


class MediaAsset(PublicMediaAsset):
    local_path: str


class Retrieval(ContractModel):
    retrieved_at: UtcTimestamp
    provider: str
    metadata_source_urls: list[str] = Field(default_factory=list, strict=True)
    unavailable_fields: list[str] = Field(default_factory=list, strict=True)
    warnings: list[str] = Field(default_factory=list, strict=True)


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
    processed_ranges: list[TimeRange] = Field(default_factory=list, strict=True)
    sentences: list[TranscriptSentence] = Field(default_factory=list, strict=True)


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
    frames: list[Frame] = Field(default_factory=list, strict=True)
    visual_text: list[VisualText] = Field(default_factory=list, strict=True)
    warnings: list[str] = Field(default_factory=list, strict=True)


# --- verify_and_align --------------------------------------------------------


class ConfidenceObject(ContractModel):
    level: Annotated[ConfidenceLevel, BeforeValidator(_string_value)]
    method: StrictStr
    basis: list[StrictStr] = Field(default_factory=list, strict=True)
    limitations: list[StrictStr] = Field(default_factory=list, strict=True)


class MatchEvidence(ContractModel):
    method: StrictStr
    clip_refs: list[str] = Field(default_factory=list, strict=True)
    source_refs: list[str] = Field(default_factory=list, strict=True)
    description: str


class Match(ContractModel):
    match_id: str
    clip_start_seconds: Seconds
    clip_end_seconds: Seconds
    source_start_seconds: Seconds
    source_end_seconds: Seconds
    evidence: list[MatchEvidence] = Field(default_factory=list, strict=True)


class MatchQuality(ContractModel):
    confidence: ConfidenceObject
    matched_clip_seconds: Seconds | None = None
    clip_coverage_ratio: Annotated[float, Field(ge=0, le=1, strict=True)] | None = None
    metrics: list[dict[str, Any]] = Field(default_factory=list, strict=True)
    methods_used: list[str] = Field(default_factory=list, strict=True)


class SourceGap(ContractModel):
    start_seconds: Seconds
    end_seconds: Seconds
    before_match_id: str
    after_match_id: str


class EditCounts(ContractModel):
    matched_passages: Count | None = None
    source_gaps: Count | None = None
    unmatched_clip_segments: Count | None = None
    confirmed_added_segments: Count | None = None
    confirmed_deleted_segments: Count | None = None


class EditAnalysis(ContractModel):
    status: Literal["assessed", "not_assessed"]
    scope: str
    minimum_gap_seconds: Seconds
    source_gaps: list[SourceGap] = Field(default_factory=list, strict=True)
    unmatched_clip_ranges: list[TimeRange] = Field(default_factory=list, strict=True)
    counts: EditCounts


class Alignment(ContractModel):
    status: Literal["verified", "no_verified_match", "not_assessed"]
    clip_media_id: str
    source_media_id: str
    matches: list[Match] = Field(default_factory=list, strict=True)
    match_quality: MatchQuality
    edit_analysis: EditAnalysis
    limitations: list[str] = Field(default_factory=list, strict=True)


# --- select_surrounding_context ----------------------------------------------


class ContextWindow(ContractModel):
    context_id: str
    match_ids: list[str] = Field(default_factory=list, strict=True)
    requested_start_seconds: Seconds
    requested_end_seconds: Seconds
    coverage: Literal["complete", "partial", "unavailable"]
    sentences: list[TranscriptSentence] = Field(default_factory=list, strict=True)


class Context(ContractModel):
    source_media_id: str
    windows: list[ContextWindow] = Field(default_factory=list, strict=True)
    limitations: list[str] = Field(default_factory=list, strict=True)


# --- compare_context_and_caption ---------------------------------------------

AssessmentConfidence = ConfidenceObject


class Evidence(ContractModel):
    media_id: StrictStr
    sentence_ids: list[StrictStr] = Field(min_length=1, strict=True)
    start_seconds: Seconds
    end_seconds: Seconds
    quote: StrictStr = Field(min_length=1)


class Finding(ContractModel):
    finding_id: StrictStr
    type: StrictStr
    explanation: StrictStr
    evidence: list[Evidence] = Field(min_length=1, strict=True)


class Comparison(ContractModel):
    context_assessment: Annotated[ContextAssessment, BeforeValidator(_string_value)]
    caption_assessment: Annotated[CaptionAssessment, BeforeValidator(_string_value)]
    assessment_confidence: AssessmentConfidence
    summary: StrictStr
    findings: list[Finding] = Field(default_factory=list, strict=True)
    limitations: list[StrictStr] = Field(default_factory=list, strict=True)


# --- report ------------------------------------------------------------------


class ReportMedia(ContractModel):
    post: PublicPost
    transcript: Transcript
    frames: list[PublicFrame] = Field(default_factory=list, strict=True)
    visual_text: list[VisualText] = Field(default_factory=list, strict=True)


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
    limitations: list[str] = Field(default_factory=list, strict=True)


class SavedReportRef(ContractModel):
    """The reference fields already returned by completed report saves."""

    job_id: str
    report_id: str
    report_url: str


class JobCompletionResult(SavedReportRef):
    status: Literal["completed"]
    completed_at: UtcTimestamp


class DeliveryResult(ContractModel):
    conversation_id: str
    message_id: str
    status: Literal["accepted"]


# --- search, orchestration, and public display --------------------------------


class Queries(ContractModel):
    queries: list[str] = Field(default_factory=list, strict=True)
    warnings: list[str] = Field(default_factory=list, strict=True)


class Candidate(ContractModel):
    candidate_id: str
    url: str
    title: str | None = None
    publisher: str | None = None
    published_at: UtcTimestamp | None = None
    snippet: str | None = None
    discovered_by_queries: list[str] = Field(default_factory=list, strict=True)


class CandidateList(ContractModel):
    candidates: list[Candidate] = Field(default_factory=list, strict=True)
    warnings: list[str] = Field(default_factory=list, strict=True)


class IngestResult(ContractModel):
    job_id: str
    url: str
    post_id: str
    media_id: str
    transcript_status: TranscriptStatus
    sentence_count: Count
    frames_count: Count
    persisted_at: UtcTimestamp


class AnalysisResult(SavedReportRef):
    outcome: Outcome
    delivery_status: Literal["accepted", "failed", "skipped"]
    delivery_warning: str | None = None
    warnings: list[str] = Field(default_factory=list, strict=True)


class AssessmentBlock(ContractModel):
    label: str
    value: str
    basis: list[str] = Field(default_factory=list, strict=True)
    limitations: list[str] = Field(default_factory=list, strict=True)


class ClipCoverageBlock(ContractModel):
    label: str
    value: str
    description: str


class MediaPanel(ContractModel):
    post: dict[str, Any]
    transcript: dict[str, Any] | None = None
    media_playback_url: str | None = None


class PosterInfo(ContractModel):
    followers_count: int | str
    following_count: int | str
    posts_count: int | str


class PosterInfoBlock(ContractModel):
    submitted: PosterInfo
    source: PosterInfo | None = None


class DisplayModel(ContractModel):
    source_match: AssessmentBlock | None = None
    clip_coverage: ClipCoverageBlock | None = None
    context_assessment: AssessmentBlock | None = None
    confidence_note: str
    no_source_message: str | None = None
    context_unavailable_message: str | None = None
    submitted_panel: MediaPanel
    source_panel: MediaPanel | None = None
    poster_info: PosterInfoBlock
    matches: list[dict[str, Any]] = Field(default_factory=list, strict=True)
    edit_analysis: dict[str, Any] | None = None
    findings: list[dict[str, Any]] = Field(default_factory=list, strict=True)
    limitations: list[str] = Field(default_factory=list, strict=True)


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


__all__ = [
    "Alignment",
    "AnalysisResult",
    "AssessmentBlock",
    "AssessmentConfidence",
    "Author",
    "Candidate",
    "CandidateList",
    "CaptionAssessment",
    "ClipCoverageBlock",
    "Comparison",
    "ConfidenceLevel",
    "ConfidenceObject",
    "Context",
    "ContextAssessment",
    "ContextWindow",
    "ContractModel",
    "Count",
    "DeliveryResult",
    "DisplayModel",
    "EditAnalysis",
    "EditCounts",
    "Engagement",
    "ErrorCode",
    "Evidence",
    "Finding",
    "Frame",
    "IngestResult",
    "JobCompletionResult",
    "Match",
    "MatchEvidence",
    "MatchQuality",
    "MediaAsset",
    "MediaFeatures",
    "MediaPanel",
    "ModelT",
    "Outcome",
    "Post",
    "PosterInfo",
    "PosterInfoBlock",
    "PublicFrame",
    "PublicMediaAsset",
    "PublicPost",
    "QuantroError",
    "Queries",
    "ReportMedia",
    "ReportV2",
    "Retrieval",
    "SavedReportRef",
    "Seconds",
    "SourceGap",
    "Stage",
    "TimeRange",
    "Transcript",
    "TranscriptSentence",
    "TranscriptStatus",
    "UtcTimestamp",
    "VisualText",
    "parse_stage_output",
]
