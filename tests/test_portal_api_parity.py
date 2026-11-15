"""LOCOSP portal API parity without live requests, real uploads or hardware."""

import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from plugins.wardrive_upload import USER_AGENT, WardriveUpload


class PortalApiParityTests(unittest.TestCase):
    def setUp(self):
        with patch.object(WardriveUpload, "_load_state"):
            self.plugin = WardriveUpload()
        self.plugin._api_url = "https://portal.example.invalid/api/upload/"
        self.plugin._api_key = "a" * 64
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plugin._state_file = lambda: Path(self.temp.name) / "state.json"

    def mock_api(self, payload):
        calls = []

        def opened(req, *, timeout):
            calls.append(req)
            self.assertEqual(timeout, 15)
            return io.BytesIO(json.dumps(payload).encode("utf-8"))

        return calls, patch("plugins.wardrive_upload._open", side_effect=opened)

    def test_rabbits_get_is_authenticated_and_read_only(self):
        rabbits = [{"id": "synthetic-rabbit"}]
        calls, mocked = self.mock_api({"ok": True, "rabbits": rabbits})
        with mocked:
            self.assertEqual(self.plugin.fetch_rabbits(), rabbits)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].full_url,
                         "https://portal.example.invalid/api/rabbits")
        self.assertEqual(calls[0].get_method(), "GET")
        self.assertEqual(calls[0].get_header("X-api-key"), "a" * 64)

    def test_rabbits_reject_malformed_response(self):
        with patch("plugins.wardrive_upload._open",
                   return_value=io.BytesIO(b'{"ok":true,"rabbits":{"bad":1}}')):
            self.assertEqual(self.plugin.fetch_rabbits(), [])

    def test_rabbit_snapshot_distinguishes_successful_empty_from_failure(self):
        calls, mocked = self.mock_api({"ok": True, "rabbits": [], "now": "synthetic-server-clock"})
        with mocked:
            self.assertEqual(self.plugin.fetch_rabbit_snapshot(),
                {"ok": True, "rabbits": [], "now": "synthetic-server-clock"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get_method(), "GET")
        with patch("plugins.wardrive_upload._open", side_effect=OSError("synthetic offline")):
            self.assertIsNone(self.plugin.fetch_rabbit_snapshot())

    def test_rabbit_snapshot_bounds_response_and_row_count(self):
        for payload in (b"x" * (2 * 1024 * 1024 + 1),
                        json.dumps({"ok": True, "rabbits": [{}] * 513}).encode(),
                        b'{"ok":true,"rabbits":["invalid-row"]}'):
            with patch("plugins.wardrive_upload._open", return_value=io.BytesIO(payload)):
                self.assertIsNone(self.plugin.fetch_rabbit_snapshot())

    def test_rabbit_snapshot_retires_a_response_from_the_previous_account(self):
        plugin = self.plugin
        class ChangedResponse(io.BytesIO):
            def read(self, *args):
                plugin._api_key = "b" * 64
                return super().read(*args)
        with patch("plugins.wardrive_upload._open",
                   return_value=ChangedResponse(b'{"ok":true,"rabbits":[{}]}')):
            self.assertIsNone(self.plugin.fetch_rabbit_snapshot())

    def test_rabbits_do_not_connect_without_key_or_after_shutdown(self):
        with patch("plugins.wardrive_upload._open") as opened:
            self.plugin._api_key = ""
            self.assertEqual(self.plugin.fetch_rabbits(), [])
            self.plugin._api_key = "a" * 64
            self.plugin._sync_stop.set()
            self.assertEqual(self.plugin.fetch_rabbits(), [])
            opened.assert_not_called()

    def test_game_state_has_no_gps_by_default(self):
        calls, mocked = self.mock_api({"ok": True, "rank": 7})
        with mocked:
            self.assertEqual(self.plugin.fetch_game_state()["rank"], 7)
        self.assertEqual(calls[0].full_url,
                         "https://portal.example.invalid/api/game-state")
        self.assertEqual(calls[0].get_header("X-api-key"), "a" * 64)

    def test_game_state_sends_explicit_coordinates_only(self):
        calls, mocked = self.mock_api({"ok": True, "territory": []})
        with mocked:
            self.assertTrue(self.plugin.fetch_game_state(
                36.123456789, -6.123456789)["ok"])
        self.assertEqual(calls[0].full_url,
            "https://portal.example.invalid/api/game-state?lat=36.123457&lng=-6.123457")

    def test_bad_coordinates_never_trigger_network(self):
        cases = [(1, None), (91, 0), (float("nan"), 2),
                 (0, 181), ("bad", 0)]
        for lat, lon in cases:
            with self.subTest(lat=lat, lon=lon), \
                    patch("plugins.wardrive_upload._open") as opened:
                self.assertIsNone(self.plugin.fetch_game_state(lat, lon))
                opened.assert_not_called()

    def test_pair_request_needs_no_existing_key(self):
        self.plugin._api_key = ""
        calls, mocked = self.mock_api(
            {"ok": True, "code": "AB1234", "poll": "synthetic-secret"})
        with mocked:
            self.assertEqual(self.plugin.request_pair("Synthetic")["code"], "AB1234")
        self.assertEqual(calls[0].get_method(), "POST")
        self.assertEqual(calls[0].full_url,
                         "https://portal.example.invalid/api/device-pair")
        self.assertEqual(json.loads(calls[0].data),
                         {"action": "request", "label": "Synthetic"})
        self.assertIsNone(calls[0].get_header("X-api-key"))

    def test_pair_poll_returns_reply_without_saving_key(self):
        for result in ({"ok": True, "pending": True},
                       {"ok": True, "api_key": "b" * 64}):
            calls, mocked = self.mock_api(result)
            with mocked, patch.object(self.plugin, "_save_secret") as saved:
                self.assertEqual(self.plugin.poll_pair("AB1234", "poll-secret"), result)
            saved.assert_not_called()
            self.assertEqual(json.loads(calls[0].data),
                             {"action": "poll", "code": "AB1234",
                              "poll": "poll-secret"})
        self.assertEqual(self.plugin._api_key, "a" * 64)

    def test_pair_poll_403_has_safe_error(self):
        error = HTTPError("https://portal.example.invalid/api/device-pair",
                          403, "forbidden", {}, io.BytesIO(b"{}"))
        with patch("plugins.wardrive_upload._open", side_effect=error):
            self.assertEqual(self.plugin.poll_pair("AB1234", "private-poll"),
                             {"ok": False, "error": "bad_request"})

    def test_pairing_with_missing_url_does_not_connect(self):
        self.plugin._api_url = ""
        with patch("plugins.wardrive_upload._open") as opened:
            self.assertIsNone(self.plugin.request_pair())
            self.assertEqual(self.plugin.poll_pair("c", "p"),
                             {"ok": False, "error": "unavailable"})
            opened.assert_not_called()

    def test_adopt_api_key_is_explicit_and_validated(self):
        key = "b" * 64
        with patch.object(self.plugin, "_save_secret") as saved, \
                patch.object(self.plugin, "_start_auth_poll") as polled:
            self.plugin.adopt_api_key(key)
            saved.assert_called_once_with("WARDRIVE_API_KEY", key)
            polled.assert_called_once()
        self.assertEqual(self.plugin._api_key, key)

    def test_invalid_key_and_failed_save_preserve_existing_key(self):
        for key in ("bad", "g" * 64, None, "a" * 63):
            with self.subTest(key=key), patch.object(self.plugin, "_save_secret") as saved:
                with self.assertRaises(ValueError):
                    self.plugin.adopt_api_key(key)
                saved.assert_not_called()
                self.assertEqual(self.plugin._api_key, "a" * 64)
        with patch.object(self.plugin, "_save_secret",
                          side_effect=OSError("synthetic denied")):
            with self.assertRaises(OSError):
                self.plugin.adopt_api_key("b" * 64)
        self.assertEqual(self.plugin._api_key, "a" * 64)

    def test_user_agent_has_locosp_portal_hardware_hint_on_linux(self):
        import platform
        import re

        self.assertTrue(USER_AGENT.startswith("WatchDogsGo/"))
        if platform.system() == "Linux":
            self.assertRegex(USER_AGENT,
                r"^WatchDogsGo/[0-9.]+ \(uConsole-[A-HJ-NP-Z2-9]{6}; Python/[0-9.]+\)$")
        else:
            self.assertNotIn("uConsole-", USER_AGENT)
        from plugins.wardrive_upload import _hw_id
        self.assertEqual(_hw_id(), _hw_id())

    def test_signed_upload_contains_local_game_xp_without_mutating_records(self):
        import hashlib
        import hmac
        from types import SimpleNamespace

        self.plugin.app = SimpleNamespace(xp=316308)
        original = {"networks": [{"ssid": "synthetic"}],
                    "aircraft": [], "meshcore_nodes": []}
        envelope = json.loads(self.plugin._sign_payload(original))
        decoded = json.loads(base64.b64decode(envelope["data"]))
        self.assertEqual(decoded, {**original, "game_xp": 316308})
        self.assertNotIn("game_xp", original)
        expected = hmac.new(self.plugin._api_key.encode(),
            (envelope["nonce"] + envelope["data"]).encode(),
            hashlib.sha256).hexdigest()
        self.assertEqual(envelope["sig"], expected)

    def test_empty_test_ping_and_missing_xp_keep_original_payload(self):
        from types import SimpleNamespace

        self.plugin.app = SimpleNamespace(xp=316308)
        empty = {"networks": [], "aircraft": [], "meshcore_nodes": []}
        ping = json.loads(self.plugin._sign_payload(empty))
        self.assertEqual(json.loads(base64.b64decode(ping["data"])), empty)

        self.plugin.app.xp = 0
        with_network = {"networks": [{"ssid": "synthetic"}],
                        "aircraft": [], "meshcore_nodes": []}
        packet = json.loads(self.plugin._sign_payload(with_network))
        self.assertEqual(json.loads(base64.b64decode(packet["data"])), with_network)

    def test_real_upload_request_contains_game_xp_and_uses_selected_account(self):
        from types import SimpleNamespace

        self.plugin.app = SimpleNamespace(xp=316308)
        records = {"networks": [{"ssid": "test-device"}],
                   "aircraft": [], "meshcore_nodes": []}
        observed = []

        def fake_post(request, *, timeout):
            observed.append((request, timeout))
            return io.BytesIO(b'{"ok":true,"imported":1}')

        with patch.object(self.plugin, "_wait_upload_slot", return_value=True), \
                patch("plugins.wardrive_upload._open", side_effect=fake_post):
            result, error = self.plugin._post_upload_batch(
                records, (self.plugin._api_url, self.plugin._api_key),
                lambda: True)

        self.assertIsNone(error)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(len(observed), 1)
        request, timeout = observed[0]
        self.assertEqual(timeout, 90)
        self.assertEqual(request.full_url, self.plugin._api_url)
        self.assertEqual(request.get_header("X-api-key"), self.plugin._api_key)
        envelope = json.loads(request.data)
        decoded = json.loads(base64.b64decode(envelope["data"]))
        self.assertEqual(decoded["game_xp"], 316308)
        self.assertEqual(decoded["networks"], records["networks"])
        self.assertNotIn("game_xp", records)

    def test_all_three_real_record_types_can_report_the_same_game_total(self):
        from types import SimpleNamespace

        self.plugin.app = SimpleNamespace(xp=12345)
        for group in ("networks", "aircraft", "meshcore_nodes"):
            with self.subTest(group=group):
                records = {"networks": [], "aircraft": [],
                           "meshcore_nodes": []}
                records[group] = [{"synthetic": True}]
                envelope = json.loads(self.plugin._sign_payload(records))
                payload = json.loads(base64.b64decode(envelope["data"]))
                self.assertEqual(payload["game_xp"], 12345)
                self.assertEqual(payload[group], records[group])

    def test_game_xp_keeps_signed_batches_within_same_size_limit(self):
        from types import SimpleNamespace

        self.plugin.app = SimpleNamespace(xp=999999)
        source = {"networks": [{"ssid": "x" * 80, "index": i}
                               for i in range(16)],
                  "aircraft": [], "meshcore_nodes": []}
        with patch("plugins.wardrive_upload.UPLOAD_BATCH_BYTES", 385):
            parts = [self.plugin._payload_slice(source, *span)
                     for span in self.plugin._plan_upload_batches(source)]
            self.assertTrue(parts)
            self.assertEqual([row for part in parts for row in part["networks"]],
                             source["networks"])
            self.assertTrue(all(len(self.plugin._sign_payload(part)) <= 385
                                for part in parts))


    def test_member_territories_compact_bbox_and_authentication(self):
        payload = {"ok": True, "grid_lat": 0.02, "grid_lng": 0.02,
                   "cells": [{"lat": 36.20, "lng": -6.18, "gang_id": 7}]}
        calls, mocked = self.mock_api(payload)
        with mocked:
            self.assertEqual(self.plugin.fetch_member_territories(
                (36.1, -6.3, 36.4, -6.0)), payload)
        self.assertEqual(len(calls), 1)
        request = calls[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url,
            "https://portal.example.invalid/api/member-territories"
            "?compact=1&bbox=-6.300000,36.100000,-6.000000,36.400000&zoom=8")
        self.assertEqual(request.get_header("X-api-key"), "a" * 64)

    def test_member_territories_bad_bbox_or_zoom_never_connects(self):
        cases = [(-91, 0, 1, 1), (1, -181, 2, 1),
                 (1, 2, 0, 3), (1, 2, 3, 181),
                 (1, 2, float("nan"), 3), (1, 2, 3)]
        with patch("plugins.wardrive_upload._open") as opened:
            for bbox in cases:
                with self.subTest(bbox=bbox):
                    self.assertIsNone(self.plugin.fetch_member_territories(bbox))
            self.assertIsNone(self.plugin.fetch_member_territories(
                (36, -6, 37, -5), zoom=5))
            opened.assert_not_called()

    def test_member_territories_unavailable_does_not_use_network(self):
        with patch("plugins.wardrive_upload._open") as opened:
            self.plugin._api_key = ""
            self.assertIsNone(self.plugin.fetch_member_territories(
                (36, -6, 37, -5)))
            self.plugin._api_key = "a" * 64
            self.plugin._sync_stop.set()
            self.assertIsNone(self.plugin.fetch_member_territories(
                (36, -6, 37, -5)))
            opened.assert_not_called()

    def test_member_territories_oversize_and_bad_reply_fail_closed(self):
        with patch("plugins.wardrive_upload._open",
                   return_value=io.BytesIO(b" " * (2 * 1024 * 1024 + 1))):
            self.assertIsNone(self.plugin.fetch_member_territories(
                (36, -6, 37, -5)))
        for result in ({}, {"ok": False, "cells": []},
                       {"ok": True, "cells": "bad"}):
            calls, mocked = self.mock_api(result)
            with mocked:
                self.assertIsNone(self.plugin.fetch_member_territories(
                    (36, -6, 37, -5)))
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
