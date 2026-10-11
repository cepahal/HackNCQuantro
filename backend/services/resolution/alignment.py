"""Localize a transcript lead onto provider-supplied sentence/word timestamps.

Fuzzy scores identify lexical resemblance. They are neither calibrated accuracy
nor evidence of audio/visual authenticity; coarse caption times stay coarse.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from rapidfuzz import fuzz

from ..shared.types import Alignment, Context, MediaFeatures, TranscriptSentence
from .candidates import MATCH_THRESHOLD, normalize_text

WORDING_DIFFERENCE_LIMITATION = "Matched transcript numbers or negation differ; equivalent meaning has not been established."

_COMMON_WORDS = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "but",
    "if",
    "then",
    "this",
    "that",
    "those",
    "these",
    "it",
    "its",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "to",
    "of",
    "for",
    "in",
    "on",
    "at",
    "as",
    "i",
    "you",
    "he",
    "she",
    "we",
    "they",
    "me",
    "my",
    "your",
    "our",
    "their",
    "would",
    "could",
    "should",
    "will",
    "can",
    "do",
    "does",
    "did",
    "so",
    "not",
    "no",
    "with",
    "from",
    "by",
    "have",
    "has",
    "had",
    "just",
    "very",
    "much",
    "here",
    "there",
    "what",
    "when",
    "how",
    "all",
    "now",
    "well",
    "thank",
    "thanks",
    "everybody",
    "everyone",
    "welcome",
    "hello",
    "hi",
    "good",
    "great",
    "today",
    "day",
    "people",
    "really",
    "going",
    "get",
    "got",
    "let",
    "us",
    "about",
    "who",
    "where",
    "which",
    "one",
    "some",
    "more",
    "any",
    "also",
    "know",
    "think",
    "say",
    "said",
}


@dataclass(frozen=True)
class _Span:
    left: int
    right: int
    sentence: TranscriptSentence
    start: float
    end: float


def _distinctive(text: str) -> bool:
    words = text.split()
    meaningful = {word for word in words if word not in _COMMON_WORDS}
    return len(words) >= 8 and len(meaningful) >= 4 and len(text) >= 40


def _unbalanced_passage_match(clip_text: str, source_passage: str) -> bool:
    """A near-exact half cannot compensate for an unsupported remaining half.

    This only rejects strongly asymmetric support. Uniform ASR errors retain the
    existing fuzzy gate; abstention does not diagnose a physical splice.
    """
    words = clip_text.split()
    if len(words) < 16:
        return False
    middle = len(words) // 2
    blocks = (" ".join(words[:middle]), " ".join(words[middle:]))
    if not all(_distinctive(block) for block in blocks):
        return False
    scores = [fuzz.partial_ratio(block, source_passage) for block in blocks]
    return max(scores) >= 95.0 and min(scores) < MATCH_THRESHOLD


def _terminal_anchor_outside_passage(
    clip_text: str, source_text: str, left: int, right: int
) -> bool:
    """Reject a claimed contiguous passage whose observed ending occurs elsewhere.

    A strong majority match can conceal a short, decisive missing conclusion.
    Require a distinctive verbatim anchor outside the enclosing provider cues,
    plus weak support inside them. Ordinary ASR substitutions or an unobserved
    conclusion alone do not establish this inconsistency.
    """
    words = clip_text.split()
    tail_words = words[-max(8, min(32, len(words) // 5)) :]
    passage = source_text[left:right]
    outside = (" " + source_text[:left] + " ", " " + source_text[right:] + " ")
    for index in range(len(tail_words) - 7):
        anchor = " ".join(tail_words[index : index + 8])
        if (
            _distinctive(anchor)
            and any(" " + anchor + " " in part for part in outside)
            and fuzz.partial_ratio(anchor, passage) < MATCH_THRESHOLD
        ):
            return True
    return False


def _wording_differences(
    clip_passage: str,
    source_passage: str,
    clip_numbers: list[Decimal] | None = None,
    source_numbers: list[Decimal] | None = None,
) -> list[str]:
    """Flag observed lexical differences, never infer a factual contradiction."""
    negations = {
        "not",
        "no",
        "never",
        "neither",
        "nor",
        "cannot",
        "cant",
        "dont",
        "doesnt",
        "didnt",
        "wont",
        "wouldnt",
        "shouldnt",
        "couldnt",
        "isnt",
        "arent",
        "wasnt",
        "werent",
        "hasnt",
        "havent",
        "hadnt",
        "mustnt",
    }
    clip_words, source_words = clip_passage.split(), source_passage.split()
    differences = []
    def canonical(word: str) -> str:
        return word if word in {"no", "never", "neither", "nor"} else "not"

    if Counter(canonical(w) for w in clip_words if w in negations) != Counter(
        canonical(w) for w in source_words if w in negations
    ):
        differences.append("negation")
    if (
        clip_numbers is not None
        and source_numbers is not None
        and clip_numbers != source_numbers
    ):
        differences.append("numeric")
    return differences


def _matched_numbers(
    features: MediaFeatures, left: int, right: int
) -> list[Decimal] | None:
    """Retain raw numeric values with exact normalized-token character ownership.

    Abstain on clipped numeric tokens or unsupported notation rather than compare
    invented numbers. This does not infer spoken-out numbers or numeric units.
    """
    values: list[Decimal] = []
    offset = 0
    for sentence in sorted(
        features.transcript.sentences, key=lambda s: (s.start_seconds, s.end_seconds)
    ):
        if offset >= right:
            break
        text = normalize_text(sentence.text)
        if not text:
            continue
        tokens = [(raw, normalize_text(raw)) for raw in sentence.text.split()]
        tokens = [(raw, normalized) for raw, normalized in tokens if normalized]
        if " ".join(normalized for _, normalized in tokens) != text:
            return None
        for raw, normalized in tokens:
            end = offset + len(normalized)
            if offset < right and end > left and any(c.isdigit() for c in normalized):
                if offset < left or end > right:
                    return None
                numeric = raw.strip("\"'()[]{}:;!?").rstrip(",.")
                if not re.fullmatch(
                    r"[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?", numeric
                ):
                    return None
                try:
                    values.append(Decimal(numeric.replace(",", "")))
                except InvalidOperation:
                    return None
            offset = end + 1
    return values


def _timed_text(features: MediaFeatures) -> tuple[str, list[_Span]]:
    """Keep character-to-time ownership; never interpolate times from text."""
    duration = features.duration_seconds
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Recording duration is unavailable.")
    pieces: list[str] = []
    spans: list[_Span] = []
    offset = 0
    for sentence in sorted(
        features.transcript.sentences,
        key=lambda item: (item.start_seconds, item.end_seconds),
    ):
        text = normalize_text(sentence.text)
        if not text:
            continue
        if not 0 <= sentence.start_seconds < sentence.end_seconds <= duration:
            raise ValueError("A transcript sentence has invalid provider timestamps.")
        if pieces:
            offset += 1
        # Providers may supply word times. Only use them when they fully explain
        # the sentence text and stay within its real enclosing time interval.
        word_parts = [(normalize_text(word.text), word) for word in sentence.words]
        word_parts = [(value, word) for value, word in word_parts if value]
        usable_words = (
            bool(word_parts) and " ".join(value for value, _ in word_parts) == text
        )
        if usable_words:
            previous_end = sentence.start_seconds
            for _, word in word_parts:
                if not (
                    sentence.start_seconds
                    <= word.start_seconds
                    < word.end_seconds
                    <= sentence.end_seconds
                    and word.start_seconds >= previous_end - 1e-6
                ):
                    usable_words = False
                    break
                previous_end = word.end_seconds
        if usable_words:
            word_offset = offset
            for value, word in word_parts:
                spans.append(
                    _Span(
                        word_offset,
                        word_offset + len(value),
                        sentence,
                        word.start_seconds,
                        word.end_seconds,
                    )
                )
                word_offset += len(value) + 1
        else:
            spans.append(
                _Span(
                    offset,
                    offset + len(text),
                    sentence,
                    sentence.start_seconds,
                    sentence.end_seconds,
                )
            )
        pieces.append(text)
        offset += len(text)
    return " ".join(pieces), spans


def _overlapping(spans: list[_Span], left: int, right: int) -> list[_Span]:
    return [span for span in spans if span.left < right and span.right > left]


def _repeated(needle: str, haystack: str) -> bool:
    if not needle:
        return False
    first = haystack.find(needle)
    return first >= 0 and haystack.find(needle, first + 1) >= 0


def _competing_location(clip_text: str, source_text: str, matched) -> bool:
    """Do not assign one timestamp to competing near-identical passages."""
    for remaining in (
        source_text[: matched.dest_start],
        source_text[matched.dest_end :],
    ):
        if len(remaining) < len(clip_text) * 0.8:
            continue
        alternate = fuzz.partial_ratio_alignment(clip_text, remaining)
        if alternate is None:
            continue
        if (
            alternate.score >= max(90.0, matched.score - 10.0)
            and (alternate.src_end - alternate.src_start) / len(clip_text) >= 0.8
            and _distinctive(clip_text[alternate.src_start : alternate.src_end])
            and _distinctive(remaining[alternate.dest_start : alternate.dest_end])
        ):
            return True
    return False


def _empty_alignment(
    clip: MediaFeatures,
    source: MediaFeatures,
    score: float,
    limitations: list[str],
    *,
    unassessed: bool = False,
) -> Alignment:
    return Alignment.model_validate(
        {
            "status": "not_assessed" if unassessed else "no_verified_match",
            "clip_media_id": clip.media_id,
            "source_media_id": source.media_id,
            "matches": [],
            "match_quality": {
                "confidence": {
                    "level": "not_assessed" if unassessed else "low",
                    "method": "transcript_partial_ratio_v1",
                    "basis": [],
                    "limitations": limitations,
                },
                "matched_clip_seconds": None,
                "clip_coverage_ratio": None,
                "metrics": [
                    {
                        "name": "transcript_partial_ratio",
                        "value": score,
                        "unit": "score_0_100",
                    }
                ],
                "methods_used": [] if unassessed else ["transcript_partial_ratio"],
            },
            "edit_analysis": {
                "status": "not_assessed",
                "scope": "single_passage",
                "minimum_gap_seconds": 1.0,
                "source_gaps": [],
                "unmatched_clip_ranges": [],
                "counts": {
                    "matched_passages": None,
                    "source_gaps": None,
                    "unmatched_clip_segments": None,
                    "confirmed_added_segments": None,
                    "confirmed_deleted_segments": None,
                },
            },
            "limitations": limitations,
        }
    )


def build_alignment(
    clip_features: MediaFeatures,
    source_features: MediaFeatures,
    score: float,
) -> Alignment:
    """Accept a distinctive >=75 lexical match only when real time bounds exist."""
    clip = MediaFeatures.model_validate(clip_features)
    source = MediaFeatures.model_validate(source_features)
    score = (
        float(score)
        if isinstance(score, (int, float)) and not isinstance(score, bool)
        else 0.0
    )
    score = min(100.0, max(0.0, score)) if math.isfinite(score) else 0.0
    limitations = list(
        dict.fromkeys(
            [
                "The transcript similarity score is a heuristic, not a calibrated accuracy or authenticity probability.",
                "Audio, visual and deepfake verification were not performed.",
                "Time bounds use provider word timestamps when available, otherwise enclosing caption or ASR segments; segment boundaries may include surrounding speech.",
                "The earliest original upload was not established.",
                *clip.warnings,
                *source.warnings,
            ]
        )
    )
    if clip.media_id == source.media_id:
        limitations.append(
            "The submitted recording cannot verify itself as an independent source."
        )
        return _empty_alignment(clip, source, score, limitations)
    if any(features.transcript.status == "unavailable" for features in (clip, source)):
        limitations.append(
            "Usable timed speech is unavailable in at least one recording."
        )
        return _empty_alignment(clip, source, score, limitations, unassessed=True)
    if any(features.transcript.status != "complete" for features in (clip, source)):
        limitations.append(
            "Transcript coverage is partial; unprocessed content was not compared."
        )
    try:
        clip_text, clip_spans = _timed_text(clip)
        source_text, source_spans = _timed_text(source)
    except ValueError:
        limitations.append(
            "Supplied transcript timestamps do not define usable recording bounds."
        )
        return _empty_alignment(clip, source, score, limitations, unassessed=True)
    if not clip_text or not source_text or not clip_spans or not source_spans:
        limitations.append(
            "No usable timed transcript was available for passage alignment."
        )
        return _empty_alignment(clip, source, score, limitations, unassessed=True)
    if not _distinctive(clip_text):
        limitations.append(
            "The submitted speech is too short or generic to identify a distinctive source passage."
        )
        return _empty_alignment(clip, source, score, limitations)
    # In particular, partial_ratio must not turn a tiny source substring into a
    # high-confidence match of a much longer submitted statement.
    if len(source_text) < len(clip_text) * 0.8:
        limitations.append(
            "The source transcript does not cover enough of the submitted speech."
        )
        return _empty_alignment(clip, source, score, limitations)
    matched = fuzz.partial_ratio_alignment(clip_text, source_text)
    if matched is None:
        return _empty_alignment(clip, source, score, limitations)
    effective_score = min(score, float(matched.score))
    clip_passage = clip_text[matched.src_start : matched.src_end]
    source_passage = source_text[matched.dest_start : matched.dest_end]
    if (
        effective_score < MATCH_THRESHOLD
        or not _distinctive(clip_passage)
        or not _distinctive(source_passage)
        or (matched.src_end - matched.src_start) / len(clip_text) < 0.8
    ):
        limitations.append(
            "No sufficiently distinctive transcript passage reached the matching threshold."
        )
        return _empty_alignment(clip, source, effective_score, limitations)
    if (
        _repeated(clip_passage, source_text)
        or _repeated(source_passage, source_text)
        or _competing_location(clip_text, source_text, matched)
    ):
        limitations.append(
            "The passage occurs at multiple source locations; its timestamp is ambiguous."
        )
        return _empty_alignment(clip, source, effective_score, limitations)
    if _unbalanced_passage_match(clip_text, source_passage):
        limitations.append(
            "One part of the submitted speech closely matches, but the remaining part is not supported by the same source passage."
        )
        return _empty_alignment(clip, source, effective_score, limitations)
    clip_owners = _overlapping(clip_spans, matched.src_start, matched.src_end)
    source_owners = _overlapping(source_spans, matched.dest_start, matched.dest_end)
    if not clip_owners or not source_owners:
        limitations.append("No provider timestamps enclose the matched passage.")
        return _empty_alignment(
            clip, source, effective_score, limitations, unassessed=True
        )
    if _terminal_anchor_outside_passage(
        clip_text,
        source_text,
        min(span.left for span in source_owners),
        max(span.right for span in source_owners),
    ):
        limitations.append(
            "A distinctive ending of the submitted speech occurs outside the proposed source interval; one contiguous passage does not support the whole excerpt."
        )
        return _empty_alignment(clip, source, effective_score, limitations)
    clip_start, clip_end = (
        min(span.start for span in clip_owners),
        max(span.end for span in clip_owners),
    )
    source_start, source_end = (
        min(span.start for span in source_owners),
        max(span.end for span in source_owners),
    )
    if not (
        0 <= clip_start < clip_end <= clip.duration_seconds
        and 0 <= source_start < source_end <= source.duration_seconds
    ):
        limitations.append(
            "No valid provider timestamp range encloses the matched passage."
        )
        return _empty_alignment(
            clip, source, effective_score, limitations, unassessed=True
        )
    clip_numbers = _matched_numbers(clip, matched.src_start, matched.src_end)
    source_numbers = _matched_numbers(source, matched.dest_start, matched.dest_end)
    wording_differences = _wording_differences(
        clip_passage,
        source_passage,
        clip_numbers,
        source_numbers,
    )
    if wording_differences:
        limitations.append(WORDING_DIFFERENCE_LIMITATION)
        limitations.append(
            "Observed "
            + " and ".join(wording_differences)
            + " wording differs between the matched transcripts; source localization does not establish equivalent meaning. ASR or caption errors may explain the difference."
        )
    limitations.append(
        "Only one transcript passage was localized; edit and deletion counts were not assessed."
    )
    return Alignment.model_validate(
        {
            "status": "verified",
            "clip_media_id": clip.media_id,
            "source_media_id": source.media_id,
            "matches": [
                {
                    "match_id": "match_001",
                    "clip_start_seconds": clip_start,
                    "clip_end_seconds": clip_end,
                    "source_start_seconds": source_start,
                    "source_end_seconds": source_end,
                    "evidence": [
                        {
                            "method": "transcript_partial_ratio",
                            "clip_refs": list(
                                dict.fromkeys(
                                    span.sentence.sentence_id for span in clip_owners
                                )
                            ),
                            "source_refs": list(
                                dict.fromkeys(
                                    span.sentence.sentence_id for span in source_owners
                                )
                            ),
                            "description": "A distinctive fuzzy transcript passage was localized to supplied provider timestamps; time bounds were not interpolated from text.",
                        }
                    ],
                }
            ],
            "match_quality": {
                "confidence": {
                    "level": "high"
                    if effective_score >= 90.0 and not wording_differences
                    else "moderate",
                    "method": "transcript_partial_ratio_v1",
                    "basis": [
                        f"Distinctive transcript similarity scored {effective_score:.1f}/100 using a lexical matching heuristic.",
                        *(
                            [
                                f"Potential {' and '.join(wording_differences)} wording difference in observed transcripts: submitted '{clip_passage[:240]}'; source '{source_passage[:240]}'."
                            ]
                            if wording_differences
                            else []
                        ),
                        *(
                            [
                                f"Observed numeric values: submitted {[str(value) for value in clip_numbers or []]}; source {[str(value) for value in source_numbers or []]}."
                            ]
                            if "numeric" in wording_differences
                            else []
                        ),
                    ],
                    "limitations": limitations,
                },
                "matched_clip_seconds": clip_end - clip_start,
                "clip_coverage_ratio": min(
                    1.0, (clip_end - clip_start) / clip.duration_seconds
                ),
                "metrics": [
                    {
                        "name": "transcript_partial_ratio",
                        "value": effective_score,
                        "unit": "score_0_100",
                    }
                ],
                "methods_used": ["transcript_partial_ratio"],
            },
            "edit_analysis": {
                "status": "not_assessed",
                "scope": "single_passage",
                "minimum_gap_seconds": 1.0,
                "source_gaps": [],
                "unmatched_clip_ranges": [],
                "counts": {
                    "matched_passages": 1,
                    "source_gaps": None,
                    "unmatched_clip_segments": None,
                    "confirmed_added_segments": None,
                    "confirmed_deleted_segments": None,
                },
            },
            "limitations": limitations,
        }
    )


def _coverage(start: float, end: float, source: MediaFeatures) -> str:
    intersections = sorted(
        (max(start, item.start_seconds), min(end, item.end_seconds))
        for item in source.transcript.processed_ranges
        if item.start_seconds < end and item.end_seconds > start
    )
    covered = 0.0
    cursor = start
    for left, right in intersections:
        if right > max(cursor, left):
            covered += right - max(cursor, left)
            cursor = right
    if covered <= 0:
        return "unavailable"
    return "complete" if covered >= end - start - 1e-6 else "partial"


def build_context(
    alignment: Alignment, source_features: MediaFeatures
) -> Context | None:
    """Select ±60s of source speech while retaining whole observed segments."""
    alignment = Alignment.model_validate(alignment)
    source = MediaFeatures.model_validate(source_features)
    if (
        alignment.status != "verified"
        or alignment.source_media_id != source.media_id
        or not alignment.matches
    ):
        return None
    windows: list[dict] = []
    for match in sorted(alignment.matches, key=lambda item: item.source_start_seconds):
        start = max(0.0, match.source_start_seconds - 60.0)
        end = min(source.duration_seconds, match.source_end_seconds + 60.0)
        if not start < end:
            continue
        if windows and start <= windows[-1]["end"]:
            windows[-1]["end"] = max(windows[-1]["end"], end)
            windows[-1]["match_ids"].append(match.match_id)
        else:
            windows.append({"start": start, "end": end, "match_ids": [match.match_id]})
    if not windows:
        return None
    limitations = [*source.warnings, *alignment.limitations]
    result = []
    for index, window in enumerate(windows, 1):
        start, end = window["start"], window["end"]
        coverage = _coverage(start, end, source)
        result.append(
            {
                "context_id": f"context_{index:03d}",
                "match_ids": window["match_ids"],
                "requested_start_seconds": start,
                "requested_end_seconds": end,
                "coverage": coverage,
                "sentences": [
                    sentence
                    for sentence in sorted(
                        source.transcript.sentences,
                        key=lambda item: (item.start_seconds, item.end_seconds),
                    )
                    if sentence.start_seconds < end and sentence.end_seconds > start
                ],
            }
        )
        if coverage != "complete":
            limitations.append(
                "Surrounding transcript coverage is incomplete; missing speech was not treated as silence."
            )
    return Context.model_validate(
        {
            "source_media_id": source.media_id,
            "windows": result,
            "limitations": list(dict.fromkeys(limitations)),
        }
    )
