"""Reporting and orchestration for the Quantro pipeline.

Owns the final stages of run_analysis: building the v2 report, saving it and
completing the job, rendering it for display, and sending the iMessage summary.
"""

from .errors import ErrorCode, QuantroError, Stage, normalize_exception

__all__ = ["ErrorCode", "QuantroError", "Stage", "normalize_exception"]
