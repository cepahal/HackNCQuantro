"""display_report stage of run_analysis.

Turns a saved ReportV2 into the display model the report page renders. All
user-facing labels and missing-data wording are decided here so the frontend
never has to: missing values read "Not assessed" or "Unavailable" (never 0),
confidence is shown as a level word (never a percentage), and poster metadata
is kept in its own block, apart from the confidence blocks.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from ..shared.contracts import validate_contract_result
from ..shared.errors import ErrorCode, QuantroError, Stage
from ..shared.errors import Stage as _ContractStage
from ..shared.errors import normalize_exception as _normalize_contract_exception
from ..shared.post_identity import ensure_distinct_source
from ..shared.types import (
    AssessmentBlock,
    ClipCoverageBlock,
    ConfidenceLevel,
    DisplayModel,
    MediaPanel,
    Outcome,
    PosterInfo,
    PosterInfoBlock,
    ReportV2,
    parse_stage_output,
)
from ..shared.types import DisplayModel as _ReturnModel

SOURCE_MATCH_LABEL = "Source-match confidence"
CLIP_COVERAGE_LABEL = "Submitted clip aligned"
CONTEXT_ASSESSMENT_LABEL = "Context-assessment confidence"
CONFIDENCE_NOTE = (
    "Confidence levels describe the available evidence; they are not calibrated probabilities."
)
NO_SOURCE_MESSAGE = "No verified source found"
CONTEXT_UNAVAILABLE_MESSAGE = "Context assessment unavailable"
NOT_ASSESSED = "Not assessed"
UNAVAILABLE = "Unavailable"

_LEVEL_LABELS: dict[ConfidenceLevel, str] = {
    ConfidenceLevel.HIGH: "High",
    ConfidenceLevel.MODERATE: "Moderate",
    ConfidenceLevel.LOW: "Low",
    ConfidenceLevel.NOT_ASSESSED: NOT_ASSESSED,
}
_POSTER_COUNT_FIELDS = ("followers_count", "following_count", "posts_count")


def _display_error(message: str) -> QuantroError:
    return QuantroError(code=ErrorCode.PROCESSING_FAILED, stage=Stage.DISPLAY_REPORT, message=message)


def level_label(level: ConfidenceLevel | str) -> str:
    try:
        return _LEVEL_LABELS[ConfidenceLevel(level)]
    except ValueError as exc:
        raise _display_error(f"Unknown confidence level: {level!r}") from exc


def _assessment_block(label: str, confidence: Mapping[str, Any]) -> AssessmentBlock:
    return AssessmentBlock(
        label=label,
        value=level_label(confidence["level"]),
        basis=list(confidence.get("basis") or []),
        limitations=list(confidence.get("limitations") or []),
    )


def build_clip_coverage(
    clip_coverage_ratio: float | None, matched_clip_seconds: float | None
) -> ClipCoverageBlock | None:
    if clip_coverage_ratio is None:
        return None
    if not 0 <= clip_coverage_ratio <= 1:
        raise _display_error("clip_coverage_ratio must be between 0 and 1")
    description = (
        f"{matched_clip_seconds} seconds aligned to the selected source."
        if matched_clip_seconds is not None
        else NOT_ASSESSED
    )
    return ClipCoverageBlock(
        label=CLIP_COVERAGE_LABEL,
        value=f"{round(clip_coverage_ratio * 100)}%",
        description=description,
    )


def display_count(value: int | None, missing: str) -> int | str:
    if value is None:
        return missing
    if isinstance(value, bool) or not isinstance(value, int):
        raise _display_error("Counts must be integers or null")
    return value


def build_poster_info(author: Mapping[str, Any] | None) -> PosterInfo:
    author = author or {}
    return PosterInfo(
        **{field: display_count(author.get(field), UNAVAILABLE) for field in _POSTER_COUNT_FIELDS}
    )


def _display_edit_analysis(edit_analysis: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if edit_analysis is None:
        return None
    displayed = dict(edit_analysis)
    counts = edit_analysis.get("counts")
    if counts is not None:
        displayed["counts"] = {name: display_count(value, NOT_ASSESSED) for name, value in counts.items()}
    return displayed


def _check_generated_text(model: DisplayModel) -> None:
    """Guard the labels this module writes; user content in panels is left alone."""
    for block in (model.source_match, model.context_assessment):
        if block is not None and block.value not in _LEVEL_LABELS.values():
            raise _display_error("Confidence must be displayed as a level, not a number")
    generated = model.model_dump_json(
        include={
            "source_match",
            "clip_coverage",
            "context_assessment",
            "confidence_note",
            "no_source_message",
            "context_unavailable_message",
        }
    )
    if "100% accurate" in generated.lower():
        raise _display_error("Display text must not claim accuracy")


def build_display_model(
    *,
    outcome: Outcome | str,
    submitted_post: Mapping[str, Any],
    submitted_transcript: Mapping[str, Any] | None,
    submitted_playback_url: str | None,
    source_post: Mapping[str, Any] | None,
    source_transcript: Mapping[str, Any] | None,
    source_playback_url: str | None,
    match_confidence: Mapping[str, Any] | None,
    clip_coverage_ratio: float | None,
    matched_clip_seconds: float | None,
    assessment_confidence: Mapping[str, Any] | None,
    matches: Sequence[Mapping[str, Any]],
    edit_analysis: Mapping[str, Any] | None,
    findings: Sequence[Mapping[str, Any]],
    limitations: Sequence[str],
) -> DisplayModel:
    outcome = Outcome(outcome)
    source_match = clip_coverage = context_assessment = None
    no_source_message = context_unavailable_message = None
    source_panel = None

    if outcome is Outcome.NO_VERIFIED_MATCH:
        no_source_message = NO_SOURCE_MESSAGE
    else:
        if source_post is None or match_confidence is None:
            raise _display_error("Matched report is missing its source post or match confidence")
        source_match = _assessment_block(SOURCE_MATCH_LABEL, match_confidence)
        clip_coverage = build_clip_coverage(clip_coverage_ratio, matched_clip_seconds)
        if assessment_confidence is None:
            context_assessment = AssessmentBlock(
                label=CONTEXT_ASSESSMENT_LABEL, value=NOT_ASSESSED, basis=[], limitations=[]
            )
        else:
            context_assessment = _assessment_block(CONTEXT_ASSESSMENT_LABEL, assessment_confidence)
        if context_assessment.value == NOT_ASSESSED:
            context_unavailable_message = CONTEXT_UNAVAILABLE_MESSAGE
        source_panel = MediaPanel(
            post=dict(source_post),
            transcript=dict(source_transcript) if source_transcript is not None else None,
            media_playback_url=source_playback_url,
        )

    model = DisplayModel(
        source_match=source_match,
        clip_coverage=clip_coverage,
        context_assessment=context_assessment,
        confidence_note=CONFIDENCE_NOTE,
        no_source_message=no_source_message,
        context_unavailable_message=context_unavailable_message,
        submitted_panel=MediaPanel(
            post=dict(submitted_post),
            transcript=dict(submitted_transcript) if submitted_transcript is not None else None,
            media_playback_url=submitted_playback_url,
        ),
        source_panel=source_panel,
        poster_info=PosterInfoBlock(
            submitted=build_poster_info(submitted_post.get("author")),
            source=build_poster_info(source_post.get("author")) if source_panel is not None else None,
        ),
        matches=[dict(match) for match in matches] if source_panel is not None else [],
        edit_analysis=_display_edit_analysis(edit_analysis) if source_panel is not None else None,
        findings=[dict(finding) for finding in findings] if source_panel is not None else [],
        limitations=list(limitations),
    )
    _check_generated_text(model)
    return model


def display_report(report: ReportV2 | Mapping[str, Any]) -> DisplayModel:
    # aligned with shared/types.py on 2026-10-10
    try:
        if not isinstance(report, ReportV2):
            report = parse_stage_output(ReportV2, report, Stage.DISPLAY_REPORT)
        ensure_distinct_source(report.submitted.post, report.source.post if report.source else None, Stage.DISPLAY_REPORT)
        data = report.model_dump(mode="json")
        submitted, source = data["submitted"], data["source"]
        alignment, comparison = data["alignment"] or {}, data["comparison"] or {}
        match_quality = alignment.get("match_quality") or {}
        return validate_contract_result(_ReturnModel, build_display_model(
            outcome=data["outcome"],
            submitted_post=submitted["post"],
            submitted_transcript=submitted["transcript"],
            submitted_playback_url=submitted["post"]["media"]["playback_url"],
            source_post=source["post"] if source else None,
            source_transcript=source["transcript"] if source else None,
            source_playback_url=source["post"]["media"]["playback_url"] if source else None,
            match_confidence=match_quality.get("confidence"),
            clip_coverage_ratio=match_quality.get("clip_coverage_ratio"),
            matched_clip_seconds=match_quality.get("matched_clip_seconds"),
            assessment_confidence=comparison.get("assessment_confidence"),
            matches=alignment.get("matches") or [],
            edit_analysis=alignment.get("edit_analysis"),
            findings=comparison.get("findings") or [],
            limitations=data["limitations"],
        ), _ContractStage.DISPLAY_REPORT, json_native=False)
    except Exception as exc:  # noqa: BLE001 - Public contract boundary redacts and normalizes failures.
        raise _normalize_contract_exception(_ContractStage.DISPLAY_REPORT, exc) from None
