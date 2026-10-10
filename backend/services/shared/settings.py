"""Compatibility imports for the canonical one-time settings loader."""
from pathlib import Path
from .config import load_environment

ROOT = Path(__file__).resolve().parents[3]
ENV_FILES = (".secrets/.env", ".env.local", ".env", "backend/.env")

def load_env_files(root: Path = ROOT) -> None:
    load_environment(root)
