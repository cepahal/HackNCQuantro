"""Atomic yt-dlp downloads with the existing public-network retrieval boundary."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

from ..external_apis.retrieve_post import RetrievalError, _validate_url

logger = logging.getLogger(__name__)
Platform = Literal["youtube", "instagram", "tiktok"]
Mode = Literal["video", "audio"]
_SUPPORTED_PLATFORMS: dict[str, Platform] = {
    "youtube": "youtube",
    "instagram": "instagram",
    "tiktok": "tiktok",
}


def cache_root() -> Path:
    """Use the mounted persistent volume when configured; never the source tree."""
    configured = os.getenv("MEDIA_CACHE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (
        (Path(os.getenv("APP_DATA_DIR") or ".data") / "resolution")
        .expanduser()
        .resolve()
    )


DOWNLOADS_DIR = cache_root() / "downloads"
_MAX_DOWNLOAD_BYTES = 100_000_000


class MediaDownloadError(Exception):
    def __init__(self, code: str, message: str, url: str, platform: str | None = None):
        super().__init__(message)
        self.code, self.message, self.url, self.platform = code, message, url, platform

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": True,
            "code": self.code,
            "message": self.message,
            "url": self.url,
            "platform": self.platform,
        }


def detect_platform(url: str) -> Platform:
    try:
        _, platform = _validate_url(url)
    except RetrievalError as exc:
        raise MediaDownloadError(exc.code.lower(), exc.message, str(url)) from exc
    if platform not in {"youtube", "instagram", "tiktok"}:
        raise MediaDownloadError(
            "unsupported_platform", "Use a YouTube, Instagram, or TikTok video.", url
        )
    return _SUPPORTED_PLATFORMS[platform]


def _find_tool(name: str) -> str | None:
    configured = os.getenv("MEDIA_TOOLS_DIR")
    if configured:
        for candidate in (Path(configured) / name, Path(configured) / f"{name}.exe"):
            if candidate.is_file():
                return str(candidate.resolve())
    return shutil.which(name)


def _build_ydl_opts(out_dir: str, mode: Mode) -> dict[str, Any]:
    # Download native streams inside the guarded worker; FFmpeg only sees local files.
    options: dict[str, Any] = {
        "format": "ba[ext=m4a]/ba/b[ext=mp4]/b" if mode == "audio" else "b[ext=mp4]/b",
        "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "restrictfilenames": True,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "overwrites": True,
        "cachedir": False,
        "proxy": "",
        "max_filesize": _MAX_DOWNLOAD_BYTES,
        "compat_opts": {"prefer-legacy-http-handler", "no-certifi"},
    }
    cookies = os.getenv("YTDLP_COOKIES_FILE")
    if cookies and Path(cookies).is_file():
        options["cookiefile"] = cookies
    for name in ("deno", "node"):
        if runtime := _find_tool(name):
            options["js_runtimes"] = {name: {"path": runtime}}
            break
    return options


def _classify_error(exc: BaseException) -> tuple[str, str]:
    diagnostic = str(exc).lower()
    for code, needles, message in (
        (
            "geo_restricted",
            ("not available in your country", "geo restrict"),
            "Video is unavailable in the server's region.",
        ),
        ("private", ("private video", "account is private"), "Video is private."),
        (
            "auth_required",
            ("sign in", "login required", "log in", "cookies", "429", "rate limit"),
            "The platform requires authentication or has blocked this request.",
        ),
        (
            "unavailable",
            ("video unavailable", "has been removed", "no longer available", "404"),
            "Video is unavailable or has been removed.",
        ),
        (
            "format_unavailable",
            ("requested format is not available",),
            "No supported downloadable media format is available.",
        ),
    ):
        if any(needle in diagnostic for needle in needles):
            return code, message
    return "download_failed", "The platform could not provide this video."


def _guarded_call(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Isolate DNS/redirect checks and audit hooks from the long-running API process."""
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "backend.services.resolution.download",
                "--worker",
                operation,
            ],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=Path(__file__).resolve().parents[3],
            timeout=20 if operation == "oembed" else 240,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaDownloadError(
            "download_failed", "Media retrieval timed out.", payload.get("url", "")
        ) from exc
    try:
        result = json.loads(completed.stdout)
    except (ValueError, TypeError):
        raise MediaDownloadError(
            "download_failed",
            "The media provider did not return a usable result.",
            payload.get("url", ""),
        ) from None
    if not isinstance(result, dict):
        raise MediaDownloadError(
            "download_failed",
            "The media provider returned an invalid result.",
            payload.get("url", ""),
        )
    if result.get("error"):
        if operation.startswith("captions"):
            from youtube_transcript_api import (
                NoTranscriptFound,
                RequestBlocked,
                TranscriptsDisabled,
                VideoUnavailable,
            )

            video_id = payload.get("video_id", "")
            exception_types = {
                "RequestBlocked": RequestBlocked,
                "IpBlocked": RequestBlocked,
                "TranscriptsDisabled": TranscriptsDisabled,
                "VideoUnavailable": VideoUnavailable,
            }
            exception_type = exception_types.get(str(result.get("type") or ""))
            if exception_type:
                raise exception_type(video_id)
            if result.get("type") in {
                "NoTranscriptFound",
                "NoCaptionsViaYtDlp",
                "InvalidVideoId",
            }:
                raise NoTranscriptFound(video_id, ["en"], {})
            if result.get("code") == "auth_required":
                raise RequestBlocked(video_id)
        raise MediaDownloadError(
            result.get("code", "download_failed"),
            result.get("message", "Media retrieval failed."),
            payload.get("url", ""),
        )
    if completed.returncode:
        raise MediaDownloadError(
            "download_failed", "Media retrieval failed.", payload.get("url", "")
        )
    return result


