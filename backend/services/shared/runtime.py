"""Resolve required local media tools before the API accepts work."""

import os
import shutil
import subprocess
from pathlib import Path


def configure_media_tools() -> None:
    """Resolve and exercise both tools before accepting an analysis request."""
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

    executables = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe")}
    missing = [name for name, executable in executables.items() if executable is None]
    if missing:
        raise RuntimeError(
            "Required media tools are unavailable: " + ", ".join(missing) + ". "
            "Install FFmpeg with ffprobe and set MEDIA_TOOLS_DIR to their directory, "
            "or add them to PATH, then restart the backend."
        )

    for name, executable in executables.items():
        assert executable is not None  # All missing tools were rejected above.
        try:
            result = subprocess.run(
                [executable, "-version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            usable = result.returncode == 0 and result.stdout.startswith(f"{name} version ".encode())
        except (OSError, subprocess.SubprocessError):
            usable = False
        if not usable:
            raise RuntimeError(
                f"Required media tool could not run correctly: {name}. "
                "Install a working FFmpeg build with ffprobe, check MEDIA_TOOLS_DIR or PATH, "
                "then restart the backend."
            ) from None
