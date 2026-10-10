import io
import json
import os
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler

from backend.services.external_apis import photon


class Response:
    def __init__(self, body, status=200):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status = status
        self.read_limits = []

    def read(self, limit):
        self.read_limits.append(limit)
        return self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class PhotonTests(unittest.TestCase):
    def setUp(self):
        self.accepted = {"conversation_id": "conversation_001", "message_id": "message_001", "status": "accepted"}
        self.arguments = ("conversation_001", "No verified source was found.", "https://quantro.example/reports/report_001")
        self.environment = patch.dict(os.environ, {"PHOTON_BRIDGE_TOKEN": "test-only-bridge-token"}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.factory = patch.object(photon, "build_opener")
        self.build_opener = self.factory.start()
        self.addCleanup(self.factory.stop)
        self.opener = Mock()
        self.build_opener.return_value = self.opener
        self.opener.open.return_value = Response(self.accepted)

    def send(self, job_id="job_001"):
        return photon.send_summary_request(*self.arguments, job_id=job_id)

    def assert_safe_error(self, result, code=None):
        self.assertEqual(set(result), {"error"})
        self.assertEqual(set(result["error"]), {"code", "stage", "message", "retryable"})
        self.assertEqual(result["error"]["stage"], "send_summary")
        self.assertIs(result["error"]["retryable"], False)
        if code:
            self.assertEqual(result["error"]["code"], code)
        self.assertNotIn("test-only-bridge-token", json.dumps(result))
        self.assertNotIn("private-network-detail", json.dumps(result))

    def test_valid_request_has_fixed_loopback_headers_and_bounded_response(self):
        response = self.opener.open.return_value
        self.assertEqual(self.send(), self.accepted)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8766/send-summary")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-only-bridge-token")
        self.assertEqual(request.get_header("Idempotency-key"), "job_001")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), dict(zip(("conversation_id", "summary", "report_url"), self.arguments)))
        self.assertEqual(self.opener.open.call_args.kwargs, {"timeout": 15})
        self.assertEqual(response.read_limits, [16 * 1024 + 1])
        handlers = self.build_opener.call_args.args
        self.assertTrue(any(isinstance(handler, ProxyHandler) and handler.proxies == {} for handler in handlers))
        redirect = next(handler for handler in handlers if isinstance(handler, photon._NoRedirect))
        self.assertIsNone(redirect.redirect_request(request, None, 302, "Found", {}, "https://external.example"))
        self.opener.open.assert_called_once()

    def test_port_is_configurable_but_host_is_always_loopback(self):
        os.environ.update({"PHOTON_BRIDGE_PORT": "9123", "PHOTON_BASE_URL": "https://external.example"})
        self.assertEqual(self.send(), self.accepted)
        self.assertEqual(self.opener.open.call_args.args[0].full_url, "http://127.0.0.1:9123/send-summary")

    def test_missing_or_invalid_token_refuses_the_request(self):
        for token in ("", "  ", "token\r\nInjected: value", "x" * 4097):
            with self.subTest(token_length=len(token)):
                os.environ["PHOTON_BRIDGE_TOKEN"] = token
                self.assert_safe_error(self.send(), "PHOTON_NOT_CONFIGURED")
        self.opener.open.assert_not_called()

    def test_invalid_port_cannot_redirect_or_inject_headers(self):
        for port in ("", "0", "1023", "65536", "8766/path", "external.example", "8766\r\n", " 8766", "1.5"):
            with self.subTest(port=port):
                os.environ["PHOTON_BRIDGE_PORT"] = port
                self.assert_safe_error(self.send(), "PHOTON_NOT_CONFIGURED")
        self.opener.open.assert_not_called()

    def test_invalid_idempotency_keys_are_rejected_before_network(self):
        for job_id in ("", "job/001", "job 001", "job\r\nAuthorization: injected", "x" * 129, None):
            with self.subTest(job_id=job_id):
                self.assert_safe_error(self.send(job_id), "INVALID_IDEMPOTENCY_KEY")
        self.opener.open.assert_not_called()

    def test_invalid_summary_inputs_are_rejected_before_network(self):
        invalid = [
            ("", self.arguments[1], self.arguments[2]),
            ("conversation\n001", self.arguments[1], self.arguments[2]),
            (self.arguments[0], "", self.arguments[2]),
            (self.arguments[0], "x" * 2001, self.arguments[2]),
            (self.arguments[0], self.arguments[1], "file:///private"),
            (self.arguments[0], self.arguments[1], "https://user:password@example.com/report"),
            (self.arguments[0], self.arguments[1], "https://example.com/\nreport"),
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                result = photon.send_summary_request(*arguments, job_id="job_001")
                self.assert_safe_error(result, "INVALID_SUMMARY_INPUT")
        self.opener.open.assert_not_called()

    def test_malformed_oversized_or_mismatched_success_is_not_accepted(self):
        for payload in (
            b"private-network-detail not-json", b"\xff", b"x" * (16 * 1024 + 1), [], {},
            {**self.accepted, "conversation_id": "someone_else"},
            {**self.accepted, "message_id": " "},
            {**self.accepted, "message_id": "message\n001"},
            {**self.accepted, "status": "delivered"},
        ):
            with self.subTest(payload_type=type(payload).__name__):
                self.opener.open.return_value = Response(payload)
                self.assert_safe_error(self.send(), "INVALID_BRIDGE_RESPONSE")

    def test_success_drops_unexpected_provider_fields(self):
        self.opener.open.return_value = Response({**self.accepted, "private": "private-network-detail"})
        self.assertEqual(self.send(), self.accepted)

    def test_network_failures_never_echo_details_or_retry(self):
        for error in (URLError("private-network-detail test-only-bridge-token"), TimeoutError("private-network-detail"), RuntimeError("private-network-detail")):
            with self.subTest(error_type=type(error).__name__):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = error
                self.assert_safe_error(self.send(), "DELIVERY_UNCERTAIN")
                self.opener.open.assert_called_once()

    def test_known_bridge_errors_keep_safe_code_without_echoing_response(self):
        body = {"error": {"code": "DELIVERY_UNCERTAIN", "stage": "send_summary", "message": "private-network-detail test-only-bridge-token", "retryable": True}}
        self.opener.open.side_effect = HTTPError("http://127.0.0.1:8766/send-summary", 409, "private-network-detail", {}, io.BytesIO(json.dumps(body).encode()))
        self.assert_safe_error(self.send(), "DELIVERY_UNCERTAIN")
        self.opener.open.assert_called_once()

    def test_untrusted_or_invalid_http_error_bodies_use_safe_fallback(self):
        for payload in (
            b"private-network-detail test-only-bridge-token", b"x" * (16 * 1024 + 1),
            {"error": {"code": "PRIVATE_CODE", "stage": "send_summary", "message": "private-network-detail", "retryable": False}},
            {"error": {"code": "MESSAGE_SEND_FAILED", "stage": "provider", "message": "private-network-detail", "retryable": False}},
            {"error": {"code": "MESSAGE_SEND_FAILED", "stage": "send_summary", "message": "private-network-detail", "retryable": "yes"}},
        ):
            with self.subTest(payload_type=type(payload).__name__):
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.opener.open.side_effect = HTTPError("http://127.0.0.1:8766/send-summary", 502, "private-network-detail", {}, io.BytesIO(raw))
                self.assert_safe_error(self.send(), "MESSAGE_SEND_FAILED")


if __name__ == "__main__":
    unittest.main()
