"""Conservative source/context checks for bounded native YouTube speech windows.

This adapter does not manufacture downloaded media or a complete recording
duration. It verifies only distinctive text in independently requested windows;
recording edits, earliest upload and the unobserved content remain unassessed.
"""

from collections.abc import Callable
from typing import Any

from ..shared.errors import ErrorCode, QuantroError, Stage, normalize_exception
from ..shared.post_identity import post_identities, post_identity
from ..shared.types import (
    Alignment,
    Comparison,
    ConfidenceLevel,
    Context,
    ContextWindow,
    Outcome,
    ReportMedia,
    ReportV2,
)
from ..specialist.alignment import _align
from ..specialist.comparison import compare_transcript_windows
from .build_report import merge_limitations, sanitize_report
from .remote_youtube import (
    NO_DISCOVERY_LIMITATION,
    WINDOW_SECONDS,
    build_remote_youtube_report,
    canonical_youtube_url,
)

MAX_SOURCE_CANDIDATES = 2
CONTEXT_PADDING_SECONDS = 30.0
_UNASSESSED = {
    NO_DISCOVERY_LIMITATION,
    "Candidate recording downloads, alignment and source-context assessment were not performed in this fallback.",
    "Splicing, visual edits, speaker identity, deepfakes, caption meaning and surrounding context were not assessed.",
}
_WINDOW_LIMITS = [
    "Source matching compares distinctive exact speech only within independently requested, provider-estimated windows of up to 60 seconds; unobserved speech was not checked.",
    "The earliest or original upload was not established; an accepted source is a recording with matching observed speech.",
    "Recording duration and full-clip coverage remain unknown; transcript timestamps are estimates, not independently verified audio timing.",
    "Splicing, visual or audio edits, speaker identity and deepfakes were not assessed.",
    "At most two distinct public YouTube source candidates were examined; other recordings and later portions may contain the source.",
]


def _validate_window(media: ReportMedia) -> None:
    """Require observed sentence bounds, never interpolate missing timing."""
    transcript = media.transcript
    if transcript.status != "partial" or not transcript.sentences or not transcript.processed_ranges:
        raise ValueError("Native source evidence must contain partial observed speech")
    if media.post.media.duration_seconds is not None or media.frames or media.visual_text:
        raise ValueError("Native source evidence cannot assert recording duration or extracted frames")
    if len(transcript.sentences) > 150 or sum(len(s.text) for s in transcript.sentences) > 16_000:
        raise ValueError("Native source evidence exceeds its bounded window")
    ids = set()
    previous_end = 0.0
    for interval in transcript.processed_ranges:
        if not 0 <= interval.start_seconds < interval.end_seconds <= WINDOW_SECONDS:
            raise ValueError("Invalid observed interval")
    for sentence in transcript.sentences:
        if (not sentence.sentence_id.strip() or sentence.sentence_id in ids or not sentence.text.strip() or sentence.words
                or not 0 <= sentence.start_seconds < sentence.end_seconds <= WINDOW_SECONDS
                or sentence.start_seconds < previous_end):
            raise ValueError("Invalid observed speech")
        if not any(interval.start_seconds <= sentence.start_seconds < sentence.end_seconds <= interval.end_seconds
                   for interval in transcript.processed_ranges):
            raise ValueError("Speech is outside observed evidence")
        ids.add(sentence.sentence_id)
        previous_end = sentence.end_seconds


def _projection(media: ReportMedia) -> dict[str, Any]:
    return {"media_id": media.post.media.media_id,
            "transcript": media.transcript.model_dump(mode="json"),
            "warnings": ["Provider-estimated partial transcript windows were used; audio and timing were not independently verified."]}


