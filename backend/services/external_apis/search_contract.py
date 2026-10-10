"""JSON-native contracts for functions 3 and 4 (team contract v2)."""

from typing import TypedDict


class Queries(TypedDict):
    queries: list[str]
    warnings: list[str]


class Candidate(TypedDict):
    candidate_id: str
    url: str
    title: str | None
    publisher: str | None
    published_at: str | None
    snippet: str | None
    discovered_by_queries: list[str]


class CandidateList(TypedDict):
    candidates: list[Candidate]
    warnings: list[str]
