"""save_report_and_complete_job stage of run_analysis.

Persists the finished report, then marks the job completed, and returns the
public report URL that send_summary will share. The report is always written
before the job status changes, so a storage failure leaves the job in its prior
state, and nothing here ever deletes a saved report.

Idempotency: report_id is derived deterministically from job_id
(report_<job_id>), so retries overwrite the same record instead of creating a
duplicate.
"""

import os
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import quote

from .errors import ErrorCode, QuantroError, Stage, normalize_exception
from .types import JobCompletionResult

_DEFAULT_REPORT_BASE_URL = "https://app.example"


class _Report(Protocol):
    job_id: str

    def model_dump_json(self) -> str: ...


class ReportStore(Protocol):
    def put(self, report_id: str, report_json: str) -> None: ...

    def get(self, report_id: str) -> str | None: ...


class InMemoryReportStore:
    """Process-local store for the hackathon demo; contents are lost on restart."""

    def __init__(self) -> None:
        self._reports: dict[str, str] = {}

    def put(self, report_id: str, report_json: str) -> None:
        self._reports[report_id] = report_json

    def get(self, report_id: str) -> str | None:
        return self._reports.get(report_id)


class SnowflakeReportStore:
    def put(self, report_id: str, report_json: str) -> None:
        raise NotImplementedError("TODO: persist reports via the Snowflake track integration")

    def get(self, report_id: str) -> str | None:
        raise NotImplementedError("TODO: read reports via the Snowflake track integration")


class JobStore(Protocol):
    def mark_completed(self, job_id: str, report_id: str, completed_at: str) -> None: ...

    def get_status(self, job_id: str) -> str | None: ...


class InMemoryJobStore:
    """Process-local job status until the operational database is wired in."""

    def __init__(self) -> None:
        self._statuses: dict[str, str] = {}

    def mark_completed(self, job_id: str, report_id: str, completed_at: str) -> None:
        self._statuses[job_id] = "completed"

    def get_status(self, job_id: str) -> str | None:
        return self._statuses.get(job_id)


_memory_report_store = InMemoryReportStore()
_memory_job_store = InMemoryJobStore()


def get_report_store() -> ReportStore:
    backend = os.getenv("REPORT_STORE", "memory").strip().lower()
    if backend == "memory":
        return _memory_report_store
    if backend == "snowflake":
        return SnowflakeReportStore()
    raise ValueError(f"REPORT_STORE must be 'memory' or 'snowflake', got {backend!r}")


def get_job_store() -> JobStore:
    return _memory_job_store


def report_id_for_job(job_id: str) -> str:
    return f"report_{job_id}"


def _report_url(report_id: str) -> str:
    base_url = os.getenv("REPORT_BASE_URL") or _DEFAULT_REPORT_BASE_URL
    return f"{base_url.rstrip('/')}/reports/{quote(report_id, safe='')}"


def save_report_and_complete_job(
    job_id: str,
    report: _Report,
    *,
    report_store: ReportStore | None = None,
    job_store: JobStore | None = None,
) -> JobCompletionResult:
    if report.job_id != job_id:
        raise QuantroError(
            code=ErrorCode.PROCESSING_FAILED,
            stage=Stage.SAVE_REPORT_AND_COMPLETE_JOB,
            message="job_id mismatch",
        )

    report_store = report_store if report_store is not None else get_report_store()
    job_store = job_store if job_store is not None else get_job_store()
    report_id = report_id_for_job(job_id)

    try:
        report_store.put(report_id, report.model_dump_json())
    except Exception as exc:
        raise normalize_exception(Stage.SAVE_REPORT_AND_COMPLETE_JOB, exc) from exc

    completed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    try:
        job_store.mark_completed(job_id, report_id, completed_at)
    except Exception as exc:
        raise normalize_exception(Stage.SAVE_REPORT_AND_COMPLETE_JOB, exc) from exc

    return JobCompletionResult(
        job_id=job_id,
        status="completed",
        report_id=report_id,
        report_url=_report_url(report_id),
        completed_at=completed_at,
    )
