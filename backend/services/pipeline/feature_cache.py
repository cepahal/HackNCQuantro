"""Internal candidate snapshot keys; no provider requests or credentials."""

import hashlib
import json
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def extraction_signature(options: dict) -> str:
    """Separate model/settings/options/code revisions before reusing a snapshot.

    Model names may be floating upstream aliases. This key describes the installed
    environment, not a verified remote weight revision; reuse is TTL-bounded.
    """
    services = Path(__file__).resolve().parents[1]
    revisions = {}
    for relative in (
        "processing/preprocessing.py",
        "specialist/transcription.py",
        "specialist/validation.py",
        "shared/types.py",
    ):
        revisions[relative] = hashlib.sha256(
            (services / relative).read_bytes()
        ).hexdigest()
    dependencies: dict[str, str | None] = {}
    for package in ("faster-whisper", "ctranslate2", "google-genai"):
        try:
            dependencies[package] = version(package)
        except PackageNotFoundError:
            dependencies[package] = None
    payload = {
        "revision": "candidate-snapshot-v1",
        "options": options,
        "provider": (os.getenv("TRANSCRIPTION_PROVIDER") or "whisper").strip().lower(),
        "whisper_model": os.getenv("WHISPER_MODEL") or "small",
        "gemini_model": os.getenv("GEMINI_MODEL") or "gemini-flash-latest",
        "code": revisions,
        "dependencies": dependencies,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def media_sha256(path: str) -> str:
    """Hash a retrieved local file with bounded memory, never a network URL."""
    with Path(path).open("rb") as media:
        return hashlib.file_digest(media, "sha256").hexdigest()