def _context(source: ReportMedia, alignment: Alignment) -> Context:
    transcript = source.transcript
    observed_start = min(item.start_seconds for item in transcript.processed_ranges)
    observed_end = max(item.end_seconds for item in transcript.processed_ranges)
    spans: list[tuple[float, float, list[str]]] = []
    for match in sorted(alignment.matches, key=lambda item: (item.source_start_seconds, item.source_end_seconds)):
        start = max(observed_start, match.source_start_seconds - CONTEXT_PADDING_SECONDS)
        end = min(observed_end, match.source_end_seconds + CONTEXT_PADDING_SECONDS)
        if spans and start <= spans[-1][1]:
            prior_start, prior_end, prior_ids = spans[-1]
            spans[-1] = prior_start, max(prior_end, end), [*prior_ids, match.match_id]
        else:
            spans.append((start, end, [match.match_id]))
    return Context(source_media_id=source.post.media.media_id, windows=[
        ContextWindow(context_id=f"remote_context_{index:03d}", match_ids=ids,
                      requested_start_seconds=start, requested_end_seconds=end, coverage="partial",
                      sentences=[item.model_copy(deep=True) for item in transcript.sentences
                                 if item.start_seconds < end and item.end_seconds > start])
        for index, (start, end, ids) in enumerate(spans, 1)
    ], limitations=[
        "Context is restricted to observed speech within the requested 0–60-second source window, with up to 30 seconds of padding.",
        "Coverage is partial; absent surrounding words cannot establish that an edit removed context.",
        "Sentence text and timestamps are provider estimates; original sentence IDs and supplied bounds are preserved.",
    ])


def _not_assessed(caption: str | None) -> Comparison:
    return Comparison.model_validate({
        "context_assessment": "not_assessed", "caption_assessment": "not_provided" if caption is None else "not_assessed",
        "assessment_confidence": {"level": "not_assessed", "method": "partial_transcript_context_v1",
                                  "basis": [], "limitations": ["Semantic context interpretation was unavailable."]},
        "summary": "Matching observed speech was found, but semantic context interpretation was unavailable.",
        "findings": [], "limitations": ["Semantic context interpretation was unavailable."],
    })


def _safe_warning(candidate_url: str, exc: Exception) -> str:
    error = normalize_exception(Stage.RETRIEVE_CANDIDATE_MEDIA, exc)
    return f"Source candidate {candidate_url} could not be assessed ({error.code.value}); its unknown content was not treated as a mismatch."


def _clean_stage_limits(media: ReportMedia) -> None:
    media.post.retrieval.warnings = [item for item in media.post.retrieval.warnings if item not in _UNASSESSED]


