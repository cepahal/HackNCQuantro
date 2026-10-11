"""Adaptive formats remain guarded downloads followed by local-only muxing."""
import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest

from backend.services.external_apis import retrieve_post
from backend.services.resolution import download

URL = "https://www.youtube.com/watch?v=xm-eaneh31E"


@pytest.fixture
def split(monkeypatch):
    monkeypatch.setattr(retrieve_post, "_is_complete_mp4", lambda *args: True)
    monkeypatch.setattr(download, "_find_tool", lambda name: "/usr/bin/ffmpeg")
    calls = []
    def guarded(operation, payload):
        calls.append(operation)
        if operation == "download":
            raise download.MediaDownloadError("format_unavailable", "Missing format", URL)
        folder = Path(payload["out_dir"])
        (folder / "video.mp4").write_bytes(b"video")
        (folder / "audio.m4a").write_bytes(b"audio")
        return {"info": {"id": "source"}}
    monkeypatch.setattr(download, "_guarded_call", guarded)
    def mux(arguments, **kwargs):
        Path(arguments[-1]).write_bytes(b"merged")
    monkeypatch.setattr(download.subprocess, "run", Mock(side_effect=mux))
    return calls


def test_adaptive_fallback_muxes_only_owned_local_inputs(tmp_path, split):
    result = download._download_sync(URL, str(tmp_path), "video")
    assert split == ["download", "download_split"]
    assert Path(result["file_path"]).read_bytes() == b"merged"
    arguments = download.subprocess.run.call_args.args[0]
    assert arguments.count("-protocol_whitelist") == 2
    assert "https://" not in " ".join(arguments)
    assert arguments.count("mov") == 2


def test_real_ffmpeg_merges_adaptive_audio_and_video(tmp_path, monkeypatch):
    from backend.services.resolution import context_video

    streams = tmp_path / "streams"
    streams.mkdir()
    context_video._run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        "color=c=navy:s=320x240:r=30:d=2", "-c:v", "libx264",
                        str(streams / "video.mp4")])
    context_video._run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=2", "-c:a", "aac",
                        str(streams / "audio.m4a")])

    def guarded(operation, payload):
        if operation == "download":
            raise download.MediaDownloadError("format_unavailable", "Missing format", URL)
        for name in ("video.mp4", "audio.m4a"):
            (Path(payload["out_dir"]) / name).write_bytes((streams / name).read_bytes())
        return {"info": {"id": "source"}}

    monkeypatch.setattr(download, "_guarded_call", guarded)
    output = tmp_path / "download"
    output.mkdir()
    result = download._download_sync(URL, str(output), "video")
    info = context_video._probe(result["file_path"])
    assert {stream["codec_name"] for stream in info["streams"]} == {"h264", "aac"}
    assert abs(float(info["format"]["duration"]) - 2) < 0.1


@pytest.mark.parametrize("mode,code", [("audio", "format_unavailable"), ("video", "auth_required")])
def test_other_failures_do_not_retry_split(monkeypatch, tmp_path, mode, code):
    guarded = Mock(side_effect=download.MediaDownloadError(code, "No access", URL))
    monkeypatch.setattr(download, "_guarded_call", guarded)
    with pytest.raises(download.MediaDownloadError):
        download._download_sync(URL, str(tmp_path), mode)
    assert guarded.call_count == 1


def test_combined_budget_is_checked_before_mux(tmp_path, split, monkeypatch):
    monkeypatch.setattr(download, "_MAX_DOWNLOAD_BYTES", 9)
    with pytest.raises(download.MediaDownloadError) as caught:
        download._download_sync(URL, str(tmp_path), "video")
    assert caught.value.code == "file_too_large"
    download.subprocess.run.assert_not_called()


def test_invalid_stream_is_rejected_before_mux(tmp_path, split, monkeypatch):
    monkeypatch.setattr(retrieve_post, "_is_complete_mp4", lambda *args: False)
    with pytest.raises(download.MediaDownloadError):
        download._download_sync(URL, str(tmp_path), "video")
    download.subprocess.run.assert_not_called()


def test_failed_adaptive_download_cleans_owned_staging(tmp_path, monkeypatch):
    async def fail_download(*args):
        raise download.MediaDownloadError("download_failed", "Failed", URL)
    monkeypatch.setattr(download.asyncio, "to_thread", fail_download)
    with pytest.raises(download.MediaDownloadError):
        asyncio.run(download.download_short_media(URL, dest_dir=tmp_path))
    assert list((tmp_path / ".staging").iterdir()) == []


def test_split_worker_prefers_compact_streams_and_checks_progress_budget(tmp_path, monkeypatch):
    from types import SimpleNamespace
    video = {"url": "https://video.example/360", "format_id": "v360", "protocol": "https", "ext": "mp4", "vcodec": "avc1", "acodec": "none", "height": 360, "width": 640, "fps": 30, "filesize": 4}
    high = {**video, "format_id": "v720", "height": 720, "width": 1280}
    audio = {"url": "https://video.example/audio", "format_id": "audio", "protocol": "https", "ext": "m4a", "vcodec": "none", "acodec": "mp4a", "filesize": 3}
    observed = []
    class Downloader:
        def __init__(self, options):
            self.params = options
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def extract_info(self, url, download):
            assert download is False
            return {"id": "source", "formats": [high, video, audio], "height": 720}
        def dl(self, filename, selected):
            observed.append(selected["format_id"])
            assert selected["requested_formats"] is None
            for hook in self.params["progress_hooks"]:
                hook({"downloaded_bytes": 6})
            Path(filename).write_bytes(b"data")
            return True, None
        def sanitize_info(self, info):
            return info
    result = download._download_split_worker(SimpleNamespace(YoutubeDL=Downloader), URL, str(tmp_path))
    assert observed == ["v360", "audio"]
    assert result["info"]["height"] == 360 and result["info"]["width"] == 640
    assert result["info"]["acodec"] == "mp4a"
    # A second stream must account for bytes already downloaded, not reset its budget.
    monkeypatch.setattr(download, "_MAX_DOWNLOAD_BYTES", 8)
    next_dir = tmp_path / "budget"
    next_dir.mkdir()
    with pytest.raises(download.MediaDownloadError) as caught:
        download._download_split_worker(SimpleNamespace(YoutubeDL=Downloader), URL, str(next_dir))
    assert caught.value.code == "file_too_large"
