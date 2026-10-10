"""run_analysis orchestrator for a single job.

Drives the pipeline: retrieve_post -> extract_media_features ->
build_search_queries -> search_candidates -> per candidate
(retrieve_candidate_media -> extract_media_features -> verify_and_align) ->
select_surrounding_context -> compare_context_and_caption -> build_report ->
save_report_and_complete_job -> send_summary. Messaging happens strictly after
the report is saved, and a messaging failure becomes a delivery warning rather
than undoing the save.
"""

import logging
from collections.abc import Sequence
from typing import Any, TypeVar

from .errors import QuantroError
from .send_summary import DeliveryTracker, MessagingProvider, send_summary
from .types import ConfidenceLevel, DeliveryResult

logger = logging.getLogger(__name__)

MULTIPLE_PLAUSIBLE_SOURCES = "Multiple plausible sources remain."

_LEVEL_RANK: dict[ConfidenceLevel, int] = {
    ConfidenceLevel.HIGH: 3,
    ConfidenceLevel.MODERATE: 2,
    ConfidenceLevel.LOW: 1,
    ConfidenceLevel.NOT_ASSESSED: 0,
}

T = TypeVar("T")


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

    def rank(entry: tuple[T, ConfidenceLevel | str, float | None]) -> tuple[int, float]:
        _, level, ratio = entry
        return _LEVEL_RANK[ConfidenceLevel(level)], ratio if ratio is not None else -1.0

    ordered = sorted(verified, key=rank, reverse=True)
    limitations = [MULTIPLE_PLAUSIBLE_SOURCES] if len(ordered) > 1 and rank(ordered[0]) == rank(ordered[1]) else []
    return ordered[0][0], limitations


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


def run_analysis(job_id: str) -> Any:
    raise NotImplementedError(
        "Needs the v2 contract signatures for the upstream stages (retrieve_post, "
        "extract_media_features, build_search_queries, search_candidates, "
        "retrieve_candidate_media, verify_and_align, select_surrounding_context, "
        "compare_context_and_caption) and for SavedReportRef"
    )
