"""Supplied transcript-first resolver, using the existing routes, Tiger and Photon."""

import asyncio
import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from ..external_apis.retrieve_post import _validate_url
from ..external_apis.tigerdata_storage import TigerDataPostStore
from ..reporting.build_report import build_report
from ..reporting.save_report import (
    _report_url,
    report_id_for_job,
    save_report_and_complete_job,
)
from ..reporting.send_summary import DeliveryTracker, MessagingProvider, send_summary
from ..shared.config import environment, validate_search_settings
from ..shared.errors import (
    ErrorCode,
    JobConflictError,
    QuantroError,
    Stage,
    normalize_exception,
)
from ..shared.post_identity import ensure_distinct_source, normalized_post_url
from ..shared.types import AnalysisResult, IngestResult, Post, ReportV2
from ..storage.tiger import TigerJobStore, TigerReportStore, TigerStore
from . import context_video
from .alignment import build_alignment, build_context
from .analysis import compare_clip, interpret_clip
from .candidates import CandidateSearchError, check_source_video, find_source_video
from .diagnostics import DiagnosticCapture, media_snapshot, record
from .media import resolve_media

logger = logging.getLogger(__name__)


def _persist_media(post: Post) -> None:
    """Keep downloaded recordings available through the existing /media route."""
    if not post.media.local_path:
        return
    media_store = TigerDataPostStore.from_env()
    try:
        media_store.save_post(post)
    finally:
        media_store.close()


def _failed(store: TigerStore, job_id: str, error: QuantroError) -> None:
    try:
        store.set_job_status(
            job_id,
            "failed",
            error.code.value,
            error.stage.value,
            str(error.public_dict()["message"]),
            error.retryable,
        )
    except Exception:  # noqa: BLE001 - Recording a failure is best effort; details remain private.
        logger.warning(
            "Could not persist resolver failure; private diagnostics omitted."
        )


def _id(url: str) -> str | None:
    parts = urlsplit(url)
    value = (
        parts.path.strip("/").split("/")[-1]
        if parts.hostname == "youtu.be"
        or parts.path.startswith(("/shorts/", "/embed/"))
        else parse_qs(parts.query).get("v", [None])[0]
    )
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}", value)
        else None
    )


def _validate(job_id: str, url: str) -> None:
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
        raise QuantroError(
            ErrorCode.PROCESSING_FAILED, Stage.RUN_ANALYSIS, "Invalid job identifier."
        )
    _validate_url(url)


def _deliver(
    report: ReportV2,
    conversation_id: str | None,
    store: TigerStore,
    provider: MessagingProvider | None,
    tracker: DeliveryTracker | None,
) -> AnalysisResult:
    warning = None
    status = "skipped"
    if conversation_id:
        try:
            send_summary(
                conversation_id,
                report.summary or "Analysis is ready. Read the evidence in the report.",
                _report_url(report_id_for_job(report.job_id)),
                job_id=report.job_id,
                provider=provider,
                tracker=tracker,
            )
            status = "accepted"
        except Exception as exc:  # noqa: BLE001 - Delivery errors preserve the completed report.
            status = "failed"
            warning = normalize_exception(Stage.SEND_SUMMARY, exc).message
    return AnalysisResult(
        job_id=report.job_id,
        report_id=report_id_for_job(report.job_id),
        report_url=_report_url(report_id_for_job(report.job_id)),
        outcome=report.outcome,
        delivery_status=status,
        delivery_warning=warning,
        warnings=list(report.limitations) + ([warning] if warning else []),
    )


