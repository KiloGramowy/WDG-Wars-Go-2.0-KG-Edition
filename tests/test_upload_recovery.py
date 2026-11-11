"""Accounting recovery with synthetic CSV/state only: no live API, GPS or radio."""
from copy import deepcopy
import csv
import io
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from plugins.wardrive_upload import WardriveUpload, _upload_receipt
from watchdogs.loot_manager import LootManager


DUPLICATE_CACHE_RESPONSE = {
    "ok": True, "imported": 0, "updated": 0, "captured": 0, "skipped": 0,
    "cooldown": 0, "cap_hits": 0, "aircraft_imported": 0,
    "aircraft_already_seen": 0, "meshcore_imported": 0,
    "info": "This payload was already uploaded recently — no new processing.",
}


class UploadRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.state_path = self.base / "synthetic-upload-state.json"
        self.session = self.base / "closed"
        self.session.mkdir()
        self.write_mesh()
        self.plugin = self.new_plugin()

    def new_plugin(self, *, active="active", key="synthetic-account-one", url="https://example.invalid/api/upload/"):
        with patch.object(WardriveUpload, "_state_file", return_value=self.state_path), \
                patch("plugins.wardrive_upload._load_secrets_conf", return_value={
                    "WARDRIVE_API_URL": url, "WARDRIVE_API_KEY": key}):
            plugin = WardriveUpload()
        plugin._state_file = lambda: self.state_path
        plugin.app = SimpleNamespace(
            loot=SimpleNamespace(_base=str(self.base), session_dir=str(self.base / active),
                                 prepare_upload_log_discard=lambda *_args: True),
            _term_add=lambda *args, **kwargs: None)
        plugin._badge_sync_worker = Mock()
        plugin._handle_upload_badges = Mock()
        plugin._wait_upload = Mock(return_value=True)
        return plugin

    def write_mesh(self, *, no_gps=False, second=False):
        rows = [["2026-10-29T12:00:00", "!12345678", "Client", "synthetic",
                 0 if no_gps else 36.4, 0 if no_gps else -6.2, -100, -5]]
        if second:
            rows.append(["2026-10-29T12:01:00", "!87654321", "Client", "synthetic-2", 36.5, -6.1, -105, -7])
        with (self.session / "meshtastic_nodes.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["timestamp", "node_id", "type", "name", "lat", "lon", "rssi", "snr"])
            writer.writerows(rows)

    def payload(self, plugin=None):
        plugin = plugin or self.plugin
        return {"networks": [], "aircraft": [], "meshcore_nodes":
                plugin._parse_meshtastic(self.session / "meshtastic_nodes.csv")}

    def registry_rows(self, payload=None):
        """The documented mesh listing schema, populated with synthetic data."""
        payload = self.payload() if payload is None else payload
        return [{"node_id": row["node_id"], "node_type": row["node_type"],
                 "name": row["name"], "network": row["network"],
                 "latitude": row["lat"], "longitude": row["lon"],
                 "rssi": row["rssi"]} for row in payload["meshcore_nodes"]]

    def write_upload_csvs(self, *, valid=False, mixed=False):
        mesh_header = ["timestamp", "node_id", "type", "name", "lat", "lon", "rssi", "snr"]
        mesh_good = ["now", "!12345678", "Client", "synthetic", 36.4, -6.2, -90, 0]
        mesh_bad = ["now", "!87654321", "Client", "synthetic", 0, 0, -90, 0]
        wifi_header = ["MAC", "SSID", "AuthMode", "FirstSeen", "Channel", "RSSI",
                       "CurrentLatitude", "CurrentLongitude", "AltitudeMeters", "AccuracyMeters", "Type"]
        wifi_good = ["00:11:22:33:44:55", "synthetic", "[ESS]", "now", 1, -50, 36.4, -6.2, 10, 1, "WIFI"]
        wifi_bad = ["00:11:22:33:44:66", "synthetic-no-gps", "[ESS]", "now", 1, -50, 0, 0, 0, 1, "WIFI"]
        adsb_header = ["time", "icao", "callsign", "lat", "lon", "alt_ft", "speed_kt", "heading"]
        adsb_good = ["now", "abc123", "synthetic", 36.4, -6.2, 10000, 200, 20]
        adsb_bad = ["now", "def456", "synthetic-no-gps", 0, 0, 10000, 200, 20]
        for filename, header, good, bad in (("meshtastic_nodes.csv", mesh_header, mesh_good, mesh_bad),
                ("meshcore_nodes.csv", mesh_header, mesh_good, mesh_bad),
                ("wardriving.csv", wifi_header, wifi_good, wifi_bad),
                ("adsb_aircraft.csv", adsb_header, adsb_good, adsb_bad)):
            with (self.session / filename).open("w", encoding="utf-8", newline="") as stream:
                if filename == "wardriving.csv":
                    stream.write("WigleWifi-1.4,synthetic\n")
                writer = csv.writer(stream)
                writer.writerow(header)
                if valid:
                    writer.writerow(good)
                if mixed:
                    writer.writerow(bad)

    @staticmethod
    def response(body):
        return io.BytesIO(json.dumps(body).encode("utf-8"))

    def test_observed_duplicate_response_does_not_prove_mesh_accounting(self):
        confirmed, info, imported = _upload_receipt(DUPLICATE_CACHE_RESPONSE, self.payload())
        self.assertFalse(confirmed)
        self.assertIn("unknown 1", info)
        self.assertEqual(imported, 0)

    def test_observed_mixed_76_network_76_mesh_receipt_accounts_for_cooldown(self):
        payload = {"networks": [{}] * 76, "aircraft": [], "meshcore_nodes": [{}] * 76}
        receipt = {"ok": True, "imported": 19, "duplicates": 1, "cooldown": 56,
                   "cap_hits": 0, "captured": 0, "meshcore_imported": 43,
                   "meshcore_already_seen": 33, "meshcore_rejected": 0}
        confirmed, info, imported = _upload_receipt(receipt, payload)
        self.assertTrue(confirmed)
        self.assertEqual(imported, 19)
        self.assertIn("nets: +19, known 1, cooldown 56, rejected 0, unknown 0", info)
        self.assertIn("mesh: +43, known 33, rejected 0, unknown 0", info)

    def test_observed_28_network_receipt_does_not_count_cooldown_as_new_credit(self):
        payload = {"networks": [{}] * 28, "aircraft": [], "meshcore_nodes": []}
        confirmed, info, imported = _upload_receipt(
            {"ok": True, "imported": 1, "duplicates": 10, "cooldown": 17}, payload)
        self.assertTrue(confirmed)
        self.assertEqual(imported, 1)
        self.assertIn("known 10, cooldown 17, rejected 0, unknown 0", info)

    def test_cooldown_requires_nonnegative_integer_and_exact_accounting(self):
        payload = {"networks": [{}] * 28}
        receipt = {"ok": True, "imported": 1, "duplicates": 10, "cooldown": 17}
        for value in (True, False, "17", -1, 17.0, None):
            with self.subTest(invalid_cooldown=value):
                confirmed, info, _ = _upload_receipt(dict(receipt, cooldown=value), payload)
                self.assertFalse(confirmed)
                self.assertEqual(info, "invalid cooldown counter")
        for value in (0, 16, 18, 28):
            with self.subTest(incomplete_or_overcounted=value):
                self.assertFalse(_upload_receipt(dict(receipt, cooldown=value), payload)[0])
        self.assertFalse(_upload_receipt(dict(receipt, cooldown=16, rejected=1), payload)[0])

    def test_unverified_cap_and_capture_counters_do_not_close_accounting_gap(self):
        payload = {"networks": [{}] * 28}
        for extra in ({"cap_hits": 17}, {"captured": 17}, {"cap_hits": 8, "captured": 9}):
            with self.subTest(extra=extra):
                receipt = {"ok": True, "imported": 1, "duplicates": 10, **extra}
                self.assertFalse(_upload_receipt(receipt, payload)[0])

    def test_network_cooldown_receipt_closes_session_and_survives_restart(self):
        self.write_upload_csvs(valid=True)
        for filename in ("meshtastic_nodes.csv", "meshcore_nodes.csv", "adsb_aircraft.csv"):
            (self.session / filename).unlink()
        p = self.plugin
        payload = p._session_upload_data(self.session)
        self.assertEqual(len(payload["networks"]), 1)
        self.assertEqual(p._pending_sessions(), [self.session])
        receipt = {"ok": True, "imported": 0, "duplicates": 0, "cooldown": 1}
        with patch("plugins.wardrive_upload._open", return_value=self.response(receipt)) as network:
            p._upload_worker([self.session])
        network.assert_called_once()
        self.assertEqual(p._cached_receipt(payload), receipt)
        self.assertEqual(p._pending_sessions(), [])
        self.assertEqual(p._last_upload_summary["new_networks"], 0)
        self.assertEqual(p._last_upload_summary["pending"], 0)
        self.assertEqual(self.new_plugin()._pending_sessions(), [])

    def test_mixed_duplicate_cache_response_alone_never_confirms_upload(self):
        self.write_upload_csvs(valid=True)
        p = self.plugin
        payload = p._session_upload_data(self.session)
        original = {path.name: path.read_bytes() for path in self.session.iterdir()}
        with patch("plugins.wardrive_upload._open", return_value=self.response(
                dict(DUPLICATE_CACHE_RESPONSE, cooldown=0))) as network:
            p._upload_worker([self.session])
        self.assertEqual(network.call_count,2)  # A child POST still needs its own full ACK.
        self.assertTrue(all(call.args[0].get_method()=="POST" for call in network.call_args_list))
        self.assertIsNone(p._cached_receipt(payload))
        self.assertNotIn(self.session.name, p._uploaded_sessions)
        self.assertNotIn(self.session.name, p._uploaded_meshtastic_sessions)
        self.assertEqual(p._pending_sessions(), [self.session])
        self.assertEqual(p._last_upload_summary["pending"], 1)
        self.assertEqual({path.name: path.read_bytes() for path in self.session.iterdir()}, original)

    def test_duplicate_without_saved_receipt_stays_pending_after_restart(self):
        p = self.plugin
        with patch("plugins.wardrive_upload._open", side_effect=[
                self.response(DUPLICATE_CACHE_RESPONSE), self.response([])]) as network:
            p._upload_worker([self.session])
        self.assertEqual(network.call_count, 2)  # POST, then bounded documented registry GET
        self.assertNotIn(self.session.name, p._uploaded_sessions)
        self.assertNotIn(self.session.name, p._uploaded_meshtastic_sessions)
        self.assertIsNone(p._cached_receipt(self.payload()))
        self.assertIsNone(self.new_plugin()._cached_receipt(self.payload()))
        self.assertIn(self.session, p._pending_sessions())

    def test_complete_receipt_survives_restart_without_persisting_api_key(self):
        p = self.plugin
        payload, receipt = self.payload(), {"ok": True, "meshcore_imported": 1}
        self.assertIs(p._remember_receipt(payload, receipt), True)
        self.assertEqual(p._cached_receipt(payload), receipt)
        restarted = self.new_plugin()
        self.assertEqual(restarted._cached_receipt(payload), receipt)
        self.assertEqual(restarted._pending_sessions(), [])
        stored = self.state_path.read_text(encoding="utf-8")
        self.assertIn("receipts", json.loads(stored))
        self.assertNotIn(p._api_key, stored)

    def test_canonical_key_ignores_dictionary_order_but_not_record_content(self):
        p = self.plugin
        payload = self.payload()
        reordered = {"meshcore_nodes": [dict(reversed(list(payload["meshcore_nodes"][0].items())))],
                     "aircraft": [], "networks": []}
        self.assertEqual(p._receipt_key(payload), p._receipt_key(reordered))
        p._remember_receipt(payload, {"ok": True, "meshcore_imported": 1})
        self.assertIsNotNone(p._cached_receipt(reordered))
        for field, value in (("node_id", "87654321"), ("lat", 36.6), ("name", "different synthetic")):
            changed = deepcopy(payload)
            changed["meshcore_nodes"][0][field] = value
            with self.subTest(field=field):
                self.assertNotEqual(p._receipt_key(payload), p._receipt_key(changed))
                self.assertIsNone(p._cached_receipt(changed))

    def test_receipts_cannot_cross_accounts_or_upload_endpoints(self):
        p = self.plugin
        payload = self.payload()
        self.assertTrue(p._remember_receipt(payload, {"ok": True, "meshcore_imported": 1}))
        for changed in (self.new_plugin(key="synthetic-account-two"),
                        self.new_plugin(url="https://other.example.invalid/api/upload/"),
                        self.new_plugin(url="https://example.invalid/another/upload/")):
            with self.subTest(endpoint=changed._api_url):
                self.assertNotEqual(p._receipt_key(payload), changed._receipt_key(payload))
                self.assertIsNone(changed._cached_receipt(payload))

    def test_rejected_incomplete_and_cache_only_responses_are_never_saved(self):
        payload = self.payload()
        for receipt in ({"ok": False}, {"ok": True, "meshcore_imported": 0},
                        {"ok": True, "meshcore_imported": 0, "meshcore_rejected": 1,
                         "meshcore_reject_reasons": {"no_gps": 1}},
                        {"ok": True, "meshcore_imported": 1, "meshcore_rejected": 1},
                        {"ok": True, "meshcore_imported": True}, DUPLICATE_CACHE_RESPONSE):
            with self.subTest(receipt=receipt):
                self.assertIs(self.plugin._remember_receipt(payload, receipt), False)
                self.assertIsNone(self.plugin._cached_receipt(payload))

    def test_atomic_receipt_save_failure_preserves_previous_state_and_does_not_cache(self):
        p = self.plugin
        previous_payload = self.payload()
        previous_receipt = {"ok": True, "meshcore_imported": 1}
        self.assertTrue(p._remember_receipt(previous_payload, previous_receipt))
        previous_bytes = self.state_path.read_bytes()
        new_payload = deepcopy(previous_payload)
        new_payload["meshcore_nodes"][0]["node_id"] = "87654321"
        with patch("os.replace", side_effect=OSError("synthetic atomic-write failure")):
            self.assertIs(p._remember_receipt(new_payload, previous_receipt), False)
        self.assertEqual(self.state_path.read_bytes(), previous_bytes)
        self.assertEqual(p._cached_receipt(previous_payload), previous_receipt)
        self.assertIsNone(p._cached_receipt(new_payload))
        restarted = self.new_plugin()
        self.assertEqual(restarted._cached_receipt(previous_payload), previous_receipt)
        self.assertIsNone(restarted._cached_receipt(new_payload))

    def test_receipt_persistence_failure_does_not_mark_a_closed_session_uploaded(self):
        p = self.plugin
        with patch("plugins.wardrive_upload._open", return_value=self.response({"ok": True, "meshcore_imported": 1})), \
                patch("os.replace", side_effect=OSError("synthetic atomic-write failure")):
            p._upload_worker([self.session])
        self.assertNotIn(self.session.name, p._uploaded_sessions)
        self.assertNotIn(self.session.name, p._uploaded_meshtastic_sessions)
        self.assertIn(self.session, p._pending_sessions())
        self.assertIsNone(self.new_plugin()._cached_receipt(self.payload()))

    def test_worker_persists_complete_receipt_before_marking_closed_session(self):
        p = self.plugin
        payload = self.payload()
        original_mark = p._mark_uploaded
        order = []
        def mark(session, *, meshtastic=False):
            self.assertIn(p._receipt_key(payload), json.loads(self.state_path.read_text())["receipts"])
            self.assertEqual(self.new_plugin()._cached_receipt(payload), {"ok": True, "meshcore_imported": 1})
            order.append("mark-after-durable-receipt")
            return original_mark(session, meshtastic=meshtastic)
        with patch.object(p, "_mark_uploaded", side_effect=mark), \
                patch("plugins.wardrive_upload._open", return_value=self.response({"ok": True, "meshcore_imported": 1})):
            p._upload_worker([self.session])
        self.assertEqual(order, ["mark-after-durable-receipt"])
        self.assertIn(self.session.name, p._uploaded_meshtastic_sessions)
        self.assertNotIn(self.session, p._pending_sessions())

    def test_previously_confirmed_exact_payload_is_not_resent_or_counted_as_new_import(self):
        p = self.plugin
        network_row = {"bssid": "00:11:22:33:44:55", "ssid": "synthetic", "lat": 36.4, "lon": -6.2}
        p._parse_csv = lambda path: [dict(network_row)]
        payload = self.payload()
        payload["networks"] = [dict(network_row)]
        receipt = {"ok": True, "imported": 1, "meshcore_imported": 1, "new_badges": ["synthetic"]}
        self.assertTrue(p._remember_receipt(payload, receipt))
        with patch("plugins.wardrive_upload._open") as network:
            p._upload_worker([self.session])
        network.assert_not_called()
        p._handle_upload_badges.assert_not_called()
        self.assertIn(self.session.name, p._uploaded_meshtastic_sessions)
        text = "\n".join(line for line, _ in p._log)
        self.assertRegex(text.lower(), r"(?:previous|earlier|already).*confirm|confirm.*(?:previous|earlier|already)")
        self.assertIn("+0 new WiFi/BLE networks", text)
        self.assertEqual(p._last_upload_summary["reused"], 1)
        self.assertEqual(p._last_upload_summary["new_networks"], 0)

    def test_active_session_remains_open_and_new_content_requires_a_new_receipt(self):
        p = self.new_plugin(active=self.session.name)
        with patch("plugins.wardrive_upload._open", return_value=self.response({"ok": True, "meshcore_imported": 1})):
            p._upload_worker([self.session])
        self.assertNotIn(self.session.name, p._uploaded_sessions)
        self.assertNotIn(self.session.name, p._uploaded_meshtastic_sessions)
        self.assertIsNotNone(p._cached_receipt(self.payload(p)))
        self.assertEqual(p._pending_sessions(), [])
        with patch("plugins.wardrive_upload._open") as network:
            p._upload_worker([self.session])
        network.assert_not_called()
        self.write_mesh(second=True)
        self.assertIsNone(p._cached_receipt(self.payload(p)))
        self.assertEqual(p._pending_sessions(), [self.session])
        receipt = {"ok": True, "meshcore_imported": 1, "meshcore_already_seen": 1}
        with patch("plugins.wardrive_upload._open", return_value=self.response(receipt)) as network:
            p._upload_worker([self.session])
        network.assert_called_once()
        self.assertNotIn(self.session.name, p._uploaded_meshtastic_sessions)
        self.assertEqual(p._pending_sessions(), [])

    def test_inactive_no_gps_mesh_log_is_cleaned_after_attempt_without_request_or_success_mark(self):
        self.write_mesh(no_gps=True)
        original = (self.session / "meshtastic_nodes.csv").read_bytes()
        p = self.plugin
        with patch("plugins.wardrive_upload._open") as network:
            p._upload_worker([self.session])
        network.assert_not_called()
        self.assertNotIn(self.session.name, p._uploaded_sessions)
        self.assertNotIn(self.session.name, p._uploaded_meshtastic_sessions)
        self.assertFalse((self.session / "meshtastic_nodes.csv").exists())
        self.assertEqual(p._pending_sessions(), [])
        text = "\n".join(line for line, _ in p._log)
        self.assertNotRegex(text.lower(), r"upload error|upload failed|http [45]\d\d")

    def test_existing_uploaded_session_history_is_preserved_when_receipts_are_added(self):
        self.state_path.write_text(json.dumps({"uploaded": ["earlier-session"],
            "uploaded_meshtastic": ["earlier-mt-session"]}))
        p = self.new_plugin()
        self.assertEqual(p._uploaded_sessions, {"earlier-session"})
        self.assertEqual(p._uploaded_meshtastic_sessions, {"earlier-mt-session"})
        self.assertTrue(p._remember_receipt(self.payload(p), {"ok": True, "meshcore_imported": 1}))
        restarted = self.new_plugin()
        self.assertEqual(restarted._uploaded_sessions, {"earlier-session"})
        self.assertEqual(restarted._uploaded_meshtastic_sessions, {"earlier-mt-session"})

    def test_http_413_reads_json_reason_without_resending_unchanged_payload(self):
        p = self.plugin
        body = self.response({"error": "Payload too large; reduce the batch"})
        error = HTTPError(p._api_url, 413, "Payload Too Large", {}, body)
        with patch.object(error, "read", wraps=error.read) as read, \
                patch("plugins.wardrive_upload._open", side_effect=error) as network, \
                patch("time.sleep") as sleep:
            p._upload_worker([self.session])
        network.assert_called_once()
        self.assertEqual(network.call_args.args[0].get_method(), "POST")
        read.assert_called_once()
        sleep.assert_not_called()
        self.assertNotIn(self.session.name, p._uploaded_meshtastic_sessions)
        self.assertIsNone(p._cached_receipt(self.payload()))
        text = "\n".join(line for line, _ in p._log)
        self.assertIn("Payload too large", text)
        self.assertIn("HTTP 413", text)
        self.assertEqual(p._last_upload_summary["pending"], 1)

    def test_http_429_waits_retry_after_once_then_accepts_complete_receipt(self):
        p = self.plugin
        error = HTTPError(p._api_url, 429, "Too Many Requests", {"Retry-After": "17"},
                          self.response({"error": "Rate limited"}))
        receipt = {"ok": True, "meshcore_imported": 1}
        with patch("plugins.wardrive_upload._open", side_effect=[error, self.response(receipt)]) as network, \
                patch("time.sleep") as sleep:
            p._upload_worker([self.session])
        self.assertEqual(network.call_count, 2)
        self.assertTrue(any(call.args[0] == 17 for call in p._wait_upload.call_args_list))
        sleep.assert_not_called()
        self.assertEqual(p._cached_receipt(self.payload()), receipt)
        self.assertIn(self.session.name, p._uploaded_meshtastic_sessions)

    def test_http_429_never_retries_more_than_once(self):
        p = self.plugin
        errors = [HTTPError(p._api_url, 429, "Too Many Requests", {"Retry-After": "3"},
                            self.response({"error": "Rate limited"})) for _ in range(2)]
        with patch("plugins.wardrive_upload._open", side_effect=errors) as network, \
                patch("time.sleep") as sleep:
            p._upload_worker([self.session])
        self.assertEqual(network.call_count, 2)
        self.assertTrue(any(call.args[0] == 3 for call in p._wait_upload.call_args_list))
        sleep.assert_not_called()
        self.assertNotIn(self.session.name, p._uploaded_meshtastic_sessions)
        self.assertIsNone(p._cached_receipt(self.payload()))

    def test_http_429_long_retry_after_is_left_pending_without_early_retry(self):
        p = self.plugin
        error = HTTPError(p._api_url, 429, "Too Many Requests", {"Retry-After": "120"},
                          self.response({"error": "Rate limited"}))
        with patch("plugins.wardrive_upload._open", side_effect=error) as network, \
                patch("time.sleep") as sleep:
            p._upload_worker([self.session])
        network.assert_called_once()
        sleep.assert_not_called()
        self.assertNotIn(self.session.name, p._uploaded_meshtastic_sessions)
        self.assertEqual(p._last_upload_summary["pending"], 1)

    def test_error_detail_drops_raw_html_and_redacts_api_key(self):
        p = self.plugin
        self.assertEqual(p._error_detail(("<html><title>Error</title>" + p._api_key + "</html>").encode()), "")
        self.assertEqual(p._error_detail(b"not-json"), "")
        self.assertEqual(p._error_detail(b'["error"]'), "")
        body = json.dumps({"error": "Invalid key " + p._api_key + "\n  retry later " + "x" * 200}).encode()
        detail = p._error_detail(body)
        self.assertIn("[redacted]", detail)
        self.assertNotIn(p._api_key, detail)
        self.assertNotIn("\n", detail)
        self.assertLessEqual(len(detail), 160)

    def test_exact_registry_match_persists_verification_without_accounting_counters(self):
        p = self.plugin
        payload = self.payload()
        body = self.response(self.registry_rows(payload))
        with patch.object(body, "read", wraps=body.read) as read, \
                patch("plugins.wardrive_upload._open", return_value=body) as network:
            self.assertTrue(p._verify_duplicate_mesh(payload, DUPLICATE_CACHE_RESPONSE))
        network.assert_called_once()
        request = network.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertTrue(request.full_url.endswith("/api/meshcore"))
        self.assertEqual(request.get_header("X-api-key"), p._api_key)
        self.assertTrue(read.call_args.args)
        self.assertGreater(read.call_args.args[0], 0)
        self.assertLess(read.call_args.args[0], 100 * 1024 * 1024)
        self.assertTrue(p._cached_mesh_verification(payload))
        self.assertIsNone(p._cached_receipt(payload))
        record = json.loads(self.state_path.read_text())["receipts"][p._receipt_key(payload)]
        self.assertEqual(record, {"verification": "mesh_registry", "source": request.full_url, "matched": 1})
        self.assertNotIn(p._api_key, self.state_path.read_text())
        self.assertTrue(self.new_plugin()._cached_mesh_verification(payload))
        self.assertEqual(p._pending_sessions(), [])
        self.assertEqual(self.new_plugin()._pending_sessions(), [])

    def test_worker_saves_registry_verification_before_marking_without_new_credit(self):
        p = self.plugin
        payload = self.payload()
        original_mark = p._mark_uploaded
        marked = []
        def mark(session, *, meshtastic=False):
            self.assertTrue(self.new_plugin()._cached_mesh_verification(payload))
            marked.append(session.name)
            return original_mark(session, meshtastic=meshtastic)
        with patch.object(p, "_mark_uploaded", side_effect=mark), \
                patch("plugins.wardrive_upload._open", side_effect=[
                    self.response(DUPLICATE_CACHE_RESPONSE), self.response(self.registry_rows(payload))]):
            p._upload_worker([self.session])
        self.assertEqual(marked, [self.session.name])
        self.assertNotIn(self.session, p._pending_sessions())
        p._handle_upload_badges.assert_not_called()
        p._badge_sync_worker.assert_not_called()
        self.assertEqual(p._last_upload_summary["confirmed"], 0)
        self.assertEqual(p._last_upload_summary["verified"], 1)
        self.assertEqual(p._last_upload_summary["pending"], 0)
        self.assertEqual(p._last_upload_summary["new_networks"], 0)
        text = "\n".join(line for line, _ in p._log).lower()
        self.assertRegex(text, r"verified present (?:on server|in server mesh registry)")
        self.assertIn("credit unknown", text)

    def test_cached_registry_verification_requires_exact_account_endpoint_and_content(self):
        p = self.plugin
        payload = self.payload()
        with patch("plugins.wardrive_upload._open", return_value=self.response(self.registry_rows(payload))):
            self.assertTrue(p._verify_duplicate_mesh(payload, DUPLICATE_CACHE_RESPONSE))
        for changed in (self.new_plugin(key="synthetic-account-two"),
                        self.new_plugin(url="https://other.example.invalid/api/upload/")):
            self.assertFalse(changed._cached_mesh_verification(payload))
        changed_payload = deepcopy(payload)
        changed_payload["meshcore_nodes"][0]["name"] = "changed synthetic name"
        self.assertFalse(p._cached_mesh_verification(changed_payload))
        p = self.new_plugin()
        with patch("plugins.wardrive_upload._open") as network:
            p._upload_worker([self.session])
        network.assert_not_called()
        p._handle_upload_badges.assert_not_called()
        self.assertEqual(p._last_upload_summary["confirmed"], 0)
        self.assertEqual(p._last_upload_summary["new_networks"], 0)

    def test_registry_verification_rejects_each_mismatched_identity_or_observation_field(self):
        p = self.plugin
        payload = self.payload()
        for field, value in (("node_id", "87654321"), ("network", "meshcore"),
                             ("latitude", 36.4000000001), ("longitude", -6.2000000001),
                             ("name", "Synthetic"), ("node_type", "Router"), ("rssi", -101)):
            rows = self.registry_rows(payload)
            rows[0][field] = value
            with self.subTest(field=field), \
                    patch("plugins.wardrive_upload._open", return_value=self.response(rows)):
                self.assertFalse(p._verify_duplicate_mesh(payload, DUPLICATE_CACHE_RESPONSE))
                self.assertFalse(p._cached_mesh_verification(payload))

    def test_registry_verification_requires_every_explicit_field_without_defaults(self):
        p = self.plugin
        payload = self.payload()
        for field in self.registry_rows(payload)[0]:
            rows = self.registry_rows(payload)
            del rows[0][field]
            with self.subTest(missing=field), \
                    patch("plugins.wardrive_upload._open", return_value=self.response(rows)):
                self.assertFalse(p._verify_duplicate_mesh(payload, DUPLICATE_CACHE_RESPONSE))
                self.assertFalse(p._cached_mesh_verification(payload))

    def test_registry_verification_rejects_ambiguous_duplicate_protocol_and_id(self):
        p = self.plugin
        payload = self.payload()
        for different_name in (False, True):
            rows = self.registry_rows(payload)
            rows.append(dict(rows[0]))
            if different_name:
                rows[1]["name"] = "ambiguous other observation"
            with self.subTest(different_name=different_name), \
                    patch("plugins.wardrive_upload._open", return_value=self.response(rows)):
                self.assertFalse(p._verify_duplicate_mesh(payload, DUPLICATE_CACHE_RESPONSE))

    def test_registry_verification_keeps_equal_ids_in_other_protocol_separate(self):
        p = self.plugin
        payload = self.payload()
        rows = self.registry_rows(payload)
        other_protocol = dict(rows[0], network="meshcore", name="separate synthetic")
        with patch("plugins.wardrive_upload._open", return_value=self.response([other_protocol] + rows)):
            self.assertTrue(p._verify_duplicate_mesh(payload, DUPLICATE_CACHE_RESPONSE))

    def test_registry_verification_requires_all_nodes_in_the_batch(self):
        self.write_mesh(second=True)
        payload = self.payload()
        with patch("plugins.wardrive_upload._open", return_value=self.response(self.registry_rows(payload)[:1])):
            self.assertFalse(self.plugin._verify_duplicate_mesh(payload, DUPLICATE_CACHE_RESPONSE))
        self.assertFalse(self.plugin._cached_mesh_verification(payload))

    def test_registry_fallback_is_limited_to_literal_duplicate_mesh_only_without_rejections(self):
        payload = self.payload()
        mixed_wifi = deepcopy(payload)
        mixed_wifi["networks"] = [{"bssid": "00:11:22:33:44:55"}]
        mixed_aircraft = deepcopy(payload)
        mixed_aircraft["aircraft"] = [{"icao": "abc123"}]
        cases = [(mixed_wifi, DUPLICATE_CACHE_RESPONSE), (mixed_aircraft, DUPLICATE_CACHE_RESPONSE),
                 ({"meshcore_nodes": [], "networks": [], "aircraft": []}, DUPLICATE_CACHE_RESPONSE),
                 (payload, {"ok": True, "meshcore_imported": 0}),
                 (payload, dict(DUPLICATE_CACHE_RESPONSE, ok=False)),
                 (payload, dict(DUPLICATE_CACHE_RESPONSE, info="already uploaded; a different generic message")),
                 (payload, dict(DUPLICATE_CACHE_RESPONSE,
                                info="This payload was already uploaded recently but processing failed")),
                 (payload, dict(DUPLICATE_CACHE_RESPONSE, meshcore_rejected=1)),
                 (payload, dict(DUPLICATE_CACHE_RESPONSE, meshcore_reject_reasons={"invalid": 1}))]
        for index, (data, result) in enumerate(cases):
            with self.subTest(case=index), patch("plugins.wardrive_upload._open") as network:
                self.assertFalse(self.plugin._verify_duplicate_mesh(data, result))
            network.assert_not_called()

    def test_invalid_or_unavailable_registry_response_preserves_pending_state(self):
        cases = [[], {}, [{"node_id": "12345678"}], b"not-json", URLError("synthetic API outage"),
                 HTTPError(self.plugin._api_url, 503, "Unavailable", {}, self.response({"error": "outage"}))]
        for index, result in enumerate(cases):
            response = result if isinstance(result, Exception) else (
                io.BytesIO(result) if isinstance(result, bytes) else self.response(result))
            p = self.new_plugin()
            with self.subTest(case=index), patch("plugins.wardrive_upload._open", side_effect=[
                    self.response(DUPLICATE_CACHE_RESPONSE), response]) as network:
                p._upload_worker([self.session])
            self.assertEqual(network.call_count, 2)
            self.assertIn(self.session, p._pending_sessions())
            self.assertFalse(p._cached_mesh_verification(self.payload(p)))
            self.assertIsNone(p._cached_receipt(self.payload(p)))
            p._handle_upload_badges.assert_not_called()

    def test_registry_verification_persistence_failure_does_not_mark_or_cache(self):
        p = self.plugin
        with patch("plugins.wardrive_upload._open", side_effect=[
                self.response(DUPLICATE_CACHE_RESPONSE), self.response(self.registry_rows())]), \
                patch("os.replace", side_effect=OSError("synthetic atomic-write failure")):
            p._upload_worker([self.session])
        self.assertIn(self.session, p._pending_sessions())
        self.assertFalse(p._cached_mesh_verification(self.payload()))
        self.assertFalse(self.new_plugin()._cached_mesh_verification(self.payload()))

    def test_active_registry_verified_session_accepts_new_content_after_restart(self):
        p = self.new_plugin(active=self.session.name)
        with patch("plugins.wardrive_upload._open", side_effect=[
                self.response(DUPLICATE_CACHE_RESPONSE), self.response(self.registry_rows())]):
            p._upload_worker([self.session])
        self.assertNotIn(self.session.name, p._uploaded_meshtastic_sessions)
        self.assertEqual(p._pending_sessions(), [])
        p = self.new_plugin(active=self.session.name)
        with patch("plugins.wardrive_upload._open") as network:
            p._upload_worker([self.session])
        network.assert_not_called()
        self.write_mesh(second=True)
        self.assertFalse(p._cached_mesh_verification(self.payload(p)))
        self.assertEqual(p._pending_sessions(), [self.session])
        with patch("plugins.wardrive_upload._open", side_effect=[
                self.response(DUPLICATE_CACHE_RESPONSE), self.response(self.registry_rows())]) as network:
            p._upload_worker([self.session])
        self.assertEqual(network.call_count, 2)
        self.assertNotIn(self.session.name, p._uploaded_meshtastic_sessions)
        self.assertTrue(p._cached_mesh_verification(self.payload(p)))
        self.assertEqual(p._pending_sessions(), [])

    def test_registry_verification_does_not_infer_id_from_a_json_number(self):
        p = self.plugin
        payload = self.payload()
        rows = self.registry_rows(payload)
        rows[0]["node_id"] = 12345678
        with patch("plugins.wardrive_upload._open", return_value=self.response(rows)):
            self.assertFalse(p._verify_duplicate_mesh(payload, DUPLICATE_CACHE_RESPONSE))
        self.assertFalse(p._cached_mesh_verification(payload))

    def test_registry_verification_requires_finite_numeric_observations_not_booleans(self):
        p = self.plugin
        payload = self.payload()
        for field, value in (("latitude", float("nan")), ("longitude", float("inf")),
                             ("rssi", None), ("rssi", "-100"), ("latitude", True)):
            rows = self.registry_rows(payload)
            rows[0][field] = value
            with self.subTest(field=field, value=value), \
                    patch("plugins.wardrive_upload._open", return_value=self.response(rows)):
                self.assertFalse(p._verify_duplicate_mesh(payload, DUPLICATE_CACHE_RESPONSE))

    def test_oversized_registry_response_is_not_parsed_or_saved(self):
        p = self.plugin
        payload = self.payload()
        with patch("plugins.wardrive_upload._open", return_value=io.BytesIO(b" " * (16 * 1024 * 1024 + 1))):
            self.assertFalse(p._verify_duplicate_mesh(payload, DUPLICATE_CACHE_RESPONSE))
        self.assertFalse(p._cached_mesh_verification(payload))

    def test_prune_removes_only_empty_known_upload_csvs_and_keeps_other_data(self):
        self.write_upload_csvs()
        untouched = {"contacts.json": b"synthetic contacts", "progress.db": b"synthetic XP",
                     "messages.log": b"synthetic messages", "unrelated.csv": b"unrelated data"}
        for filename, data in untouched.items():
            (self.session / filename).write_bytes(data)
        nested = self.session / "nested"
        nested.mkdir()
        (nested / "meshtastic_nodes.csv").write_text("leave nested files untouched")
        p = self.plugin
        self.assertEqual(p._prune_upload_logs(self.session), 4)
        for filename, data in untouched.items():
            self.assertEqual((self.session / filename).read_bytes(), data)
        self.assertEqual((nested / "meshtastic_nodes.csv").read_text(), "leave nested files untouched")
        self.assertTrue(self.session.is_dir())
        self.assertEqual(p._pending_sessions(), [])

    def test_no_gps_backlog_is_cleaned_after_explicit_upload_attempt(self):
        self.write_upload_csvs(mixed=True)
        p = self.plugin
        with patch("plugins.wardrive_upload._open") as network:
            self.assertEqual(p._pending_sessions(), [])
            p._start_upload(all_sessions=True)
            try:
                self.assertTrue(p._sync_idle.wait(5), "Upload preparation did not finish")
            finally:
                self.assertTrue(p.on_unload(), "Upload preparation did not stop")
        network.assert_not_called()
        self.assertEqual({p.name for p in self.session.glob("*.csv")}, set())
        self.assertEqual(p._overlay_items()[0][0], "upload_all")
        self.assertIn("(0 ", p._overlay_items()[0][1])
        self.assertNotIn(self.session.name, p._uploaded_sessions)
        self.assertNotIn(self.session.name, p._uploaded_meshtastic_sessions)

    def test_prune_removes_invalid_upload_log_without_touching_game_data(self):
        (self.session / "meshtastic_nodes.csv").write_text(
            "timestamp,node_id,type,name,lat,lon,rssi\nnow,not-a-node,Client,synthetic,36.4,-6.2,-90\n")
        survivor = self.session / "contacts.db"
        survivor.write_bytes(b"synthetic persistent contacts")
        self.assertEqual(self.plugin._prune_upload_logs(self.session), 1)
        self.assertFalse((self.session / "meshtastic_nodes.csv").exists())
        self.assertEqual(survivor.read_bytes(), b"synthetic persistent contacts")

    def test_prune_preserves_valid_and_mixed_csv_bytes_and_reports_one_pending_session(self):
        self.write_upload_csvs(valid=True, mixed=True)
        originals = {path.name: path.read_bytes() for path in self.session.glob("*.csv")}
        p = self.plugin
        self.assertEqual(p._prune_upload_logs(self.session), 0)
        self.assertEqual(p._pending_sessions(), [self.session])
        self.assertEqual({path.name: path.read_bytes() for path in self.session.glob("*.csv")}, originals)

    def test_prune_preserves_confirmed_session_markers_and_already_confirmed_groups(self):
        self.write_upload_csvs(mixed=True)
        p = self.plugin
        p._uploaded_sessions.add(self.session.name)
        p._uploaded_meshtastic_sessions.add(self.session.name)
        originals = {path.name: path.read_bytes() for path in self.session.glob("*.csv")}
        self.assertEqual(p._prune_upload_logs(self.session), 0)
        self.assertEqual(p._pending_sessions(), [])
        self.assertEqual(p._uploaded_sessions, {self.session.name})
        self.assertEqual(p._uploaded_meshtastic_sessions, {self.session.name})
        self.assertEqual({path.name: path.read_bytes() for path in self.session.glob("*.csv")}, originals)

    def test_prune_only_unconfirmed_meshtastic_group_with_legacy_receipt(self):
        self.write_upload_csvs(mixed=True)
        p = self.plugin
        p._uploaded_sessions.add(self.session.name)
        self.assertEqual(p._prune_upload_logs(self.session), 0)
        self.assertTrue((self.session / "meshtastic_nodes.csv").is_file())
        for filename in ("meshcore_nodes.csv", "wardriving.csv", "adsb_aircraft.csv"):
            self.assertTrue((self.session / filename).is_file())
        self.assertEqual(p._uploaded_sessions, {self.session.name})
        self.assertEqual(p._uploaded_meshtastic_sessions, set())
        self.assertEqual(p._pending_sessions(), [])

    def test_waiting_active_mesh_file_can_gain_valid_position_and_be_queued(self):
        self.write_mesh(no_gps=True)
        p = self.new_plugin(active=self.session.name)
        self.assertEqual(p._pending_sessions(), [])
        self.assertTrue((self.session / "meshtastic_nodes.csv").is_file())
        self.assertNotIn(self.session.name, p._uploaded_meshtastic_sessions)
        self.write_mesh()
        self.assertEqual(p._pending_sessions(), [self.session])
        self.assertTrue((self.session / "meshtastic_nodes.csv").is_file())
        self.assertNotIn(self.session.name, p._uploaded_meshtastic_sessions)

    def test_prune_prepares_outside_loot_lock_and_unlinks_under_it(self):
        self.write_mesh(no_gps=True)
        path = self.session / "meshtastic_nodes.csv"
        path.write_text(path.read_text().replace("!12345678", "malformed-node-id"))
        p = self.plugin
        tracker = SimpleNamespace(held=False, entered=0)
        class LootLock:
            def __enter__(inner):
                tracker.held = True
                tracker.entered += 1
            def __exit__(inner, *args):
                tracker.held = False
        p.app.loot._db_lock = LootLock()
        original_parse = p._parse_meshtastic
        original_unlink = Path.unlink
        def parse(path):
            self.assertFalse(tracker.held, "slow CSV preparation must not block gameplay writers")
            return original_parse(path)
        def unlink(path, *args, **kwargs):
            self.assertTrue(tracker.held, "upload CSV must be unlinked while holding the loot lock")
            return original_unlink(path, *args, **kwargs)
        with patch.object(p, "_parse_meshtastic", side_effect=parse), patch.object(Path, "unlink", unlink):
            self.assertEqual(p._prune_upload_logs(self.session), 1)
        self.assertGreaterEqual(tracker.entered, 1)
        self.assertFalse(tracker.held)

    def test_prune_rechecks_file_after_taking_lock_and_keeps_new_valid_row(self):
        self.write_mesh(no_gps=True)
        path = self.session / "meshtastic_nodes.csv"
        path.write_text(path.read_text().replace("!12345678", "malformed-node-id"))
        p = self.plugin
        tracker = SimpleNamespace(entered=False)
        class LootLock:
            def __enter__(inner):
                tracker.entered = True
                self.write_mesh()
            def __exit__(inner, *args):
                pass
        p.app.loot._db_lock = LootLock()
        self.assertEqual(p._prune_upload_logs(self.session), 0)
        self.assertTrue(tracker.entered)
        self.assertTrue((self.session / "meshtastic_nodes.csv").is_file())
        self.assertEqual(len(p._parse_meshtastic(self.session / "meshtastic_nodes.csv")), 1)

    def test_prune_does_not_touch_a_session_outside_the_loot_base(self):
        with tempfile.TemporaryDirectory() as outside:
            session = Path(outside)
            path = session / "meshtastic_nodes.csv"
            path.write_text("timestamp,node_id,type,name,lat,lon,rssi\n")
            original = path.read_bytes()
            self.assertEqual(self.plugin._prune_upload_logs(session), 0)
            self.assertEqual(path.read_bytes(), original)

    def test_prune_does_not_follow_upload_file_symlinks(self):
        self.write_mesh(no_gps=True)
        path = self.session / "meshtastic_nodes.csv"
        path.write_text(path.read_text().replace("!12345678", "malformed-node-id"))
        target = self.base / "synthetic-contact-export.csv"
        path.replace(target)
        original = target.read_bytes()
        try:
            path.symlink_to(target)
        except OSError as error:
            self.skipTest("Creating symlinks is unavailable on this host: " + str(error))
        self.assertEqual(self.plugin._prune_upload_logs(self.session), 0)
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_bytes(), original)

    def test_valid_file_cache_does_not_hide_later_no_gps_content(self):
        p = self.plugin
        self.assertEqual(p._pending_sessions(), [self.session])
        self.write_mesh(no_gps=True)
        self.assertEqual(p._pending_sessions(), [])
        self.assertTrue((self.session / "meshtastic_nodes.csv").is_file())
        self.write_mesh()
        self.assertEqual(p._pending_sessions(), [self.session])

    def test_changed_file_is_not_unlinked_using_a_stale_empty_parse(self):
        self.write_mesh(no_gps=True)
        path = self.session / "meshtastic_nodes.csv"
        path.write_text(path.read_text().replace("!12345678", "malformed-node-id"))
        p = self.plugin
        original_parse = p._parse_meshtastic
        def parse_then_new_valid_row(path):
            result = original_parse(path)
            self.write_mesh()
            return result
        with patch.object(p, "_parse_meshtastic", side_effect=parse_then_new_valid_row):
            self.assertEqual(p._prune_upload_logs(self.session), 0)
        self.assertTrue((self.session / "meshtastic_nodes.csv").is_file())
        self.assertEqual(p._pending_sessions(), [self.session])

    def test_nonfinite_wifi_altitude_only_log_is_deleted_without_an_api_call(self):
        (self.session / "meshtastic_nodes.csv").unlink()
        self.write_upload_csvs(valid=True)
        for filename in ("meshtastic_nodes.csv", "meshcore_nodes.csv", "adsb_aircraft.csv"):
            (self.session / filename).unlink()
        path = self.session / "wardriving.csv"
        text = path.read_text()
        path.write_text(text.replace(",10,1,WIFI", ",nan,1,WIFI"))
        with patch("plugins.wardrive_upload._open") as network:
            self.assertEqual(self.plugin._pending_sessions(), [])
        network.assert_not_called()
        self.assertFalse(path.exists())

    def test_actual_loot_manager_updates_waiting_export_without_contact_or_xp_loss(self):
        with patch.object(LootManager, "_periodic_backup_loop", lambda self: None):
            loot = LootManager(str(self.base / "real-loot"))
        try:
            p = self.plugin
            p.app.loot = loot
            session = Path(loot.session_dir)
            data = {"name": "synthetic retained contact", "type": "Client", "lat": 0, "lon": 0,
                    "rssi": -90, "snr": 0, "note": "synthetic retained note"}
            loot.register_mesh_contact("meshtastic", "!12345678", data, current_xp=100)
            original_db = deepcopy(loot._db)
            original_contacts = loot.load_meshtastic_contacts()
            self.assertEqual(p._pending_sessions(), [])
            self.assertTrue((session / "meshtastic_nodes.csv").is_file())
            self.assertEqual(loot._db, original_db)
            self.assertEqual(loot.load_meshtastic_contacts(), original_contacts)
            result = loot.register_mesh_contact("meshtastic", "!12345678",
                dict(data, lat=36.4, lon=-6.2), current_xp=original_db["xp"])
            self.assertFalse(result["new_event"])
            self.assertEqual(result["xp_award"], 0)
            self.assertEqual(result["xp_total"], original_db["xp"])
            self.assertEqual(result["contact"]["note"], "synthetic retained note")
            self.assertTrue((session / "meshtastic_nodes.csv").is_file())
            self.assertEqual(p._pending_sessions(), [session])
        finally:
            with patch.object(os, "sync", lambda: None, create=True):
                loot.close()

    def test_replacing_same_key_receipt_invalidates_pending_cache_without_restart(self):
        p = self.new_plugin(active=self.session.name)
        payload = self.payload(p)
        key = p._receipt_key(payload)
        p._upload_receipts[key] = {"ok": True, "meshcore_imported": 0}
        self.assertEqual(p._pending_sessions(), [self.session])
        self.assertTrue(p._pending_log_checks)
        self.assertEqual(len(p._upload_receipts), 1)
        self.assertTrue(p._remember_receipt(payload, {"ok": True, "meshcore_imported": 1}))
        self.assertEqual(len(p._upload_receipts), 1)
        self.assertEqual(p._pending_log_checks, {})
        self.assertEqual(p._pending_sessions(), [])
        self.assertEqual(self.new_plugin(active=self.session.name)._pending_sessions(), [])

    def test_failed_receipt_replacement_preserves_existing_pending_cache(self):
        p = self.new_plugin(active=self.session.name)
        payload = self.payload(p)
        key = p._receipt_key(payload)
        previous = {"ok": True, "meshcore_imported": 0}
        p._upload_receipts[key] = previous
        self.assertEqual(p._pending_sessions(), [self.session])
        cache = p._pending_log_checks.copy()
        generation = p._receipt_generation
        with patch.object(p, "_save_state", return_value=False):
            self.assertFalse(p._remember_receipt(payload, {"ok": True, "meshcore_imported": 1}))
        self.assertEqual(p._upload_receipts[key], previous)
        self.assertEqual(p._receipt_generation, generation)
        self.assertEqual(p._pending_log_checks, cache)
        self.assertEqual(p._pending_sessions(), [self.session])

    def test_receipt_saved_during_pending_check_invalidates_late_stale_cache_write(self):
        p = self.new_plugin(active=self.session.name)
        payload = self.payload(p)
        key = p._receipt_key(payload)
        p._upload_receipts[key] = {"ok": True, "meshcore_imported": 0}
        cached_receipt = p._cached_receipt
        stored = False
        generation = p._receipt_generation

        def save_after_read(data):
            nonlocal stored
            previous = cached_receipt(data)
            if not stored:
                stored = True
                self.assertTrue(p._remember_receipt(payload, {"ok": True, "meshcore_imported": 1}))
            return previous

        with patch.object(p, "_cached_receipt", side_effect=save_after_read):
            self.assertEqual(p._pending_sessions(), [self.session])
        self.assertEqual(len(p._upload_receipts), 1)
        self.assertEqual(p._receipt_generation, generation + 1)
        self.assertIsNotNone(cached_receipt(payload))
        self.assertEqual(p._pending_sessions(), [])

    def test_pending_cache_changes_when_the_configured_account_or_endpoint_changes(self):
        p = self.new_plugin(active=self.session.name)
        self.assertTrue(p._remember_receipt(self.payload(p), {"ok": True, "meshcore_imported": 1}))
        self.assertEqual(p._pending_sessions(), [])
        original_key, original_url = p._api_key, p._api_url
        p._api_key = "synthetic-account-two"
        self.assertEqual(p._pending_sessions(), [self.session])
        p._api_key = original_key
        self.assertEqual(p._pending_sessions(), [])
        p._api_url = "https://other.example.invalid/api/upload/"
        self.assertEqual(p._pending_sessions(), [self.session])
        p._api_url = original_url
        self.assertEqual(p._pending_sessions(), [])

    def test_reset_upload_history_clears_receipts_and_requeues_unchanged_active_data(self):
        p = self.new_plugin(active=self.session.name)
        self.assertTrue(p._remember_receipt(self.payload(p), {"ok": True, "meshcore_imported": 1}))
        self.assertEqual(p._pending_sessions(), [])
        p._exec("reset")
        self.assertIsNone(p._cached_receipt(self.payload(p)))
        self.assertEqual(p._pending_sessions(), [self.session])

    def test_active_wifi_and_adsb_cleanup_is_refused_on_worker_and_main_thread(self):
        self.write_upload_csvs(mixed=True)
        for filename in ("meshtastic_nodes.csv", "meshcore_nodes.csv"):
            (self.session / filename).unlink()
        p = self.new_plugin(active=self.session.name)
        removed, errors = [], []
        def cleanup_in_worker():
            try:
                removed.append(p._prune_upload_logs(self.session))
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=cleanup_in_worker)
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(removed, [0])
        for filename in ("wardriving.csv", "adsb_aircraft.csv"):
            self.assertTrue((self.session / filename).is_file())
        self.assertEqual(p._prune_upload_logs(self.session), 0)
        for filename in ("wardriving.csv", "adsb_aircraft.csv"):
            self.assertTrue((self.session / filename).is_file())
        self.assertEqual(p._pending_sessions(), [])


if __name__ == "__main__":
    unittest.main()
