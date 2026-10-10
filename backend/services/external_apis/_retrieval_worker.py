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


def main() -> None:
    for name in tuple(os.environ):
        if name.lower().endswith("_proxy"):
            os.environ.pop(name, None)
    socket.getaddrinfo = _public_getaddrinfo
    sys.addaudithook(_audit_network)
    if sys.argv[1:2] == ["--direct-image"]:
        _direct_image_main(sys.argv[1:])
        return
    runpy.run_module("yt_dlp", run_name="__main__")


if __name__ == "__main__":
    main()
