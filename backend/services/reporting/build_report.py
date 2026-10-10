"""build_report stage of run_analysis.

Assembles the ReportV2 payload from the outputs of the earlier stages (post
retrieval, feature extraction, alignment, context selection, comparison) and runs
a sanitization pass so no local paths or secrets reach the saved or displayed
report. It copies upstream confidence verbatim and never derives credibility
from author or engagement fields.
"""

from collections.abc import Iterable
from typing import Any

from .errors import ErrorCode, QuantroError, Stage

_STRIPPED_KEYS = frozenset(
    {"local_path", "image_path", "file_path", "credentials", "api_key", "token", "secret"}
)
_FORBIDDEN_SUBSTRINGS = ("/tmp/", "file://")


def sanitize_report(report: Any) -> Any:
    """Return a copy of a JSON-like report with sensitive keys removed.

    Raises QuantroError if any remaining string value points at a local file.
    """
    if isinstance(report, dict):
        return {
            key: sanitize_report(value)
            for key, value in report.items()
            if key.lower() not in _STRIPPED_KEYS
        }
    if isinstance(report, list):
        return [sanitize_report(item) for item in report]
    if isinstance(report, str) and any(marker in report for marker in _FORBIDDEN_SUBSTRINGS):
        raise QuantroError(
            code=ErrorCode.PROCESSING_FAILED,
            stage=Stage.BUILD_REPORT,
            message="Report contains a local file reference",
        )
    return report


def merge_limitations(*groups: Iterable[str] | None) -> list[str]:
    """Order-preserving, de-duplicated union of limitation/warning lists."""
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group or ():
            if item not in seen:
                seen.add(item)
                merged.append(item)
    return merged
