"""run_analysis orchestrator for a single job.

Drives the v2 flow: run_ingest (retrieve_post -> extract_media_features) ->
build_search_queries -> search_candidates -> per candidate
(retrieve_candidate_media -> extract_media_features -> verify_and_align) ->
select_surrounding_context -> compare_context_and_caption -> build_report ->
save_report_and_complete_job -> send_summary.

A failed search is an error, never "no source found". Unassessable candidates
are skipped and disclosed. Messaging happens strictly after the report is saved,
and a messaging failure becomes a delivery warning rather than undoing the save.
"""

import logging
import time
from collections.abc import Callable, Sequence
from ..pipeline.artifacts import artifact_scope
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..external_apis.build_search_queries import build_search_queries
from ..external_apis.retrieve_candidate_media import retrieve_candidate_media
from ..external_apis.search_candidates import search_candidates
from ..pipeline.run_ingest import EXTRACTION_OPTIONS, ingest_post_and_features
from ..processing.preprocessing import extract_media_features
from ..shared.contracts import validate_contract_result
from ..shared.errors import ErrorCode, QuantroError, Stage, normalize_exception
from ..shared.errors import Stage as _ContractStage
from ..shared.errors import normalize_exception as _normalize_contract_exception
from ..shared.types import (
    Alignment,
    AnalysisResult,
    Comparison,
    ConfidenceLevel,
    Context,
    DeliveryResult,
    MediaFeatures,
    Post,
    ReportV2,
    parse_stage_output,
)
from ..shared.types import AnalysisResult as _ReturnModel
from ..shared.types import CandidateList as _CandidateSearch
from ..shared.types import Queries as _SearchQueries
from ..specialist.alignment import verify_and_align
from ..specialist.comparison import compare_context_and_caption
from ..specialist.context import select_surrounding_context
from ..storage.tiger import TigerJobStore, TigerReportStore, TigerStore
from .build_report import build_report
from .save_report import report_id_for_job, save_report_and_complete_job
from .send_summary import DeliveryTracker, MessagingProvider, send_summary

logger = logging.getLogger(__name__)

SEARCH_LIMIT = 5
CONTEXT_OPTIONS = {"padding_seconds": 30.0, "merge_overlapping_windows": True}
MULTIPLE_PLAUSIBLE_SOURCES = "Multiple plausible sources remain."
NO_SOURCE_SUMMARY = "No verified source was found for this clip."

_VIABLE_LEVELS = {ConfidenceLevel.HIGH, ConfidenceLevel.MODERATE}
_LEVEL_RANK: dict[ConfidenceLevel, int] = {
    ConfidenceLevel.HIGH: 3,
    ConfidenceLevel.MODERATE: 2,
    ConfidenceLevel.LOW: 1,
    ConfidenceLevel.NOT_ASSESSED: 0,
}

T = TypeVar("T")








@dataclass(frozen=True)
class _AssessedSource:
    post: Post
    features: MediaFeatures
    alignment: Alignment


def _rank(level: ConfidenceLevel | str, ratio: float | None) -> tuple[int, float]:
    return _LEVEL_RANK[ConfidenceLevel(level)], ratio if ratio is not None else -1.0


def select_source(
    verified: Sequence[tuple[T, ConfidenceLevel | str, float | None]],
) -> tuple[T | None, list[str]]:
    """Pick the strongest verified candidate without hiding a tie.

    Each entry is (candidate, match confidence level, clip_coverage_ratio).
    Ranks by level, then by ratio (null ranks lowest). If the top two are still
    tied, the earlier one is returned along with a limitation saying so.
    """
    if not verified:
        return None, []

    ordered = sorted(verified, key=lambda entry: _rank(entry[1], entry[2]), reverse=True)
    tied = len(ordered) > 1 and _rank(*ordered[0][1:]) == _rank(*ordered[1][1:])
    return ordered[0][0], [MULTIPLE_PLAUSIBLE_SOURCES] if tied else []


