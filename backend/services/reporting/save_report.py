"""save_report_and_complete_job stage of run_analysis.

Persists the finished report, then marks the job completed, and returns the
public report URL that send_summary will share. The report is always written
before the job status changes, so a storage failure leaves the job in its prior
state, and nothing here ever deletes a saved report.

Idempotency: report_id is derived deterministically from job_id
(report_<job_id>), so retries reuse the same record instead of creating a
duplicate. The Tiger Data store keeps the first saved report for a report_id.
"""

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import cache
from typing import Any, Protocol
from urllib.parse import quote

from ..shared.contracts import validate_contract_result
from ..shared.errors import ErrorCode, QuantroError, Stage, normalize_exception
from ..shared.errors import Stage as _ContractStage
from ..shared.errors import normalize_exception as _normalize_contract_exception
from ..shared.types import JobCompletionResult, ReportV2, parse_stage_output
from ..shared.types import JobCompletionResult as _ReturnModel
from ..storage.tiger import TigerJobStore, TigerReportStore, TigerStore

_DEFAULT_REPORT_BASE_URL = "http://localhost:3000"


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
        self._reports.setdefault(report_id, report_json)

    def get(self, report_id: str) -> str | None:
        return self._reports.get(report_id)


class JobStore(Protocol):
    def mark_completed(self, job_id: str, report_id: str, completed_at: str) -> None: ...

    def get_status(self, job_id: str) -> str | None: ...


class InMemoryJobStore:
    """Explicitly injected test job store; production persistence uses Tiger Data."""

    def __init__(self) -> None:
        self._statuses: dict[str, str] = {}

    def mark_completed(self, job_id: str, report_id: str, completed_at: str) -> None:
        self._statuses[job_id] = "completed"

    def get_status(self, job_id: str) -> str | None:
        return self._statuses.get(job_id)


@cache
def _shared_tiger_store() -> TigerStore:
    store = TigerStore.from_env()
    store.ensure_schema()
    return store


def _store_backend() -> str:
    backend = os.getenv("REPORT_STORE", "tiger").strip().lower() or "tiger"
    if backend != "tiger":
        raise ValueError("REPORT_STORE must be 'tiger'; inject an in-memory store explicitly in tests.")
    return backend


def get_report_store() -> ReportStore:
    _store_backend()
    return TigerReportStore(_shared_tiger_store())


def get_job_store() -> JobStore:
    _store_backend()
    return TigerJobStore(_shared_tiger_store())


def report_id_for_job(job_id: str) -> str:
    return f"report_{job_id}"


def _report_url(report_id: str) -> str:
    base_url = os.getenv("REPORT_BASE_URL") or _DEFAULT_REPORT_BASE_URL
    return f"{base_url.rstrip('/')}/reports/{quote(report_id, safe='')}"


def save_report_and_complete_job(
    job_id: str,
    report: _Report | Mapping[str, Any],
    *,
    report_store: ReportStore | None = None,
    job_store: JobStore | None = None,
) -> JobCompletionResult:
    # aligned with shared/types.py on 2026-10-10
    try:
        if isinstance(report, Mapping):
            report = parse_stage_output(ReportV2, report, Stage.SAVE_REPORT_AND_COMPLETE_JOB)
        if report.job_id != job_id:
            raise QuantroError(
                code=ErrorCode.PROCESSING_FAILED,
                stage=Stage.SAVE_REPORT_AND_COMPLETE_JOB,
                message="job_id mismatch",
            )

        try:
            report_store = report_store if report_store is not None else get_report_store()
            job_store = job_store if job_store is not None else get_job_store()
        except Exception as exc:
            raise normalize_exception(Stage.SAVE_REPORT_AND_COMPLETE_JOB, exc) from exc
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

        return validate_contract_result(_ReturnModel, JobCompletionResult(
            job_id=job_id,
            status="completed",
            report_id=report_id,
            report_url=_report_url(report_id),
            completed_at=completed_at,
        ), _ContractStage.SAVE_REPORT_AND_COMPLETE_JOB, json_native=False)
    except Exception as exc:  # noqa: BLE001 - Public contract boundary redacts and normalizes failures.
        raise _normalize_contract_exception(_ContractStage.SAVE_REPORT_AND_COMPLETE_JOB, exc) from None
