"""Search the Tiger Data corpus; a returned candidate is not a verified match."""

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from ..reporting.errors import QuantroError, Stage
from .retrieve_post import _validate_url
from .search_contract import (
    Candidate,
    CandidateList,
    Queries,
)
from .tigerdata_storage import TigerDataPostStore

logger = logging.getLogger(__name__)


class CandidateSearch(Protocol):
    def find_posts(
        self, queries: list[str], limit: int
    ) -> list[tuple[object, object]]: ...


def _nullable_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError
    return value if value.strip() else None


def _date(value: object) -> str | None:
    text = _nullable_text(value)
    if text is None:
        return None
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def search_candidates(
    queries: Queries | list[str], limit: int, *, provider: CandidateSearch | None = None
) -> CandidateList:
    """Accept function 3's object or its queries array; return the exact v2 envelope.

    The default provider searches already stored post titles/captions/authors in
    Tiger Data. Curate source recordings into that corpus using retrieve_post.
    No search key is required, and no public-web discovery is implied.
    """
    try:
        supplied = queries["queries"] if isinstance(queries, dict) else queries
        warnings = queries["warnings"][:] if isinstance(queries, dict) else []
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 50
            or not isinstance(supplied, list)
            or len(supplied) > 20
            or not isinstance(warnings, list)
            or any(not isinstance(w, str) for w in warnings)
        ):
            raise ValueError
        normalized: list[str] = []
        for query in supplied:
            if not isinstance(query, str) or not query.strip() or len(query) > 512:
                raise ValueError
            if query.strip() not in normalized:
                normalized.append(query.strip())
    except (KeyError, TypeError, ValueError):
        raise QuantroError(
            "SEARCH_FAILED",
            Stage.SEARCH_CANDIDATES,
            "Provide at most 20 nonempty queries and a candidate limit from 1 to 50.",
        ) from None
    if not normalized:
        return {
            "candidates": [],
            "warnings": warnings + ["No search queries were available."],
        }
    owned: TigerDataPostStore | None = None
    try:
        if provider is None:
            owned = TigerDataPostStore.from_env()
            provider = owned
        rows = provider.find_posts(normalized, limit)
        if not isinstance(rows, list) or len(rows) > limit:
            raise ValueError
        candidates: list[Candidate] = []
        seen: set[str] = set()
        for raw, matched in rows:
            if not isinstance(raw, dict) or not isinstance(matched, list):
                raise TypeError
            metadata = cast(dict[str, Any], raw)
            url = metadata["post_url"]
            if not isinstance(url, str):
                raise TypeError
            url, _ = _validate_url(url)
            if url in seen:
                raise ValueError("Provider must deduplicate sources before limiting.")
            seen.add(url)
            if not matched or any(
                not isinstance(q, str) or q not in normalized for q in matched
            ):
                raise ValueError
            author = metadata["author"]
            if not isinstance(author, dict):
                raise TypeError
            caption = _nullable_text(metadata["caption"])
            candidates.append(
                {
                    "candidate_id": "candidate_"
                    + hashlib.sha256(url.encode()).hexdigest()[:24],
                    "url": url,
                    "title": _nullable_text(metadata["title"]),
                    "publisher": _nullable_text(author.get("display_name"))
                    or _nullable_text(author.get("handle")),
                    "published_at": _date(metadata["published_at"]),
                    "snippet": caption[:500] if caption else None,
                    "discovered_by_queries": [q for q in normalized if q in matched],
                }
            )
        if owned is not None:
            warnings.append(
                "Search covers the stored Tiger Data corpus only; results are unverified possible sources."
            )
        return {"candidates": candidates, "warnings": warnings}
    except Exception as error:  # noqa: BLE001 - redact SQL, DSNs and provider diagnostics
        raise QuantroError(
            "SEARCH_FAILED",
            Stage.SEARCH_CANDIDATES,
            "The source search could not be completed.",
            retryable=isinstance(error, (TimeoutError, ConnectionError)),
        ) from None
    finally:
        if owned is not None:
            try:
                owned.close()
            except Exception as error:  # noqa: BLE001 - preserve result and redact cleanup diagnostics
                logger.warning(
                    "Candidate search cleanup failed (%s)", type(error).__name__
                )
