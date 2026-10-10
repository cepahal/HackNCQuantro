import json

from pydantic import BaseModel

from backend.services.reporting.run_analysis import (
    MULTIPLE_PLAUSIBLE_SOURCES,
    deliver_summary_after_save,
    select_source,
)
from backend.services.reporting.save_report import (
    InMemoryJobStore,
    InMemoryReportStore,
    save_report_and_complete_job,
)
from backend.services.reporting.send_summary import InMemoryDeliveryTracker, SendReceipt


class FakeReport(BaseModel):
    job_id: str
    schema_version: str = "2.1"


class CountingProvider:
    def __init__(self):
        self.calls = 0

    def send(self, conversation_id, text):
        self.calls += 1
        return SendReceipt(accepted=True, message_id=f"msg_{self.calls}")


class FailingProvider:
    def send(self, conversation_id, text):
        raise ConnectionError("provider down")


def test_message_failure_after_save_keeps_report_retrievable():
    report_store, job_store = InMemoryReportStore(), InMemoryJobStore()
    saved = save_report_and_complete_job(
        "job_1", FakeReport(job_id="job_1"), report_store=report_store, job_store=job_store
    )

    delivery, warning = deliver_summary_after_save(
        "job_1", "conv_1", "summary", saved.report_url,
        provider=FailingProvider(), tracker=InMemoryDeliveryTracker(),
    )

    assert delivery is None
    assert warning == "Summary delivery could not be confirmed. Verify its outcome before another send."
    assert json.loads(report_store.get(saved.report_id))["job_id"] == "job_1"
    assert job_store.get_status("job_1") == "completed"


def test_duplicate_send_for_same_job_and_conversation_issues_one_message():
    provider, tracker = CountingProvider(), InMemoryDeliveryTracker()

    first, _ = deliver_summary_after_save("job_1", "conv_1", "s", "u", provider=provider, tracker=tracker)
    second, _ = deliver_summary_after_save("job_1", "conv_1", "s", "u", provider=provider, tracker=tracker)

    assert provider.calls == 1
    assert first.message_id == second.message_id == "msg_1"


def test_no_conversation_id_skips_delivery():
    provider = CountingProvider()

    assert deliver_summary_after_save("job_1", None, "s", "u", provider=provider) == (None, None)
    assert provider.calls == 0


def test_select_source_prefers_highest_level_not_first():
    chosen, limitations = select_source([("cand_a", "low", 1.0), ("cand_b", "high", 0.4)])

    assert chosen == "cand_b"
    assert limitations == [MULTIPLE_PLAUSIBLE_SOURCES]


def test_select_source_breaks_level_ties_by_clip_coverage():
    chosen, limitations = select_source([("cand_a", "moderate", 0.5), ("cand_b", "moderate", 0.9)])

    assert chosen == "cand_b"
    assert limitations == [MULTIPLE_PLAUSIBLE_SOURCES]


def test_select_source_discloses_unresolved_tie():
    chosen, limitations = select_source([("cand_a", "high", 0.8), ("cand_b", "high", 0.8)])

    assert chosen == "cand_a"
    assert limitations == [MULTIPLE_PLAUSIBLE_SOURCES]


def test_select_source_with_no_verified_candidates():
    assert select_source([]) == (None, [])
