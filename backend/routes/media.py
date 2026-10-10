"""Browser playback of downloaded media stored in Tiger Data."""

import re
import threading
from collections.abc import Callable, Iterator
from typing import Annotated, TypeVar

from fastapi import APIRouter, Header, Path, Request
from fastapi.responses import Response, StreamingResponse

from backend.routes.common import error_response, not_found
from backend.services.external_apis.tigerdata_storage import (
    StorageError,
    TigerDataPostStore,
)
from backend.services.shared.errors import ErrorCode, Stage

router = APIRouter()
MediaId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
T = TypeVar("T")

CHUNK_BYTES = 1024 * 1024
_store: TigerDataPostStore | None = None
_store_lock = threading.Lock()


def _shared_store() -> tuple[TigerDataPostStore, bool]:
    """The process-wide playback connection and whether it was just opened."""
    global _store
    with _store_lock:
        if _store is not None:
            return _store, False
        _store = TigerDataPostStore.from_env()
        return _store, True


def _close(store: TigerDataPostStore) -> None:
    try:
        store.close()
    except StorageError:
        pass


def _discard(store: TigerDataPostStore) -> None:
    global _store
    with _store_lock:
        if _store is store:
            _store = None
    _close(store)


def close_media_store() -> None:
    global _store
    with _store_lock:
        store, _store = _store, None
    if store is not None:
        _close(store)


def _read(operation: Callable[[TigerDataPostStore], T]) -> T:
    """Run one read on the shared connection, reconnecting once if it went stale."""
    while True:
        store, fresh = _shared_store()
        try:
            return operation(store)
        except StorageError:
            _discard(store)
            if fresh:
                raise


def _range(value: str | None, size: int) -> tuple[int, int] | None:
    """The single byte range to serve; None means the full body (RFC 9110 lets a
    server ignore non-bytes units and multi-range requests). ValueError -> 416."""
    if value is None:
        return None
    unit, _, spec = value.partition("=")
    if unit.strip().lower() != "bytes" or not spec or "," in spec:
        return None
    match = re.fullmatch(r"\s*(\d*)-(\d*)\s*", spec)
    if match is None or not any(match.groups()):
        raise ValueError
    left, right = match.groups()
    if left:
        start, end = int(left), min(int(right), size - 1) if right else size - 1
    else:
        count = int(right)
        if count <= 0:
            raise ValueError
        start, end = max(0, size - count), size - 1
    if not 0 <= start <= end < size:
        raise ValueError
    return start, end


def _etag_matches(if_none_match: str, etag: str) -> bool:
    candidates = [tag.strip().removeprefix("W/") for tag in if_none_match.split(",")]
    return "*" in candidates or etag in candidates


def _chunks(media_id: str, digest: str, start: int, end: int) -> Iterator[bytes]:
    offset = start
    while offset <= end:
        length = min(CHUNK_BYTES, end - offset + 1)
        yield _read(lambda store: store.read_media_slice(media_id, digest, offset, length))
        offset += length


@router.get("/media/{media_id}")
@router.head("/media/{media_id}")
def get_media(
    request: Request,
    media_id: MediaId,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    try:
        media = _read(lambda store: store.read_media_info(media_id))
    except StorageError:
        return error_response(
            503, ErrorCode.STORAGE_FAILED, Stage.DISPLAY_REPORT,
            "The stored media could not be loaded.", retryable=True,
        )
    if media is None:
        return not_found("Media not found", Stage.DISPLAY_REPORT, ErrorCode.MEDIA_UNAVAILABLE)
    mime, size, digest = media
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": '"' + digest + '"',
        "Cache-Control": "private, max-age=3600",
        "X-Content-Type-Options": "nosniff",
    }
    if if_none_match is not None and _etag_matches(if_none_match, headers["ETag"]):
        return Response(status_code=304, headers=headers)
    try:
        byte_range = _range(range_header, size)
    except (ValueError, OverflowError):
        return error_response(
            416, ErrorCode.PROCESSING_FAILED, Stage.DISPLAY_REPORT,
            "The requested media range is not available.",
            headers={"Content-Range": f"bytes */{size}"},
        )
    status_code = 200
    start, end = 0, size - 1
    if byte_range is not None:
        status_code = 206
        start, end = byte_range
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(end - start + 1)
    if request.method == "HEAD":
        return Response(status_code=status_code, media_type=mime, headers=headers)
    return StreamingResponse(
        _chunks(media_id, digest, start, end), status_code=status_code, media_type=mime, headers=headers
    )
