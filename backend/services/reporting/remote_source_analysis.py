"""Conservative source/context checks for bounded native YouTube speech windows.

This adapter does not manufacture downloaded media or a complete recording
duration. It verifies only distinctive text in independently requested windows;
recording edits, earliest upload and the unobserved content remain unassessed.
"""

import re
from collections.abc import Callable
from typing import Any

from ..external_apis.youtube_transcript import TimedCaptionCue, fetch_youtube_transcript
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
    Transcript,
)
from ..specialist.alignment import _STOP, _align, _distinctive
from ..specialist.comparison import compare_transcript_windows
from .build_report import merge_limitations, sanitize_report
from .remote_youtube import (
    NO_DISCOVERY_LIMITATION,
    WINDOW_SECONDS,
    build_remote_youtube_report,
    canonical_youtube_url,
    youtube_window_start,
)

MAX_SOURCE_CANDIDATES = 2
CONTEXT_PADDING_SECONDS = 30.0
_CONTEXT_NOISE = (_STOP - {"not", "no"}) | {
    "thank", "thanks", "everyone", "watching", "please", "subscribe", "channel", "like", "share", "welcome", "hello",
    "morning", "evening", "afternoon", "ladies", "gentlemen", "very", "much", "today", "video",
}
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


def _validate_window(media: ReportMedia, window_start_seconds: int = 0) -> None:
    """Require observed sentence bounds, never interpolate missing timing."""
    transcript = media.transcript
    if transcript.status != "partial" or not transcript.sentences or not transcript.processed_ranges:
        raise ValueError("Native source evidence must contain partial observed speech")
    if media.post.media.duration_seconds is not None or media.frames or media.visual_text:
        raise ValueError("Native source evidence cannot assert recording duration or extracted frames")
    if len(transcript.sentences) > 150 or sum(len(s.text) for s in transcript.sentences) > 16_000:
        raise ValueError("Native source evidence exceeds its bounded window")
    ids = set()
    previous_end = float(window_start_seconds)
    window_end = window_start_seconds + WINDOW_SECONDS
    for interval in transcript.processed_ranges:
        if not window_start_seconds <= interval.start_seconds < interval.end_seconds <= window_end:
            raise ValueError("Invalid observed interval")
    for sentence in transcript.sentences:
        if (not sentence.sentence_id.strip() or sentence.sentence_id in ids or not sentence.text.strip() or sentence.words
                or not window_start_seconds <= sentence.start_seconds < sentence.end_seconds <= window_end
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
        "Context is restricted to observed speech within the requested source window, with up to 30 seconds of padding.",
        "Coverage is partial; absent surrounding words cannot establish that an edit removed context.",
        "Sentence text and timestamps are provider estimates; original sentence IDs and supplied bounds are preserved.",
    ])


def _has_surrounding_speech(submitted: Transcript, source: Transcript, alignment: Alignment) -> bool:
    """Require substantive new speech in validated transcripts, not a copied clip.

    Four words and two new informative words are conservative eligibility
    heuristics, not a semantic verdict or evidence of an original uploader.
    Negation remains informative so short corrective qualifiers can qualify.
    """
    submitted_words = set(re.findall(r"\w+(?:['’]\w+)?", " ".join(
        sentence.text for sentence in submitted.sentences
    ).casefold()))
    matched_ids = {reference for match in alignment.matches for evidence in match.evidence
                   for reference in evidence.source_refs}
    for sentence in source.sentences:
        if sentence.sentence_id in matched_ids or any(
            sentence.start_seconds < match.source_end_seconds
            and sentence.end_seconds > match.source_start_seconds
            for match in alignment.matches
        ):
            continue
        if not any(sentence.start_seconds < match.source_end_seconds + CONTEXT_PADDING_SECONDS
                   and sentence.end_seconds > max(0.0, match.source_start_seconds - CONTEXT_PADDING_SECONDS)
                   for match in alignment.matches):
            continue
        words = re.findall(r"\w+(?:['’]\w+)?", sentence.text.casefold())
        if any(phrase in " ".join(words) for phrase in (
            "thanks for watching", "thank you for watching", "please subscribe",
            "like and subscribe", "like and share", "subscribe to",
        )):
            continue
        novel = set(words) - submitted_words - _CONTEXT_NOISE
        if len(words) >= 4 and len(novel) >= 2:
            return True
    return False


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


