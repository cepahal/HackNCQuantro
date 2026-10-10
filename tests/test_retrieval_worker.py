"""The child downloader checks resolved destinations rather than trusting URLs."""

import json
import os
import runpy
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Self
from urllib.request import Request

import pytest

from backend.services.external_apis import _retrieval_worker as _worker

if TYPE_CHECKING:
    from socket import _GetAddrInfoResult


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.0.1",
        "169.254.169.254",
        "0.0.0.0",
        "100.64.0.1",
        "224.0.0.1",
        "::1",
        "::",
        "fe80::1",
        "fec0::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "2002:7f00:1::",
        "2606:4700:4700::1111%eth0",
        "localhost",
        "not-an-ip",
    ],
)
def test_rejects_nonpublic_addresses(address: str) -> None:
    assert not _worker._is_public_ip(address)
    with pytest.raises(_worker.UnsafeDestinationError):
        _worker._audit_network("socket.connect", (object(), (address, 443)))


@pytest.mark.parametrize(
    "address", ["1.1.1.1", "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"]
)
def test_allows_public_addresses(address: str) -> None:
    assert _worker._is_public_ip(address)
    _worker._audit_network("socket.connect", (object(), (address, 443)))


def _resolver(addresses: list[str]) -> Callable[..., "_GetAddrInfoResult"]:
    def resolve(
        host: bytes | str | None,
        port: bytes | str | int | None,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> "_GetAddrInfoResult":
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
            for address in addresses
        ]

    return resolve


def test_dns_rejects_mixed_public_private_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_worker, "_getaddrinfo", _resolver(["8.8.8.8", "127.0.0.1"]))
    with pytest.raises(_worker.UnsafeDestinationError):
        _worker._public_getaddrinfo("public-looking.example", 443)


def test_dns_preserves_public_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_worker, "_getaddrinfo", _resolver(["8.8.8.8", "1.1.1.1"]))
    results = _worker._public_getaddrinfo("public-looking.example", 443)
    assert [result[4][0] for result in results] == ["8.8.8.8", "1.1.1.1"]


def test_dns_preserves_lookup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(
        host: bytes | str | None,
        port: bytes | str | int | None,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> "_GetAddrInfoResult":
        raise socket.gaierror("DNS unavailable")

    monkeypatch.setattr(_worker, "_getaddrinfo", fail)
    with pytest.raises(socket.gaierror, match="DNS unavailable"):
        _worker._public_getaddrinfo("unresolved.example", 443)


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://example.com/a.mp4", "data:text/plain,hello"]
)
def test_rejects_non_http_requests(url: str) -> None:
    with pytest.raises(_worker.UnsafeDestinationError, match="HTTP"):
        _worker._audit_network("urllib.Request", (url, None, {}, "GET"))


def test_allows_https_requests() -> None:
    _worker._audit_network(
        "urllib.Request", ("https://example.com/video.mp4", None, {}, "GET")
    )


@pytest.mark.parametrize("event", ["subprocess.Popen", "os.system", "os.posix_spawn"])
def test_rejects_external_downloaders(event: str) -> None:
    with pytest.raises(_worker.UnsafeDestinationError, match="External"):
        _worker._audit_network(event, ())


@pytest.mark.parametrize("name", ["curl_cffi", "curl_cffi.requests", "pycurl"])
def test_rejects_c_network_transports(name: str) -> None:
    with pytest.raises(ImportError, match="Python HTTP transport"):
        _worker._audit_network("import", (name, None))


def test_main_installs_guards_before_running_yt_dlp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hooks: list[Callable[[str, tuple[object, ...]], None]] = []
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:3128")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:3128")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setattr(sys, "addaudithook", hooks.append)
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)

    def run_module(module_name: str, *, run_name: str) -> dict[str, object]:
        assert module_name == "yt_dlp"
        assert run_name == "__main__"
        assert hooks == [_worker._audit_network]
        assert socket.getaddrinfo is _worker._public_getaddrinfo
        assert not any(name.lower().endswith("_proxy") for name in os.environ)
        return {}

    monkeypatch.setattr(runpy, "run_module", run_module)
    _worker.main()


