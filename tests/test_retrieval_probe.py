"""Probe the actual saved stream rather than extractor estimates."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.services.external_apis import retrieve_post as retrieval


def test_probe_parses_fractional_rate_and_actual_duration(monkeypatch, tmp_path):
    monkeypatch.setattr(retrieval.shutil, "which", lambda name: "ffprobe")
    payload = {
        "format": {"duration": "14.375"},
        "streams": [
            {
                "codec_type": "video",
                "width": 1080,
                "height": 1920,
                "avg_frame_rate": "30000/1001",
            },
            {"codec_type": "audio"},
        ],
    }
    calls = []

    def run(command, **options):
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(retrieval.subprocess, "run", run)
    result = retrieval._probe_download(tmp_path / "actual.mp4", is_image=False)
    assert result == {
        "duration_seconds": 14.375,
        "width": 1080,
        "height": 1920,
        "fps": pytest.approx(29.97002997),
        "has_audio": True,
    }
    assert calls[0][1]["timeout"] == 30
    assert "shell" not in calls[0][1]


def test_probe_unreadable_media_has_safe_processing_error(monkeypatch, tmp_path):
    monkeypatch.setattr(retrieval.shutil, "which", lambda name: "ffprobe")
    monkeypatch.setattr(
        retrieval.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, "{}", "private provider response"
        ),
    )
    with pytest.raises(retrieval.RetrievalError) as caught:
        retrieval._probe_download(tmp_path / "bad.mp4", is_image=False)
    assert caught.value.code == "PROCESSING_FAILED"
    assert "private" not in str(caught.value)


def test_probe_missing_binary_has_clear_error(monkeypatch, tmp_path):
    monkeypatch.setattr(retrieval.shutil, "which", lambda name: None)
    with pytest.raises(retrieval.RetrievalError, match="ffprobe is required"):
        retrieval._probe_download(tmp_path / "video.mp4", is_image=False)


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="system FFmpeg required",
)
def test_actual_mp4_is_measured_from_local_bytes(tmp_path: Path):
    path = tmp_path / "actual.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x120:r=25",
            "-t",
            "0.8",
            "-c:v",
            "mpeg4",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    measured = retrieval._probe_download(path, is_image=False)
    assert measured["duration_seconds"] == pytest.approx(0.8, abs=0.05)
    assert (measured["width"], measured["height"], measured["fps"]) == (160, 120, 25.0)
    assert measured["has_audio"] is False


def test_merge_rejects_aggregate_size_before_external_process(monkeypatch, tmp_path):
    (tmp_path / "media.mp4").write_bytes(b"v" * 6)
    (tmp_path / "media.m4a").write_bytes(b"a" * 6)

    def forbidden(*args, **kwargs):
        pytest.fail("oversized streams must not reach FFmpeg")

    monkeypatch.setattr(retrieval.subprocess, "run", forbidden)
    with pytest.raises(retrieval.RetrievalError, match="exceed the size limit"):
        retrieval._merge_local_streams(tmp_path, 10)


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="system FFmpeg required",
)
def test_actual_separate_streams_merge_into_audio_video_mp4(tmp_path):
    video = tmp_path / "media.mp4"
    audio = tmp_path / "media.m4a"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x120:r=25",
            "-t",
            "0.8",
            "-c:v",
            "mpeg4",
            str(video),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440",
            "-t",
            "0.8",
            "-c:a",
            "aac",
            str(audio),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    retrieval._merge_local_streams(tmp_path, 1_000_000)
    result = retrieval._probe_download(video, is_image=False)
    assert result["has_audio"] is True
    assert result["duration_seconds"] == pytest.approx(0.8, abs=0.1)
    assert (result["width"], result["height"]) == (160, 120)
    assert not audio.exists() and not (tmp_path / "merged.mp4").exists()
