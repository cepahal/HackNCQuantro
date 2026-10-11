import importlib
import unittest
from unittest.mock import Mock, call, patch

from backend.services.reporting.run_analysis import deliver_summary_after_save
from backend.services.shared.errors import ErrorCode, QuantroError, Stage
from backend.services.shared.types import ReportV2

stage = importlib.import_module("backend.services.reporting.send_summary")


def context_report():
    def media(identity, sentences):
        return {
            "post": {"post_id": identity, "post_url": f"https://example.com/{identity}",
                     "platform": "youtube", "author": {}, "engagement": {},
                     "media": {"media_id": identity, "mime_type": "video/mp4"},
                     "retrieval": {"retrieved_at": "2026-10-11T00:00:00Z", "provider": "test"}},
            "transcript": {"status": "partial", "processed_ranges": [{"start_seconds": 0.0, "end_seconds": 60.0}],
                           "sentences": sentences},
        }

    sentences = [{"sentence_id": identity, "start_seconds": start, "end_seconds": end, "text": text}
                 for identity, start, end, text in (
                     ("before_far", 1.0, 4.0, "Earlier introduction."),
                     ("before", 5.0, 10.0, "This promise has a condition."),
                     ("matched", 10.0, 20.0, "The matched clip words."),
                     ("after", 20.0, 25.0, "The condition is completing the repairs."),
                     ("after_far", 30.0, 35.0, "Later remarks."),
                 )]
    return ReportV2.model_validate({
        "job_id": "context_job", "outcome": "matched", "summary": "An inference that must not replace quoted context.",
        "submitted": media("clip", [{"sentence_id": "clip_1", "start_seconds": 0.0, "end_seconds": 10.0, "text": "The matched clip words."}]),
        "source": media("source", sentences),
        "alignment": {"status": "verified", "clip_media_id": "clip", "source_media_id": "source",
                      "matches": [{"match_id": "m1", "clip_start_seconds": 0.0, "clip_end_seconds": 10.0,
                                   "source_start_seconds": 10.0, "source_end_seconds": 20.0}],
                      "match_quality": {"confidence": {"level": "moderate", "method": "test", "basis": [], "limitations": []}},
                      "edit_analysis": {"status": "not_assessed", "scope": "test", "minimum_gap_seconds": 0.0, "counts": {}}},
        "context": {"source_media_id": "source", "windows": [{"context_id": "w1", "match_ids": ["m1"],
                    "requested_start_seconds": 0.0, "requested_end_seconds": 40.0, "coverage": "partial", "sentences": sentences}]},
    })


