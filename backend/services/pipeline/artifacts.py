"""Track only temporary directories created during this synchronous pipeline call."""
from contextlib import contextmanager
from contextvars import ContextVar
import logging
from pathlib import Path
import shutil

_owned: ContextVar[list[Path] | None] = ContextVar("quantro_owned_artifacts", default=None)
logger = logging.getLogger(__name__)


def register_directory(path: Path) -> None:
    """Called at directory creation, never with paths from reports or user input."""
    owned = _owned.get()
    if owned is not None:
        owned.append(path)


@contextmanager
def artifact_scope():
    owned: list[Path] = []
    token = _owned.set(owned)
    try:
        yield
    finally:
        _owned.reset(token)
        for path in reversed(owned):
            try:
                shutil.rmtree(path)
            except FileNotFoundError:
                pass
            except OSError as error:
                logger.warning("Temporary artifact cleanup failed (%s)", type(error).__name__)
