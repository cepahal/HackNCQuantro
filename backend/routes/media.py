"""Browser playback of downloaded media stored in Tiger Data."""

import re
from collections.abc import Iterator
from threading import Lock
from typing import Annotated

import anyio
from fastapi import APIRouter, Header, Path, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from backend.routes.common import request_error_response
from backend.services.external_apis.tigerdata_storage import (
    MEDIA_CHUNK_BYTES,
    StorageError,
    TigerDataPostStore,
)
from backend.services.shared.errors import ErrorCode, Stage

router = APIRouter()
MediaId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,128}$")]


class _MediaResponse(StreamingResponse):
    """Keep the owned connection alive while streaming and close on every ASGI exit."""

    def __init__(
        self,
        store: TigerDataPostStore,
        media_id: str,
        first: bytes,
        start: int,
        end: int,
        size: int,
        digest: str,
        **kwargs,
    ):
        self._store = store
        self._lock = Lock()

        def chunks() -> Iterator[bytes]:
            yield first
            offset = start + len(first)
            while offset <= end:
                with self._lock:
                    chunk = store.read_media_chunk(
                        media_id,
                        offset,
                        min(MEDIA_CHUNK_BYTES, end - offset + 1),
                        size=size,
                        digest=digest,
                    )
                yield chunk
                offset += len(chunk)

        super().__init__(chunks(), **kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:

            def close() -> None:
                # A disconnect must not close a connection still used by a worker's SELECT.
                with self._lock:
                    try:
                        self._store.close()
                    except StorageError:
                        pass

            with anyio.CancelScope(shield=True):
                await run_in_threadpool(close)


def _range(value: str, size: int) -> tuple[int, int]:
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
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


def _not_modified(value: str | None, etag: str) -> bool:
    if value is None:
        return False
    # If-None-Match uses weak comparison for GET/HEAD, including lists and '*'.
    return any(
        item == "*" or item.removeprefix("W/") == etag
        for item in (part.strip() for part in value.split(","))
    )


@router.get("/media/{media_id}")
@router.head("/media/{media_id}")
def get_media(
    media_id: MediaId,
    request: Request,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    store = None
    try:
        pool = request.app.state.media_pool
        if pool is None:
            raise StorageError("Media storage is not configured.")
        store = pool.acquire()
        media = store.read_media_info(media_id)
        if media is None:
            return request_error_response(
                404,
                code=ErrorCode.MEDIA_UNAVAILABLE,
                stage=Stage.DISPLAY_REPORT,
                message="Media not found",
            )
        mime, size, digest = media
        headers = {
            "Accept-Ranges": "bytes",
            "ETag": '"' + digest + '"',
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        }
        if _not_modified(if_none_match, headers["ETag"]):
            return Response(status_code=304, headers=headers)
        start, end, status = 0, size - 1, 200
        # Range applies only to GET. Unsupported units and multipart ranges may
        # be ignored: this endpoint deliberately serves only one byte range.
        if (
            request.method == "GET"
            and range_header is not None
            and range_header.startswith("bytes=")
            and "," not in range_header
        ):
            try:
                start, end = _range(range_header, size)
            except (ValueError, OverflowError):
                range_error = request_error_response(416, stage=Stage.DISPLAY_REPORT)
                range_error.headers["Content-Range"] = f"bytes */{size}"
                return range_error
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(end - start + 1)
        if request.method == "HEAD":
            return Response(status_code=200, media_type=mime, headers=headers)
        # Fail before sending HTTP headers when the initial bounded database read is unavailable.
        first = store.read_media_chunk(
            media_id,
            start,
            min(MEDIA_CHUNK_BYTES, end - start + 1),
            size=size,
            digest=digest,
        )
        response = _MediaResponse(
            store,
            media_id,
            first,
            start,
            end,
            size,
            digest,
            status_code=status,
            media_type=mime,
            headers=headers,
        )
        store = None  # Ownership transfers to the response, including disconnect/error cleanup.
        return response
    except StorageError:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "STORAGE_FAILED",
                    "stage": "display_report",
                    "message": "The stored media could not be loaded.",
                    "retryable": True,
                }
            },
        )
    finally:
        if store is not None:
            try:
                store.close()
            except StorageError:
                pass