def _resolve_file_path(info: dict[str, Any], out_dir: str) -> str:
    root = Path(out_dir).resolve()
    candidates = [
        Path(item["filepath"])
        for item in info.get("requested_downloads") or []
        if item.get("filepath")
    ]
    candidates.extend(root.iterdir())
    files = [
        path
        for path in candidates
        if path.is_file()
        and not path.is_symlink()
        and path.resolve().parent == root
        and not path.name.endswith((".part", ".ytdl"))
    ]
    if not files:
        raise FileNotFoundError("No finished media file was written.")
    return str(max(files, key=lambda path: path.stat().st_size))


def _download_sync(url: str, out_dir: str, mode: Mode) -> dict[str, Any]:
    return _guarded_call("download", {"url": url, "out_dir": out_dir, "mode": mode})


def _extract_info_sync(url: str) -> dict[str, Any]:
    return _guarded_call("metadata", {"url": url})["info"]


def _metadata(info: dict[str, Any], platform: str) -> dict[str, Any]:
    duration = info.get("duration")
    valid_duration = (
        isinstance(duration, (float, int))
        and not isinstance(duration, bool)
        and math.isfinite(duration)
        and duration >= 0
    )
    return {
        "id": info.get("id"),
        "title": str(info.get("title") or "").strip(),
        "duration": float(duration)
        if isinstance(duration, (float, int)) and valid_duration
        else None,
        "platform": platform,
        "caption": info.get("description"),
        "author_name": info.get("uploader"),
        "author_id": info.get("channel_id") or info.get("uploader_id"),
        "author_url": info.get("channel_url") or info.get("uploader_url"),
        "width": info.get("width"),
        "height": info.get("height"),
        "fps": info.get("fps"),
    }


async def fetch_media_info(url: str) -> dict[str, Any]:
    platform = detect_platform(url)
    return _metadata(await asyncio.to_thread(_extract_info_sync, url.strip()), platform)


async def fetch_youtube_oembed(url: str) -> dict[str, Any]:
    """Small, fixed-host title lookup that does not need media format extraction."""
    if detect_platform(url) != "youtube":
        raise ValueError("YouTube metadata requires a YouTube URL.")
    return await asyncio.to_thread(_guarded_call, "oembed", {"url": url})