def _caption_window_start(transcript: Transcript, cues: list[TimedCaptionCue]) -> int | None:
    """Locate unique speech across cue boundaries without treating captions as proof.

    Eight-word fingerprints keep the scan linear in caption length. Caption cue
    starts locate a 60-second native-video request; no cue endings are invented.
    Repeated fingerprints cannot choose a timestamp. Native speech must still
    pass the independent alignment and surrounding-context checks below.
    """
    def tokenize(text: str) -> list[str]:
        return re.findall(r"\w+(?:['’]\w+)?", text.casefold())
    spoken = tokenize(' '.join(sentence.text for sentence in transcript.sentences))[:1_000]
    tokens: list[str] = []
    starts: list[float] = []
    for cue in cues:
        words = tokenize(cue.text)
        if len(tokens) + len(words) > 40_000:
            break
        tokens.extend(words)
        starts.extend([cue.start_seconds] * len(words))
    size = 8
    positions: dict[tuple[str, ...], int | None] = {}
    for index in range(len(tokens) - size + 1):
        fingerprint = tuple(tokens[index:index + size])
        positions[fingerprint] = None if fingerprint in positions else index
    located: dict[tuple[str, ...], float] = {}
    for index in range(len(spoken) - size + 1):
        fingerprint = tuple(spoken[index:index + size])
        position = positions.get(fingerprint)
        if position is not None and _distinctive(fingerprint):
            located[fingerprint] = starts[position]
    if not located:
        return None
    times = sorted(located.values())
    left = 0
    best_count, best_start = 0, 0.0
    for right, timestamp in enumerate(times):
        while timestamp - times[left] > 30.0:
            left += 1
        if right - left + 1 > best_count:
            best_count, best_start = right - left + 1, times[left]
    return min(86_400, max(0, int(best_start) - 15))


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
        _validate_window(result.submitted, youtube_window_start(result.submitted.post.post_url))
    except (TypeError, ValueError) as exc:
        raise QuantroError(ErrorCode.PROCESSING_FAILED, Stage.VERIFY_AND_ALIGN,
                          "Partial submitted speech failed source matching validation.") from exc
    seen = post_identities(result.submitted.post)
    viable: list[tuple[float, str, ReportMedia, Alignment]] = []
    attempted = 0
    contextless_matches = 0
    for candidate in candidates:
        if attempted >= MAX_SOURCE_CANDIDATES:
            break
        try:
            url = canonical_youtube_url(candidate["url"])
            try:
                offset = youtube_window_start(candidate["url"])
            except ValueError:
                offset = 0
                warnings.append("A candidate timestamp was invalid; its location remains unknown and was not guessed.")
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
            if not offset:
                try:
                    cues = fetch_youtube_transcript(url)
                    located = _caption_window_start(result.submitted.transcript, cues) if cues else None
                    if located is not None:
                        offset = located
                        warnings.append(f"Caption text located candidate {url} near {offset} seconds; captions are locator hints, and the requested video speech is checked separately.")
                    elif cues:
                        warnings.append(f"Candidate {url} had no unique distinctive caption locator; only its initial native window was checked.")
                except QuantroError:
                    warnings.append(f"Timed captions were unavailable for candidate {url}; only its initial native window was checked.")
            source_report = (build_remote_youtube_report(result.job_id, url, window_start_seconds=offset)
                             if offset else build_remote_youtube_report(result.job_id, url))
            source = ReportMedia.model_validate(source_report.submitted.model_dump(mode="json"))
            if (canonical_youtube_url(source.post.post_url) != url
                    or post_identities(result.submitted.post) & post_identities(source.post)
                    or result.submitted.post.media.media_id == source.post.media.media_id):
                raise ValueError("Source evidence did not identify the distinct requested recording")
            _validate_window(source, offset)
            alignment = Alignment.model_validate(_align(_projection(result.submitted), _projection(source), None))
            if alignment.status != "verified" or not alignment.matches:
                warnings.append(f"Source candidate {url} had no distinctive, uniquely located exact speech match in the observed window.")
                continue
            if not _has_surrounding_speech(result.submitted.transcript, source.transcript, alignment):
                contextless_matches += 1
                warnings.append(
                    f"Unverified source candidate: {url}. Matching observed speech was found, but no substantial new "
                    "surrounding speech was observed; this may be a duplicate clip and was not selected as a contextual source."
                )
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
        if contextless_matches:
            result.summary = (f"Limited transcript analysis: {attempted} source candidate windows were examined; "
                              "matching speech was found, but no source with substantial additional observed context was verified. "
                              "A matching repost alone does not establish the broader source context.")
        else:
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
