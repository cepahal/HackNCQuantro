"""Download a candidate through the existing bounded downloader and Tiger store."""

import logging
from pathlib import Path

from ..shared.contracts import contract_input, validate_contract_result
from ..shared.errors import ErrorCode, QuantroError, Stage
from ..shared.errors import Stage as _ContractStage
from ..shared.errors import normalize_exception as _normalize_contract_exception
from ..shared.types import Candidate, Post
from ..shared.types import Post as _ReturnModel
from .retrieve_post import (
    PostStorage,
    RetrievalError,
    retrieve_post,
)
from .tigerdata_storage import TigerDataPostStore

logger = logging.getLogger(__name__)


class _LinkedStorage:
    def __init__(self, candidate_id: str, storage: PostStorage | None) -> None:
        self.candidate_id = candidate_id
        self.storage = storage

    def save_post(self, post: Post) -> None:
        # Link before the single transaction, so returned and persisted metadata match.
        post["candidate_id"] = self.candidate_id
        owned: TigerDataPostStore | None = None
        try:
            store = self.storage
            if store is None:
                owned = TigerDataPostStore.from_env()
                store = owned
            store.save_post(post)
        finally:
            if owned is not None:
                try:
                    owned.close()
                except Exception as error:  # noqa: BLE001 - do not mask a committed save or leak diagnostics
                    logger.warning(
                        "Candidate storage cleanup failed (%s)", type(error).__name__
                    )


def retrieve_candidate_media(
    candidate: Candidate,
    *,
    storage: PostStorage | None = None,
    output_dir: str | Path | None = None,
    timeout_seconds: float = 120,
    max_bytes: int = 100_000_000,
) -> Post:
    """Return a fresh v2 Post/MediaAsset linked to the complete search candidate.

    Search snippets never replace the retrieved caption or other post metadata.
    Success requires persisting the candidate relationship and media bytes.
    """
    # aligned with shared/types.py on 2026-10-10
    candidate = contract_input(candidate)
    try:
        stage = Stage.RETRIEVE_CANDIDATE_MEDIA
        try:
            if not isinstance(candidate, dict):
                raise TypeError
            identity, url = candidate["candidate_id"], candidate["url"]
            if (
                not isinstance(identity, str)
                or not identity.strip()
                or len(identity) > 255
                or not isinstance(url, str)
            ):
                raise ValueError
            for key in ("title", "publisher", "published_at", "snippet"):
                if candidate[key] is not None and not isinstance(candidate[key], str):
                    raise ValueError
            discovered = candidate["discovered_by_queries"]
            if not isinstance(discovered, list) or any(
                not isinstance(q, str) or not q.strip() for q in discovered
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise QuantroError(
                "MEDIA_UNAVAILABLE", stage, "Provide a complete search candidate."
            ) from None
        try:
            return validate_contract_result(_ReturnModel, retrieve_post(
                url,
                storage=_LinkedStorage(identity, storage),
                output_dir=output_dir,
                timeout_seconds=timeout_seconds,
                max_bytes=max_bytes,
            ), _ContractStage.RETRIEVE_CANDIDATE_MEDIA, json_native=True)
        except RetrievalError as error:
            try:
                code = ErrorCode(error.code)
            except ValueError:
                code = ErrorCode.MEDIA_UNAVAILABLE
            raise QuantroError(
                code, stage, error.message, retryable=error.retryable
            ) from None
        except (ValueError, TypeError):
            raise QuantroError(
                "MEDIA_UNAVAILABLE", stage, "Candidate retrieval options are invalid."
            ) from None
    except Exception as exc:  # noqa: BLE001 - Public contract boundary redacts and normalizes failures.
        raise _normalize_contract_exception(_ContractStage.RETRIEVE_CANDIDATE_MEDIA, exc) from None
