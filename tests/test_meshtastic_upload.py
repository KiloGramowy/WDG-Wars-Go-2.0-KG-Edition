"""Upload acknowledgement and protocol boundaries; no network calls."""
import base64
import csv
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from plugins.wardrive_upload import WardriveUpload, _upload_receipt
from watchdogs.loot_manager import LootManager


class UploadTests(unittest.TestCase):
    def plugin(self, base):
        with patch.object(WardriveUpload, "_load_state"):
            p = WardriveUpload()
        p.app = SimpleNamespace(loot=SimpleNamespace(_base=base, session_path=Path(base)/"active",
                                prepare_upload_log_discard=lambda *_args: True),
                                _term_add=lambda *args, **kwargs: None)
        p._api_url = "https://example.invalid/api/upload/"
        p._api_key = "test-only"
        p._save_state = lambda: None
        p._badge_sync_worker = lambda: None
        p._handle_upload_badges = lambda result: None
        return p

    def mesh_csv(self, session, filename="meshtastic_nodes.csv", rows=None):
        fields = ["timestamp", "node_id", "type", "name", "lat", "lon", "rssi", "snr"]
        with (session/filename).open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(fields)
            writer.writerows(rows or [["2026-10-29T12:00:00", "!12345678", "Client", "synthetic", 36.4, -6.2, -110, -5]])

    def response(self, body):
        return io.BytesIO(json.dumps(body).encode())

    def test_http_success_not_application_acknowledgement(self):
        for body in ({}, {"ok": False}, {"ok": True}, {"ok": True, "imported": "1"}):
            self.assertFalse(_upload_receipt(body, {"networks": [{}]})[0])

    def test_complete_accepted_known_receipt(self):
        body = {"ok": True, "imported": 1, "updated": 1, "duplicates": 1,
                "aircraft_imported": 1, "aircraft_already_seen": 1,
                "meshcore_imported": 1}
        ok, info, imported = _upload_receipt(body, {"networks": [{}, {}, {}], "aircraft": [{}, {}], "meshcore_nodes": [{}]})
        self.assertTrue(ok)
        self.assertEqual(imported, 1)
        self.assertIn("known 2", info)

    def test_missing_mesh_known_counter_is_unknown(self):
        ok, info, _ = _upload_receipt({"ok": True, "meshcore_imported": 0}, {"meshcore_nodes": [{}]})
        self.assertFalse(ok)
        self.assertIn("rejected ?", info)
        self.assertIn("unknown 1", info)

    def test_real_103_node_receipt_ownership_split_includes_merged_nodes(self):
        payload = {"meshcore_nodes": [{}] * 103}
        body = {"ok": True, "meshcore_imported": 100, "meshcore_already_seen": 0,
                "meshcore_merged": 3, "meshcore_yours_known": 2, "meshcore_owned_by_others": 1,
                "meshcore_rejected": 0, "meshcore_reject_reasons": {"no_gps": 0}}
        ok, info, _ = _upload_receipt(body, payload)
        self.assertTrue(ok)
        self.assertIn("+100, known 3, rejected 0, unknown 0", info)
        self.assertFalse(_upload_receipt(dict(body, meshcore_yours_known=3), payload)[0])
        self.assertFalse(_upload_receipt(body, {"meshcore_nodes": [{}] * 104})[0])
        without_aggregate = dict(body)
        del without_aggregate["meshcore_already_seen"]
        self.assertFalse(_upload_receipt(without_aggregate, payload)[0])

    def test_partial_rejected_and_inconsistent_receipts_remain_pending(self):
        for body in ({"ok": True, "imported": 1, "rejected": 1},
                     {"ok": True, "imported": 3},
                     {"ok": True, "imported": -1},
                     {"ok": True, "imported": True}):
            self.assertFalse(_upload_receipt(body, {"networks": [{}, {}]})[0])

    def test_mt_only_uses_documented_shared_slot_and_explicit_network(self):
        with tempfile.TemporaryDirectory() as base:
            session = Path(base)/"closed"
            session.mkdir()
            self.mesh_csv(session)
            p = self.plugin(base)
            with patch("plugins.wardrive_upload._open", return_value=self.response({"ok": True, "meshcore_imported": 1})) as network:
                self.assertEqual(p._pending_sessions(), [session])
                p._upload_worker([session])
                network.assert_called_once()
                signed = json.loads(network.call_args.args[0].data)
                payload = json.loads(base64.b64decode(signed["data"]))
            record = payload["meshcore_nodes"][0]
            self.assertEqual(record["network"], "meshtastic")
            self.assertEqual(record["node_id"], "12345678")
            self.assertNotIn("type", record)
            self.assertIn("closed", p._uploaded_meshtastic_sessions)
            self.assertEqual(p._pending_sessions(), [])
            self.assertTrue(any("MT eligible 1, local no GPS 0" in line for line, _ in p._log))

    def test_old_legacy_receipt_does_not_block_new_mt_upload(self):
        with tempfile.TemporaryDirectory() as base:
            session = Path(base)/"closed"
            session.mkdir()
            self.mesh_csv(session)
            p = self.plugin(base)
            p._uploaded_sessions.add("closed")
            self.assertEqual(p._pending_sessions(), [session])
            with patch("plugins.wardrive_upload._open", return_value=self.response({"ok": True, "meshcore_already_seen": 1})) as network:
                p._upload_worker([session])
                network.assert_called_once()
            self.assertEqual(p._pending_sessions(), [])

    def test_same_id_in_both_networks_is_never_collapsed(self):
        with tempfile.TemporaryDirectory() as base:
            session = Path(base)
            self.mesh_csv(session)
            self.mesh_csv(session, "meshcore_nodes.csv")
            p = self.plugin(base)
            mc = p._parse_meshcore(session/"meshcore_nodes.csv")
            mt = p._parse_meshtastic(session/"meshtastic_nodes.csv")
            merged = p._merge_mesh_nodes(mc, mt, mc, mt)
            self.assertEqual(len(merged), 2)
            self.assertEqual({n["network"] for n in merged}, {"meshcore", "meshtastic"})
            self.assertEqual({n["node_id"] for n in merged}, {"12345678"})
            self.assertEqual(mc[0]["type"], "MESHCORE")

    def test_inactive_no_gps_mesh_log_is_cleaned_after_upload_attempt_without_api_call(self):
        with tempfile.TemporaryDirectory() as base:
            session = Path(base)/"closed"
            session.mkdir()
            self.mesh_csv(session, rows=[["now", "!12345678", "Client", "synthetic", 0, 0, -90, 0]])
            p = self.plugin(base)
            with patch("plugins.wardrive_upload._open") as network:
                p._upload_worker([session])
                network.assert_not_called()
            self.assertNotIn("closed", p._uploaded_meshtastic_sessions)
            self.assertFalse((session / "meshtastic_nodes.csv").is_file())
            self.assertEqual(p._pending_sessions(), [])

    def test_rejected_or_unknown_mesh_receipt_cannot_clear_session(self):
        bodies = [{"ok": True, "meshcore_imported": 0},
                  {"ok": True, "meshcore_reject_reasons": {"bad_network": 1}},
                  {"ok": True, "meshcore_imported": 0, "meshcore_rejected": 1,
                   "meshcore_reject_reasons": {"no_gps": 1}}]
        for body in bodies:
            with tempfile.TemporaryDirectory() as base:
                session = Path(base)/"closed"
                session.mkdir()
                self.mesh_csv(session)
                p = self.plugin(base)
                with patch("plugins.wardrive_upload._open", return_value=self.response(body)):
                    p._upload_worker([session])
                self.assertNotIn("closed", p._uploaded_meshtastic_sessions)
                self.assertEqual(p._pending_sessions(), [session])

    def test_mesh_receipt_counts_merged_and_ownership_split_without_double_counting(self):
        body = {"ok": True, "meshcore_imported": 1, "meshcore_already_seen": 2,
                "meshcore_merged": 1, "meshcore_yours_known": 1, "meshcore_owned_by_others": 1,
                "meshcore_reject_reasons": {"no_gps": 0, "bad_node_id": 0, "bad_network": 0}}
        ok, info, _ = _upload_receipt(body, {"meshcore_nodes": [{}, {}, {}, {}]})
        self.assertTrue(ok)
        self.assertIn("known 3", info)
        self.assertIn("other players 1", info)
        for reasons in ({"no_gps": True}, {"no_gps": -1}, [], {"bad_network": "1"}):
            self.assertFalse(_upload_receipt(dict(body, meshcore_reject_reasons=reasons), {"meshcore_nodes": [{}, {}, {}, {}]})[0])

    def test_active_session_dir_api_never_marks_receipt_complete(self):
        with tempfile.TemporaryDirectory() as base:
            session = Path(base)/"active"
            session.mkdir()
            self.mesh_csv(session)
            p = self.plugin(base)
            p.app.loot = SimpleNamespace(_base=base, session_dir=str(session))
            with patch("plugins.wardrive_upload._open", return_value=self.response({"ok": True, "meshcore_imported": 1})):
                p._upload_worker([session])
            self.assertNotIn("active", p._uploaded_sessions)
            self.assertNotIn("active", p._uploaded_meshtastic_sessions)
            self.assertEqual(p._pending_sessions(), [])

    def test_actual_loot_manager_active_session_is_not_finalized(self):
        with tempfile.TemporaryDirectory() as base:
            with patch.object(LootManager, "_periodic_backup_loop", lambda self: None):
                loot = LootManager(base)
            try:
                session = Path(loot.session_dir)
                self.mesh_csv(session)
                p = self.plugin(loot._base)
                p.app.loot = loot
                self.assertEqual(p._active_session_name(), session.name)
                with patch("plugins.wardrive_upload._open", return_value=self.response({"ok": True, "meshcore_imported": 1})):
                    p._upload_worker([session])
                self.assertNotIn(session.name, p._uploaded_sessions)
                self.assertNotIn(session.name, p._uploaded_meshtastic_sessions)
                self.assertEqual(p._pending_sessions(), [])
            finally:
                with patch.object(os, "sync", lambda: None, create=True):
                    loot.close()

    def test_mt_receipts_persist_separately_from_old_uploaded_state(self):
        with tempfile.TemporaryDirectory() as base:
            p = self.plugin(base)
            state = Path(base)/"state.json"
            p._state_file = lambda: state
            p._save_state = lambda: WardriveUpload._save_state(p)
            p._mark_uploaded(Path(base)/"closed", meshtastic=True)
            with patch.object(WardriveUpload, "_state_file", return_value=state), patch("plugins.wardrive_upload._load_secrets_conf", return_value={}):
                reloaded = WardriveUpload()
            self.assertEqual(reloaded._uploaded_meshtastic_sessions, {"closed"})
            self.assertEqual(reloaded._uploaded_sessions, {"closed"})

    def test_timeout_never_marks_success(self):
        with tempfile.TemporaryDirectory() as base:
            session = Path(base)/"closed"
            session.mkdir()
            p = self.plugin(base)
            p._parse_csv = lambda path: [{}]
            with patch("plugins.wardrive_upload._open", side_effect=TimeoutError):
                p._upload_worker([session])
            self.assertNotIn("closed", p._uploaded_sessions)

    def test_finite_meshcore_positions_only(self):
        with tempfile.TemporaryDirectory() as base:
            csv = Path(base)/"meshcore_nodes.csv"
            csv.write_text("time,id,type,name,lat,lon,rssi\nnow,abcd,Client,test,nan,10,-80\n")
            self.assertEqual(self.plugin(base)._parse_meshcore(csv), [])


if __name__ == "__main__":
    unittest.main()
