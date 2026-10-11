"""Deterministic second-pass clues; never rewrite the submitted claim."""

import re

from ..shared.types import Queries

_EXACT_PHRASE = re.compile(r'"([^"\r\n]+)"|“([^“”\r\n]+)”')
_WARNING = (
    "A bounded second search uses previously unused clues or removes exact-phrase "
    "quotes without changing their words; discovered sources remain unverified."
)


def widen_queries(queries: Queries) -> Queries:
    """Return at most three unused clues, then quote-relaxed original clues.

    Original clues after index two were not sent in the first Exa pass, so they
    may be reused verbatim here. Quote-relaxed clues must differ from every
    original clue. Length overflow is skipped rather than truncating meaning.
    """
    original = [query.strip() for query in queries.queries]
    first_pass = set(original[:3])
    expanded: list[str] = []

    def add(query: str) -> None:
        if query and len(query) <= 512 and query not in first_pass and query not in expanded:
            expanded.append(query)

    for query in original[3:]:
        add(query)
        if len(expanded) == 3:
            break
    if len(expanded) < 3:
        for query in original:
            relaxed = _EXACT_PHRASE.sub(lambda match: match.group(1) or match.group(2), query)
            if relaxed != query and relaxed not in original:
                add(relaxed)
            if len(expanded) == 3:
                break
    warnings = list(queries.warnings)
    if expanded and _WARNING not in warnings:
        warnings.append(_WARNING)
    return Queries(queries=expanded, warnings=warnings)
