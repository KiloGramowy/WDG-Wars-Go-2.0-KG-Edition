"""Local queue cleanup with native saved history; no API, GUI or physical RF."""
import copy
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from plugins.wardrive_upload import WardriveUpload
from test_scan_session_map import loot_at, gps_at, game, receive, WIFI, BLE


def positioned_gps():
    gps = gps_at()
    gps.radio_fix = lambda **kwargs: dict(lat=12.5, lon=34.5,
        observed_at=kwargs["now"] - 0.1, quality=1, sats=8, hdop=0.8)
    return gps


class PendingLogCleanupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        with loot_at(str(self.root), "2020-01-01T12:00:00", gps_at(valid=False)) as loot:
            loot.save_wardriving_network(SimpleNamespace(bssid=WIFI, ssid='"AP, Example',
                auth="WPA2", channel="6", rssi="-60"))
            loot.save_bt_device(BLE, -70, '"BLE, Example', False, False)
            loot.save_wardriving_bt(BLE, -70, '"BLE, Example')
            for protocol, node_id in (("meshcore", "11111111"), ("meshtastic", "!22222222")):
                loot.register_mesh_contact(protocol, node_id,
                    dict(name="Saved", type="Client", lat=0, lon=0, rssi=-90))
            loot.save_adsb_aircraft("abc123", "Saved", 0, 0)
            loot.save_xp(12345)
            loot.save_badge("saved-badge")
            loot.log_serial("Keep the original serial history")
            self.session = Path(loot.session_dir)
        self.loot = self.enterContext(loot_at(str(self.root), "2022-01-01T12:00:00", positioned_gps()))
        with patch.object(WardriveUpload, "_load_state"):
            self.plugin = WardriveUpload()
        self.plugin.app = SimpleNamespace(loot=self.loot, _term_add=lambda *_args, **_kwargs: None)
        self.plugin._api_url = "https://example.invalid/api/upload/"
        self.plugin._api_key = "synthetic-test-only"
        self.plugin._save_state = Mock(return_value=True)
        self.plugin._badge_sync_worker = Mock()
        self.plugin._handle_upload_badges = Mock()
        self.before = copy.deepcopy(self.loot._db)

    def protected(self, loot=None):
        database = (loot or self.loot)._db
        return {key: copy.deepcopy(database.get(key)) for key in
                ("xp", "badges", "mc_contacts", "mt_contacts", "mesh_discovery_events", "sessions", "totals")}

    def entry(self, filename):
        return next(item for item in self.plugin.list_pending_logs()
                    if item["session"] == self.session.name and item["filename"] == filename)

    def delete(self, filename):
        item = self.entry(filename)
        return self.plugin.delete_pending_log(item["session"], filename, item["token"])

    def close_active(self):
        with patch.object(os, "sync", lambda: None, create=True):
            self.loot.close()

    def make_positioned(self, filename, *, mixed=False):
        path = self.session / filename
        lines = path.read_text().splitlines(keepends=True)
        start = 2 if filename == "wardriving.csv" else 1
        output = lines[:start] + (lines[start:] if mixed else [])
        for line in lines[start:]:
            if filename == "wardriving.csv":
                from watchdogs.loot_manager import LootManager
                row = LootManager._wardrive_fields(line)
                row[6:8] = ["12.5", "34.5"]
            else:
                row = next(csv.reader([line]))
                offset = 3 if filename == "adsb_aircraft.csv" else 4
                row[offset:offset + 2] = ["12.5", "34.5"]
            buffer = io.StringIO()
            csv.writer(buffer).writerow(row)
            output.append(buffer.getvalue())
        path.write_text("".join(output))

    def test_read_only_browser_lists_waiting_logs_even_with_zero_uploadable_sessions(self):
        original = {path: path.read_bytes() for path in self.session.iterdir() if path.is_file()}
        entries = self.plugin.list_pending_logs()
        self.assertEqual(len(entries), 4)
        self.assertTrue(all(item["status"] == "waiting_gps" and item["eligible"] == 0
                            and item["no_gps"] > 0 and item["can_delete"] for item in entries))
        self.assertEqual({path: path.read_bytes() for path in original}, original)
        self.assertEqual(self.loot._db, self.before)

    def test_manual_wifi_delete_preserves_native_known_identities_xp_and_other_history_after_restart(self):
        target = self.session / "wardriving.csv"
        originals = {path: path.read_bytes() for path in self.session.iterdir() if path.is_file() and path != target}
        progress = self.protected()
        with patch("plugins.wardrive_upload._open") as network:
            self.assertTrue(self.delete(target.name)["deleted"])
        network.assert_not_called()
        self.assertEqual(self.protected(), progress)
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)
        self.close_active()
        with loot_at(str(self.root), "2023-01-01T12:00:00", positioned_gps()) as reopened:
            value = game(reopened, reopened._gps)
            self.assertEqual(value._known_wifi, {WIFI})
            self.assertEqual(value._known_ble, {BLE})
            self.assertEqual(value.xp, 12345)
            receive(value, "wifi", identity=WIFI)
            receive(value, "bt", identity=BLE)
            self.assertEqual(value.xp, 12347)
            self.assertFalse(target.exists())
            self.assertEqual(reopened.load_badges(), {"saved-badge"})

    def test_bulk_removes_only_unlocated_files_even_for_done_groups_without_new_receipts(self):
        self.make_positioned("wardriving.csv", mixed=True)
        valid = self.session / "wardriving.csv"
        original = valid.read_bytes()
        self.plugin._uploaded_sessions.add(self.session.name)
        self.plugin._uploaded_meshtastic_sessions.add(self.session.name)
        progress = self.protected()
        receipts = copy.deepcopy(self.plugin._upload_receipts)
        with patch("plugins.wardrive_upload._open") as network:
            result = self.plugin.cleanup_no_gps_logs()
        self.assertEqual(result["deleted"], 3)
        self.assertEqual(valid.read_bytes(), original)
        self.assertEqual(self.protected(), progress)
        self.assertEqual(self.plugin._upload_receipts, receipts)
        self.assertEqual(self.plugin._uploaded_sessions, {self.session.name})
        network.assert_not_called()

    def test_all_positioned_and_mixed_files_are_automatically_kept(self):
        for filename in self.plugin._upload_log_specs():
            self.make_positioned(filename, mixed=filename != "adsb_aircraft.csv")
        original = {path: path.read_bytes() for path in self.session.glob("*.csv")}
        self.assertEqual(self.plugin.cleanup_no_gps_logs()["deleted"], 0)
        self.assertEqual({path: path.read_bytes() for path in original}, original)

    def test_nothing_to_upload_cleans_waiting_logs_without_api_key_or_request(self):
        self.plugin._api_key = ""
        with patch("plugins.wardrive_upload._open") as network:
            self.plugin._start_upload()
        network.assert_not_called()
        self.assertFalse(any((self.session / name).exists() for name in self.plugin._upload_log_specs()))
        self.assertEqual(self.loot.load_xp(), 12345)
        self.assertEqual(self.plugin._upload_receipts, {})

    def test_finished_upload_attempt_cleans_waiting_files_without_fabricating_credit(self):
        with patch("plugins.wardrive_upload._open") as network:
            self.plugin._upload_worker([self.session])
        network.assert_not_called()
        self.assertEqual(self.plugin._upload_receipts, {})
        self.assertEqual(self.plugin._uploaded_sessions, set())
        self.assertFalse((self.session / "meshtastic_nodes.csv").exists())
        self.plugin._handle_upload_badges.assert_not_called()

    def test_periodic_prune_preserves_native_wifi_identity_after_restart(self):
        self.assertEqual(self.plugin._prune_upload_logs(self.session), 2)
        self.close_active()
        with loot_at(str(self.root), "2023-01-01T12:00:00", gps_at()) as reopened:
            value = game(reopened, reopened._gps)
            self.assertEqual(value._known_wifi, {WIFI})
            self.assertEqual(value._known_ble, {BLE})
            receive(value, "wifi", identity=WIFI)
            self.assertEqual(value.xp, 12346)

    def test_new_gps_after_bulk_preview_preserves_the_changed_export(self):
        entry = self.entry("meshtastic_nodes.csv")
        self.make_positioned(entry["filename"])
        path = self.session / entry["filename"]
        original = path.read_bytes()
        with patch.object(self.plugin, "list_pending_logs", return_value=[entry]):
            result = self.plugin.cleanup_no_gps_logs()
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(path.read_bytes(), original)

    def test_discard_generation_survives_restart_blocks_gps_backfill_and_fresh_rx_unblocks(self):
        for filename in ("meshcore_nodes.csv", "meshtastic_nodes.csv"):
            self.assertTrue(self.delete(filename)["deleted"])
        progress = self.protected()
        self.assertFalse(any(self.loot.assign_receiver_gps_to_unlocated_contacts()["contacts"].values()))
        self.assertFalse((Path(self.loot.session_dir) / "meshtastic_nodes.csv").exists())
        self.assertEqual(self.protected(), progress)
        self.close_active()
        with loot_at(str(self.root), "2023-01-01T12:00:00", positioned_gps()) as reopened:
            self.assertFalse(any(reopened.assign_receiver_gps_to_unlocated_contacts()["contacts"].values()))
            outcome = reopened.register_mesh_contact("meshtastic", "!22222222",
                dict(type="Client", name="Fresh RX", lat=0, lon=0, rssi=-80), current_xp=12345)
            self.assertEqual(outcome["xp_award"], 0)
            self.assertEqual(outcome["xp_total"], 12345)
            self.assertNotIn("!22222222", reopened._db["discarded_mesh_uploads"]["meshtastic"])
            self.assertEqual(len(reopened.assign_receiver_gps_to_unlocated_contacts()["contacts"]["meshtastic"]), 1)
            self.assertTrue((Path(reopened.session_dir) / "meshtastic_nodes.csv").is_file())

    def test_deleting_old_export_does_not_discard_newer_fresh_reception_before_restart(self):
        self.loot.register_mesh_contact("meshtastic", "!22222222",
            dict(type="Client", name="New current RX", lat=0, lon=0, rssi=-75), current_xp=12345)
        self.assertTrue(self.delete("meshtastic_nodes.csv")["deleted"])
        self.assertNotIn("!22222222", self.loot._db["discarded_mesh_uploads"]["meshtastic"])
        self.close_active()
        with loot_at(str(self.root), "2023-01-01T12:00:00", positioned_gps()) as reopened:
            changed = reopened.assign_receiver_gps_to_unlocated_contacts()["contacts"]["meshtastic"]
            self.assertEqual(len(changed), 1)
            self.assertEqual(changed[0]["name"], "New current RX")
            self.assertTrue((Path(reopened.session_dir) / "meshtastic_nodes.csv").exists())
            self.assertEqual(reopened.load_xp(), 12345)

    def test_active_inflight_stale_unreadable_and_unsupported_targets_are_kept(self):
        item = self.entry("wardriving.csv")
        path = self.session / item["filename"]
        self.plugin._uploading = True
        self.assertFalse(self.plugin.delete_pending_log(item["session"], item["filename"], item["token"])["deleted"])
        self.assertEqual(self.plugin.cleanup_no_gps_logs()["deleted"], 0)
        self.plugin._uploading = False
        path.write_bytes(path.read_bytes() + b"\n")
        self.assertFalse(self.plugin.delete_pending_log(item["session"], item["filename"], item["token"])["deleted"])
        for session, filename in (("..", item["filename"]), (item["session"], "loot_db.json"),
                                  (item["session"] + "/nested", item["filename"])):
            self.assertFalse(self.plugin.delete_pending_log(session, filename, item["token"])["deleted"])
        active_file = Path(self.loot.session_dir) / "meshtastic_nodes.csv"
        active_file.write_bytes((self.session / "meshtastic_nodes.csv").read_bytes())
        active = next(row for row in self.plugin.list_pending_logs() if row["session"] == Path(self.loot.session_dir).name)
        self.assertFalse(active["can_delete"])
        self.assertFalse(self.plugin.delete_pending_log(active["session"], active["filename"], active["token"])["deleted"])
        self.assertEqual(self.plugin._prune_upload_logs(Path(self.loot.session_dir)), 0)
        original = path.read_bytes()
        path.write_bytes(b"\xffinvalid UTF-8")
        self.assertFalse(self.entry(path.name)["can_delete"])
        self.assertEqual(self.plugin.cleanup_no_gps_logs()["deleted"], 3)
        self.assertEqual(path.read_bytes(), b"\xffinvalid UTF-8")
        path.write_bytes(original)

    def test_preservation_write_error_and_unlink_error_never_claim_deletion(self):
        path = self.session / "wardriving.csv"
        original = path.read_bytes()
        with patch.object(self.loot, "_save_db", return_value=False):
            self.assertFalse(self.delete(path.name)["deleted"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.loot._db, self.before)
        unlink = Path.unlink
        def fail_target(current, *args, **kwargs):
            if current == path:
                raise OSError("synthetic delete failure")
            return unlink(current, *args, **kwargs)
        with patch.object(Path, "unlink", fail_target):
            self.assertFalse(self.delete(path.name)["deleted"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.loot.load_xp(), 12345)

    def test_confirmed_payload_without_session_flag_is_protected_and_receipt_bytes_stay(self):
        self.make_positioned("wardriving.csv")
        payload = self.plugin._session_upload_data(self.session)
        receipt = {"ok": True, "imported": len(payload["networks"]), "rejected": 0}
        self.assertTrue(self.plugin._remember_receipt(payload, receipt))
        saved = copy.deepcopy(self.plugin._upload_receipts)
        entry = self.entry("wardriving.csv")
        self.assertEqual(entry["status"], "confirmed")
        self.assertFalse(entry["can_delete"])
        self.assertFalse(self.delete("wardriving.csv")["deleted"])
        self.assertEqual(self.plugin._upload_receipts, saved)

    def test_background_cleanup_uses_closed_files_only_and_invalidates_caches(self):
        item = self.entry("meshtastic_nodes.csv")
        path = self.session / item["filename"]
        self.plugin._upload_log_checks[str(path)] = ("old",)
        self.plugin._pending_log_checks[str(self.session)] = ("old",)
        self.loot._gps_points_cache = [dict(lat=1, lon=2)]
        outcomes = []
        thread = threading.Thread(target=lambda: outcomes.append(self.plugin.cleanup_no_gps_logs()))
        thread.start()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes[0]["deleted"], 4)
        self.assertNotIn(str(path), self.plugin._upload_log_checks)
        self.assertNotIn(str(self.session), self.plugin._pending_log_checks)
        self.assertIsNone(self.loot._gps_points_cache)

    def test_file_and_session_symlinks_are_never_followed(self):
        path = self.session / "meshtastic_nodes.csv"
        saved = path.with_suffix(".keep")
        path.replace(saved)
        try:
            path.symlink_to(saved)
        except OSError:
            self.skipTest("Creating symlinks requires platform privileges")
        original = saved.read_bytes()
        item = self.entry(path.name)
        self.assertFalse(item["can_delete"])
        self.assertFalse(self.plugin.delete_pending_log(item["session"], path.name, item["token"])["deleted"])
        alias = self.loot._base / "linked-session"
        alias.symlink_to(self.session, target_is_directory=True)
        self.assertFalse(self.plugin.delete_pending_log(alias.name, path.name, item["token"])["deleted"])
        self.assertEqual(saved.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
