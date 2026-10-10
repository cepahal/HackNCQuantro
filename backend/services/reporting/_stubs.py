"""Placeholders for the upstream stages that are not in the repo yet.

run_analysis imports this module under a single alias (candidate_search); point
that alias at the real module once it lands. The stubs raise instead of returning
empty results so a missing integration can never be mistaken for "no source found".
"""

from typing import Any


# STUB — replace with real import from Abdullah
def build_search_queries(features: dict[str, Any], post: dict[str, Any]) -> dict[str, Any]:
    raise NotImplementedError("build_search_queries is not implemented yet")


# STUB — replace with real import from Abdullah
def search_candidates(queries: list[Any], limit: int) -> dict[str, Any]:
    raise NotImplementedError("search_candidates is not implemented yet")


# STUB — replace with real import from Abdullah
def retrieve_candidate_media(candidate: dict[str, Any]) -> dict[str, Any]:
    raise NotImplementedError("retrieve_candidate_media is not implemented yet")
