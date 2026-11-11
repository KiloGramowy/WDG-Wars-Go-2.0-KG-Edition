"""Auth polling stays paced for ignored PINs, normal replies and failures."""

import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from plugins.wardrive_upload import WardriveUpload


class UnexpectedPoll(BaseException):
    """Bound regressions which accidentally skip the fake sleep forever."""


class AuthPollIntervalTests(unittest.TestCase):
    def setUp(self):
        self.plugin = SimpleNamespace(
            _auth_polling=True,
            _api_url="https://example.invalid/api/upload/",
            _api_key="synthetic-test-only",
            _auth_dismissed_token="dismissed-request",
            _auth_pin="",
            _auth_pin_expiry=0,
            _auth_current_token="",
            _log_add=Mock(),
            app=SimpleNamespace(msg=Mock()),
        )

    def run_poll(self, responses):
        events = []

        def opened(request, *, timeout):
            events.append("request")
            self.assertEqual(request.full_url,
                             "https://example.invalid/api/auth/pending/")
            self.assertEqual(timeout, 5)
            self.assertEqual(request.get_header("X-api-key"),
                             "synthetic-test-only")
            if not responses:
                raise UnexpectedPoll("A new poll began before the interval elapsed")
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return io.BytesIO(json.dumps(response).encode())

        def slept(seconds):
            events.append(("sleep", seconds))
            if not responses:
                self.plugin._auth_polling = False

        with patch("plugins.wardrive_upload._open", side_effect=opened) as network, \
                patch("time.sleep", side_effect=slept), \
                patch("time.time", return_value=1000):
            WardriveUpload._auth_poll_worker(self.plugin)
        return events, network

    def test_repeated_dismissed_request_waits_between_polls_without_redisplaying_pin(self):
        reply = dict(pending=True, token="dismissed-request", pin="123456")
        events, network = self.run_poll([reply, reply])
        self.assertEqual(events, ["request", ("sleep", 5),
                                  "request", ("sleep", 5)])
        self.assertEqual(network.call_count, 2)
        self.assertEqual(self.plugin._auth_pin, "")
        self.plugin._log_add.assert_not_called()
        self.plugin.app.msg.assert_not_called()

    def test_new_pin_keeps_existing_display_and_expiry(self):
        events, network = self.run_poll([
            dict(pending=True, token="new-request", pin=123456, expires=60)])
        self.assertEqual(events, ["request", ("sleep", 5)])
        network.assert_called_once()
        self.assertEqual(self.plugin._auth_pin, "123456")
        self.assertEqual(self.plugin._auth_current_token, "new-request")
        self.assertEqual(self.plugin._auth_pin_expiry, 1020)
        self.plugin._log_add.assert_called_once_with("LOGIN PIN: 123456", 10)
        self.plugin.app.msg.assert_called_once_with("[AUTH] Login PIN: 123456", 10)

    def test_no_pending_request_still_waits(self):
        events, _ = self.run_poll([dict(pending=False)])
        self.assertEqual(events, ["request", ("sleep", 5)])
        self.plugin._log_add.assert_not_called()
        self.plugin.app.msg.assert_not_called()

    def test_network_failure_waits_before_retry(self):
        events, _ = self.run_poll([OSError("synthetic offline"), dict(pending=False)])
        self.assertEqual(events, ["request", ("sleep", 5),
                                  "request", ("sleep", 5)])

    def test_invalid_pending_response_waits_before_retry(self):
        events, _ = self.run_poll([
            dict(pending=True, token="new-request"), dict(pending=False)])
        self.assertEqual(events, ["request", ("sleep", 5),
                                  "request", ("sleep", 5)])
        self.plugin.app.msg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