def assess_remote_sources(
    report: ReportV2,
    candidates: list[dict[str, Any]],
    warnings: list[str],
    heartbeat: Callable[[], None],
) -> ReportV2:
    """Assess at most two unique public YouTube windows and one interpretation."""
    # aligned with shared/types.py on 2026-10-10
    result = ReportV2.model_validate(report.model_dump(mode="json"))
    try:
        _validate_window(result.submitted)
    except (TypeError, ValueError) as exc:
        raise QuantroError(ErrorCode.PROCESSING_FAILED, Stage.VERIFY_AND_ALIGN,
                          "Partial submitted speech failed source matching validation.") from exc
    seen = post_identities(result.submitted.post)
    viable: list[tuple[float, str, ReportMedia, Alignment]] = []
    attempted = 0
    for candidate in candidates:
        if attempted >= MAX_SOURCE_CANDIDATES:
            break
        try:
            url = canonical_youtube_url(candidate["url"])
        except (KeyError, TypeError, QuantroError):
            warnings.append("A source candidate was not a supported public YouTube recording and was not examined.")
            continue
        identity = post_identity(url)
        if identity in seen:
            continue
        seen.add(identity)
        attempted += 1
        heartbeat()  # Storage/liveness failures must propagate, not become candidate misses.
        try:
            source_report = build_remote_youtube_report(result.job_id, url)
            source = ReportMedia.model_validate(source_report.submitted.model_dump(mode="json"))
            if (canonical_youtube_url(source.post.post_url) != url
                    or post_identities(result.submitted.post) & post_identities(source.post)
                    or result.submitted.post.media.media_id == source.post.media.media_id):
                raise ValueError("Source evidence did not identify the distinct requested recording")
            _validate_window(source)
            alignment = Alignment.model_validate(_align(_projection(result.submitted), _projection(source), None))
            if alignment.status != "verified" or not alignment.matches:
                warnings.append(f"Source candidate {url} had no distinctive, uniquely located exact speech match in the observed window.")
                continue
            alignment.match_quality.confidence.level = ConfidenceLevel.MODERATE
            alignment.match_quality.clip_coverage_ratio = None
            alignment.edit_analysis.status = "not_assessed"
            alignment.edit_analysis.source_gaps = []
            alignment.edit_analysis.unmatched_clip_ranges = []
            alignment.edit_analysis.counts.source_gaps = None
            alignment.edit_analysis.counts.unmatched_clip_segments = None
            alignment.edit_analysis.counts.confirmed_added_segments = None
            alignment.edit_analysis.counts.confirmed_deleted_segments = None
            identifier = candidate.get("candidate_id")
            if isinstance(identifier, str) and identifier.strip() and len(identifier) <= 200:
                source.post.candidate_id = identifier
            title = candidate.get("title")
            if (source.post.title is None and isinstance(title, str) and title.strip()
                    and not any(item in title for item in ("/tmp/", "file://"))):
                source.post.title = title.strip()[:500]
                source.post.retrieval.warnings.append("The source title was supplied by discovery and was not independently verified.")
            _clean_stage_limits(source)
            viable.append((alignment.match_quality.matched_clip_seconds or 0.0, url, source, alignment))
        except Exception as exc:  # noqa: BLE001 - A candidate failure must preserve other candidates.
            warnings.append(_safe_warning(url, exc))
    result.limitations = [item for item in result.limitations if item not in _UNASSESSED]
    warnings[:] = [item for item in warnings if item not in _UNASSESSED]
    _clean_stage_limits(result.submitted)
    if not viable:
        result.summary = (f"Limited transcript analysis: {attempted} source candidate windows were examined; "
                          "no distinctive unique speech match was verified. Unavailable and unobserved content remains unknown.")
        result.limitations = merge_limitations(result.limitations, warnings, _WINDOW_LIMITS)
        return ReportV2.model_validate(sanitize_report(result.model_dump(mode="json")))
    viable.sort(key=lambda item: (-item[0], item[1]))
    matched_seconds, chosen_url, source, alignment = viable[0]
    if len(viable) > 1 and abs(matched_seconds - viable[1][0]) < 1e-6:
        warnings.append("Multiple plausible sources remain; equally supported transcript matches were ordered deterministically, not by originality.")
    context = _context(source, alignment)
    heartbeat()
    try:
        comparison = Comparison.model_validate(compare_transcript_windows(
            result.submitted.post.media.media_id, result.submitted.transcript, context, result.submitted.post.caption,
        ))
    except Exception as exc:  # noqa: BLE001 - Keep valid source evidence when semantic interpretation is unavailable.
        error = normalize_exception(Stage.COMPARE_CONTEXT_AND_CAPTION, exc)
        warnings.append(f"Semantic context interpretation was unavailable ({error.code.value}); the observed speech match was retained.")
        comparison = _not_assessed(result.submitted.post.caption)
    result.outcome = Outcome.MATCHED
    result.source = source
    result.alignment = alignment
    result.context = context
    result.comparison = comparison
    result.summary = ("A distinctive speech passage matches a candidate recording in the observed transcript windows; "
                      "the earliest original and complete context were not established. " + comparison.summary)
    chosen_note = f"Unverified source candidate: {chosen_url}"
    result.limitations = [item for item in result.limitations if item != chosen_note]
    warnings[:] = [item for item in warnings if item != chosen_note]
    result.submitted.post.retrieval.warnings = [
        item for item in result.submitted.post.retrieval.warnings if item != chosen_note
    ]
    result.limitations = merge_limitations(result.limitations, warnings, _WINDOW_LIMITS,
                                           alignment.limitations, context.limitations, comparison.limitations)
    return ReportV2.model_validate(sanitize_report(result.model_dump(mode="json")))
