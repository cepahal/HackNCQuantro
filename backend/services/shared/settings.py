"""One place that fills process settings from local env files.

Real process environment (Render, CI, the shell) always wins: files only supply
keys that are unset. Earlier files take precedence over later ones. Loaded once
per process; call load_environment() from any entry point that needs settings.
"""

from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ENV_FILES = (".secrets/.env", ".env.local", ".env", "backend/.env")


def load_env_files(root: Path = ROOT) -> None:
    # Imported lazily: retrieval modules must stay importable without optional packages.
    from dotenv import load_dotenv

    for name in ENV_FILES:
        load_dotenv(root / name, override=False)


@cache
def load_environment() -> None:
    load_env_files()
