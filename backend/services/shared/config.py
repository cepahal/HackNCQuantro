"""One-time local configuration; explicitly supplied process values always win."""

import os
from functools import cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]


@cache
def load_environment(root: Path = _ROOT) -> None:
    from dotenv import load_dotenv

    for relative in (".secrets/.env", ".env.local", ".env", "backend/.env"):
        load_dotenv(root / relative, override=False, interpolate=False)


def environment() -> dict[str, str]:
    load_environment()
    return dict(os.environ)


def validate_search_configuration() -> None:
    """Fail before downloads or storage connections; no provider request is made."""
    modes = {os.getenv("APP_ENV", "").lower(), os.getenv("ENVIRONMENT", "").lower()}
    if "test" in modes and "production" not in modes:
        return
    validate_search_settings(dict(os.environ))


def validate_search_settings(settings: dict[str, str]) -> None:
    provider = settings.get("SEARCH_PROVIDER", "").strip().lower()
    required = {
        "exa": ("EXA_API_KEY",),
        "hybrid": ("EXA_API_KEY",),
        "serpapi": ("SEARCH_PROVIDER_API_KEY",),
        "brave": ("SEARCH_PROVIDER_API_KEY",),
        "google_cse": ("SEARCH_PROVIDER_API_KEY",),
        "tiger": (), "tigerdata": (),
    }
    if provider not in required:
        raise RuntimeError("Configure a supported SEARCH_PROVIDER before API startup: exa, hybrid, serpapi, brave, google_cse, or tiger.")
    if any(not settings.get(name, "").strip() for name in required[provider]):
        raise RuntimeError("The selected search provider is missing its API credential. Configure it before API startup.")
    if provider == "google_cse" and not (settings.get("SEARCH_GOOGLE_CSE_ID", "") or settings.get("GOOGLE_CSE_ID", "")).strip():
        raise RuntimeError("The selected search provider is missing its search engine identifier. Configure it before API startup.")
