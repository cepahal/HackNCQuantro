"""Search the configured Exa web provider or explicit Tiger Data corpus."""

import hashlib
import ipaddress
import logging
import math
import ssl
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from ..shared.contracts import contract_input, validate_contract_result
from ..shared.errors import QuantroError, Stage
from ..shared.errors import Stage as _ContractStage
from ..shared.errors import normalize_exception as _normalize_contract_exception
from ..shared.types import CandidateList as _ReturnModel
from ..shared.types import (
    Queries,
)
from .exa_search import ExaSearch
from .retrieve_post import _validate_url
from .tigerdata_storage import TigerDataPostStore, _environment

logger = logging.getLogger(__name__)


def _candidate_url(value: object) -> str:
    """Discovery may include public articles; retrieval separately enforces platforms."""
    if not isinstance(value, str) or not value.strip() or len(value) > 8192:
        raise ValueError("Invalid candidate URL")
    value = value.strip()
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 80, 443}
        or "\\" in value
        or any(c.isspace() or ord(c) < 32 for c in value)
    ):
        raise ValueError("Invalid candidate URL")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError("Invalid candidate hostname") from None
    else:
        if not address.is_global:
            raise ValueError("Invalid candidate address")
    return value


def _web_search(queries: list[str], limit: int, warnings: list[str], settings: dict[str, str]) -> dict[str, Any]:
    """Use fixed provider hosts; never expose provider diagnostics or credentials."""
    name = settings.get("SEARCH_PROVIDER", "").strip().lower()
    key = settings.get("SEARCH_PROVIDER_API_KEY", "").strip()
    if not name or not key:
        raise QuantroError(
            "SEARCH_FAILED",
            Stage.SEARCH_CANDIDATES,
            "SEARCH_PROVIDER not configured",
            retryable=False,
        )
    if name not in {"serpapi", "brave", "bing", "google_cse"}:
        raise QuantroError(
            "SEARCH_FAILED",
            Stage.SEARCH_CANDIDATES,
            "SEARCH_PROVIDER is unsupported",
            retryable=False,
        )
    if name == "bing":
        raise QuantroError(
            "SEARCH_FAILED",
            Stage.SEARCH_CANDIDATES,
            "Bing Search API is retired; configure brave, serpapi, or google_cse.",
            retryable=False,
        )
    engine = (
        settings.get("SEARCH_GOOGLE_CSE_ID", "") or settings.get("GOOGLE_CSE_ID", "")
    ).strip()
    if name == "google_cse" and not engine:
        raise QuantroError(
            "SEARCH_FAILED",
            Stage.SEARCH_CANDIDATES,
            "SEARCH_GOOGLE_CSE_ID not configured",
            retryable=False,
        )
    count = min(10, max(1, math.ceil(limit / len(queries))))
    found: dict[str, dict[str, Any]] = {}
    failures = 0
    retryable = False
    with httpx.Client(
        timeout=20.0,
        follow_redirects=False,
        trust_env=False,
        verify=ssl.create_default_context(),
    ) as client:
        def fetch(query: str) -> list[Any]:
            headers: dict[str, str] = {}
            params: dict[str, str | int]
            if name == "serpapi":
                endpoint = "https://serpapi.com/search.json"
                params = {
                    "engine": "google",
                    "q": query,
                    "api_key": key,
                    "num": count,
                }
            elif name == "brave":
                endpoint = "https://api.search.brave.com/res/v1/web/search"
                params = {"q": query, "count": count}
                headers = {"X-Subscription-Token": key}
            else:
                endpoint = "https://www.googleapis.com/customsearch/v1"
                params = {"q": query, "key": key, "cx": engine, "num": count}
            response = client.get(endpoint, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get("error"):
                raise ValueError("Invalid provider response")
            rows = (
                data.get("organic_results", [])
                if name == "serpapi"
                else data.get("web", {}).get("results", [])
                if name == "brave"
                else data.get("items", [])
            )
            if not isinstance(rows, list):
                raise TypeError("Invalid provider results")
            return rows[:count]

        # Overlap network waits, then merge in query order to preserve ranking,
        # first-seen metadata, provenance, and failure behavior.
        with ThreadPoolExecutor(max_workers=min(3, len(queries))) as executor:
            futures = [executor.submit(fetch, query) for query in queries]
            for query, future in zip(queries, futures, strict=True):
                try:
                    rows = future.result()
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        raw_url = (
                            row.get("link")
                            if name in {"serpapi", "google_cse"}
                            else row.get("url")
                        )
                        try:
                            url = _candidate_url(raw_url)
                        except (QuantroError, TypeError, ValueError):
                            continue
                        if url in found:
                            if query not in found[url]["discovered_by_queries"]:
                                found[url]["discovered_by_queries"].append(query)
                            continue
                        title = row.get("title", row.get("name"))
                        snippet = row.get("snippet", row.get("description"))
                        found[url] = {
                            "candidate_id": "candidate_" + uuid4().hex[:8],
                            "url": url,
                            "title": title if isinstance(title, str) else None,
                            "publisher": urlsplit(url).hostname,
                            "published_at": None,
                            "snippet": snippet[:500] if isinstance(snippet, str) else None,
                            "discovered_by_queries": [query],
                        }
                except (httpx.HTTPError, ValueError, TypeError, AttributeError) as error:
                    failures += 1
                    retryable = (
                        retryable
                        or not isinstance(error, httpx.HTTPStatusError)
                        or error.response.status_code in {408, 429}
                        or (
                            isinstance(error, httpx.HTTPStatusError)
                            and error.response.status_code >= 500
                        )
                    )
    if failures == len(queries):
        raise QuantroError(
            "SEARCH_FAILED",
            Stage.SEARCH_CANDIDATES,
            "The source search could not be completed.",
            retryable=retryable,
        )
    if failures:
        warnings.append(f"{failures} search queries failed; results may be incomplete.")

    def retrieval_priority(candidate: dict[str, Any]) -> int:
        # Prefer analyzable media without discarding public article provenance.
        try:
            _validate_url(candidate["url"])
        except (QuantroError, TypeError, ValueError):
            return 1
        return 0

    ranked = sorted(found.values(), key=retrieval_priority)
    return {"candidates": ranked[:limit], "warnings": warnings}


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
) -> dict[str, Any]:
    """Accept function 3's object or its queries array; return the exact v2 envelope.

    SEARCH_PROVIDER=exa selects public web discovery. SEARCH_PROVIDER=tiger
    (legacy default) searches stored Post metadata. No silent fallback occurs.
    Candidate pages are attempted by the guarded retrieval worker downstream.
    """
    # aligned with shared/types.py on 2026-10-10
    queries = contract_input(queries)
    try:
        try:
            supplied = queries["queries"] if isinstance(queries, dict) else queries
            warnings: list[str] = (
                queries["warnings"][:] if isinstance(queries, dict) else []
            )
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
            return validate_contract_result(_ReturnModel, {
                "candidates": [],
                "warnings": warnings + ["No search queries were available."],
            }, _ContractStage.SEARCH_CANDIDATES, json_native=True)
        owned: TigerDataPostStore | ExaSearch | None = None
        try:
            if provider is None:
                settings = _environment()
                name = settings.get("SEARCH_PROVIDER", "").strip().lower()
                if name in {"serpapi", "brave", "bing", "google_cse"}:
                    return cast(dict[str, Any], validate_contract_result(
                        _ReturnModel, _web_search(normalized, limit, warnings, settings),
                        _ContractStage.SEARCH_CANDIDATES, json_native=True,
                    ))
                if name == "exa":
                    owned = ExaSearch(settings.get("EXA_API_KEY", ""))
                elif name in {"tiger", "tigerdata"}:
                    owned = TigerDataPostStore.from_env()
                else:
                    raise QuantroError("SEARCH_FAILED", Stage.SEARCH_CANDIDATES,
                                       "SEARCH_PROVIDER is not configured or unsupported.", retryable=False)
                provider = owned
            rows = provider.find_posts(normalized, limit)
            if not isinstance(rows, list) or len(rows) > limit:
                raise ValueError
            candidates: list[dict[str, Any]] = []
            seen: set[str] = set()
            for raw, matched in rows:
                if not isinstance(raw, dict) or not isinstance(matched, list):
                    raise TypeError
                metadata = cast(dict[str, Any], raw)
                url = metadata["post_url"]
                if not isinstance(url, str):
                    raise TypeError
                url, _ = _validate_url(url, allow_webpage=True)
                if url in seen:
                    raise ValueError(
                        "Provider must deduplicate sources before limiting."
                    )
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
                        "discovered_by_queries": [
                            q for q in normalized if q in matched
                        ],
                    }
                )
            if isinstance(provider, ExaSearch):
                warnings.extend(provider.warnings)
            elif owned is not None:
                warnings.append(
                    "Search covers the stored Tiger Data corpus only; results are unverified possible sources."
                )
            return validate_contract_result(_ReturnModel, {"candidates": candidates, "warnings": warnings}, _ContractStage.SEARCH_CANDIDATES, json_native=True)
        except QuantroError:
            raise
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
    except Exception as exc:  # noqa: BLE001 - Public contract boundary redacts and normalizes failures.
        raise _normalize_contract_exception(
            _ContractStage.SEARCH_CANDIDATES, exc
        ) from None