def run_ingest(
    job_id: str, url: str, *, store: TigerStore | None = None
) -> IngestResult:
    """Keep the ingest contract; the route owns its atomic job reservation."""
    _validate(job_id, url)
    owned = store is None
    store = store or TigerStore.from_env()
    stage = Stage.RETRIEVE_POST
    try:
        if owned:
            store.ensure_schema()
            if not store.try_create_job(job_id, url):
                raise JobConflictError(
                    "This job already exists. Use a new job identifier."
                )
        post, features = asyncio.run(resolve_media(url))
        stage = Stage.EXTRACT_MEDIA_FEATURES
        try:
            _persist_media(post)
            store.save_post_and_features(
                job_id, post.model_dump(mode="json"), features.model_dump(mode="json")
            )
            store.set_job_status(job_id, "ingested")
        except Exception as exc:
            error = QuantroError(ErrorCode.STORAGE_FAILED, stage, retryable=True)
            error.exception_type = type(exc).__name__
            raise error from exc
        return IngestResult(
            job_id=job_id,
            url=url,
            post_id=post.post_id,
            media_id=post.media.media_id,
            transcript_status=features.transcript.status,
            sentence_count=len(features.transcript.sentences),
            frames_count=len(features.frames),
            persisted_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
    except JobConflictError:
        raise
    except Exception as exc:  # noqa: BLE001 - Normalize all provider failures at the public boundary.
        error = normalize_exception(stage, exc)
        logger.warning(
            "Pipeline failure code=%s stage=%s type=%s",
            error.code.value,
            error.stage.value,
            error.exception_type,
        )
        _failed(store, job_id, error)
        raise error from None
    finally:
        if owned:
            store.close()


async def _build(job_id: str, url: str, store: TigerStore, *, source_url: str | None = None) -> ReportV2:
    stage = Stage.RETRIEVE_POST
    try:
        started = time.monotonic()
        post, features = await resolve_media(url)
        record(stage.value, "completed", media_snapshot(post, features), started=started)
        stage = Stage.SAVE_REPORT_AND_COMPLETE_JOB
        started = time.monotonic()
        _persist_media(post)
        store.save_post_and_features(
            job_id, post.model_dump(mode="json"), features.model_dump(mode="json")
        )
        store.set_job_status(job_id, "ingested")
        record("persist_submitted_media", "completed", {"media_id": post.media.media_id}, started=started)
        stage = Stage.SEARCH_CANDIDATES
        started = time.monotonic()
        short_text = getattr(features, "full_transcript_text", None) or " ".join(
            sentence.text for sentence in features.transcript.sentences
        )
        discovered = await check_source_video(short_text, source_url) if source_url else await find_source_video(
            short_text,
            title=post.title or post.caption,
            exclude_ids=[identity] if (identity := _id(url)) else [],
        )
        record(stage.value, "completed", {"status": discovered.get("status"), "match_score": discovered.get("match_score"),
                "search_metadata": discovered.get("search_metadata"), "leads": discovered.get("leads", []),
                "candidate_count": len(discovered.get("candidates", [])), "warnings": discovered.get("warnings", [])}, started=started)
        store.touch_job(job_id)
        chosen = None
        viable = []
        warnings = list(discovered.get("warnings", []))
        for number, candidate in enumerate(discovered.get("candidates", []), 1):
            candidate_started = time.monotonic()
            candidate_data = {"candidate_number": number, **candidate}
            record("candidate_evaluation", "scored", candidate_data, started=candidate_started)
            # The full provider transcript is captured above; decisions only reference this candidate.
            candidate_data = {key: value for key, value in candidate_data.items() if key != "transcript_result"}
            if candidate["match_score"] < 75:
                record("candidate_evaluation", "rejected", {**candidate_data, "reason": "lexical_score_below_threshold", "threshold": 75}, started=candidate_started)
                continue
            stage = Stage.RETRIEVE_CANDIDATE_MEDIA
            try:
                source_post, source_features = await resolve_media(
                    candidate["video_url"],
                    candidate_id=f"candidate_{number}",
                    transcript_result=candidate["transcript_result"],
                    metadata=candidate,
                )
                _persist_media(source_post)
                record(stage.value, "completed", {"candidate_number": number, **media_snapshot(source_post, source_features)}, started=candidate_started)
                ensure_distinct_source(post, source_post, stage)
                if source_features.duration_seconds < max(
                    180.0, features.duration_seconds + 20.0
                ):
                    warnings.append(
                        "A repeated short excerpt was retained only as a source lead."
                    )
                    record("candidate_evaluation", "rejected", {**candidate_data, "reason": "recording_too_short_for_surrounding_context"}, started=candidate_started)
                    continue
                alignment = build_alignment(
                    features, source_features, candidate["match_score"]
                )
                if alignment.status != "verified" or alignment.match_quality.confidence.level not in ("high", "moderate"):
                    record("candidate_evaluation", "rejected", {**candidate_data, "reason": "timed_alignment_not_verified", "alignment": alignment.model_dump(mode="json")}, started=candidate_started)
                    continue
                context = build_context(alignment, source_features)
                if context is None:
                    record("candidate_evaluation", "rejected", {**candidate_data, "reason": "surrounding_context_unavailable", "alignment": alignment.model_dump(mode="json")}, started=candidate_started)
                    continue
                # A longer title alone cannot supply the missing surrounding words.
                match = alignment.matches[0]
                if not any(
                    sentence.end_seconds <= match.source_start_seconds
                    or sentence.start_seconds >= match.source_end_seconds
                    for window in context.windows
                    for sentence in window.sentences
                ):
                    record("candidate_evaluation", "rejected", {**candidate_data, "reason": "no_surrounding_words_outside_match", "alignment": alignment.model_dump(mode="json"), "context": context.model_dump(mode="json")}, started=candidate_started)
                    continue
                viable.append(
                    (source_post, source_features, alignment, context, candidate)
                )
                record("candidate_evaluation", "eligible", {**candidate_data, "alignment": alignment.model_dump(mode="json"), "context": context.model_dump(mode="json")}, started=candidate_started)
            except Exception:  # noqa: BLE001 - One unavailable candidate must not stop other candidates.
                record("candidate_evaluation", "rejected", {**candidate_data, "reason": "candidate_media_or_alignment_unavailable"}, started=candidate_started)
                warnings.append(
                    f"Source candidate {number} could not be aligned; other candidates were checked."
                )
            store.touch_job(job_id)
        if viable:
            chosen = viable[0]
            if len(viable) > 1:
                warnings.append(
                    "Multiple plausible recordings contain the excerpt. The strongest transcript match is shown."
                )
        chosen_url = chosen[0].post_url if chosen else None
        record("select_source", "completed", {"url": chosen_url, "selected": chosen is not None, "candidate_count": len(viable)})
        for candidate in discovered.get("leads", discovered.get("candidates", []))[:5]:
            candidate_url = candidate.get("video_url")
            if candidate_url and candidate_url != chosen_url:
                warnings.append(f"Unverified source candidate: {candidate_url}")
                warnings.append(
                    "Unverified source candidate details: "
                    + json.dumps(
                        {
                            "url": candidate_url,
                            "title": candidate.get("title"),
                            "reason": "A discovery lead; the longer recording has not passed transcript alignment.",
                        },
                        ensure_ascii=False,
                    )
                )
        context_asset, context_selection = None, None
        if chosen and os.getenv('CONTEXT_VIDEO_ENABLED', 'true').lower() == 'true':
            try:
                selected_context, context_selection = await asyncio.to_thread(context_video.select, chosen[2], chosen[1])
                chosen = (*chosen[:3], selected_context, chosen[4])
                record('sentence_context', 'completed', context_selection)
            except Exception:  # noqa: BLE001 - Optional context selection cannot discard the verified source.
                warnings.append('Sentence-based context selection was unavailable; the existing source context is shown.')
            if context_selection is not None:
                try:
                    store.touch_job(job_id)
                    context_asset = await context_video.export(chosen[0],chosen[2],context_selection)
                    record('context_video', 'completed', context_asset.model_dump(mode='json'))
                except Exception:  # noqa: BLE001 - Export failure retains the source recording and saved analysis.
                    warnings.append('The labeled context video could not be exported or stored; the original source remains available.')
                    record('context_video', 'unavailable', {'reason': 'Export or storage unavailable; original source retained.'})
        stage = Stage.COMPARE_CONTEXT_AND_CAPTION
        started = time.monotonic()
        comparison = compare_clip(post, features, chosen[3]) if chosen else None
        summary = comparison.summary if comparison else interpret_clip(post, features)
        record(stage.value, "completed", {"comparison": comparison.model_dump(mode="json") if comparison else None, "summary": summary}, started=started)
        stage = Stage.BUILD_REPORT
        started = time.monotonic()
        report = build_report(
            job_id,
            post,
            features,
            chosen[0] if chosen else None,
            chosen[1] if chosen else None,
            chosen[2] if chosen else None,
            chosen[3] if chosen else None,
            comparison,
            extra_limitations=warnings,
        )
        report.summary = summary
        report.context_video = context_asset
        report.context_selection = context_selection
        record(stage.value, "completed", {"report": report.model_dump(mode="json")}, started=started)
        return ReportV2.model_validate(report.model_dump(mode="json"))
    except CandidateSearchError:
        raise QuantroError(
            ErrorCode.SEARCH_FAILED, Stage.SEARCH_CANDIDATES, retryable=True
        ) from None
    except Exception as exc:  # noqa: BLE001 - Normalize all provider failures at the public boundary.
        raise normalize_exception(stage, exc) from None


def run_analysis(
    job_id: str,
    url: str,
    conversation_id: str | None = None,
    *,
    store: TigerStore | None = None,
    provider: MessagingProvider | None = None,
    tracker: DeliveryTracker | None = None,
    source_url: str | None = None,
) -> AnalysisResult:
    """Use the handover resolver as the public analysis path, without replaying jobs."""
    _validate(job_id, url)
    owned = store is None
    store = store or TigerStore.from_env()
    reserved = False
    capture = None
    try:
        if owned:
            store.ensure_schema()
        existing = store.get_job(job_id)
        if existing:
            if normalized_post_url(existing["url"]) != normalized_post_url(url):
                raise JobConflictError()
            if existing["status"] == "completed":
                saved = store.get_report(report_id_for_job(job_id))
                if saved is None:
                    raise QuantroError(
                        ErrorCode.STORAGE_FAILED, Stage.SAVE_REPORT_AND_COMPLETE_JOB
                    )
                return _deliver(
                    ReportV2.model_validate(saved),
                    conversation_id,
                    store,
                    provider,
                    tracker,
                )
            raise JobConflictError(
                "This analysis job already exists. Use a new job identifier to retry."
            )
        try:
            validate_search_settings(environment())
        except RuntimeError:
            logger.error(
                "Invalid SEARCH_PROVIDER configuration; ingest was not started."
            )
            raise QuantroError(
                ErrorCode.SEARCH_FAILED,
                Stage.SEARCH_CANDIDATES,
                "The source search is not configured correctly.",
            ) from None
        if not store.try_create_job(job_id, url):
            raise JobConflictError("This analysis is already running.")
        reserved = True
        capture = DiagnosticCapture(job_id, store)
        with capture.bind():
            record(Stage.RUN_ANALYSIS.value, "started", {"job_id": job_id, "url": url})
            report = asyncio.run(_build(job_id, url, store, source_url=source_url)) if source_url else asyncio.run(_build(job_id, url, store))
            started = time.monotonic()
            save_report_and_complete_job(
                job_id,
                report,
                report_store=TigerReportStore(store),
                job_store=TigerJobStore(store),
            )
            record(Stage.SAVE_REPORT_AND_COMPLETE_JOB.value, "completed", {"report_id": report_id_for_job(job_id), "outcome": report.outcome.value}, started=started)
        saved = store.get_report(report_id_for_job(job_id))
        if saved is None:
            raise QuantroError(
                ErrorCode.STORAGE_FAILED, Stage.SAVE_REPORT_AND_COMPLETE_JOB
            )
        result = _deliver(
            ReportV2.model_validate(saved), conversation_id, store, provider, tracker
        )
        capture.record(Stage.SEND_SUMMARY.value, result.delivery_status, {"delivery_status": result.delivery_status})
        capture.record(Stage.RUN_ANALYSIS.value, "completed", {"report_id": result.report_id, "outcome": result.outcome.value})
        return result
    except JobConflictError:
        raise
    except Exception as exc:  # noqa: BLE001 - Persist only safe normalized failure details.
        error = normalize_exception(Stage.RUN_ANALYSIS, exc)
        if capture is not None:
            capture.record(error.stage.value, "failed", error.public_dict())
        if reserved:
            _failed(store, job_id, error)
        raise error from None
    finally:
        if owned:
            store.close()
