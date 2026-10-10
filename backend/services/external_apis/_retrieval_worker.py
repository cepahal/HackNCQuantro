"""Run yt-dlp in an isolated, public-HTTP-only child process.

Install guards before importing yt-dlp. DNS results are checked at connection
time, including redirects and media URLs, without changing the API process.
External downloaders are forbidden because they bypass Python's socket guards.
JPEG mode accepts a direct public image URL, not a social-post page. Its optional
dimensions describe encoded pixels; unavailable metadata remains null.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import runpy
import socket
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from socket import _GetAddrInfoResult

_getaddrinfo = socket.getaddrinfo


class UnsafeDestinationError(OSError):
    """A retrieval tried to leave the public HTTP network boundary."""


class ImageTooLargeError(ValueError):
    """The direct image would exceed the caller's download budget."""


def _is_public_ip(value: object) -> bool:
    if not isinstance(value, str) or "%" in value:
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return _is_public_ip(str(address.ipv4_mapped))
        if address.is_site_local:
            return False
        if address.sixtofour is not None and not _is_public_ip(str(address.sixtofour)):
            return False
        if address.teredo is not None and not all(
            _is_public_ip(str(part)) for part in address.teredo
        ):
            return False
    return address.is_global and not address.is_reserved and not address.is_multicast


