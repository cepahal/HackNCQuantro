"""Build bounded, evidence-derived queries without requiring an AI provider."""

import re
from typing import Any

from ..shared.contracts import contract_input, validate_contract_result
from ..shared.errors import QuantroError, Stage
from ..shared.errors import Stage as _ContractStage
from ..shared.errors import normalize_exception as _normalize_contract_exception
from ..shared.types import Post, Queries
from ..shared.types import Queries as _ReturnModel

_WORDS = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
_STOP = {
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
    "it",
    "is",
    "are",
    "was",
    "were",
    "to",
    "of",
    "for",
    "in",
    "on",
    "at",
    "i",
    "you",
    "we",
    "they",
    "thank",
    "thanks",
    "hello",
    "goodbye",
}
MAX_QUERIES = 8


def _phrase(text: object, *, minimum: int = 2) -> str | None:
    if not isinstance(text, str):
        raise TypeError("Search clues must be strings.")
    words: list[str] = []
    length = 0
    for word in _WORDS.findall(text[:4000]):
        if len(word) > 100:
            continue
        if len(words) == 12 or length + len(word) + bool(words) > 510:
            break
        length += len(word) + bool(words)
        words.append(word)
    informative = {word.casefold() for word in words} - _STOP
    if len(words) < minimum or len(informative) < 2:
        return None
    return '"' + " ".join(words) + '"'


def build_search_queries(features: dict[str, Any], post: Post) -> Queries:
    """Use spoken phrases, OCR and post clues; captions remain unverified clues."""
    # aligned with shared/types.py on 2026-10-10
    features = contract_input(features)
    post = contract_input(post)
    try:
        try:
            if not isinstance(features, dict) or not isinstance(post, dict):
                raise TypeError
            if (
                not isinstance(features["media_id"], str)
                or not features["media_id"]
                or features["media_id"] != post["media"]["media_id"]
            ):
                raise ValueError("Features must belong to the submitted post.")
            transcript = features["transcript"]
            if transcript["status"] not in ("complete", "partial", "unavailable"):
                raise ValueError
            sentences = transcript["sentences"]
            visible = features["visual_text"]
            if not isinstance(sentences, list) or not isinstance(visible, list):
                raise TypeError
            result: Queries = {"queries": [], "warnings": []}
            seen: set[str] = set()

            def append(phrase: str | None) -> None:
                if (
                    phrase
                    and phrase.casefold() not in seen
                    and len(result["queries"]) < MAX_QUERIES
                ):
                    result["queries"].append(phrase)
                    seen.add(phrase.casefold())

            spoken = []
            if transcript["status"] != "unavailable":
                spoken = [p for item in sentences[:500] if (p := _phrase(item["text"], minimum=4))][:4]
            visual = [p for item in visible[:3] if (p := _phrase(item["text"]))]
            title = _phrase(post["title"]) if post["title"] is not None else None
            # Providers may execute only four queries. Put different evidence types
            # in that prefix so a speech-heavy clip cannot crowd out visual clues.
            for phrase in [*spoken[:1], *visual[:1], title, *spoken[1:2]]:
                append(phrase)
            for phrase in [*spoken[2:], *visual[1:]]:
                append(phrase)
            caption = post["caption"]
            if caption is not None:
                caption_phrase = _phrase(caption)
                if caption_phrase:
                    # Preserve an explicit provenance warning even when the query cap is full.
                    result["warnings"].append(
                        "The submitted caption is an unverified search clue, not established fact."
                    )
                    append(caption_phrase)
            if transcript["status"] != "complete":
                result["warnings"].append(
                    "Transcript coverage is incomplete; queries use only available evidence."
                )
            if len(sentences) > 500 or len(visible) > 3:
                result["warnings"].append(
                    "Search clues were bounded; not every sentence or OCR item was used."
                )
            if not result["queries"]:
                result["warnings"].append("No distinctive searchable text was available.")
            return validate_contract_result(_ReturnModel, result, _ContractStage.BUILD_SEARCH_QUERIES, json_native=True)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise QuantroError(
                "PROCESSING_FAILED",
                Stage.BUILD_SEARCH_QUERIES,
                "Search clues do not match the submitted media contract.",
            ) from None
    except Exception as exc:  # noqa: BLE001 - Public contract boundary redacts and normalizes failures.
        raise _normalize_contract_exception(_ContractStage.BUILD_SEARCH_QUERIES, exc) from None