async def download_short_media(
    url: str, *, mode: Mode = "video", dest_dir: str | os.PathLike[str] | None = None
) -> dict[str, Any]:
    platform = detect_platform(url)
    if mode not in {"video", "audio"}:
        raise ValueError("mode must be video or audio")
    target = Path(dest_dir).resolve() if dest_dir is not None else DOWNLOADS_DIR
    target.mkdir(parents=True, exist_ok=True)
    staging = target / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    out_dir = tempfile.mkdtemp(prefix=f"{platform}_", dir=staging)
    try:
        result = await asyncio.to_thread(_download_sync, url.strip(), out_dir, mode)
        raw_source = Path(result["file_path"])
        source = raw_source.resolve()
        if (
            source.parent != Path(out_dir).resolve()
            or raw_source.is_symlink()
            or not source.is_file()
        ):
            raise MediaDownloadError(
                "download_failed",
                "The media provider returned an invalid file.",
                url,
                platform,
            )
        if source.stat().st_size > _MAX_DOWNLOAD_BYTES:
            raise MediaDownloadError(
                "file_too_large", "Media exceeds the download budget.", url, platform
            )
        info = result["info"]
        identity = re.sub(
            r"[^A-Za-z0-9_-]", "_", str(info.get("id") or uuid.uuid4().hex)
        )
        # Separate each completed artifact so concurrent requests never replace an in-use file.
        destination = (
            target / f"{platform}_{identity}_{uuid.uuid4().hex[:8]}{source.suffix}"
        )
        os.replace(source, destination)
        return {**_metadata(info, platform), "file_path": str(destination)}
    except MediaDownloadError:
        raise
    except Exception as exc:
        code, message = _classify_error(exc)
        raise MediaDownloadError(code, message, url, platform) from exc
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def delete_download(result: dict[str, Any]) -> None:
    if result.get("file_path"):
        path = Path(result["file_path"]).resolve()
        if path.parent == DOWNLOADS_DIR.resolve() and path.is_file():
            path.unlink()


def _worker(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    import socket

    from ..external_apis._retrieval_worker import _audit_network, _public_getaddrinfo

    # Preserve the established DNS and redirect guard for captions as well as media.
    for name in tuple(os.environ):
        if name.lower().endswith("_proxy"):
            os.environ.pop(name, None)
    socket.getaddrinfo = _public_getaddrinfo
    sys.addaudithook(_audit_network)
    if operation == "oembed":
        from urllib.request import urlopen

        detect_platform(payload["url"])
        endpoint = "https://www.youtube.com/oembed?" + urlencode(
            {"url": payload["url"], "format": "json"}
        )
        with urlopen(endpoint, timeout=10) as response:
            data = json.loads(response.read(128 * 1024))
        return {
            "title": data.get("title"),
            "author_name": data.get("author_name"),
            "author_url": data.get("author_url"),
        }
    import yt_dlp

    if operation in {"captions_api", "captions_ytdlp"}:
        from .transcribe import _fetch_with_transcript_api, _fetch_with_ytdlp

        provider = (
            _fetch_with_transcript_api
            if operation == "captions_api"
            else _fetch_with_ytdlp
        )
        return provider(payload["video_id"])
    if operation == "search":
        opts = _build_ydl_opts(tempfile.gettempdir(), "audio")
        opts.update(
            extract_flat="in_playlist", skip_download=True, ignore_no_formats_error=True
        )
        limit = max(1, min(10, int(payload.get("limit", 5))))
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"ytsearch{limit}:{str(payload['query'])[:1000]}", download=False
            )
            entries = (info or {}).get("entries") or []
            return {
                "entries": [
                    {
                        "id": item.get("id"),
                        "title": item.get("title"),
                        "duration": item.get("duration"),
                    }
                    for item in entries
                    if isinstance(item, dict)
                ]
            }
    url = payload["url"]
    detect_platform(url)
    output = payload.get("out_dir") or tempfile.gettempdir()
    opts = _build_ydl_opts(output, payload.get("mode", "audio"))
    if operation == "metadata":
        opts.update(skip_download=True, ignore_no_formats_error=True)
    else:

        def budget(progress: dict[str, Any]) -> None:
            if (progress.get("downloaded_bytes") or 0) > _MAX_DOWNLOAD_BYTES:
                raise ValueError("Media exceeds the download budget.")

        opts["progress_hooks"] = [budget]
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=operation == "download")
        if (
            not isinstance(info, dict)
            or info.get("_type") == "playlist"
            or info.get("entries") is not None
        ):
            raise ValueError("Use a single video URL.")
        if info.get("is_live") or info.get("live_status") in {
            "is_live",
            "is_upcoming",
            "post_live",
        }:
            raise ValueError("Use a finished recording.")
        result = {"info": ydl.sanitize_info(info)}
    if operation == "download":
        result["file_path"] = _resolve_file_path(result["info"], output)
    return result


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3 or sys.argv[1] != "--worker":
            raise ValueError("Worker arguments are required.")
        response = _worker(sys.argv[2], json.loads(sys.stdin.read()))
    except Exception as exc:  # noqa: BLE001 - child boundary must redact arbitrary provider failures.
        code, message = _classify_error(exc)
        response = {
            "error": True,
            "type": type(exc).__name__,
            "code": code,
            "message": message,
        }
    print(json.dumps(response, ensure_ascii=False))