class ContextSummaryTests(unittest.TestCase):
    def test_quotes_nearest_observed_source_sentences_and_preserves_report(self):
        report = context_report()
        original = report.model_dump()
        self.assertEqual(stage.format_context_summary(report),
                         "Context before: This promise has a condition.\n\nContext after: The condition is completing the repairs.")
        self.assertEqual(report.model_dump(), original)

    def test_sentence_order_and_duplicate_windows_do_not_change_selection(self):
        report = context_report()
        window = report.context.windows[0]
        window.sentences.reverse()
        report.context.windows.append(window.model_copy(deep=True))
        self.assertEqual(stage.format_context_summary(report), stage.format_context_summary(context_report()))

    def test_missing_side_and_unavailable_windows_are_explicit(self):
        report = context_report()
        report.context.windows[0].sentences = [sentence for sentence in report.context.windows[0].sentences if sentence.start_seconds >= 10.0]
        self.assertEqual(stage.format_context_summary(report),
                         "Context before: Not available in the source transcript.\n\nContext after: The condition is completing the repairs.")
        report.context.windows[0].coverage = "unavailable"
        self.assertEqual(stage.format_context_summary(report),
                         "Context before: Not available in the source transcript.\n\nContext after: Not available in the source transcript.")

    def test_no_match_or_untrusted_source_never_uses_summary_or_submitted_words(self):
        mutations = (
            lambda report: setattr(report, "outcome", "no_verified_match"),
            lambda report: setattr(report.alignment, "status", "not_assessed"),
            lambda report: setattr(report.alignment.match_quality.confidence, "level", "low"),
            lambda report: setattr(report.context, "source_media_id", "other_source"),
            lambda report: setattr(report.alignment, "clip_media_id", "other_clip"),
            lambda report: setattr(report.source.post.media, "media_id", "clip"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                report = context_report()
                mutate(report)
                self.assertEqual(stage.format_context_summary(report),
                                 "Context before: No verified source context available.\n\nContext after: No verified source context available.")

    def test_unknown_match_ids_and_invalid_window_bounds_supply_no_context(self):
        for mutation in ("unknown_id", "outside_match", "reversed"):
            with self.subTest(mutation=mutation):
                report = context_report()
                window = report.context.windows[0]
                if mutation == "unknown_id":
                    window.match_ids = ["not_a_verified_match"]
                elif mutation == "outside_match":
                    window.requested_start_seconds = 15.0
                else:
                    window.requested_end_seconds = 0.0
                self.assertEqual(stage.format_context_summary(report),
                                 "Context before: Not available in the source transcript.\n\nContext after: Not available in the source transcript.")

    def test_sentences_overlapping_match_boundaries_are_never_called_before_or_after(self):
        report = context_report()
        report.source.transcript.sentences[1].end_seconds = 11.0
        report.source.transcript.sentences[3].start_seconds = 19.0
        report.context.windows[0].sentences = [sentence.model_copy(deep=True) for sentence in report.source.transcript.sentences[1:4]]
        self.assertEqual(stage.format_context_summary(report),
                         "Context before: Not available in the source transcript.\n\nContext after: Not available in the source transcript.")

    def test_noncanonical_or_unprocessed_sentences_cannot_supply_context(self):
        report = context_report()
        report.context.windows[0].sentences = [sentence.model_copy(deep=True) for sentence in report.source.transcript.sentences[1:4]]
        report.context.windows[0].sentences[0].text = "Invented replacement."
        report.source.transcript.processed_ranges[0].end_seconds = 20.0
        self.assertEqual(stage.format_context_summary(report),
                         "Context before: Not available in the source transcript.\n\nContext after: Not available in the source transcript.")

    def test_context_sentences_absent_from_canonical_source_are_unavailable(self):
        report = context_report()
        report.source.transcript.sentences = []
        self.assertEqual(stage.format_context_summary(report),
                         "Context before: Not available in the source transcript.\n\nContext after: Not available in the source transcript.")

    def test_multiple_matches_use_outer_source_bounds_and_never_internal_gaps(self):
        report = context_report()
        second = report.alignment.matches[0].model_copy(update={"match_id": "m2", "source_start_seconds": 30.0, "source_end_seconds": 35.0})
        report.alignment.matches.insert(0, second)
        report.context.windows[0].match_ids = ["m2", "m1"]
        self.assertEqual(stage.format_context_summary(report),
                         "Context before: This promise has a condition.\n\nContext after: Not available in the source transcript.")

    def test_long_source_sentence_is_explicitly_truncated_to_side_budget(self):
        report = context_report()
        report.source.transcript.sentences[1].text = "Known source words " * 80
        report.context.windows[0].sentences = [sentence.model_copy(deep=True) for sentence in report.source.transcript.sentences]
        before = stage.format_context_summary(report).split("\n\n", 1)[0].removeprefix("Context before: ")
        self.assertLessEqual(len(before), 650)
        self.assertTrue(before.endswith("…"))

    def test_active_and_legacy_delivery_use_context_lines_without_rewriting_saved_summary(self):
        report = context_report()
        expected = stage.format_context_summary(report)
        active = importlib.import_module("backend.services.resolution.run")
        legacy = importlib.import_module("backend.services.reporting.run_analysis")
        with patch.object(active, "send_summary") as send:
            active._deliver(report, "conversation_001", Mock(), None, None)
            self.assertEqual(send.call_args.args[1], expected)
        saved = {"job_id": report.job_id, "report_id": "report_context_job", "report_url": "https://example.com/reports/report_context_job",
                 "status": "completed", "completed_at": "2026-10-11T00:00:00Z"}
        store = Mock()
        store.get_report.return_value = report.model_dump(mode="json")
        with patch.object(legacy, "_call", return_value=saved), patch.object(legacy, "deliver_summary_after_save", return_value=(None, None)) as deliver:
            legacy._complete_and_deliver(report.job_id, report, "conversation_001", store, None, None, [], interpret=False)
            self.assertEqual(deliver.call_args.args[2], expected)
        self.assertEqual(report.summary, "An inference that must not replace quoted context.")


class PhotonReportingTests(unittest.TestCase):
    def setUp(self):
        self.arguments = (
            "conversation_001",
            "No verified source was found.",
            "https://quantro.example/reports/report_001",
        )
        self.accepted = {
            "conversation_id": self.arguments[0],
            "message_id": "provider_message_001",
            "status": "accepted",
        }

    def test_canonical_stage_forwards_stable_job_and_provider_receipt(self):
        tracker = Mock(spec=stage.InMemoryDeliveryTracker)
        with patch.object(stage, "send_summary_request", return_value=self.accepted) as request:
            for _ in range(2):
                result = stage.send_summary(
                    *self.arguments,
                    job_id="job_stable_001",
                    provider=stage.PhotonProvider(),
                    tracker=tracker,
                )
                self.assertEqual(result.model_dump(), self.accepted)

        self.assertEqual(request.call_args_list, [call(*self.arguments, job_id="job_stable_001")] * 2)
        tracker.check_and_record.assert_not_called()
        tracker.record_message_id.assert_not_called()
        tracker.release.assert_not_called()

    def test_normalized_bridge_failure_preserves_retryability_without_retrying(self):
        for code, retryable in (("DELIVERY_UNCERTAIN", False), ("CONVERSATION_UNAVAILABLE", True)):
            with self.subTest(code=code):
                error = {"error": {
                    "code": code,
                    "stage": "send_summary",
                    "message": "Delivery could not be confirmed.",
                    "retryable": retryable,
                }}
                with patch.object(stage, "send_summary_request", return_value=error) as request, self.assertRaises(QuantroError) as caught:
                    stage.send_summary(
                        *self.arguments, job_id="job_stable_001", provider=stage.PhotonProvider()
                    )
                self.assertIs(caught.exception.code, ErrorCode.MESSAGE_SEND_FAILED)
                self.assertIs(caught.exception.stage, Stage.SEND_SUMMARY)
                self.assertIs(caught.exception.retryable, retryable)
                self.assertEqual(caught.exception.message, error["error"]["message"])
                request.assert_called_once_with(*self.arguments, job_id="job_stable_001")

    def test_three_argument_contract_derives_stable_job_from_report_url(self):
        with patch.object(stage, "send_summary_request", return_value=self.accepted) as request:
            result = stage.send_summary(*self.arguments, provider=stage.PhotonProvider())
        request.assert_called_once_with(*self.arguments, job_id="001")
        self.assertEqual(result.model_dump(), self.accepted)

    def test_post_save_delivery_failure_returns_warning_without_retry(self):
        error = {"error": {
            "code": "DELIVERY_UNCERTAIN",
            "stage": "send_summary",
            "message": "Verify delivery before requesting another send.",
            "retryable": False,
        }}
        with patch.object(stage, "send_summary_request", return_value=error) as request, self.assertLogs("backend.services.reporting.run_analysis", level="WARNING") as logs:
            delivery, warning = deliver_summary_after_save(
                "job_stable_001", *self.arguments, provider=stage.PhotonProvider()
            )
        self.assertIsNone(delivery)
        self.assertEqual(warning, error["error"]["message"])
        self.assertIn("MESSAGE_SEND_FAILED", logs.output[0])
        request.assert_called_once_with(*self.arguments, job_id="job_stable_001")


if __name__ == "__main__":
    unittest.main()
