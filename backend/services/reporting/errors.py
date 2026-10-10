"""Compatibility re-exports from the canonical services.shared.errors module."""

from ..shared.errors import (
    ErrorCode,
    QuantroError,
    Stage,
    normalize_exception,
)

__all__ = ["ErrorCode", "QuantroError", "Stage", "normalize_exception"]