def _public_getaddrinfo(
    host: bytes | str | None,
    port: bytes | str | int | None,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> _GetAddrInfoResult:
    addresses = _getaddrinfo(host, port, family, type, proto, flags)
    if not addresses or any(not _is_public_ip(address[4][0]) for address in addresses):
        raise UnsafeDestinationError(
            "Post retrieval requires a public network destination"
        )
    return addresses


def _audit_network(event: str, arguments: tuple[object, ...]) -> None:
    if event == "socket.connect":
        destination = arguments[1]
        # Numeric addresses only: resolving a hostname inside the C socket API
        # would bypass our getaddrinfo wrapper and allow a DNS rebinding race.
        if (
            not isinstance(destination, tuple)
            or not destination
            or not _is_public_ip(destination[0])
        ):
            raise UnsafeDestinationError(
                "Post retrieval requires a public network destination"
            )
    elif event == "urllib.Request":
        url = arguments[0]
        if not isinstance(url, str) or urlsplit(url).scheme.lower() not in {
            "http",
            "https",
        }:
            raise UnsafeDestinationError("Post retrieval supports HTTP and HTTPS only")
    elif event in {"subprocess.Popen", "os.system", "os.posix_spawn"}:
        raise UnsafeDestinationError("External post downloaders are disabled")
    elif event == "import":
        name = arguments[0]
        if isinstance(name, str) and name.split(".", 1)[0] in {"curl_cffi", "pycurl"}:
            # Optional C HTTP clients do not use Python's socket/audit guards.
            raise ImportError("Post retrieval requires a Python HTTP transport")


def _download_image(url: str, output_file: Path, max_bytes: int) -> None:
    if urlsplit(url).scheme.lower() not in {"http", "https"}:
        raise UnsafeDestinationError("Post retrieval supports HTTP and HTTPS only")
    if max_bytes < 1 or not output_file.is_absolute():
        raise ValueError(
            "Direct image retrieval requires an absolute path and positive byte limit"
        )
    request = Request(url, headers={"User-Agent": "Quantro/1.0"})
    with urlopen(request, timeout=15) as response:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = 0
            if declared_size > max_bytes:
                raise ImageTooLargeError("Post media exceeds maximum size")
        downloaded_bytes = 0
        # Exclusive creation protects an existing file even if a caller reuses a
        # path. Remove only this worker's partial file on a failed read/budget.
        with output_file.open("xb") as output:
            try:
                while chunk := response.read(64 * 1024):
                    downloaded_bytes += len(chunk)
                    if downloaded_bytes > max_bytes:
                        raise ImageTooLargeError("Post media exceeds maximum size")
                    output.write(chunk)
            except BaseException:
                output.close()
                output_file.unlink(missing_ok=True)
                raise


def _jpeg_dimensions(path: Path) -> tuple[int | None, int | None]:
    """Read JPEG SOF dimensions without decoding pixels or assuming EXIF rotation."""
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    try:
        with path.open("rb") as image:
            if image.read(2) != b"\xff\xd8":
                return None, None
            # Header metadata is optional: cap scanning rather than inspecting
            # unbounded application segments or attempting pixel decoding.
            while image.tell() < 1024 * 1024:
                if image.read(1) != b"\xff":
                    return None, None
                marker = image.read(1)
                while marker == b"\xff" and image.tell() < 1024 * 1024:
                    marker = image.read(1)
                if not marker or marker[0] in {0x00, 0xDA, 0xD9}:
                    return None, None
                if marker[0] in {0x01, 0xD8, *range(0xD0, 0xD8)}:
                    continue
                length_bytes = image.read(2)
                if len(length_bytes) != 2:
                    return None, None
                length = int.from_bytes(length_bytes, "big")
                if length < 2 or image.tell() + length - 2 > 1024 * 1024:
                    return None, None
                segment = image.read(length - 2)
                if len(segment) != length - 2:
                    return None, None
                if marker[0] in sof_markers:
                    if (
                        len(segment) < 6
                        or segment[5] < 1
                        or length != 8 + 3 * segment[5]
                    ):
                        return None, None
                    height = int.from_bytes(segment[1:3], "big")
                    width = int.from_bytes(segment[3:5], "big")
                    return (width, height) if width > 0 and height > 0 else (None, None)
    except OSError:
        return None, None
    return None, None


def _direct_image_main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(
        description="Download one public JPEG within a byte budget"
    )
    parser.add_argument("--direct-image", required=True)
    parser.add_argument("--output-file", required=True, type=Path)
    parser.add_argument("--max-bytes", required=True, type=int)
    options = parser.parse_args(arguments)
    try:
        _download_image(options.direct_image, options.output_file, options.max_bytes)
    except ImageTooLargeError:
        print("Post media exceeds maximum size", file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError):
        print("Direct image retrieval failed", file=sys.stderr)
        raise SystemExit(1) from None
    width, height = _jpeg_dimensions(options.output_file)
    print(
        json.dumps(
            {
                "_type": "image",
                "ext": "jpg",
                "description": None,
                "uploader": None,
                "upload_date": None,
                "width": width,
                "height": height,
            }
        )
    )


class SplitMediaTooLargeError(ValueError):
    """No available stream pair fits the shared download budget."""


def _budgeted_streams(info, max_bytes):
    """Prefer the best known-size safe pair that fits, including audio."""
    def size(stream):
        for key in ("filesize", "filesize_approx"):
            value = stream.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
                return value
        return None

    formats = info.get("formats") or info.get("requested_formats", [])
    safe = [f for f in formats if f.get("protocol") == "https"
            and urlsplit(f.get("url", "")).scheme == "https" and not f.get("has_drm")]
    videos = [f for f in safe if f.get("ext") == "mp4"
              and str(f.get("vcodec", "")).startswith("avc1")
              and 0 < (f.get("height") or 0) <= 720 and f.get("acodec") in (None, "none")]
    audios = [f for f in safe if f.get("ext") == "m4a" and f.get("vcodec") in (None, "none")]
    pairs = [(v, a) for v in videos for a in audios if size(v) and size(a)
             and size(v) + size(a) <= max_bytes * 0.98]
    if not pairs:
        # Metadata cannot rule out unknown-size alternatives. Keep their
        # downloads subject to the same progress and actual-size byte caps.
        pairs = [(v, a) for v in videos for a in audios
                 if (size(v) is None or size(a) is None)
                 and sum(value for value in (size(v), size(a)) if value is not None)
                 <= max_bytes * 0.98]
    if pairs:
        return list(max(pairs, key=lambda pair: (pair[0].get("height") or 0,
                    pair[0].get("tbr") or 0, pair[1].get("abr") or pair[1].get("tbr") or 0)))
    selected = info.get("requested_formats", [])
    if len(selected) == 2 and all(size(f) for f in selected) and sum(size(f) for f in selected) > max_bytes * 0.98:
        raise SplitMediaTooLargeError("Available streams exceed download limit")
    # Unknown sizes remain subject to the existing streaming byte limit.
    return selected


def _split_media_main(arguments: list[str]) -> None:
    """Download two direct MP4-family streams; never invoke a merger in this worker."""
    from yt_dlp import YoutubeDL

    parser = argparse.ArgumentParser()
    parser.add_argument("--split-media", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--max-bytes", type=int, required=True)
    args = parser.parse_args(arguments)
    if not args.workspace.is_absolute() or not args.workspace.is_dir() or args.max_bytes < 1:
        raise ValueError("Invalid split download workspace or budget")
    completed_bytes = 0

    def budget(progress):
        if completed_bytes + progress.get("downloaded_bytes", 0) > args.max_bytes:
            raise ValueError("Combined media exceeds download limit")

    options = {
        "format": "bv[ext=mp4][vcodec^=avc1][height<=720][protocol=https]+ba[ext=m4a][protocol=https]",
        "quiet": True, "no_warnings": True, "noprogress": True,
        "noplaylist": True, "playlist_items": "1", "proxy": "",
        "cachedir": False, "socket_timeout": 15, "retries": 1,
        "extractor_retries": 1, "max_filesize": args.max_bytes,
        "compat_opts": {"prefer-legacy-http-handler", "no-certifi"},
        "progress_hooks": [budget], "buffersize": 65536, "noresizebuffer": True,
    }
    with YoutubeDL(options) as downloader:
        info = downloader.extract_info(args.split_media, download=False)
        if not isinstance(info, dict) or "entries" in info or info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
            raise ValueError("Only single finished videos are supported")
        streams = _budgeted_streams(info, args.max_bytes)
        if len(streams) != 2:
            raise ValueError("Expected one video and one audio stream")
        for stream, name, extension in zip(streams, ("video.mp4", "audio.m4a"), ("mp4", "m4a")):
            if stream.get("protocol") != "https" or stream.get("ext") != extension or stream.get("has_drm"):
                raise ValueError("Unsupported split stream")
            if urlsplit(stream.get("url", "")).scheme != "https":
                raise ValueError("Split streams must use HTTPS")
            path = args.workspace / name
            if path.exists() or path.is_symlink():
                raise ValueError("Split output already exists")
            downloader.params["max_filesize"] = args.max_bytes - completed_bytes
            if downloader.params["max_filesize"] <= 0:
                raise ValueError("Combined media exceeds download limit")
            success, _ = downloader.dl(str(path), {**info, **stream, "requested_formats": None})
            if not success or path.is_symlink() or not path.is_file():
                raise ValueError("Stream download failed")
            completed_bytes += path.stat().st_size
            if completed_bytes > args.max_bytes:
                raise ValueError("Combined media exceeds download limit")
        info.update(ext="mp4", vcodec=streams[0].get("vcodec"), acodec=streams[1].get("acodec"),
                    width=streams[0].get("width"), height=streams[0].get("height"))
        print(json.dumps(downloader.sanitize_info(info)))


def main() -> None:
    for name in tuple(os.environ):
        if name.lower().endswith("_proxy"):
            os.environ.pop(name, None)
    socket.getaddrinfo = _public_getaddrinfo
    sys.addaudithook(_audit_network)
    if sys.argv[1:2] == ["--direct-image"]:
        _direct_image_main(sys.argv[1:])
        return
    if sys.argv[1:2] == ["--split-media"]:
        try:
            _split_media_main(sys.argv[1:])
        except SplitMediaTooLargeError:
            print("Available streams exceed download limit", file=sys.stderr)
            raise SystemExit(4) from None
        return
    runpy.run_module("yt_dlp", run_name="__main__")


if __name__ == "__main__":
    main()
