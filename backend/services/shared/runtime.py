"""Resolve required local media tools before the API accepts work."""

import os
import shutil
from pathlib import Path


def configure_media_tools() -> None:
    """Apply an explicit tools directory and fail fast for missing executables."""
    configured = os.getenv("MEDIA_TOOLS_DIR", "").strip()
    if configured:
        try:
            directory = Path(configured).expanduser().resolve(strict=True)
            if not directory.is_dir():
                raise ValueError("Not a directory")
        except (OSError, ValueError):
            raise RuntimeError(
                "MEDIA_TOOLS_DIR must name an existing directory. "
                "Set it to the directory containing ffmpeg and ffprobe."
            ) from None
        existing = os.environ.get("PATH", "")
        if existing.split(os.pathsep)[0] != str(directory):
            os.environ["PATH"] = str(directory) + (os.pathsep + existing if existing else "")

    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        raise RuntimeError(
            "Required media tools are unavailable: " + ", ".join(missing) + ". "
            "Install FFmpeg with ffprobe and set MEDIA_TOOLS_DIR to their directory, "
            "or add them to PATH, then restart the backend."
        )
