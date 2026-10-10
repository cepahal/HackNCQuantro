"""Browser playback of downloaded media stored in Tiger Data."""

import re
from typing import Annotated

from fastapi import APIRouter, Header, Path
from fastapi.responses import JSONResponse, Response

from backend.services.external_apis.tigerdata_storage import (
    StorageError,
    TigerDataPostStore,
)

router = APIRouter()
MediaId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,128}$")]


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


@router.get("/media/{media_id}")
def get_media(
    media_id: MediaId, range_header: Annotated[str | None, Header(alias="Range")] = None
) -> Response:
    store = None
    try:
        store = TigerDataPostStore.from_env()
        media = store.read_media(media_id)
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
    if media is None:
        return JSONResponse(
            status_code=404, content={"error": {"message": "Media not found"}}
        )
    mime, data, digest = media
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": '"' + digest + '"',
        "Cache-Control": "private, max-age=3600",
        "X-Content-Type-Options": "nosniff",
    }
    if range_header is None:
        return Response(content=data, media_type=mime, headers=headers)
    try:
        start, end = _range(range_header, len(data))
    except (ValueError, OverflowError):
        return Response(
            status_code=416, headers={"Content-Range": f"bytes */{len(data)}"}
        )
    headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
    return Response(
        content=data[start : end + 1], status_code=206, media_type=mime, headers=headers
    )
