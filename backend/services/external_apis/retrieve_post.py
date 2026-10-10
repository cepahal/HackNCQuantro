"""Synchronous, bounded public-post retrieval for the media-processing pipeline."""

import ipaddress
import json
import logging
import math
import shutil
import struct
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from ..shared.contracts import contract_input, validate_contract_result
from ..shared.errors import ErrorCode, QuantroError, Stage
from ..shared.errors import Stage as _ContractStage
from ..shared.errors import normalize_exception as _normalize_contract_exception
from ..shared.types import (
    Author,
    Engagement,
    MediaAsset,
    Post,
)
from ..shared.types import Post as _ReturnModel
from .tigerdata_storage import TigerDataPostStore

logger = logging.getLogger(__name__)

_PLATFORMS = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
    "instagram.com": "instagram",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "vimeo.com": "vimeo",
    "reddit.com": "reddit",
    "redd.it": "reddit",
}


class RetrievalError(QuantroError):
    """Pipeline failure; use as_dict() at the orchestrator boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        status_code: int = 502,
    ) -> None:
        normalized_code = code if code in ErrorCode._value2member_map_ else "MEDIA_UNAVAILABLE"
        super().__init__(normalized_code, Stage.RETRIEVE_POST, message, retryable)
        self.status_code = status_code

    def as_dict(self) -> dict[str, dict[str, str | bool]]:
        return {
            "error": {
                "code": self.code,
                "stage": self.stage,
                "message": self.message,
                "retryable": self.retryable,
            }
        }


def _validate_url(url: str) -> tuple[str, str]:
    if not isinstance(url, str) or not url.strip() or len(url) > 8192:
        raise RetrievalError(
            "INVALID_URL", "Provide a valid public post URL.", status_code=400
        )
    url = url.strip()
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80, 443}
            or any(c.isspace() or ord(c) < 32 for c in url)
            or "\\" in url
        ):
            raise ValueError("invalid URL")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            if "." not in hostname or hostname.endswith(
                (".localhost", ".local", ".internal")
            ):
                raise ValueError("local hostname") from None
        else:
            if not address.is_global:
                raise ValueError("private address")
    except ValueError:
        raise RetrievalError(
            "INVALID_URL", "Provide a valid public post URL.", status_code=400
        ) from None
    for domain, platform in _PLATFORMS.items():
        if hostname == domain or hostname.endswith("." + domain):
            return url, platform
    if parsed.path.lower().endswith((".mp4", ".jpg", ".jpeg")):
        return url, "direct"
    raise RetrievalError(
        "UNSUPPORTED_PLATFORM", "This post platform is not supported.", status_code=400
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _first_text(info: dict[str, Any], *names: str) -> str | None:
    for name in names:
        if (value := _text(info.get(name))) is not None:
            return value
    return None


def _count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if (
        isinstance(value, float)
        and math.isfinite(value)
        and value >= 0
        and value.is_integer()
    ):
        return int(value)
    return None


def _first_count(info: dict[str, Any], *names: str) -> int | None:
    for name in names:
        if (value := _count(info.get(name))) is not None:
            return value
    return None


def _number(value: object, *, positive: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        return None
    return number


def _source_url(value: object) -> str | None:
    """Keep optional provenance URLs public and credential-free, without fetching them."""
    url = _text(value)
    if (
        url is None
        or len(url) > 8192
        or "\\" in url
        or any(c.isspace() or ord(c) < 32 for c in url)
    ):
        return None
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        if (
            parts.scheme not in {"http", "https"}
            or not host
            or parts.username is not None
            or parts.password is not None
            or parts.port not in {None, 80, 443}
            or host.endswith((".local", ".localhost", ".internal"))
        ):
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return url if "." in host else None
        return url if address.is_global and not address.is_multicast else None
    except ValueError:
        return None


def _has_audio(info: dict[str, Any], *, is_image: bool) -> bool | None:
    if is_image:
        return False
    if isinstance(info.get("has_audio"), bool):
        return bool(info["has_audio"])
    codec = _text(info.get("acodec"))
    if codec is None or codec.lower() == "unknown":
        return None
    return codec.lower() != "none"


def _published_at(info: dict[str, Any]) -> str | None:
    # upload_date alone has no time/zone: do not invent midnight or use event/release dates.
    timestamp = info.get("timestamp")
    if not isinstance(timestamp, (float, int)) or isinstance(timestamp, bool):
        return None
    try:
        if not math.isfinite(timestamp):
            return None
        return (
            datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace("+00:00", "Z")
        )
    except (ValueError, OverflowError, OSError):
        return None


def _is_complete_mp4(path: Path, file_size: int) -> bool:
    """Reject HTML, partial containers and header-only files without loading video bytes."""
    boxes: set[bytes] = set()
    with path.open("rb") as media:
        position = 0
        while position < file_size:
            header = media.read(8)
            if len(header) != 8:
                return False
            size, kind = struct.unpack(">I4s", header)
            header_size = 8
            if size == 1:
                extended = media.read(8)
                if len(extended) != 8:
                    return False
                size = int.from_bytes(extended, "big")
                header_size = 16
            elif size == 0:
                size = file_size - position
            if size < header_size or position + size > file_size:
                return False
            boxes.add(kind)
            position += size
            media.seek(position)
    return {b"ftyp", b"moov", b"mdat"} <= boxes


def _result(
    url: str, platform: str, workspace: Path, stdout: str, max_bytes: int
) -> Post:
    try:
        info = json.loads(stdout)
    except (ValueError, TypeError):
        raise RetrievalError(
            "MEDIA_UNAVAILABLE", "The media downloader returned invalid metadata."
        ) from None
    if (
        not isinstance(info, dict)
        or info.get("_type", "video") not in ("video", "image")
        or "entries" in info
    ):
        raise RetrievalError(
            "MEDIA_UNAVAILABLE",
            "Submit a post containing one video, not a playlist or carousel.",
        )
    if info.get("is_live") is True or info.get("live_status") in (
        "is_live",
        "is_upcoming",
        "post_live",
    ):
        raise RetrievalError(
            "MEDIA_UNAVAILABLE", "Live and upcoming recordings are not supported."
        )
    # Fixed output name avoids trusting provider paths or filenames. Only completed media counts.
    is_image = info.get("_type") == "image"
    media_path = workspace / ("media.jpg" if is_image else "media.mp4")
    if media_path.is_symlink() or not media_path.is_file():
        raise RetrievalError(
            "MEDIA_UNAVAILABLE", "The submitted video could not be downloaded."
        )
    size = media_path.stat().st_size
    if size > max_bytes:
        raise RetrievalError(
            "MEDIA_TOO_LARGE",
            "The submitted video exceeds the download limit.",
            status_code=413,
        )
    with media_path.open("rb") as media_stream:
        header = media_stream.read(32)
    if is_image:
        with media_path.open("rb") as image:
            if size >= 2:
                image.seek(-2, 2)
            trailer = image.read(2)
        valid = (
            size > 4 and header.startswith(b"\xff\xd8\xff") and trailer == b"\xff\xd9"
        )
    else:
        valid = (
            len(header) >= 12
            and header[4:8] == b"ftyp"
            and _is_complete_mp4(media_path, size)
        )
    if not valid:
        raise RetrievalError(
            "MEDIA_UNAVAILABLE", "The downloaded file is not valid JPG or MP4 media."
        )
    verified: bool | None = None
    for name in ("channel_is_verified", "uploader_verified"):
        if isinstance(info.get(name), bool):
            verified = bool(info[name])
            break
    profile_url = _source_url(info.get("uploader_url")) or _source_url(
        info.get("channel_url")
    )
    author: Author = {
        "id": _first_text(info, "channel_id", "uploader_id"),
        "handle": _first_text(info, "uploader_id"),
        "display_name": _first_text(info, "uploader", "channel"),
        "profile_url": profile_url,
        "bio": _first_text(info, "uploader_description", "channel_description"),
        "verified": verified,
        "followers_count": _first_count(
            info, "channel_follower_count", "uploader_follower_count"
        ),
        "following_count": _first_count(info, "uploader_following_count"),
        "posts_count": _first_count(
            info, "uploader_video_count", "channel_video_count"
        ),
    }
    engagement: Engagement = {
        "views_count": _count(info.get("view_count")),
        "likes_count": _count(info.get("like_count")),
        "comments_count": _count(info.get("comment_count")),
        "shares_count": _first_count(info, "repost_count", "share_count"),
    }
    media: MediaAsset = {
        "media_id": "media_" + uuid4().hex,
        "local_path": str(media_path.resolve()),
        "mime_type": "image/jpeg" if is_image else "video/mp4",
        "playback_url": None,
        "duration_seconds": None if is_image else _number(info.get("duration")),
        "width": _count(info.get("width")) or None,
        "height": _count(info.get("height")) or None,
        "fps": None if is_image else _number(info.get("fps"), positive=True),
        "has_audio": _has_audio(info, is_image=is_image),
    }
    source_urls = [url]
    for source in (_source_url(info.get("webpage_url")),):
        if source is not None and source not in source_urls:
            source_urls.append(source)
    post: Post = {
        "post_id": "post_" + uuid4().hex,
        "candidate_id": None,
        "post_url": url,
        "platform": platform,
        "title": _text(info.get("title")),
        "caption": _text(info.get("description")),
        "author": author,
        "engagement": engagement,
        "published_at": _published_at(info),
        "media": media,
        "retrieval": {
            "retrieved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "provider": "direct-http" if is_image else "yt-dlp",
            "metadata_source_urls": source_urls,
            "unavailable_fields": [],
            "warnings": [],
        },
    }
    unavailable = [
        name for name in ("title", "caption", "published_at") if post[name] is None
    ]
    for prefix, fields in (
        ("author", author),
        ("engagement", engagement),
        ("media", media),
    ):
        unavailable.extend(
            f"{prefix}.{name}"
            for name, value in fields.items()
            if value is None and name != "playback_url"
        )
    post["retrieval"]["unavailable_fields"] = unavailable
    if unavailable:
        post["retrieval"]["warnings"] = [
            "Some optional metadata was unavailable; see unavailable_fields."
        ]
    return Post.model_validate(post).model_dump(mode="json")


class PostStorage(Protocol):
    def save_post(self, post: Post) -> None: ...


def retrieve_post(
    url: str,
    *,
    storage: PostStorage | None = None,
    output_dir: str | Path | None = None,
    timeout_seconds: float = 120,
    max_bytes: int = 100_000_000,
) -> Post:
    """Retrieve JPG/MP4, persist bytes and metadata in Tiger Data, then return the Post.

    Call from a worker thread in async code. Caller owns successful files until all
    downstream consumers finish. Failure removes this call's isolated workspace.
    Public single-video posts only; no cookies, API keys, playlists or DRM bypass.
    """
    # aligned with shared/types.py on 2026-10-10
    url = contract_input(url)
    try:
        url, platform = _validate_url(url)
        try:
            timeout = float(timeout_seconds)
        except (ValueError, TypeError, OverflowError):
            raise ValueError("timeout_seconds must be a positive finite limit") from None
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("max_bytes and timeout_seconds must be positive finite limits")
        workspace: Path | None = None
        base = Path(output_dir or tempfile.gettempdir()).resolve()
        try:
            base.mkdir(parents=True, exist_ok=True)
            workspace = Path(tempfile.mkdtemp(prefix="quantro-post-", dir=base)).resolve()
            command = [
                sys.executable,
                "-m",
                "backend.services.external_apis._retrieval_worker",
                "--ignore-config",
                "--no-plugin-dirs",
                "--no-cache-dir",
                "--no-playlist",
                "--playlist-items",
                "1",
                "--proxy",
                "",
                "--no-progress",
                "--quiet",
                "--no-warnings",
                "--socket-timeout",
                "15",
                "--retries",
                "1",
                "--extractor-retries",
                "1",
                "--max-filesize",
                str(max_bytes),
                "--compat-options",
                "prefer-legacy-http-handler,no-certifi",
                "--format",
                "b[ext=mp4][protocol=https]/b[ext=mp4][protocol=http]",
                "--match-filter",
                "!is_live & !is_upcoming",
                "--output",
                str(workspace / "media.%(ext)s"),
                "--dump-single-json",
                "--no-simulate",
                "--",
                url,
            ]
            if urlsplit(url).path.lower().endswith((".jpg", ".jpeg")):
                command = [
                    sys.executable,
                    "-m",
                    "backend.services.external_apis._retrieval_worker",
                    "--direct-image",
                    url,
                    "--output-file",
                    str(workspace / "media.jpg"),
                    "--max-bytes",
                    str(max_bytes),
                ]
            # Worker owns networking guards. No shell, user config, cookies, external ffmpeg, or parent env changes.
            completed = subprocess.run(
                command,
                cwd=Path(__file__).resolve().parents[3],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
            if completed.returncode != 0:
                # Never expose downloader stderr: it can contain signed URLs and provider responses.
                raise RetrievalError(
                    "MEDIA_UNAVAILABLE", "The submitted video could not be retrieved."
                )
            post = _result(url, platform, workspace, completed.stdout, max_bytes)
            owned_store: TigerDataPostStore | None = None
            try:
                if storage is None:
                    owned_store = TigerDataPostStore.from_env()
                    storage = owned_store
                storage.save_post(post)
            except Exception:  # noqa: BLE001 - redact connector diagnostics at the persistence boundary
                raise RetrievalError(
                    "STORAGE_FAILED",
                    "The retrieved media could not be saved to storage.",
                ) from None
            finally:
                if owned_store is not None:
                    try:
                        owned_store.close()
                    except Exception as close_error:  # noqa: BLE001 - preserve the committed save and redact diagnostics
                        logger.warning(
                            "Tiger Data connection cleanup failed (%s)",
                            type(close_error).__name__,
                        )

        except Exception as exc:
            if (
                workspace is not None
                and workspace.parent == base
                and workspace.name.startswith("quantro-post-")
            ):
                try:
                    shutil.rmtree(workspace)
                except OSError as cleanup_error:
                    logger.warning(
                        "Retrieval workspace cleanup failed (%s)",
                        type(cleanup_error).__name__,
                    )
            if isinstance(exc, RetrievalError):
                raise
            if isinstance(exc, subprocess.TimeoutExpired):
                raise RetrievalError(
                    "MEDIA_TIMEOUT",
                    "Video retrieval timed out.",
                    retryable=True,
                    status_code=504,
                ) from None
            if isinstance(exc, OSError):
                raise RetrievalError(
                    "STORAGE_FAILED", "The downloaded media could not be stored."
                ) from None
            raise
        return validate_contract_result(_ReturnModel, post, _ContractStage.RETRIEVE_POST, json_native=True)
    except Exception as exc:  # noqa: BLE001 - Public contract boundary redacts and normalizes failures.
        raise _normalize_contract_exception(_ContractStage.RETRIEVE_POST, exc) from None


def handle_post_trigger(
    url: str, **options: Any
) -> Post | dict[str, dict[str, str | bool]]:
    """Pipeline trigger boundary: return the PDF success object or shared error envelope."""
    try:
        return retrieve_post(url, **options)
    except RetrievalError as error:
        return error.as_dict()
