"""Uploaded-only retention with real temporary loot, mocked HTTP and clock."""

import copy
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from plugins.wardrive_upload import WardriveUpload
from test_scan_session_map import loot_at, gps_at, WIFI, BLE


DAY = 86400
EXPORTS = ("wardriving.csv", "adsb_aircraft.csv", "meshcore_nodes.csv", "meshtastic_nodes.csv")


class UploadedLogRetentionCleanupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state = self.root / "synthetic-sync-state.json"
        with loot_at(str(self.root), "2020-01-01T12:00:00", gps_at()) as loot:
            loot.save_wardriving_network(SimpleNamespace(bssid=WIFI, ssid="Synthetic WiFi",
                auth="WPA2", channel="6", rssi="-60"))
            loot.save_bt_device(BLE, -70, "Synthetic BLE", False, False)
            loot.save_wardriving_bt(BLE, -70, "Synthetic BLE")
            for protocol, identity in (("meshcore", "11111111"), ("meshtastic", "!22222222")):
                loot.register_mesh_contact(protocol, identity,
                    dict(name="Saved", type="Client", lat=12.5, lon=34.5, rssi=-90))
            loot.save_adsb_aircraft("abc123", "Synthetic", 12.5, 34.5)
            loot.save_xp(12345)
            loot.save_badge("saved-badge")
            loot.log_serial("Keep the original serial history")
            self.session = Path(loot.session_dir)
        self.extra = self.session / "keep-this.txt"
        self.extra.write_bytes(b"not an uploaded export")
        self.loot = self.enterContext(loot_at(str(self.root), "2022-01-01T12:00:00", gps_at()))
        self.now = 1_800_000_000.0
        self.plugin = self.new_plugin()

    def new_plugin(self, *, key="synthetic-account", url="https://example.invalid/api/upload/"):
        with patch.object(WardriveUpload, "_state_file", return_value=self.state), \
                patch("plugins.wardrive_upload._load_secrets_conf", return_value={
                    "WARDRIVE_API_KEY": key, "WARDRIVE_API_URL": url}):
            plugin = WardriveUpload()
        plugin._state_file = lambda: self.state
        plugin.app = SimpleNamespace(loot=self.loot, _term_add=lambda *_a, **_k: None)
        plugin._badge_sync_worker = Mock()
        plugin._handle_upload_badges = Mock()
        plugin._get_retention()._clock = lambda: self.now
        return plugin

    def data(self, plugin=None):
        return (plugin or self.plugin)._session_upload_data(self.session)

    def proof_rows(self, plugin=None):
        path = (plugin or self.plugin)._get_retention().path
        return json.loads(path.read_text(encoding="utf-8"))["proofs"] if path.exists() else []

    def files(self):
        return {name: (self.session / name).read_bytes() for name in EXPORTS
                if (self.session / name).exists()}

    def progress(self):
        return {key: copy.deepcopy(self.loot._db.get(key)) for key in
                ("xp", "badges", "mc_contacts", "mt_contacts", "mesh_discovery_events", "sessions", "totals")}

    def upload(self, result=None, mutate=None):
        data = self.data()
        if result is None:
            result = dict(ok=True, imported=len(data["networks"]),
                aircraft_imported=len(data["aircraft"]), meshcore_imported=len(data["meshcore_nodes"]))
        def response(*_a, **_k):
            if mutate:
                mutate()
            return io.BytesIO(json.dumps(result).encode())
        self.plugin._uploading = True
        with patch("plugins.wardrive_upload._open", side_effect=response) as http:
            self.plugin._upload_worker([self.session])
        return http

    def uploaded_and_aged(self, days=1):
        self.assertTrue(self.plugin._set_retention_days(days))
        self.upload()
        self.assertEqual(len(self.proof_rows()), 4)
        self.now += days * DAY

    def test_default_keep_all_records_success_without_deleting_any_exports(self):
        original = self.files()
        self.upload()
        self.assertEqual(self.plugin._get_retention().days, 0)
        self.assertEqual(len(self.proof_rows()), 4)
        self.now += 100 * DAY
        self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 0)
        self.assertEqual(self.files(), original)

    def test_complete_ack_deletes_only_four_exports_and_preserves_real_player_history(self):
        self.uploaded_and_aged()
        progress = self.progress()
        other = {p.name: p.read_bytes() for p in self.session.iterdir()
                 if p.is_file() and p.name not in EXPORTS}
        with patch("plugins.wardrive_upload._open") as http:
            result = self.plugin.cleanup_uploaded_logs()
        self.assertEqual(result["deleted"], 4)
        self.assertFalse(self.files())
        self.assertTrue(self.session.is_dir())
        self.assertEqual({p.name: p.read_bytes() for p in self.session.iterdir() if p.is_file()}, other)
        self.assertEqual(self.progress(), progress)
        self.assertEqual(self.loot.load_xp(), 12345)
        self.assertIn("saved-badge", self.loot.load_badges())
        known = self.loot._db["known_device_identities"]
        self.assertIn(WIFI, known["wifi"])
        self.assertIn(BLE, known["ble"])
        self.assertFalse(self.proof_rows())
        http.assert_not_called()

    def test_age_uses_first_confirmation_not_old_file_or_session_date(self):
        self.assertTrue(self.plugin._set_retention_days(1))
        for name in EXPORTS:
            os.utime(self.session / name, (1, 1))
        self.upload()
        self.assertTrue(all(p["confirmed_at"] == self.now for p in self.proof_rows()))
        self.now += DAY - 0.001
        self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 0)
        self.assertEqual(len(self.files()), 4)
        self.now += 0.001
        self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 4)

    def test_each_preset_and_custom_uses_exact_day_boundary(self):
        self.upload()
        original_now = self.now
        for days in (1, 5, 15, 31, 42):
            with self.subTest(days=days):
                self.assertTrue(self.plugin._set_retention_days(days))
                self.now = original_now + days * DAY - 0.01
                self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 0)
                self.assertEqual(len(self.plugin._get_retention().due(now=original_now + days * DAY)), 4)

    def test_policy_and_exact_confirmations_survive_plugin_restart(self):
        self.uploaded_and_aged(5)
        fresh = self.new_plugin()
        self.assertEqual(fresh._get_retention().days, 5)
        self.assertEqual(fresh.cleanup_uploaded_logs()["deleted"], 4)

    def test_repeated_exact_ack_does_not_restart_confirmation_age(self):
        self.assertTrue(self.plugin._set_retention_days(1))
        data = self.data()
        captured = self.plugin._retention_capture(self.session, data)
        self.upload()
        first = self.now
        self.now += DAY - 1
        self.plugin._retention_confirm_files(captured, data)
        self.assertTrue(all(p["confirmed_at"] == first for p in self.proof_rows()))
        self.now += 1
        self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 4)

    def test_rejected_or_queued_receipts_do_not_create_deletion_authority(self):
        for result in ({"ok": False}, {"ok": True, "job_id": "synthetic-job"},
                       {"ok": True, "imported": 1, "rejected": 1},
                       {"ok": True, "info": "already uploaded"}):
            with self.subTest(result=result):
                self.plugin = self.new_plugin()
                self.assertTrue(self.plugin._set_retention_days(1))
                with patch.object(self.plugin, "_verify_duplicate_mesh", return_value=False):
                    self.upload(result)
                self.assertFalse(self.proof_rows())
                self.now += 100 * DAY
                self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 0)
                self.assertEqual(len(self.files()), 4)

    def test_registry_presence_and_legacy_uploaded_flags_are_not_file_acknowledgements(self):
        self.assertTrue(self.plugin._set_retention_days(1))
        self.plugin._uploaded_sessions.add(self.session.name)
        self.plugin._uploaded_meshtastic_sessions.add(self.session.name)
        self.plugin._save_state()
        self.upload()
        self.now += 100 * DAY
        self.assertFalse(self.proof_rows())
        self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 0)
        self.assertEqual(len(self.files()), 4)
        self.plugin._uploaded_sessions.clear()
        self.plugin._uploaded_meshtastic_sessions.clear()
        with patch.object(self.plugin, "_cached_receipt", return_value=None), \
                patch.object(self.plugin, "_cached_mesh_verification", return_value=True):
            self.upload()
        self.assertFalse(self.proof_rows())

    def test_receipt_persistence_failure_never_mints_retention_proof(self):
        self.assertTrue(self.plugin._set_retention_days(1))
        with patch.object(self.plugin, "_save_state", return_value=False):
            self.upload()
        self.now += 100 * DAY
        self.assertFalse(self.proof_rows())
        self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 0)
        self.assertEqual(len(self.files()), 4)

    def test_retention_ledger_write_failure_keeps_uploaded_files(self):
        store = self.plugin._get_retention()
        with patch.object(store, "record_many", return_value=False):
            self.upload()
        self.assertIn(self.session.name, self.plugin._uploaded_sessions)
        self.assertEqual(len(self.files()), 4)
        self.assertFalse(self.proof_rows())

    def test_changed_file_during_server_request_does_not_get_a_proof(self):
        original = self.session / "wardriving.csv"
        self.upload(mutate=lambda: original.write_text(original.read_text() + "malformed\n"))
        self.assertNotIn("wardriving.csv", {p["filename"] for p in self.proof_rows()})
        self.assertEqual(len(self.proof_rows()), 3)

    def test_account_or_endpoint_change_during_http_never_reassigns_old_ack(self):
        for field, value in (("_api_key", "different-synthetic-account"),
                             ("_api_url", "https://different.invalid/api/upload/")):
            with self.subTest(field=field):
                self.plugin = self.new_plugin()
                original_url, original_key = self.plugin._api_url, self.plugin._api_key
                originals = self.files()
                http = self.upload(mutate=lambda: setattr(self.plugin, field, value))
                self.assertFalse(self.proof_rows())
                self.assertEqual(self.plugin._upload_receipts, {})
                self.assertNotIn(self.session.name, self.plugin._uploaded_sessions)
                self.assertEqual(self.files(), originals)
                request = http.call_args.args[0]
                self.assertEqual(request.full_url, original_url)
                self.assertEqual(request.get_header("X-api-key"), original_key)
                payload = json.loads(request.data)
                expected = hmac.new(original_key.encode(),
                    (payload["nonce"] + payload["data"]).encode(), hashlib.sha256).hexdigest()
                self.assertEqual(payload["sig"], expected)

    def test_malformed_wifi_row_keeps_the_whole_mixed_file(self):
        path = self.session / "wardriving.csv"
        path.write_text(path.read_text() + "not,a,usable,row\n")
        self.uploaded_and_aged_except("wardriving.csv")

    def uploaded_and_aged_except(self, filename):
        self.assertTrue(self.plugin._set_retention_days(1))
        self.upload()
        self.assertNotIn(filename, {p["filename"] for p in self.proof_rows()})
        self.now += DAY
        result = self.plugin.cleanup_uploaded_logs()
        self.assertEqual(result["deleted"], 3)
        self.assertTrue((self.session / filename).exists())

    def test_no_gps_wifi_row_keeps_the_whole_mixed_file(self):
        path = self.session / "wardriving.csv"
        path.write_text(path.read_text() +
            "02:00:00:00:00:FF,NoGPS,WPA2,now,6,-60,0,0,0,0,WIFI\n")
        self.uploaded_and_aged_except("wardriving.csv")

    def test_wifi_observation_in_place_of_preheader_is_not_silently_certified(self):
        path = self.session / "wardriving.csv"
        lines = path.read_text().splitlines(keepends=True)
        lines[0] = lines[2].replace(WIFI, "02:00:00:00:00:FE")
        path.write_text("".join(lines))
        self.uploaded_and_aged_except("wardriving.csv")

    def test_aircraft_observation_in_place_of_header_is_not_silently_certified(self):
        path = self.session / "adsb_aircraft.csv"
        lines = path.read_text().splitlines(keepends=True)
        lines[0] = lines[1].replace("abc123", "def456")
        path.write_text("".join(lines))
        self.uploaded_and_aged_except("adsb_aircraft.csv")

    def test_deduplicated_extra_aircraft_row_keeps_its_unsent_history(self):
        path = self.session / "adsb_aircraft.csv"
        rows = path.read_text().splitlines(keepends=True)
        path.write_text("".join(rows) + rows[-1])
        self.uploaded_and_aged_except("adsb_aircraft.csv")

    def test_invalid_extra_mesh_row_keeps_its_unsent_history(self):
        path = self.session / "meshcore_nodes.csv"
        path.write_text(path.read_text() + "now,not-a-node,Client,Invalid,12.5,34.5,-90,0\n")
        self.uploaded_and_aged_except("meshcore_nodes.csv")

    def test_no_gps_extra_mesh_row_keeps_its_unsent_history(self):
        path = self.session / "meshtastic_nodes.csv"
        lines = path.read_text().splitlines(keepends=True)
        # Native header may include extra provenance fields; retain its shape.
        import csv
        row = next(csv.reader([lines[-1]]))
        header = next(csv.reader([lines[0]]))
        row[header.index("node_id")] = "!33333333"
        row[header.index("lat")] = row[header.index("lon")] = "0"
        buffer = io.StringIO()
        csv.writer(buffer).writerow(row)
        path.write_text("".join(lines) + buffer.getvalue())
        self.uploaded_and_aged_except("meshtastic_nodes.csv")

    def test_changed_after_confirmation_is_kept_without_another_ack(self):
        self.uploaded_and_aged()
        path = self.session / "wardriving.csv"
        path.write_bytes(path.read_bytes().replace(b"Synthetic WiFi", b"Changed WiFi"))
        self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 3)
        self.assertTrue(path.exists())

    def test_changed_account_or_endpoint_keeps_confirmed_files(self):
        self.uploaded_and_aged()
        for key, url in (("another-account", self.plugin._api_url),
                         (self.plugin._api_key, "https://elsewhere.invalid/api/upload/")):
            with self.subTest(key=key, url=url):
                fresh = self.new_plugin(key=key, url=url)
                self.assertEqual(fresh.cleanup_uploaded_logs()["deleted"], 0)
                self.assertEqual(len(self.files()), 4)

    def test_removed_or_changed_durable_ack_keeps_files(self):
        self.uploaded_and_aged()
        receipts = copy.deepcopy(self.plugin._upload_receipts)
        self.plugin._upload_receipts.clear()
        self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 0)
        self.plugin._upload_receipts = receipts
        key = self.proof_rows()[0]["receipt_key"]
        self.plugin._upload_receipts[key] = {"ok": True, "imported": 0}
        self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 0)
        self.assertEqual(len(self.files()), 4)

    def test_active_session_upload_in_flight_and_open_browser_are_protected(self):
        self.uploaded_and_aged()
        with patch.object(self.plugin, "_active_session_name", return_value=self.session.name):
            self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 0)
        for field in ("_uploading", "_pending_browser"):
            with self.subTest(field=field):
                setattr(self.plugin, field, True)
                self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 0)
                setattr(self.plugin, field, False)
        self.assertEqual(len(self.files()), 4)

    def test_history_persistence_failure_prevents_unlink(self):
        self.uploaded_and_aged()
        with patch.object(self.loot, "prepare_upload_log_discard", return_value=False):
            self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 0)
        self.assertEqual(len(self.files()), 4)
        self.assertEqual(len(self.proof_rows()), 4)

    def test_history_preservation_error_keeps_files_and_reports_error(self):
        self.uploaded_and_aged()
        with patch.object(self.loot, "prepare_upload_log_discard", side_effect=OSError("synthetic")):
            result = self.plugin.cleanup_uploaded_logs()
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["errors"], 4)
        self.assertEqual(len(self.files()), 4)

    def test_keep_all_after_enabling_cancels_due_deletion(self):
        self.uploaded_and_aged()
        self.assertTrue(self.plugin._set_retention_days(0))
        self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 0)
        self.assertEqual(len(self.files()), 4)

    def test_changed_during_history_preservation_is_rechecked(self):
        self.uploaded_and_aged()
        original = self.loot.prepare_upload_log_discard
        def mutate(directory, filename, digest):
            result = original(directory, filename, digest)
            if filename == "wardriving.csv":
                path = directory / filename
                path.write_text(path.read_text() + "changed\n")
            return result
        with patch.object(self.loot, "prepare_upload_log_discard", side_effect=mutate):
            self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 3)
        self.assertTrue((self.session / "wardriving.csv").exists())

    def test_upload_start_during_history_preservation_cancels_deletion(self):
        self.uploaded_and_aged()
        original = self.loot.prepare_upload_log_discard
        def flip(*args):
            result = original(*args)
            self.plugin._uploading = True
            return result
        with patch.object(self.loot, "prepare_upload_log_discard", side_effect=flip):
            self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 0)
        self.plugin._uploading = False
        self.assertEqual(len(self.files()), 4)

    def test_session_becoming_active_during_history_preservation_cancels_deletion(self):
        self.uploaded_and_aged()
        active = {"name": Path(self.loot.session_dir).name}
        original = self.loot.prepare_upload_log_discard
        def flip(*args):
            result = original(*args)
            active["name"] = self.session.name
            return result
        with patch.object(self.plugin, "_active_session_name", side_effect=lambda: active["name"]), \
                patch.object(self.loot, "prepare_upload_log_discard", side_effect=flip):
            self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 0)
        self.assertEqual(len(self.files()), 4)

    def test_account_change_during_history_preservation_cancels_deletion(self):
        self.uploaded_and_aged()
        original = self.loot.prepare_upload_log_discard
        def flip(*args):
            result = original(*args)
            self.plugin._api_key = "a-different-synthetic-account"
            return result
        with patch.object(self.loot, "prepare_upload_log_discard", side_effect=flip):
            self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 0)
        self.assertEqual(len(self.files()), 4)

    def test_ack_revocation_during_history_preservation_cancels_deletion(self):
        self.uploaded_and_aged()
        original = self.loot.prepare_upload_log_discard
        def flip(*args):
            result = original(*args)
            self.plugin._upload_receipts.clear()
            return result
        with patch.object(self.loot, "prepare_upload_log_discard", side_effect=flip):
            self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 0)
        self.assertEqual(len(self.files()), 4)

    def test_historical_exact_ack_starts_age_at_first_verification_not_old_mtime(self):
        data = self.data()
        self.assertTrue(self.plugin._remember_receipt(data, dict(ok=True,
            imported=len(data["networks"]), aircraft_imported=len(data["aircraft"]),
            meshcore_imported=len(data["meshcore_nodes"]))))
        self.assertTrue(self.plugin._mark_uploaded(self.session, meshtastic=True))
        for name in EXPORTS:
            os.utime(self.session / name, (1, 1))
        self.assertFalse(self.proof_rows())
        self.assertTrue(self.plugin._set_retention_days(1))
        with patch("plugins.wardrive_upload._open") as http:
            self.plugin._retention_tick()
        self.assertEqual(len(self.proof_rows()), 4)
        self.assertTrue(all(p["confirmed_at"] == self.now for p in self.proof_rows()))
        self.assertEqual(len(self.files()), 4)
        http.assert_not_called()
        self.now += DAY
        self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 4)

    def test_historical_separate_legacy_and_meshtastic_acks_seed_their_own_files(self):
        data = self.data()
        legacy = dict(data, meshcore_nodes=[row for row in data["meshcore_nodes"]
                                           if row["network"] != "meshtastic"])
        mt_only = dict(networks=[], aircraft=[], meshcore_nodes=[row
            for row in data["meshcore_nodes"] if row["network"] == "meshtastic"])
        self.assertTrue(self.plugin._remember_receipt(legacy, dict(ok=True,
            imported=len(legacy["networks"]), aircraft_imported=len(legacy["aircraft"]),
            meshcore_imported=len(legacy["meshcore_nodes"]))))
        self.assertTrue(self.plugin._remember_receipt(mt_only, dict(ok=True,
            meshcore_imported=len(mt_only["meshcore_nodes"]))))
        self.assertTrue(self.plugin._mark_uploaded(self.session, meshtastic=True))
        self.assertTrue(self.plugin._set_retention_days(1))
        self.plugin._retention_seed_existing()
        proofs = {item["filename"]: item for item in self.proof_rows()}
        self.assertEqual(set(proofs), set(EXPORTS))
        self.assertEqual(proofs["meshtastic_nodes.csv"]["receipt_key"], self.plugin._receipt_key(mt_only))
        for name in EXPORTS[:3]:
            self.assertEqual(proofs[name]["receipt_key"], self.plugin._receipt_key(legacy))

    def test_historical_flags_and_registry_presence_without_full_ack_do_not_seed_proofs(self):
        self.plugin._uploaded_sessions.add(self.session.name)
        self.plugin._uploaded_meshtastic_sessions.add(self.session.name)
        self.assertTrue(self.plugin._set_retention_days(1))
        self.plugin._retention_seed_existing()
        self.assertFalse(self.proof_rows())
        for name in ("wardriving.csv", "adsb_aircraft.csv"):
            (self.session / name).unlink()
        data = {"networks": [], "aircraft": [], "meshcore_nodes":
            self.plugin._merge_mesh_nodes(self.plugin._parse_meshcore(self.session / "meshcore_nodes.csv"),
                self.plugin._parse_meshtastic(self.session / "meshtastic_nodes.csv"))}
        self.assertTrue(self.plugin._store_receipt(data, dict(verification="mesh_registry",
            source=self.plugin._mesh_registry_url(), matched=len(data["meshcore_nodes"]))))
        self.assertTrue(self.plugin._cached_mesh_verification(data))
        self.plugin._retention_seed_scope = None
        self.plugin._retention_seed_existing()
        self.assertFalse(self.proof_rows())
        self.assertEqual(len(self.files()), 2)

    def test_seed_existing_does_not_refresh_an_already_confirmed_file_age(self):
        self.assertTrue(self.plugin._set_retention_days(1))
        self.upload()
        first = self.now
        self.now += DAY - 1
        self.plugin._retention_seed_existing()
        self.assertTrue(all(p["confirmed_at"] == first for p in self.proof_rows()))
        self.now += 1
        self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 4)

    def test_default_keep_all_never_scans_historical_sessions(self):
        with patch.object(Path, "iterdir", side_effect=AssertionError("unexpected catalog scan")), \
                patch.object(self.plugin, "_parse_csv") as parser, \
                patch.object(self.plugin, "_cached_receipt") as receipt:
            self.plugin._retention_seed_existing()
        parser.assert_not_called()
        receipt.assert_not_called()

    def test_historical_seed_processes_at_most_eight_sessions_per_tick(self):
        self.assertTrue(self.plugin._set_retention_days(1))
        for index in range(10):
            (Path(self.loot._base) / f"00-synthetic-{index:02}").mkdir()
        with patch.object(self.plugin, "_parse_csv", return_value=[]) as wifi, \
                patch.object(self.plugin, "_parse_adsb", return_value=[]), \
                patch.object(self.plugin, "_parse_meshcore", return_value=[]), \
                patch.object(self.plugin, "_parse_meshtastic", return_value=[]):
            self.plugin._retention_seed_existing()
            self.assertEqual(wifi.call_count, 8)
            self.plugin._retention_seed_existing()
            self.assertEqual(wifi.call_count, 11)  # ten fixtures + closed; active is skipped
            self.plugin._retention_seed_existing()
            self.assertEqual(wifi.call_count, 11)

    def make_due_copies(self, count=11):
        """Exact same acknowledged observations, copied into closed test exports."""
        data = self.data()
        ack = dict(ok=True, imported=len(data["networks"]),
            aircraft_imported=len(data["aircraft"]), meshcore_imported=len(data["meshcore_nodes"]))
        self.assertTrue(self.plugin._remember_receipt(data, ack))
        self.assertTrue(self.plugin._set_retention_days(1))
        content = (self.session / "wardriving.csv").read_bytes()
        paths, files = [], []
        for index in range(count):
            directory = Path(self.loot._base) / f"batch-{index:02}"
            directory.mkdir()
            path = directory / "wardriving.csv"
            path.write_bytes(content)
            paths.append(path)
            files.append(dict(session=directory.name, filename=path.name,
                digest=hashlib.sha256(content).hexdigest()))
        self.assertTrue(self.plugin._get_retention().record_many(files,
            scope=self.plugin._retention_scope(), receipt_key=self.plugin._receipt_key(data),
            receipt_digest=self.plugin._retention_receipt_digest(ack)))
        self.now += DAY
        return paths

    def test_cleanup_limits_each_call_to_eight_due_files_then_finishes_remaining_three(self):
        paths = self.make_due_copies()
        self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 8)
        self.assertEqual(sum(path.exists() for path in paths), 3)
        self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 3)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(len(self.files()), 4)

    def test_eight_stale_due_files_do_not_starve_later_confirmed_exports(self):
        paths = self.make_due_copies()
        for path in paths[:8]:
            path.write_bytes(path.read_bytes() + b"changed after upload\n")
        with patch("plugins.wardrive_upload.time.monotonic", return_value=100):
            self.plugin._retention_tick()
        self.assertTrue(all(path.exists() for path in paths))
        with patch("plugins.wardrive_upload.time.monotonic", return_value=160):
            self.plugin._retention_tick()
        self.assertTrue(all(path.exists() for path in paths[:8]))
        self.assertTrue(all(not path.exists() for path in paths[8:]))
        self.assertEqual(len(self.files()), 4)

    def test_tick_handles_historical_scan_error_without_crashing_or_deleting(self):
        self.assertTrue(self.plugin._set_retention_days(1))
        with patch.object(self.plugin, "_retention_seed_existing", side_effect=OSError("synthetic")), \
                patch.object(self.plugin, "cleanup_uploaded_logs") as cleanup, \
                patch.object(self.plugin, "_sync_log") as report:
            self.plugin._retention_tick()
        cleanup.assert_not_called()
        report.assert_called_once()
        self.assertEqual(len(self.files()), 4)

    def test_symlink_export_never_removes_external_target(self):
        self.uploaded_and_aged()
        target = self.root / "outside.csv"
        target.write_bytes((self.session / "wardriving.csv").read_bytes())
        path = self.session / "wardriving.csv"
        path.unlink()
        try:
            path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertEqual(self.plugin.cleanup_uploaded_logs()["deleted"], 3)
        self.assertTrue(path.is_symlink())
        self.assertTrue(target.is_file())

    def test_keep_all_and_disabled_tick_do_not_scan_or_parse_logs(self):
        with patch.object(self.plugin, "list_pending_logs") as catalog, \
                patch.object(self.plugin, "_retention_capture") as capture, \
                patch.object(self.plugin._get_retention(), "due") as due:
            self.plugin._retention_tick()
        catalog.assert_not_called()
        capture.assert_not_called()
        due.assert_not_called()

    def test_retention_tick_runs_at_most_once_per_minute_and_not_while_browsing(self):
        with patch.object(self.plugin, "cleanup_uploaded_logs") as cleanup, \
                patch("plugins.wardrive_upload.time.monotonic", return_value=100):
            self.plugin._retention_tick()
            self.plugin._retention_tick()
            self.assertEqual(cleanup.call_count, 1)
        with patch.object(self.plugin, "cleanup_uploaded_logs") as cleanup, \
                patch("plugins.wardrive_upload.time.monotonic", return_value=159.9):
            self.plugin._retention_tick()
            cleanup.assert_not_called()
        with patch.object(self.plugin, "cleanup_uploaded_logs") as cleanup, \
                patch("plugins.wardrive_upload.time.monotonic", return_value=160):
            self.plugin._pending_browser = True
            self.plugin._retention_tick()
            cleanup.assert_not_called()
            self.plugin._pending_browser = False
            self.plugin._retention_tick()
            cleanup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
