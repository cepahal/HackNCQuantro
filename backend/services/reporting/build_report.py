"""build_report stage of run_analysis.

Assembles the ReportV2 payload from the outputs of the earlier stages (post
retrieval, feature extraction, alignment, context selection, comparison) and runs
a sanitization pass so no local paths or secrets reach the saved or displayed
report. It copies upstream confidence verbatim and never derives credibility
from author or engagement fields.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..shared.contracts import validate_contract_result
from ..shared.errors import ErrorCode, QuantroError, Stage
from ..shared.errors import Stage as _ContractStage
from ..shared.errors import normalize_exception as _normalize_contract_exception
from ..shared.post_identity import ensure_distinct_source
from ..shared.types import (
    Alignment,
    Comparison,
    ConfidenceLevel,
    Context,
    MediaFeatures,
    Outcome,
    Post,
    ReportV2,
    parse_stage_output,
)
from ..shared.types import ReportV2 as _ReturnModel

NO_VERIFIED_SOURCE_LIMITATION = "No verified source was found for the submitted excerpt."

_STRIPPED_KEYS = frozenset(
    {"local_path", "image_path", "file_path", "credentials", "api_key", "token", "secret"}
)
_FORBIDDEN_SUBSTRINGS = ("/tmp/", "file://")


def sanitize_report(report: Any) -> Any:
    """Return a copy of a JSON-like report with sensitive keys removed.

    Raises QuantroError if any remaining string value points at a local file.
    """
    if isinstance(report, dict):
        return {
            key: sanitize_report(value)
            for key, value in report.items()
            if key.lower() not in _STRIPPED_KEYS
        }
    if isinstance(report, list):
        return [sanitize_report(item) for item in report]
    if isinstance(report, str) and any(marker in report for marker in _FORBIDDEN_SUBSTRINGS):
        raise QuantroError(
            code=ErrorCode.PROCESSING_FAILED,
            stage=Stage.BUILD_REPORT,
            message="Report contains a local file reference",
        )
    return report


def merge_limitations(*groups: Iterable[str] | None) -> list[str]:
    """Order-preserving, de-duplicated union of limitation/warning lists."""
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group or ():
            if item not in seen:
                seen.add(item)
                merged.append(item)
    return merged


def _build_error(message: str) -> QuantroError:
    return QuantroError(code=ErrorCode.PROCESSING_FAILED, stage=Stage.BUILD_REPORT, message=message)


def _report_media(post: Post, features: MediaFeatures) -> dict[str, Any]:
    return {
        "post": post.model_dump(mode="json"),
        "transcript": features.transcript.model_dump(mode="json"),
        "frames": [frame.model_dump(mode="json") for frame in features.frames],
        "visual_text": [entry.model_dump(mode="json") for entry in features.visual_text],
    }


def build_report(
    job_id: str,
    submitted_post: Post | Mapping[str, Any],
    submitted_features: MediaFeatures | Mapping[str, Any],
    source_post: Post | Mapping[str, Any] | None,
    source_features: MediaFeatures | Mapping[str, Any] | None,
    alignment: Alignment | Mapping[str, Any] | None,
    context: Context | Mapping[str, Any] | None,
    comparison: Comparison | Mapping[str, Any] | None,
    *,
    extra_limitations: Sequence[str] = (),
) -> ReportV2:
    # aligned with shared/types.py on 2026-10-10
    try:
        submitted_post = parse_stage_output(Post, submitted_post, Stage.BUILD_REPORT)
        submitted_features = parse_stage_output(MediaFeatures, submitted_features, Stage.BUILD_REPORT)
        source_post = parse_stage_output(Post, source_post, Stage.BUILD_REPORT) if source_post is not None else None
        source_features = (
            parse_stage_output(MediaFeatures, source_features, Stage.BUILD_REPORT)
            if source_features is not None else None
        )
        alignment = parse_stage_output(Alignment, alignment, Stage.BUILD_REPORT) if alignment is not None else None
        context = parse_stage_output(Context, context, Stage.BUILD_REPORT) if context is not None else None
        comparison = parse_stage_output(Comparison, comparison, Stage.BUILD_REPORT) if comparison is not None else None
        if submitted_post.media.media_id != submitted_features.media_id:
            raise _build_error("Submitted post and features describe different media")

        report: dict[str, Any] = {
            "schema_version": "2.1",
            "job_id": job_id,
            "submitted": _report_media(submitted_post, submitted_features),
            "source": None,
            "alignment": None,
            "context": None,
            "comparison": None,
            "summary": None,
        }

        if source_post is not None and alignment is not None and alignment.status == "verified":
            ensure_distinct_source(submitted_post, source_post, Stage.BUILD_REPORT)
            if alignment.match_quality.confidence.level not in {ConfidenceLevel.HIGH, ConfidenceLevel.MODERATE} or not alignment.matches:
                raise _build_error("A verified source requires an accepted passage and high or moderate match confidence")
            if source_features is None or context is None or comparison is None:
                raise _build_error("Missing confidence object for matched report")
            if source_post.candidate_id is None:
                raise _build_error("Source post is missing its candidate_id")
            source_media_ids = {
                source_post.media.media_id,
                source_features.media_id,
                alignment.source_media_id,
                context.source_media_id,
            }
            if len(source_media_ids) != 1 or alignment.clip_media_id != submitted_features.media_id:
                raise _build_error("Source post, features, alignment and context describe different media")

            report.update(
                outcome=Outcome.MATCHED.value,
                source=_report_media(source_post, source_features),
                alignment=alignment.model_dump(mode="json"),
                context=context.model_dump(mode="json"),
                comparison=comparison.model_dump(mode="json"),
                summary=comparison.summary,
                limitations=merge_limitations(
                    alignment.limitations,
                    alignment.match_quality.confidence.limitations,
                    comparison.limitations,
                    comparison.assessment_confidence.limitations,
                    context.limitations,
                    submitted_post.retrieval.warnings,
                    source_post.retrieval.warnings,
                    submitted_features.warnings,
                    source_features.warnings,
                    extra_limitations,
                ),
            )
        else:
            report.update(
                outcome=Outcome.NO_VERIFIED_MATCH.value,
                limitations=merge_limitations(
                    [NO_VERIFIED_SOURCE_LIMITATION],
                    submitted_post.retrieval.warnings,
                    submitted_features.warnings,
                    extra_limitations,
                ),
            )

        return validate_contract_result(_ReturnModel, parse_stage_output(ReportV2, sanitize_report(report), Stage.BUILD_REPORT), _ContractStage.BUILD_REPORT, json_native=False)
    except Exception as exc:  # noqa: BLE001 - Public contract boundary redacts and normalizes failures.
        raise _normalize_contract_exception(_ContractStage.BUILD_REPORT, exc) from None
