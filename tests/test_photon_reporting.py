import importlib
import unittest
from unittest.mock import Mock, call, patch

from backend.services.reporting.errors import ErrorCode, QuantroError, Stage
from backend.services.reporting.run_analysis import deliver_summary_after_save


stage = importlib.import_module("backend.services.reporting.send_summary")


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
                with patch.object(stage, "send_summary_request", return_value=error) as request:
                    with self.assertRaises(QuantroError) as caught:
                        stage.send_summary(
                            *self.arguments, job_id="job_stable_001", provider=stage.PhotonProvider()
                        )
                self.assertIs(caught.exception.code, ErrorCode.MESSAGE_SEND_FAILED)
                self.assertIs(caught.exception.stage, Stage.SEND_SUMMARY)
                self.assertIs(caught.exception.retryable, retryable)
                self.assertEqual(caught.exception.message, error["error"]["message"])
                request.assert_called_once_with(*self.arguments, job_id="job_stable_001")

    def test_missing_job_uses_helper_validation_and_is_not_retryable(self):
        error = {"error": {
            "code": "INVALID_IDEMPOTENCY_KEY",
            "stage": "send_summary",
            "message": "Provide a valid job ID for summary delivery.",
            "retryable": False,
        }}
        with patch.object(stage, "send_summary_request", return_value=error) as request:
            with self.assertRaises(QuantroError) as caught:
                stage.send_summary(*self.arguments, provider=stage.PhotonProvider())
        request.assert_called_once_with(*self.arguments, job_id="")
        self.assertIs(caught.exception.code, ErrorCode.MESSAGE_SEND_FAILED)
        self.assertIs(caught.exception.retryable, False)

    def test_post_save_delivery_failure_returns_warning_without_retry(self):
        error = {"error": {
            "code": "DELIVERY_UNCERTAIN",
            "stage": "send_summary",
            "message": "Verify delivery before requesting another send.",
            "retryable": False,
        }}
        with patch.object(stage, "send_summary_request", return_value=error) as request:
            with self.assertLogs("backend.services.reporting.run_analysis", level="WARNING") as logs:
                delivery, warning = deliver_summary_after_save(
                    "job_stable_001", *self.arguments, provider=stage.PhotonProvider()
                )
        self.assertIsNone(delivery)
        self.assertEqual(warning, error["error"]["message"])
        self.assertIn("MESSAGE_SEND_FAILED", logs.output[0])
        request.assert_called_once_with(*self.arguments, job_id="job_stable_001")


if __name__ == "__main__":
    unittest.main()