def deliver_summary_after_save(
    job_id: str,
    conversation_id: str | None,
    summary: str,
    report_url: str,
    *,
    provider: MessagingProvider | None = None,
    tracker: DeliveryTracker | None = None,
) -> tuple[DeliveryResult | None, str | None]:
    """Send the summary for an already-saved report.

    Returns (delivery, delivery_warning). Never raises for messaging failures,
    so callers cannot accidentally roll back the saved report.
    """
    if not conversation_id:
        return None, None
    try:
        delivery = send_summary(
            conversation_id, summary, report_url, job_id=job_id, provider=provider, tracker=tracker
        )
    except QuantroError as error:
        logger.warning("send_summary failed for job %s: %s", job_id, error.code.value)
        return None, error.message
    return delivery, None


def _call(stage: Stage, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    started = time.monotonic()
    try:
        return function(*args, **kwargs)
    except Exception as exc:
        raise normalize_exception(stage, exc) from exc
    finally:
        logger.info("analysis_stage stage=%s seconds=%.3f", stage.value, time.monotonic() - started)


def _candidate_label(candidate: dict[str, Any], index: int) -> str:
    candidate_id = candidate.get("candidate_id")
    return candidate_id if isinstance(candidate_id, str) and candidate_id else f"#{index}"


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


def _assess_candidate(candidate: dict[str, Any], clip_features: MediaFeatures) -> _AssessedSource:
    raw_post = _call(Stage.RETRIEVE_CANDIDATE_MEDIA, retrieve_candidate_media, candidate)
    post = parse_stage_output(Post, raw_post, Stage.RETRIEVE_CANDIDATE_MEDIA)
    raw_features = _call(
        Stage.EXTRACT_MEDIA_FEATURES,
        extract_media_features,
        post.media.model_dump(mode="json"),
        {**EXTRACTION_OPTIONS, "extract_frames": False, "extract_visual_text": False, "max_frames": 0},
    )
    raw_features["warnings"].append("Candidate visual extraction was skipped; source verification uses transcript alignment only.")
    features = parse_stage_output(MediaFeatures, raw_features, Stage.EXTRACT_MEDIA_FEATURES)
    raw_alignment = _call(
        Stage.VERIFY_AND_ALIGN,
        verify_and_align,
        clip_features.model_dump(mode="json"),
        features.model_dump(mode="json"),
    )
    alignment = parse_stage_output(Alignment, raw_alignment, Stage.VERIFY_AND_ALIGN)
    return _AssessedSource(post=post, features=features, alignment=alignment)


def _mark_failed(store: TigerStore, job_id: str, error: QuantroError) -> None:
    try:
        store.set_job_status(job_id, "failed", error.code.value, error.stage.value)
    except Exception as exc:  # noqa: BLE001 - Preserve the original stage error and redact storage details.
        logger.warning("Could not record failure for job %s (%s)", job_id, type(exc).__name__)


@artifact_scope()
def run_analysis(
    job_id: str,
    url: str,
    conversation_id: str | None = None,
    *,
    store: TigerStore | None = None,
    provider: MessagingProvider | None = None,
    tracker: DeliveryTracker | None = None,
) -> AnalysisResult:
    # aligned with shared/types.py on 2026-10-10
    try:
        if store is None:
            try:
                owned_store = TigerStore.from_env()
            except Exception as exc:
                raise QuantroError(ErrorCode.STORAGE_FAILED, Stage.RETRIEVE_POST, retryable=True) from exc
            try:
                owned_store.ensure_schema()
                return validate_contract_result(_ReturnModel, run_analysis(
                    job_id, url, conversation_id, store=owned_store, provider=provider, tracker=tracker
                ), _ContractStage.RUN_ANALYSIS, json_native=False)
            except QuantroError:
                raise
            except Exception as exc:
                raise QuantroError(ErrorCode.STORAGE_FAILED, Stage.RETRIEVE_POST, retryable=True) from exc
            finally:
                owned_store.close()

        try:
            existing_job = store.get_job(job_id)
            existing_report = store.get_report(report_id_for_job(job_id)) if existing_job and existing_job["status"] == "completed" else None
        except Exception as exc:
            raise QuantroError(ErrorCode.STORAGE_FAILED, Stage.RETRIEVE_POST, retryable=True) from exc
        if existing_job and existing_job["status"] == "completed":
            if _normalized_post_url(existing_job["url"]) != _normalized_post_url(url):
                raise QuantroError(ErrorCode.PROCESSING_FAILED, Stage.RETRIEVE_POST, "This job ID already belongs to a different submitted URL.")
            if existing_report is None:
                raise QuantroError(ErrorCode.STORAGE_FAILED, Stage.SAVE_REPORT_AND_COMPLETE_JOB)
            report = parse_stage_output(ReportV2, existing_report, Stage.BUILD_REPORT)
            return validate_contract_result(_ReturnModel, _complete_and_deliver(job_id, report, conversation_id, store, provider, tracker, []), _ContractStage.RUN_ANALYSIS, json_native=False)

        ingested = ingest_post_and_features(job_id, url, store=store)
        try:
            return validate_contract_result(_ReturnModel, _analyze(job_id, ingested.post, ingested.features, conversation_id, store, provider, tracker), _ContractStage.RUN_ANALYSIS, json_native=False)
        except QuantroError as error:
            _mark_failed(store, job_id, error)
            raise
    except Exception as exc:  # noqa: BLE001 - Public contract boundary redacts and normalizes failures.
        raise _normalize_contract_exception(_ContractStage.RUN_ANALYSIS, exc) from None


def _analyze(
    job_id: str,
    submitted_post: Post,
    submitted_features: MediaFeatures,
    conversation_id: str | None,
    store: TigerStore,
    provider: MessagingProvider | None,
    tracker: DeliveryTracker | None,
) -> AnalysisResult:
    warnings: list[str] = []

    raw_queries = _call(
        Stage.BUILD_SEARCH_QUERIES,
        build_search_queries,
        submitted_features.model_dump(mode="json"),
        submitted_post.model_dump(mode="json"),
    )
    queries = parse_stage_output(_SearchQueries, raw_queries, Stage.BUILD_SEARCH_QUERIES)
    warnings.extend(queries.warnings)

    try:
        raw_search = _call(Stage.SEARCH_CANDIDATES, search_candidates, queries.queries, limit=SEARCH_LIMIT)
    except QuantroError:
        raise
    except Exception as exc:
        raise QuantroError(
            code=ErrorCode.SEARCH_FAILED, stage=Stage.SEARCH_CANDIDATES, retryable=True
        ) from exc
    search = parse_stage_output(_CandidateSearch, raw_search, Stage.SEARCH_CANDIDATES)
    warnings.extend(search.warnings)
    submitted_url = _normalized_post_url(submitted_post.post_url)
    candidate_rows = [candidate.model_dump(mode="json") for candidate in search.candidates]
    eligible_candidates = [
        candidate for candidate in candidate_rows
        if _normalized_post_url(candidate.get("url", "")) != submitted_url
    ]
    candidates = eligible_candidates[:SEARCH_LIMIT]
    if len(eligible_candidates) < len(candidate_rows):
        warnings.append("The submitted post was excluded from its own source candidates.")

    viable: list[_AssessedSource] = []
    skipped = 0
    for index, candidate in enumerate(candidates, start=1):
        try:
            assessed = _assess_candidate(candidate, submitted_features)
        except QuantroError as error:
            skipped += 1
            warnings.append(f"Candidate {_candidate_label(candidate, index)} was skipped: {error.message}")
            continue
        confidence = assessed.alignment.match_quality.confidence
        if assessed.alignment.status == "verified" and confidence.level in _VIABLE_LEVELS:
            viable.append(assessed)

    extra_limitations = list(warnings)
    if skipped:
        extra_limitations.append(
            f"{skipped} of {len(candidates)} candidate sources could not be assessed; source coverage is limited."
        )

    chosen, tie_limitations = select_source(
        [
            (source, source.alignment.match_quality.confidence.level, source.alignment.match_quality.clip_coverage_ratio)
            for source in viable
        ]
    )
    if tie_limitations:
        extra_limitations.extend(tie_limitations)
        chosen_rank = _rank(chosen.alignment.match_quality.confidence.level, chosen.alignment.match_quality.clip_coverage_ratio)
        extra_limitations.extend(
            f"Another plausible source was not selected: {source.post.post_url}"
            for source in viable
            if source is not chosen
            and _rank(source.alignment.match_quality.confidence.level, source.alignment.match_quality.clip_coverage_ratio)
            == chosen_rank
        )

    context: Context | None = None
    comparison: Comparison | None = None
    if chosen is not None:
        raw_context = _call(
            Stage.SELECT_SURROUNDING_CONTEXT,
            select_surrounding_context,
            chosen.alignment.model_dump(mode="json"),
            chosen.features.model_dump(mode="json"),
            dict(CONTEXT_OPTIONS),
        )
        context = parse_stage_output(Context, raw_context, Stage.SELECT_SURROUNDING_CONTEXT)
        raw_comparison = _call(
            Stage.COMPARE_CONTEXT_AND_CAPTION,
            compare_context_and_caption,
            submitted_features.model_dump(mode="json"),
            context.model_dump(mode="json"),
            submitted_post.caption,
        )
        comparison = parse_stage_output(Comparison, raw_comparison, Stage.COMPARE_CONTEXT_AND_CAPTION)

    report = _call(
        Stage.BUILD_REPORT,
        build_report,
        job_id,
        submitted_post,
        submitted_features,
        chosen.post if chosen else None,
        chosen.features if chosen else None,
        chosen.alignment if chosen else None,
        context,
        comparison,
        extra_limitations=extra_limitations,
    )
    return _complete_and_deliver(job_id, report, conversation_id, store, provider, tracker, warnings)


def _complete_and_deliver(
    job_id: str,
    report: ReportV2,
    conversation_id: str | None,
    store: TigerStore,
    provider: MessagingProvider | None,
    tracker: DeliveryTracker | None,
    warnings: list[str],
) -> AnalysisResult:
    saved = save_report_and_complete_job(
        job_id, report, report_store=TigerReportStore(store), job_store=TigerJobStore(store)
    )
    try:
        saved_report = store.get_report(saved.report_id)
    except Exception as exc:
        raise QuantroError(ErrorCode.STORAGE_FAILED, Stage.SAVE_REPORT_AND_COMPLETE_JOB, retryable=True) from exc
    if saved_report is None:
        raise QuantroError(ErrorCode.STORAGE_FAILED, Stage.SAVE_REPORT_AND_COMPLETE_JOB)
    report = parse_stage_output(ReportV2, saved_report, Stage.SAVE_REPORT_AND_COMPLETE_JOB)
    if report.job_id != job_id:
        raise QuantroError(ErrorCode.STORAGE_FAILED, Stage.SAVE_REPORT_AND_COMPLETE_JOB)

    delivery, delivery_warning = deliver_summary_after_save(
        job_id,
        conversation_id,
        report.summary or NO_SOURCE_SUMMARY,
        saved.report_url,
        provider=provider,
        tracker=tracker,
    )
    if not conversation_id:
        delivery_status = "skipped"
    else:
        delivery_status = "accepted" if delivery is not None else "failed"

    return AnalysisResult(
        job_id=job_id,
        outcome=report.outcome,
        report_id=saved.report_id,
        report_url=saved.report_url,
        delivery_status=delivery_status,
        delivery_warning=delivery_warning,
        warnings=warnings,
    )
