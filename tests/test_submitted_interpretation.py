"""Bounded no-source interpretation adds prose, never fabricated verification."""

import copy
import json
from types import SimpleNamespace

import pytest

import backend.services.reporting.submitted_interpretation as module
from backend.services.shared.types import ReportV2


@pytest.fixture
def report():
    return ReportV2.model_validate({
        "job_id": "job_interpret", "outcome": "no_verified_match",
        "submitted": {
            "post": {"post_id": "post1", "post_url": "https://youtu.be/jNQXAC9IVRw",
                     "platform": "youtube", "author": {}, "engagement": {},
                     "media": {"media_id": "remote:youtube:abc", "mime_type": "video/x-youtube"},
                     "retrieval": {"retrieved_at": "2026-10-10T17:00:00Z", "provider": "remote"}},
            "transcript": {"status": "partial", "processed_ranges": [{"start_seconds": 0.0, "end_seconds": 4.0}],
                           "sentences": [{"sentence_id": "s1", "start_seconds": 0.0, "end_seconds": 2.0,
                                          "text": "We need to consider the effect of this policy."},
                                         {"sentence_id": "s2", "start_seconds": 2.0, "end_seconds": 4.0,
                                          "text": "The proposed spending change affects schools."}]}},
        "summary": "Source search did not establish an original recording.",
        "limitations": ["Only partial transcript evidence is available."],
    })


@pytest.fixture
def provider(monkeypatch):
    state = SimpleNamespace(calls=[], closed=False, error=None, close_error=False)
    state.payload = {"overview": "The excerpt argues that the proposed policy needs scrutiny because of its effects on schools.",
                     "cited_sentence_ids": ["s1", "s2"], "possible_references": [],
                     "caption_relation": "not_provided"}

    def close():
        state.closed = True
        if state.close_error:
            raise RuntimeError("secret cleanup diagnostic")

    state.close = close
    monkeypatch.setattr(module, "client_and_model", lambda stage: (state, "cheap-model"))

    def generate(client, model, prompt, schema, stage, **kwargs):
        state.calls.append({"client": client, "model": model, "prompt": prompt, "schema": schema, "stage": stage, **kwargs})
        if state.error:
            raise state.error
        return copy.deepcopy(state.payload)

    monkeypatch.setattr(module, "generate_json", generate)
    return state


def test_tentative_interpretation_preserves_evidence_and_calls_once(report, provider):
    before = report.model_dump(mode="json")
    result = module.interpret_submitted(report)

    assert result.summary.startswith("Assuming this is political speech,")
    assert "Transcript evidence:" in result.summary
    assert report.submitted.transcript.sentences[0].text in result.summary
    assert report.submitted.transcript.sentences[1].text in result.summary
    assert "s1" not in result.summary and "s2" not in result.summary
    assert "No submitted caption was provided" not in result.summary
    assert module.FACT_INTENT_LIMIT in result.summary
    assert before["summary"] in result.summary
    assert result.source is result.alignment is result.context is result.comparison is None
    assert result.submitted == report.submitted
    assert result.outcome == report.outcome
    assert report.model_dump(mode="json") == before
    assert len(provider.calls) == 1
    assert provider.calls[0]["max_attempts"] == 1
    assert provider.calls[0]["model"] == "cheap-model"
    assert "untrusted evidence data" in provider.calls[0]["prompt"]
    assert provider.closed


@pytest.mark.parametrize("relation", ["uncertain", "discusses_same_subject", "not_established"])
def test_caption_relation_is_tentative_without_context_verdict(relation, report, provider):
    report.submitted.post.caption = "This proposed policy affects school budgets."
    provider.payload["caption_relation"] = relation
    provider.payload["possible_references"] = ["A proposed public spending debate"]

    result = module.interpret_submitted(report)

    assert "unverified hypotheses only" in result.summary
    assert module.FACT_INTENT_LIMIT in result.summary
    assert result.comparison is None
    assert report.submitted.post.caption in provider.calls[0]["prompt"]


