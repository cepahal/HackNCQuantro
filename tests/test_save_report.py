import json
from datetime import datetime

import pytest
from pydantic import BaseModel

import backend.services.reporting.save_report as save_report_module
from backend.services.reporting.errors import ErrorCode, QuantroError, Stage
from backend.services.reporting.save_report import (
    InMemoryJobStore,
    InMemoryReportStore,
    SnowflakeReportStore,
    get_job_store,
    get_report_store,
    save_report_and_complete_job,
)
from backend.services.storage.tiger import TigerJobStore, TigerReportStore


class FakeReport(BaseModel):
    job_id: str
    schema_version: str = "2.1"


class CountingReportStore(InMemoryReportStore):
    def __init__(self) -> None:
        super().__init__()
        self.put_calls = 0

    def put(self, report_id: str, report_json: str) -> None:
        self.put_calls += 1
        super().put(report_id, report_json)


class FailingReportStore(InMemoryReportStore):
    def put(self, report_id: str, report_json: str) -> None:
        raise QuantroError(code="STORAGE_FAILED", stage="save_report_and_complete_job")


@pytest.fixture
def stores():
    return CountingReportStore(), InMemoryJobStore()


def test_happy_path_returns_expected_shape(stores, monkeypatch):
    monkeypatch.setenv("REPORT_BASE_URL", "https://quantro.tech/")
    report_store, job_store = stores

    result = save_report_and_complete_job(
        "job_123", FakeReport(job_id="job_123"), report_store=report_store, job_store=job_store
    )

    payload = result.model_dump()
    assert set(payload) == {"job_id", "status", "report_id", "report_url", "completed_at"}
    assert payload["job_id"] == "job_123"
    assert payload["status"] == "completed"
    assert payload["report_id"] == "report_job_123"
    assert payload["report_url"] == "https://quantro.tech/reports/report_job_123"
    assert payload["completed_at"].endswith("Z")
    assert datetime.fromisoformat(payload["completed_at"]).utcoffset().total_seconds() == 0
    assert json.loads(report_store.get("report_job_123")) == {"job_id": "job_123", "schema_version": "2.1"}
    assert job_store.get_status("job_123") == "completed"


def test_report_base_url_falls_back_when_unset(stores, monkeypatch):
    monkeypatch.delenv("REPORT_BASE_URL", raising=False)
    report_store, job_store = stores

    result = save_report_and_complete_job(
        "job_1", FakeReport(job_id="job_1"), report_store=report_store, job_store=job_store
    )

    assert result.report_url == "https://app.example/reports/report_job_1"


def test_repeated_calls_are_idempotent(stores):
    report_store, job_store = stores
    report = FakeReport(job_id="job_123")

    first = save_report_and_complete_job("job_123", report, report_store=report_store, job_store=job_store)
    second = save_report_and_complete_job("job_123", report, report_store=report_store, job_store=job_store)

    assert first.report_id == second.report_id == "report_job_123"
    assert list(report_store._reports) == ["report_job_123"]


def test_job_id_mismatch_raises_processing_failed(stores):
    report_store, job_store = stores

    with pytest.raises(QuantroError) as caught:
        save_report_and_complete_job(
            "job_123", FakeReport(job_id="job_999"), report_store=report_store, job_store=job_store
        )

    assert caught.value.code is ErrorCode.PROCESSING_FAILED
    assert caught.value.stage is Stage.SAVE_REPORT_AND_COMPLETE_JOB
    assert caught.value.message == "job_id mismatch"
    assert report_store.put_calls == 0
    assert job_store.get_status("job_123") is None


def test_store_failure_leaves_job_not_completed():
    job_store = InMemoryJobStore()

    with pytest.raises(QuantroError) as caught:
        save_report_and_complete_job(
            "job_123", FakeReport(job_id="job_123"), report_store=FailingReportStore(), job_store=job_store
        )

    assert caught.value.code is ErrorCode.STORAGE_FAILED
    assert job_store.get_status("job_123") is None


def test_unexpected_store_exception_is_normalized_to_storage_failed():
    class BrokenStore(InMemoryReportStore):
        def put(self, report_id: str, report_json: str) -> None:
            raise ConnectionError("db down")

    job_store = InMemoryJobStore()
    with pytest.raises(QuantroError) as caught:
        save_report_and_complete_job(
            "job_123", FakeReport(job_id="job_123"), report_store=BrokenStore(), job_store=job_store
        )

    assert caught.value.code is ErrorCode.STORAGE_FAILED
    assert caught.value.retryable is True
    assert job_store.get_status("job_123") is None


def test_report_store_selection_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TIGER_DATABASE_URL", f"sqlite:///{tmp_path / 'tiger.db'}")
    save_report_module._shared_tiger_store.cache_clear()
    monkeypatch.delenv("REPORT_STORE", raising=False)
    assert isinstance(get_report_store(), TigerReportStore)
    assert isinstance(get_job_store(), TigerJobStore)
    save_report_module._shared_tiger_store().close()
    save_report_module._shared_tiger_store.cache_clear()

    monkeypatch.setenv("REPORT_STORE", "memory")
    assert isinstance(get_report_store(), InMemoryReportStore)
    assert isinstance(get_job_store(), InMemoryJobStore)

    monkeypatch.setenv("REPORT_STORE", "snowflake")
    assert isinstance(get_report_store(), SnowflakeReportStore)

    monkeypatch.setenv("REPORT_STORE", "postgres")
    with pytest.raises(ValueError):
        get_report_store()