@pytest.mark.parametrize("host", ["127.0.0.1", "[::ffff:127.0.0.1]"])
def test_real_child_rejects_loopback_before_http_connection(host: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "backend.services.external_apis._retrieval_worker",
            "--ignore-config",
            "--no-plugin-dirs",
            "--no-cache-dir",
            "--proxy",
            "",
            "--simulate",
            "--",
            f"https://{host}/video.mp4",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode != 0
    assert "requires a public network destination" in result.stderr


class _ImageResponse:
    def __init__(self, content: bytes, declared_length: str | None = None) -> None:
        self.content = content
        self.offset = 0
        self.headers = (
            {} if declared_length is None else {"Content-Length": declared_length}
        )
        self.read_sizes: list[int] = []

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        chunk = self.content[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None


def _mock_image_response(
    monkeypatch: pytest.MonkeyPatch, response: _ImageResponse
) -> None:
    def open_image(request: Request, *, timeout: int) -> _ImageResponse:
        assert request.full_url == "https://images.example/photo.jpg"
        assert request.get_header("User-agent") == "Quantro/1.0"
        assert timeout == 15
        return response

    monkeypatch.setattr(_worker, "urlopen", open_image)


def test_direct_image_streams_in_bounded_chunks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    content = b"\xff\xd8\xff" + b"image-bytes" * 12000
    response = _ImageResponse(content, str(len(content)))
    _mock_image_response(monkeypatch, response)
    output_file = tmp_path / "post.jpg"

    _worker._download_image(
        "https://images.example/photo.jpg", output_file, len(content)
    )

    assert output_file.read_bytes() == content
    assert response.read_sizes and all(
        size == 64 * 1024 for size in response.read_sizes
    )


def test_direct_image_rejects_declared_oversize_before_creating_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    response = _ImageResponse(b"image", "1000")
    _mock_image_response(monkeypatch, response)
    output_file = tmp_path / "post.jpg"

    with pytest.raises(_worker.ImageTooLargeError):
        _worker._download_image("https://images.example/photo.jpg", output_file, 100)

    assert not output_file.exists()
    assert response.read_sizes == []


@pytest.mark.parametrize("declared_length", [None, "0", "wrong"])
def test_direct_image_enforces_actual_bytes_and_removes_partial_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, declared_length: str | None
) -> None:
    response = _ImageResponse(b"a" * (64 * 1024 + 1), declared_length)
    _mock_image_response(monkeypatch, response)
    output_file = tmp_path / "post.jpg"

    with pytest.raises(_worker.ImageTooLargeError):
        _worker._download_image(
            "https://images.example/photo.jpg", output_file, 64 * 1024
        )

    assert not output_file.exists()


def test_direct_image_never_overwrites_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mock_image_response(monkeypatch, _ImageResponse(b"image"))
    output_file = tmp_path / "post.jpg"
    output_file.write_bytes(b"teammate file")

    with pytest.raises(FileExistsError):
        _worker._download_image("https://images.example/photo.jpg", output_file, 100)

    assert output_file.read_bytes() == b"teammate file"


def test_direct_image_cli_prints_metadata_without_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _mock_image_response(monkeypatch, _ImageResponse(b"\xff\xd8\xffimage"))
    output_file = tmp_path / "post.jpg"

    _worker._direct_image_main(
        [
            "--direct-image",
            "https://images.example/photo.jpg",
            "--output-file",
            str(output_file),
            "--max-bytes",
            "100",
        ]
    )

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "_type": "image",
        "ext": "jpg",
        "description": None,
        "uploader": None,
        "upload_date": None,
        "width": None,
        "height": None,
    }
    assert captured.err == ""


def test_direct_image_cli_sanitizes_network_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(request: Request, *, timeout: int) -> _ImageResponse:
        raise OSError("private response body and token=secret")

    monkeypatch.setattr(_worker, "urlopen", fail)
    with pytest.raises(SystemExit):
        _worker._direct_image_main(
            [
                "--direct-image",
                "https://images.example/photo.jpg",
                "--output-file",
                str(tmp_path / "post.jpg"),
                "--max-bytes",
                "100",
            ]
        )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Direct image retrieval failed\n"


def _jpeg_header(width: int, height: int, marker: int = 0xC0) -> bytes:
    segment = (
        b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x01\x01\x11\x00"
    )
    return (
        b"\xff\xd8\xff"
        + bytes([marker])
        + (len(segment) + 2).to_bytes(2, "big")
        + segment
        + b"\xff\xd9"
    )


@pytest.mark.parametrize("marker", [0xC0, 0xC2, 0xC3, 0xC9, 0xCF])
def test_jpeg_dimensions_supports_sof_variants(tmp_path: Path, marker: int) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(_jpeg_header(1920, 1080, marker))

    assert _worker._jpeg_dimensions(image) == (1920, 1080)


def test_jpeg_dimensions_skips_metadata_and_marker_padding(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    header = _jpeg_header(640, 480)
    image.write_bytes(header[:2] + b"\xff\xe1\x00\x06exif\xff" + header[2:])

    assert _worker._jpeg_dimensions(image) == (640, 480)


@pytest.mark.parametrize(
    "content",
    [
        b"not jpeg",
        b"\xff\xd8",
        b"\xff\xd8\xff\xc0\x00\x01",
        b"\xff\xd8\xff\xc0\x00\x0b\x08\x00",
        b"\xff\xd8\xff\xda\x00\x08",
        b"\xff\xd8\xff\xd9",
        _jpeg_header(0, 480),
        _jpeg_header(640, 0),
    ],
)
def test_jpeg_dimensions_returns_unknown_for_invalid_headers(
    tmp_path: Path, content: bytes
) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(content)

    assert _worker._jpeg_dimensions(image) == (None, None)


def test_jpeg_dimensions_handles_missing_file(tmp_path: Path) -> None:
    assert _worker._jpeg_dimensions(tmp_path / "missing.jpg") == (None, None)


def test_direct_image_cli_reports_encoded_dimensions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _mock_image_response(monkeypatch, _ImageResponse(_jpeg_header(800, 600)))
    _worker._direct_image_main(
        [
            "--direct-image",
            "https://images.example/photo.jpg",
            "--output-file",
            str(tmp_path / "post.jpg"),
            "--max-bytes",
            "100",
        ]
    )

    metadata = json.loads(capsys.readouterr().out)
    assert metadata["width"] == 800
    assert metadata["height"] == 600