@pytest.mark.parametrize("change", [
    {"cited_sentence_ids": []}, {"cited_sentence_ids": ["invented"]},
    {"cited_sentence_ids": ["s1", "s1"]}, {"cited_sentence_ids": ["s1"] * 5},
    {"overview": ""}, {"overview": "x" * 1201}, {"overview": False},
    {"possible_references": ["x" * 201]}, {"possible_references": ["a"] * 4},
    {"caption_relation": "contradicted_by_context"}, {"caption_relation": "discusses_same_subject"},
    {"verdict": "disinformation"}, {"confidence": 1.0}, {"intent": "deceptive"},
    {"overview": "A generated local path /tmp/private should not escape."},
    {"overview": "Sentence s1 appears to discuss a proposed policy."},
    {"possible_references": ["As described in s2, a school budget debate"]},
])
def test_bad_or_overclaiming_shape_retains_prior_report(change, report, provider):
    provider.payload.update(change)

    result = module.interpret_submitted(report)

    assert result.summary == report.summary
    assert module.UNAVAILABLE_LIMIT in result.limitations
    assert result.submitted == report.submitted
    assert result.comparison is None
    assert provider.closed
    assert len(provider.calls) == 1


def test_missing_caption_cannot_be_reported_when_present(report, provider):
    report.submitted.post.caption = "A supplied caption"
    result = module.interpret_submitted(report)
    assert result.summary == report.summary
    assert module.UNAVAILABLE_LIMIT in result.limitations


def test_transcript_and_caption_budget_preserves_complete_sentences(report, provider):
    sentence = report.submitted.transcript.sentences[0]
    report.submitted.transcript.sentences = [sentence.model_copy(update={"sentence_id": f"s{i}", "text": "x" * 1000}) for i in range(20)]
    report.submitted.post.caption = "a" * 3000
    provider.payload.update(cited_sentence_ids=["s0"], caption_relation="uncertain")

    result = module.interpret_submitted(report)
    payload = json.loads(provider.calls[0]["prompt"].split("\n", 1)[1])

    assert len(payload["sentences"]) == 6
    assert sum(len(s["text"]) for s in payload["sentences"]) == 6000
    assert all(len(s["text"]) == 1000 for s in payload["sentences"])
    assert len(payload["caption"]) == 1000
    assert len(result.submitted.transcript.sentences) == 20
    assert any("bounded subset" in x for x in result.limitations)


def test_no_real_transcript_means_no_generation(report, provider):
    report.submitted.transcript.sentences = []
    result = module.interpret_submitted(report)
    assert result is report
    assert provider.calls == []
    assert not provider.closed


def test_unavailable_status_does_not_turn_stale_sentences_into_evidence(report, provider):
    report.submitted.transcript.status = "unavailable"
    result = module.interpret_submitted(report)
    assert result is report
    assert provider.calls == []


def test_generation_failure_is_safe_and_closes_client(report, provider, caplog):
    provider.error = RuntimeError("api_key=private-value file:///private")
    result = module.interpret_submitted(report)
    assert result.summary == report.summary
    assert module.UNAVAILABLE_LIMIT in result.limitations
    assert "private-value" not in json.dumps(result.model_dump(mode="json")) + caplog.text
    assert provider.closed
    assert len(provider.calls) == 1


def test_client_creation_failure_is_optional(report, provider, monkeypatch):
    def fail(stage):
        raise RuntimeError("private initialization diagnostics")

    monkeypatch.setattr(module, "client_and_model", fail)
    result = module.interpret_submitted(report)
    assert result.summary == report.summary
    assert module.UNAVAILABLE_LIMIT in result.limitations
    assert provider.calls == []


def test_cleanup_error_does_not_discard_valid_interpretation(report, provider, caplog):
    provider.close_error = True
    result = module.interpret_submitted(report)
    assert result.summary.startswith("Assuming this is political speech,")
    assert "secret cleanup diagnostic" not in caplog.text


def test_large_cited_sentences_are_not_truncated_into_fake_quotes(report, provider):
    report.submitted.transcript.sentences[0].text = "Long sentence " * 30
    provider.payload["cited_sentence_ids"] = ["s1"]
    result = module.interpret_submitted(report)
    assert result.summary.startswith("Assuming this is political speech,")
    assert "Transcript evidence:" not in result.summary
    assert "s1" not in result.summary


def test_sanitized_summary_return_is_used(report, provider, monkeypatch):
    monkeypatch.setattr(module, "sanitize_report", lambda value: {"summary": "Sanitized interpretation"})
    result = module.interpret_submitted(report)
    assert result.summary == "Sanitized interpretation"
