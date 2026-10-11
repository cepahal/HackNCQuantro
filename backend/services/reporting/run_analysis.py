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

import json
import logging
import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

from ..external_apis.build_search_queries import (
    _STOP,
    _phrase,
    _title_clue,
    _with_clue,
    build_search_queries,
)
from ..external_apis.retrieve_candidate_media import retrieve_candidate_media
from ..external_apis.retrieve_post import RetrievalError, _validate_url
from ..external_apis.search_candidates import (
    _candidate_url,
    _web_search,
    check_search_settings,
    search_candidates,
    search_scope,
)
from ..external_apis.search_expansion import widen_queries
from ..pipeline.artifacts import artifact_scope
from ..pipeline.feature_cache import extraction_signature, media_sha256
from ..pipeline.run_ingest import EXTRACTION_OPTIONS, run_ingest
from ..processing.preprocessing import extract_media_features
from ..shared.contracts import validate_contract_result
from ..shared.errors import (
    ErrorCode,
    JobConflictError,
    QuantroError,
    Stage,
    normalize_exception,
)
from ..shared.errors import Stage as _ContractStage
from ..shared.errors import normalize_exception as _normalize_contract_exception
from ..shared.post_identity import ensure_distinct_source
from ..shared.post_identity import normalized_post_url as _normalized_post_url
from ..shared.post_identity import post_identities as _post_identities
from ..shared.post_identity import post_identity as _post_identity
from ..shared.types import (
    Alignment,
    AnalysisResult,
    Comparison,
    ConfidenceLevel,
    Context,
    DeliveryResult,
    JobCompletionResult,
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
from .build_report import build_report, sanitize_report
from .remote_source_analysis import _has_surrounding_speech, assess_remote_sources
from .remote_youtube import (
    NO_DISCOVERY_LIMITATION,
    build_remote_youtube_report,
    canonical_youtube_url,
)
from .save_report import report_id_for_job, save_report_and_complete_job
from .send_summary import (
    DeliveryTracker,
    MessagingProvider,
    format_context_summary,
    send_summary,
)
from .submitted_interpretation import interpret_submitted

logger = logging.getLogger(__name__)

SEARCH_LIMIT = 5
REMOTE_SEARCH_QUERY_LIMIT = 3
CONTEXT_OPTIONS = {"padding_seconds": 30.0, "merge_overlapping_windows": True}
MULTIPLE_PLAUSIBLE_SOURCES = "Multiple plausible sources remain."
NO_SOURCE_SUMMARY = "No verified source was found for this excerpt."

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
    return ordered[0][0], [MULTIPLE_PLAUSIBLE_SOURCES] if len(ordered) > 1 else []


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
        delivery = _call(
            Stage.SEND_SUMMARY, send_summary,
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
        error = normalize_exception(stage, exc)
        logger.warning("Pipeline failure code=%s stage=%s type=%s", error.code.value, error.stage.value, type(exc).__name__)
        raise error from exc
    finally:
        logger.info("analysis_stage stage=%s seconds=%.3f", stage.value, time.monotonic() - started)


def _candidate_label(candidate: dict[str, Any], index: int) -> str:
    candidate_id = candidate.get("candidate_id")
    return candidate_id if isinstance(candidate_id, str) and candidate_id else f"#{index}"


def _assess_candidate(candidate: dict[str, Any], clip_features: MediaFeatures, submitted_identities: set[str], *, store: TigerStore | None = None) -> _AssessedSource:
    options = {**EXTRACTION_OPTIONS, "extract_frames": False, "extract_visual_text": False, "max_frames": 0}
    signature = extraction_signature(options)
    cached = None
    cached_post = None
    cached_features = None
    cache_warning = None
    if store is not None:
        try:
            cached = store.get_cached_source(candidate["url"], signature)
            if cached:
                cached_post = parse_stage_output(Post, cached["post_json"], Stage.RETRIEVE_CANDIDATE_MEDIA)
                cached_features = parse_stage_output(MediaFeatures, cached["features_json"], Stage.EXTRACT_MEDIA_FEATURES)
                if cached_post.media.media_id != cached_features.media_id:
                    raise ValueError("Snapshot media identities do not agree.")
        except Exception as exc:  # noqa: BLE001 - cache is an optional optimization
            cached = None
            logger.warning("Candidate snapshot lookup failed (%s)", type(exc).__name__)
            cache_warning = "Source snapshot reuse was unavailable; fresh retrieval was attempted."
    if cached and cached_post is not None:
        post = cached_post.model_copy(update={"candidate_id": candidate["candidate_id"]}, deep=True)
    else:
        raw_post = _call(Stage.RETRIEVE_CANDIDATE_MEDIA, retrieve_candidate_media, candidate)
        post = parse_stage_output(Post, raw_post, Stage.RETRIEVE_CANDIDATE_MEDIA)
    if _post_identities(post) & submitted_identities:
        raise QuantroError(
            ErrorCode.PROCESSING_FAILED, Stage.RETRIEVE_CANDIDATE_MEDIA,
            "The submitted post cannot be its own source.",
        )
    if cached and cached_features is not None:
        features = cached_features
    else:
        raw_features = _call(Stage.EXTRACT_MEDIA_FEATURES, extract_media_features, post.media, options)
        features = parse_stage_output(MediaFeatures, raw_features, Stage.EXTRACT_MEDIA_FEATURES)
    features = features.model_copy(deep=True)
    if cached:
        features.warnings.append(
            f"Reused a prior recording transcript snapshot retrieved at {cached['retrieved_at']}; current URL bytes were not re-downloaded."
        )
    elif store is not None and Path(post.media.local_path).is_file():
        try:
            store.cache_source_features(post.model_dump(mode="json"), features.model_dump(mode="json"),
                                        signature, media_sha256(post.media.local_path))
        except Exception as exc:  # noqa: BLE001 - preserve valid fresh evidence on optional cache failure
            logger.warning("Candidate snapshot save failed (%s)", type(exc).__name__)
            cache_warning = "Source features could not be cached for reuse."
    if cache_warning:
        features.warnings.append(cache_warning)
    features.warnings.append("Candidate visual extraction was skipped; source verification uses transcript alignment only.")
    raw_alignment = _call(
        Stage.VERIFY_AND_ALIGN,
        verify_and_align,
        clip_features.model_dump(mode="json"),
        features.model_dump(mode="json"),
    )
    alignment = parse_stage_output(Alignment, raw_alignment, Stage.VERIFY_AND_ALIGN)
    return _AssessedSource(post=post, features=features, alignment=alignment)


def _heartbeat(store: TigerStore, job_id: str) -> None:
    try:
        store.touch_job(job_id)
    except Exception as exc:
        raise QuantroError(ErrorCode.STORAGE_FAILED, Stage.RUN_ANALYSIS, retryable=True) from exc


def _mark_failed(store: TigerStore, job_id: str, error: QuantroError) -> None:
    try:
        store.set_job_status(job_id, "failed", error.code.value, error.stage.value, str(error.public_dict()["message"]), error.retryable)
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
        _validate_url(url)
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
        if existing_job and _normalized_post_url(existing_job["url"]) != _normalized_post_url(url):
            raise JobConflictError()
        if existing_job and existing_job["status"] == "completed":
            if existing_report is None:
                raise QuantroError(ErrorCode.STORAGE_FAILED, Stage.SAVE_REPORT_AND_COMPLETE_JOB)
            report = parse_stage_output(ReportV2, existing_report, Stage.BUILD_REPORT)
            ensure_distinct_source(report.submitted.post, report.source.post if report.source else None, Stage.BUILD_REPORT)
            return validate_contract_result(_ReturnModel, _complete_and_deliver(job_id, report, conversation_id, store, provider, tracker, [], interpret=False), _ContractStage.RUN_ANALYSIS, json_native=False)

        check_search_settings()
        try:
            _call(Stage.RETRIEVE_POST, run_ingest, job_id, url, store=store, _defer_platform_challenge=True)
        except QuantroError as error:
            # Native provider access is separate evidence, never a fake media download.
            if not (
                isinstance(error, RetrievalError)
                and error.code == ErrorCode.MEDIA_UNAVAILABLE
                and error.reason == "platform_challenge"
                and _validate_url(url)[1] == "youtube"
            ):
                raise
            try:
                _call(Stage.RUN_ANALYSIS, store.set_job_status, job_id, "running")
                report = _call(Stage.EXTRACT_MEDIA_FEATURES, build_remote_youtube_report, job_id, url)
                report = _discover_remote_sources(report, store, assess_candidates=True)
                return validate_contract_result(
                    _ReturnModel,
                    _complete_and_deliver(job_id, report, conversation_id, store, provider, tracker, list(report.limitations)),
                    _ContractStage.RUN_ANALYSIS, json_native=False,
                )
            except QuantroError as fallback_error:
                _mark_failed(store, job_id, fallback_error)
                raise
        try:
            ingested = store.get_job(job_id)
            if ingested is None:
                raise LookupError("Ingest job is missing.")
        except Exception as exc:
            raise QuantroError(ErrorCode.STORAGE_FAILED, Stage.EXTRACT_MEDIA_FEATURES, retryable=True) from exc
        try:
            post = parse_stage_output(Post, ingested["post_json"], Stage.RETRIEVE_POST)
            features = parse_stage_output(MediaFeatures, ingested["features_json"], Stage.EXTRACT_MEDIA_FEATURES)
            return validate_contract_result(_ReturnModel, _analyze(job_id, post, features, conversation_id, store, provider, tracker), _ContractStage.RUN_ANALYSIS, json_native=False)
        except QuantroError as error:
            _mark_failed(store, job_id, error)
            raise
    except QuantroError:
        raise
    except Exception as exc:
        raise _normalize_contract_exception(_ContractStage.RUN_ANALYSIS, exc) from exc


def _discover_sources(queries: _SearchQueries, store: TigerStore, excluded: set[str],
                      warnings: list[str], limit: int,
                      exclusion_warning: str = "The submitted post was excluded from its own source candidates.") -> list[dict[str, Any]]:
    started = time.monotonic()
    try:
        with search_scope(store, excluded):
            raw_search = search_candidates(queries, limit=limit)
    except Exception as exc:
        raise QuantroError(
            code=ErrorCode.SEARCH_FAILED, stage=Stage.SEARCH_CANDIDATES, retryable=True
        ) from exc
    finally:
        logger.info("analysis_stage stage=%s seconds=%.3f", Stage.SEARCH_CANDIDATES.value, time.monotonic() - started)
    search = parse_stage_output(_CandidateSearch, raw_search, Stage.SEARCH_CANDIDATES)
    warnings.extend(warning for warning in search.warnings if warning not in warnings)
    rows = [candidate.model_dump(mode="json") for candidate in search.candidates]
    eligible = [row for row in rows if _post_identity(row["url"]) not in excluded]
    if len(eligible) < len(rows):
        warnings.append(exclusion_warning)
    return eligible[:limit]


def _remote_metadata_clue(title: str | None, caption: str | None) -> str:
    """Bound unverified entity/event/activity hints without inferring new facts."""
    # Preserve scene/event clues at the end of long titles rather than using
    # only the first six words. Metadata remains an unverified locator.
    title_parts = [_title_clue(' '.join((title or '').split()[offset:offset + 6]))
                   for offset in range(0, min(len((title or '').split()), 36), 6)]
    title_words = list(dict.fromkeys(word for part in title_parts if part for word in part.split()
                                    if word.casefold() not in _REMOTE_ANCHOR_NOISE))[:14]
    caption_clue = _title_clue(caption)
    caption_words = [word for word in (caption_clue or '').split()
                     if word.casefold() not in {item.casefold() for item in title_words}][:4]
    return ' '.join([*title_words, *caption_words])[:240]


_REMOTE_GENERIC_WORDS = _STOP | {
    'so', 'can', 'have', 'am', 'by', 'who', 'some', 'most', 'here', 'there',
    'very', 'much', 'going', 'take', 'care', 'now', 'today', 'basically',
    'been', 'has', 'will', 'would', 'could', 'should', 'our', 'your', 'their',
    'from', 'with', 'about', 'also', 'get', 'right', 'm', 're', 've',
}

_REMOTE_ANCHOR_NOISE = _REMOTE_GENERIC_WORDS | {
    'former', 'current', 'president', 'presidential', 'minister', 'prime', 'senator',
    'governor', 'representative', 'congressman', 'congresswoman', 'u', 's', 'us',
    'usa', 'united', 'states', 'breaking', 'news', 'latest', 'watch', 'new', 'why',
    'how', 'what', 'full', 'complete', 'entire', 'official', 'original', 'video',
    'speech', 'remarks', 'interview', 'debate', 'clip', 'shorts', 'viral', 'visit',
    'visits', 'order', 'orders', 'ordering', 'stops', 'speaks', 'talks', 'takes', 'unverified',
}


def _remote_distinctive_words(text: str) -> set[str]:
    return set(re.findall(r'\w+', text.casefold())) - _REMOTE_GENERIC_WORDS


def _remote_metadata_anchors(title: str | None, caption: str | None) -> set[str]:
    """Leading proper-name clues stay separate from an activity's venue/topic.

    This bounded heuristic locates metadata, not a verified person. For a title
    like "Person Name visits Place", a shared Place alone is insufficient.
    """
    for value in (title, caption):
        if not value:
            continue
        prefix = re.split(r"[:|]|\b(?:orders?|ordering|buys?|buying|visits?|stops?|asks?|"
                          r"speaks?|talks?|says?|said|discusses?|responds?|takes?|meets?)\b",
                          value[:1_000], maxsplit=1, flags=re.IGNORECASE)[0]
        anchors = {word.casefold() for word in re.findall(r'[^\W\d_]+', prefix)
                   if len(word) >= 3 and word[0].isupper() and word.casefold() not in _REMOTE_ANCHOR_NOISE}
        if anchors:
            return anchors
    return set()


def _remote_candidate_relevant(candidate: dict[str, Any], title: str | None,
                               caption: str | None, quote: str | None) -> bool:
    """Topic-only overlap cannot link a candidate to a metadata-described event."""
    anchors = _remote_metadata_anchors(title, caption)
    words = _remote_distinctive_words(' '.join(value for key in ('title', 'snippet')
                                     if isinstance(value := candidate.get(key), str))[:1_000])
    if not anchors or not words:
        return True  # Missing metadata cannot establish a conflict.
    spoken = _remote_distinctive_words(quote or '')
    return bool(words & anchors) or len(words & spoken) >= 3


def _remote_context_quote(queries: list[str], title: str | None, caption: str | None) -> str | None:
    """Rank unchanged speech by bounded numeric and metadata-overlap clues."""
    if not queries:
        return None
    metadata_words = _remote_distinctive_words(_remote_metadata_clue(title, caption))

    def priority(text: str) -> tuple[int, int, int]:
        words = _remote_distinctive_words(text)
        clue_score = 3 * len(words & metadata_words) + 4 * sum(any(c.isdigit() for c in word) for word in words)
        return clue_score, len(words), len(text)

    searchable = [query for query in queries if _remote_distinctive_words(query)]
    return max(searchable, key=priority) if searchable else None


def _remote_context_query(queries: list[str], title: str | None, caption: str | None = None) -> str | None:
    """Use event clues when available, retaining exact speech in primary search."""
    if not queries:
        return None
    metadata = _remote_metadata_clue(title, caption)
    if metadata and _remote_metadata_anchors(title, caption):
        return metadata + ' site:youtube.com/watch'
    quote = _remote_context_quote(queries, title, caption)
    if quote is None:
        return None
    base = _with_clue(metadata, quote) or quote
    site = ' site:youtube.com/watch'
    if len(base + site) <= 512:
        return base + site
    # Preserve an unusually long complete quote rather than truncating evidence.
    return quote + site


def _remote_context_priority(candidate: dict[str, Any]) -> tuple[int, float]:
    """Unverified discovery hints prioritize inspection; they never verify context."""
    metadata = ' '.join(value for key in ('title', 'snippet')
                        if isinstance(value := candidate.get(key), str))[:1_000].casefold()
    duration: float | None = None
    clock = re.search(r'\b(?:duration|length)\s*[:=]\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\b', metadata)
    units = re.search(r'\b(?:duration|length)\s*[:=]\s*(\d{1,5}(?:\.\d{1,2})?)\s*(hours?|hrs?|minutes?|mins?|seconds?|secs?|s)\b', metadata)
    if clock and int(clock[2]) < 60 and int(clock[3]) < 60:
        duration = int(clock[1] or 0) * 3_600 + int(clock[2]) * 60 + int(clock[3])
    elif units:
        unit = units[2]
        duration = float(units[1]) * (3_600 if unit.startswith(('hour', 'hr')) else 60 if unit.startswith('min') else 1)
    if duration is not None and not 0 < duration <= 86_400:
        duration = None
    fuller = bool(re.search(r'\b(?:full|complete|entire|unedited|unabridged|extended)\s+(?:speech|remarks|address|interview|press conference|recording|debate|event|video|visit|footage|context)\b|\boriginal\s+footage\b', metadata))
    short = bool(re.search(r'\b(?:shorts?|clips?|highlights?|excerpt|compilation|meme|remix)\b', metadata))
    short = short or '/shorts/' in str(candidate.get('url', '')).casefold()
    priority = 0 if fuller or duration is not None and duration > 90 else 2 if short or duration is not None and duration <= 60 else 1
    return priority, -(duration or 0)


def _remote_relevance_priority(candidate: dict[str, Any], title: str | None,
                               caption: str | None, quote: str | None) -> tuple[int, int, int, int, float]:
    """Text relevance orders unverified leads; it is not match confidence."""
    words = _remote_distinctive_words(' '.join(value for key in ('title', 'snippet')
                                      if isinstance(value := candidate.get(key), str))[:1_000])
    metadata = _remote_distinctive_words(_remote_metadata_clue(title, caption))
    spoken = _remote_distinctive_words(quote or '')
    anchors = _remote_metadata_anchors(title, caption)
    anchor_hits = len(words & anchors)
    # A shared topic word occurs once, even when both title and speech use it.
    overlap = len(words & (metadata | spoken))
    fuller, duration = _remote_context_priority(candidate)
    relevance = 0 if anchor_hits else 1 if len(words & spoken) >= 3 else 2
    if anchors:
        # A long recording about the same person is weaker than a lead whose
        # metadata also connects the actual event, activity and spoken wording.
        return relevance, -anchor_hits, -overlap, fuller, duration
    return relevance, fuller, -anchor_hits, -overlap, duration


def _discover_remote_sources(report: ReportV2, store: TigerStore, *, assess_candidates: bool = False) -> ReportV2:
    """Discover leads from partial text without inventing timed source evidence."""
    post = report.submitted.post
    warnings: list[str] = []
    raw_queries = _call(Stage.BUILD_SEARCH_QUERIES, build_search_queries, {
        "media_id": post.media.media_id,
        "transcript": report.submitted.transcript.model_dump(mode="json"),
        "visual_text": [],
    }, post.model_dump(mode="json"))
    queries = parse_stage_output(_SearchQueries, raw_queries, Stage.BUILD_SEARCH_QUERIES)
    warnings.extend(queries.warnings)
    # Search the actual speech. The public query builder also appends unverified
    # titles/captions, so its last query is not necessarily a spoken payoff.
    parts = [part for sentence in report.submitted.transcript.sentences[:500]
             for part in re.split(r"[.!?]+", sentence.text)]
    clues = list(dict.fromkeys(phrase for part in parts if (phrase := _phrase(part, minimum=4))))
    # Short observed numeric phrases can locate an event better than a long
    # generic greeting. Keep their actual words; never generate a substitute.
    numeric_clues = list(dict.fromkeys(phrase for part in parts
                         if assess_candidates and re.search(r'\d', part)
                         and (phrase := _phrase(part, minimum=2))))
    distinctive = _remote_context_quote([*clues, *numeric_clues], post.title, post.caption) if assess_candidates else None
    if assess_candidates:
        available = list(dict.fromkeys([*clues, *numeric_clues]))
        available = [clue for clue in available if _remote_distinctive_words(clue)]
        # The first query drives the recording-host request and provider round-robin.
        # Spend that slot on actual distinctive speech, never a generic greeting.
        clues = ([distinctive] if distinctive else []) + [clue for clue in available if clue != distinctive]
        clues = clues[:REMOTE_SEARCH_QUERY_LIMIT]
    elif len(clues) > 1:
        # A short clip's distinctive payoff can follow a generic opening.
        # Cover its end, beginning and middle instead of dropping the last quote.
        clues = list(dict.fromkeys(clues[index] for index in (len(clues) - 1, 0, len(clues) // 2)))[:REMOTE_SEARCH_QUERY_LIMIT]
    if distinctive and distinctive not in clues:
        clues = [*clues[:REMOTE_SEARCH_QUERY_LIMIT - 1], distinctive]
    queries = queries.model_copy(update={"queries": clues})
    candidates: list[dict[str, Any]] = []
    if queries.queries:
        _heartbeat(store, report.job_id)
        primary_queries = queries
        if assess_candidates:
            spoken_quote = _remote_context_quote(queries.queries, post.title, post.caption)
            metadata = _remote_metadata_clue(post.title, post.caption)
            locator = _with_clue(metadata, spoken_quote)
            if locator and locator != spoken_quote:
                # The recording-host request uses the first query. Carry event
                # clues there, preserve actual speech separately, and reserve a
                # metadata-only query for ASR wording that web pages do not quote.
                located = [item for item in (locator, spoken_quote, metadata) if item]
                primary_queries = queries.model_copy(update={'queries': list(dict.fromkeys(located))[:REMOTE_SEARCH_QUERY_LIMIT]})
        candidates = _discover_sources(primary_queries, store, _post_identities(post), warnings,
                                      SEARCH_LIMIT if assess_candidates else 3)
        fallback_provider = os.getenv('SEARCH_FALLBACK_PROVIDER', '').strip().lower()
        if fallback_provider in {'serpapi', 'brave', 'google_cse'}:
            # A lexical engine complements Exa's semantic web index. This uses
            # an explicitly configured existing credential, never an inferred key.
            exact_query = (_remote_context_query(queries.queries, post.title, post.caption) if assess_candidates
                           else _with_clue(_title_clue(post.title), queries.queries[0]))
            if exact_query:
                settings = dict(os.environ)
                settings['SEARCH_PROVIDER'] = fallback_provider
                settings['SEARCH_PROVIDER_API_KEY'] = (os.getenv('SEARCH_FALLBACK_API_KEY', '')
                                                        or os.getenv('SEARCH_PROVIDER_API_KEY', ''))
                try:
                    exact = _call(Stage.SEARCH_CANDIDATES, _web_search,
                                  [exact_query if assess_candidates else exact_query + ' site:youtube.com/watch'], 5, [], settings)
                    if not isinstance(exact, dict) or not isinstance(exact.get('candidates'), list) or not isinstance(exact.get('warnings'), list):
                        raise QuantroError(ErrorCode.SEARCH_FAILED, Stage.SEARCH_CANDIDATES)
                    parsed = parse_stage_output(_CandidateSearch, exact, Stage.SEARCH_CANDIDATES)
                    candidates = [*[item.model_dump(mode='json') for item in parsed.candidates], *candidates]
                    warnings.extend(parsed.warnings)
                    warnings.append('A bounded lexical YouTube search supplemented semantic discovery; all returned links still require speech alignment.')
                except QuantroError:
                    warnings.append('The optional lexical search was unavailable; semantic search coverage remains limited.')
    else:
        warnings.append("Source discovery was skipped because the partial transcript contains no distinctive searchable phrase.")
    if assess_candidates:
        quote = _remote_context_quote(queries.queries, post.title, post.caption)
        relevant = [row for row in candidates if _remote_candidate_relevant(row, post.title, post.caption, quote)]
        if len(relevant) < len(candidates):
            warnings.append('Topic-only source candidates lacking an event or distinctive speech connection were excluded; metadata clues remain unverified and this relevance filter may miss differently labeled recordings.')
        candidates = relevant
        candidates.sort(key=lambda row: _remote_relevance_priority(row, post.title, post.caption, quote))
    seen = _post_identities(post)
    eligible: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            url = _candidate_url(candidate["url"])
        except (KeyError, TypeError, ValueError):
            warnings.append("An unsafe source-discovery URL was excluded.")
            continue
        if assess_candidates:
            try:
                canonical = canonical_youtube_url(url)
                try:
                    from .remote_youtube import youtube_window_start
                    offset = youtube_window_start(url)
                except ValueError:
                    offset = 0
                    warnings.append('A discovery timestamp was invalid; the source offset remains unknown and was not guessed.')
                url = canonical + (f'&t={offset}' if offset else '')
            except QuantroError:
                warnings.append("A source candidate outside the supported YouTube recording scope was excluded from remote assessment.")
                continue
        identity = _post_identity(url)
        if identity in seen:
            continue
        seen.add(identity)
        eligible.append(dict(candidate, url=url))
        if not assess_candidates and len(eligible) == 3:
            break
    if assess_candidates:
        # Inspect likely broader recordings before repeated short copies. Neither
        # metadata nor declared duration can satisfy the independent speech gate.
        if eligible:
            warnings.append('Source candidates were prioritized using unverified fuller-recording metadata; titles and duration hints do not establish additional context or originality.')
        if post.title or post.caption:
            warnings.append('The submitted title and caption are unverified search clues, not established speaker identity, events or facts.')
        # Display more weaker leads without increasing the independent native
        # assessment budget (at most two recordings in assess_remote_sources).
        eligible = eligible[:SEARCH_LIMIT]
    urls = [candidate['url'] for candidate in eligible]
    result = report.model_copy(deep=True)
    result.limitations = [item for item in result.limitations if item != NO_DISCOVERY_LIMITATION]
    result.submitted.post.retrieval.warnings = [
        item for item in result.submitted.post.retrieval.warnings if item != NO_DISCOVERY_LIMITATION
    ]
    if queries.queries:
        result.summary = (
            f"Limited transcript-only result: source discovery returned {len(urls)} unverified candidates. "
            "Recording alignment and context analysis were not performed; no original source was verified."
        )
        warnings.append("Source discovery used partial, provider-estimated text; candidate links are leads, not verified originals.")
    warnings.append("Candidate recording downloads, alignment and source-context assessment were not performed in this fallback.")
    warnings.extend(f"Unverified source candidate: {url}" for url in urls)
    for candidate in eligible:
        try:
            safe_url = canonical_youtube_url(candidate['url'])
        except QuantroError:
            continue
        title = candidate.get('title')
        title = ' '.join(title[:1_000].split())[:240] or None if isinstance(title, str) else None
        if title and any(marker in title for marker in ('/tmp/', 'file://')):
            title = None
        warnings.append('Unverified source candidate details: ' + json.dumps({
            'url': safe_url, 'title': title,
            'reason': 'Discovery metadata may relate to this clip; speech and broader context are not verified.',
        }, ensure_ascii=False))
    result.limitations.extend(item for item in warnings if item not in result.limitations)
    result.submitted.post.retrieval.warnings.extend(
        item for item in warnings if item not in result.submitted.post.retrieval.warnings
    )
    if assess_candidates:
        result = assess_remote_sources(result, eligible, warnings, lambda: _heartbeat(store, report.job_id))
    return ReportV2.model_validate(sanitize_report(result.model_dump(mode="json")))


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

    _heartbeat(store, job_id)
    raw_queries = _call(
        Stage.BUILD_SEARCH_QUERIES,
        build_search_queries,
        submitted_features.model_dump(mode="json"),
        submitted_post.model_dump(mode="json"),
    )
    queries = parse_stage_output(_SearchQueries, raw_queries, Stage.BUILD_SEARCH_QUERIES)
    warnings.extend(queries.warnings)

    submitted_identities = _post_identities(submitted_post)
    candidates = _discover_sources(queries, store, submitted_identities, warnings, SEARCH_LIMIT)

    viable: list[_AssessedSource] = []
    skipped = 0
    attempted_identities = set(submitted_identities)

    def assess_batch(batch: list[dict[str, Any]]) -> None:
        nonlocal skipped
        for index, candidate in enumerate(batch, start=1):
            identity = _post_identity(candidate["url"])
            if identity in attempted_identities:
                warnings.append("An alias of a previously assessed recording was skipped.")
                continue
            attempted_identities.add(identity)
            _heartbeat(store, job_id)
            try:
                assessed = _assess_candidate(candidate, submitted_features, submitted_identities, store=store)
            except QuantroError as error:
                skipped += 1
                warnings.append(f"Candidate {_candidate_label(candidate, index)} was skipped: {error.message}")
                continue
            attempted_identities.update(_post_identities(assessed.post))
            confidence = assessed.alignment.match_quality.confidence
            if assessed.alignment.status == "verified" and confidence.level in _VIABLE_LEVELS:
                if not _call(Stage.SELECT_SURROUNDING_CONTEXT, _has_surrounding_speech,
                             submitted_features.transcript, assessed.features.transcript, assessed.alignment):
                    warnings.append(f"Candidate {_candidate_label(candidate, index)} matched submitted speech but supplied no additional observed surrounding speech; it remains an unverified lead rather than a context source.")
                    continue
                viable.append(assessed)

    assess_batch(candidates)
    if not viable and os.getenv("SEARCH_PROVIDER", "").strip().lower() == "hybrid":
        expanded = widen_queries(queries)
        if expanded.queries:
            warnings.extend(warning for warning in expanded.warnings if warning not in warnings)
            more = _discover_sources(expanded, store, attempted_identities, warnings, 2,
                                     "Previously assessed source candidates were excluded from the expanded search.")
            candidates.extend(more)
            assess_batch(more)

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
    if tie_limitations and chosen is not None:
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
        _heartbeat(store, job_id)
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
    *,
    interpret: bool = True,
) -> AnalysisResult:
    if interpret and os.getenv('SUBMITTED_INTERPRETATION_ENABLED', '').strip().lower() in {'1', 'true', 'yes'}:
        _heartbeat(store, job_id)
        report = interpret_submitted(report)
    saved = _call(
        Stage.SAVE_REPORT_AND_COMPLETE_JOB, save_report_and_complete_job,
        job_id, report, report_store=TigerReportStore(store), job_store=TigerJobStore(store)
    )
    saved = parse_stage_output(JobCompletionResult, saved, Stage.SAVE_REPORT_AND_COMPLETE_JOB)
    try:
        saved_report = store.get_report(saved.report_id)
    except Exception as exc:
        raise QuantroError(ErrorCode.STORAGE_FAILED, Stage.SAVE_REPORT_AND_COMPLETE_JOB, retryable=True) from exc
    if saved_report is None:
        raise QuantroError(ErrorCode.STORAGE_FAILED, Stage.SAVE_REPORT_AND_COMPLETE_JOB)
    report = parse_stage_output(ReportV2, saved_report, Stage.SAVE_REPORT_AND_COMPLETE_JOB)
    if report.job_id != job_id:
        raise QuantroError(ErrorCode.STORAGE_FAILED, Stage.SAVE_REPORT_AND_COMPLETE_JOB)
    ensure_distinct_source(report.submitted.post, report.source.post if report.source else None, Stage.SAVE_REPORT_AND_COMPLETE_JOB)

    delivery, delivery_warning = deliver_summary_after_save(
        job_id,
        conversation_id,
        format_context_summary(report),
        saved.report_url,
        provider=provider,
        tracker=tracker,
    )
    delivery_status: Literal["accepted", "failed", "skipped"]
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
        warnings=warnings + ([delivery_warning] if delivery_warning else []),
    )
