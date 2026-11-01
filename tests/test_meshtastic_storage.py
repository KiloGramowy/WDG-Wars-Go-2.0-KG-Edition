"""Isolated storage/reward tests; never open the real WDG loot directory.

Optional WDG_MESHTASTIC_REPLAY_LOG points to a private captured JSONL outside
the repository. Frames are replayed in memory, never over RF or to a server.
"""

from contextlib import contextmanager
import copy
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from watchdogs.loot_manager import LootManager
from watchdogs.mesh_rules import MESH_NEW_CONTACT_XP


@contextmanager
def loot_at(root):
    with patch.object(LootManager, "_periodic_backup_loop", lambda self: None):
        manager = LootManager(str(root))
    try:
        yield manager
    finally:
        # os.sync() is unrelated to the transaction under test and expensive.
        with patch.object(os, "sync", lambda: None, create=True):
            manager.close()


def node(name="?", lat=0.0, lon=0.0, **extra):
    data = dict(type="Client", name=name, lat=lat, lon=lon, rssi=-115.0, snr=-7.5)
    data.update(extra)
    return data


def csv_rows(path):
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


class MeshStorageTests(unittest.TestCase):
    def test_same_existing_rule_and_namespace_separation(self):
        # The existing MeshCore path awarded 20 XP before extracting this rule.
        self.assertEqual(MESH_NEW_CONTACT_XP, 20)
        with tempfile.TemporaryDirectory() as folder, loot_at(folder) as loot:
            first = loot.register_mesh_contact("meshcore", "abcd1234", node("MC"), current_xp=100)
            second = loot.register_mesh_contact("meshtastic", "abcd1234", node("MT"), current_xp=first["xp_total"])
            self.assertEqual(first["xp_award"], MESH_NEW_CONTACT_XP)
            self.assertEqual(second["xp_award"], first["xp_award"])
            self.assertEqual(loot.load_xp(), 100 + 2 * MESH_NEW_CONTACT_XP)
            self.assertEqual(loot.load_contacts()["abcd1234"]["name"], "MC")
            self.assertEqual(loot.load_meshtastic_contacts()["abcd1234"]["name"], "MT")

    def test_eight_new_nodes_have_the_same_rewards_for_both_protocols(self):
        awards = {}
        for protocol in ("meshcore", "meshtastic"):
            with tempfile.TemporaryDirectory() as folder:
                with loot_at(folder) as loot:
                    loot.save_xp(13)
                    awards[protocol] = [loot.register_mesh_contact(protocol, f"!123456{i:02x}", node())["xp_award"]
                                        for i in range(8)]
                    self.assertEqual(loot.load_xp(), 173)
                with loot_at(folder) as restarted:
                    for i in range(8):
                        self.assertEqual(restarted.register_mesh_contact(protocol, f"!123456{i:02x}", node())["xp_award"], 0)
                    self.assertEqual(restarted.load_xp(), 173)
        self.assertEqual(awards["meshcore"], awards["meshtastic"])

    def test_repeated_frame_relay_and_known_node_packet_do_not_repeat_reward(self):
        with tempfile.TemporaryDirectory() as folder, loot_at(folder) as loot:
            created = loot.register_mesh_contact("meshtastic", "!06cec900", node("Penny"))
            for data in (node("Penny"), node("Penny", packet_id="new"), node("Penny", relay_hint=1)):
                result = loot.register_mesh_contact("meshtastic", "!06cec900", data)
                self.assertFalse(result["new_contact"])
                self.assertFalse(result["new_event"])
                self.assertEqual(result["xp_award"], 0)
                self.assertEqual(result["xp_total"], created["xp_total"])
            rows = csv_rows(loot._session / "meshtastic_nodes.csv")
            self.assertEqual(len(rows), 1)

    def test_names_position_notes_and_first_seen_update_without_new_award(self):
        with tempfile.TemporaryDirectory() as folder, loot_at(folder) as loot:
            first = loot.register_mesh_contact("meshtastic", "!06cec900", node())
            loot.save_meshtastic_contact_note("!06cec900", "keep this note")
            update = loot.register_mesh_contact("meshtastic", "!06cec900", node("Penny", 36.47, -6.19))
            stale = loot.register_mesh_contact("meshtastic", "!06cec900", node())
            contact = stale["contact"]
            self.assertEqual(contact["name"], "Penny")
            self.assertEqual((contact["lat"], contact["lon"]), (36.47, -6.19))
            self.assertEqual(contact["first_seen"], first["contact"]["first_seen"])
            self.assertEqual(contact["note"], "keep this note")
            self.assertEqual(update["xp_award"], 0)
            row = csv_rows(loot._session / "meshtastic_nodes.csv")[0]
            self.assertEqual((row["name"], row["lat"], row["lon"]), ("Penny", "36.47", "-6.19"))
            self.assertEqual(loot.loot_totals["mt_nodes"], 1)

    def test_mc_csv_later_position_fix_uses_same_upsert(self):
        with tempfile.TemporaryDirectory() as folder, loot_at(folder) as loot:
            loot.save_meshcore_node("abcd1234", "Client", "", 0, 0, -100, -2)
            loot.save_meshcore_node("abcd1234", "Client", "MC", 36.47, -6.19, -99, -1)
            loot.save_meshcore_node("abcd1234", "Client", "", 0, 0, -98, 0)
            rows = csv_rows(loot._session / "meshcore_nodes.csv")
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["name"], rows[0]["lat"], rows[0]["lon"]), ("MC", "36.47", "-6.19"))

    def test_existing_contacts_and_xp_badges_are_not_reset_or_retroactively_paid(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / "loot"
            base.mkdir()
            old = {"version": 2, "sessions": {}, "totals": {"wardriving": 10}, "xp": 12345,
                   "badges": ["meshcore", "wardriver"],
                   "mc_contacts": {"abcd1234": dict(node("Legacy"), id="abcd1234", first_seen=12, note="old")}}
            (base / "loot_db.json").write_text(json.dumps(old))
            with loot_at(folder) as loot:
                before_mc = copy.deepcopy(loot.load_contacts())
                mt = loot.register_mesh_contact("meshtastic", "!06cec900", node())
                self.assertEqual(mt["xp_total"], 12345 + MESH_NEW_CONTACT_XP)
                self.assertEqual(loot.load_contacts(), before_mc)
                mc = loot.register_mesh_contact("meshcore", "abcd1234", node())
                self.assertEqual(mc["xp_award"], 0)
                self.assertEqual(mc["contact"]["note"], "old")
                self.assertEqual(loot.load_badges(), {"meshcore", "wardriver"})
                self.assertEqual(loot._db["version"], 2)

    def test_pending_nonmesh_xp_is_carried_into_atomic_mesh_commit(self):
        with tempfile.TemporaryDirectory() as folder, loot_at(folder) as loot:
            loot.save_xp(50)
            result = loot.register_mesh_contact("meshtastic", "!06cec900", node(), current_xp=95)
            self.assertEqual(result["xp_total"], 95 + MESH_NEW_CONTACT_XP)
            stored = json.loads(loot._db_path.read_text())
            self.assertEqual(stored["xp"], result["xp_total"])
            self.assertIn("!06cec900", stored["mt_contacts"])
            self.assertIn("!06cec900", stored["mesh_discovery_events"]["meshtastic"])

    def test_persistence_failure_cannot_report_or_commit_awarded_xp(self):
        with tempfile.TemporaryDirectory() as folder, loot_at(folder) as loot:
            loot.save_xp(70)
            before = copy.deepcopy(loot._db)
            with patch.object(loot, "_save_db", return_value=False):
                with self.assertRaises(OSError):
                    loot.register_mesh_contact("meshtastic", "!06cec900", node())
            self.assertEqual(loot._db, before)
            self.assertEqual(json.loads(loot._db_path.read_text())["xp"], 70)
            retry = loot.register_mesh_contact("meshtastic", "!06cec900", node())
            self.assertEqual(retry["xp_award"], MESH_NEW_CONTACT_XP)

    def test_fresh_interpreter_restart_recognizes_known_node_without_reward(self):
        with tempfile.TemporaryDirectory() as folder:
            with loot_at(folder) as loot:
                first = loot.register_mesh_contact("meshtastic", "!06cec900", node("Penny"), current_xp=100)
                expected = first["xp_total"]
            snippet = "\n".join([
                "import json,os,sys",
                "from unittest.mock import patch",
                "from watchdogs.loot_manager import LootManager",
                "with patch.object(LootManager,'_periodic_backup_loop',lambda self:None):",
                " m=LootManager(sys.argv[1])",
                "r=m.register_mesh_contact('meshtastic','!06cec900',{'name':'Penny','lat':0,'lon':0})",
                "print(json.dumps(r))",
                "m._backup_stop=True",
                "m._serial_fh.close()",
            ])
            result = subprocess.run([sys.executable, "-B", "-c", snippet, folder],
                                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            reloaded = json.loads(result.stdout)
            self.assertEqual(reloaded["xp_total"], expected)
            self.assertFalse(reloaded["new_contact"])
            self.assertEqual(reloaded["xp_award"], 0)

    def test_deleted_visible_contact_keeps_award_evidence_for_both_protocols(self):
        with tempfile.TemporaryDirectory() as folder, loot_at(folder) as loot:
            for protocol in ("meshcore", "meshtastic"):
                loot.register_mesh_contact(protocol, "same-id", node())
                if protocol == "meshcore":
                    loot.delete_contact("same-id")
                else:
                    loot.delete_meshtastic_contact("same-id")
                again = loot.register_mesh_contact(protocol, "same-id", node("Back"))
                self.assertTrue(again["new_contact"])
                self.assertEqual(again["xp_award"], 0)

    def test_chat_history_survives_restart_and_preserves_unicode_and_linebreaks(self):
        with tempfile.TemporaryDirectory() as folder:
            with loot_at(folder) as loot:
                loot.save_meshtastic_message("SFNarrow", "Penny: hola łódź\nsegunda línea", -115)
                loot.save_meshtastic_message("SFNarrow", "WDG: outbound", 0, direction="tx")
                self.assertEqual(loot.loot_totals["mt_messages"], 1)
                self.assertEqual(loot.loot_totals["mc_messages"], 0)
            with loot_at(folder) as reloaded:
                history = reloaded.load_meshtastic_messages()
                self.assertEqual(len(history), 2)
                self.assertEqual(history[0][:3], ("SFNarrow", "Penny: hola łódź\nsegunda línea", -115.0))
                self.assertEqual(len(reloaded.load_meshtastic_messages(1)), 1)
                self.assertEqual(reloaded.load_xp(), 0)

    def test_invalid_position_does_not_replace_known_position(self):
        with tempfile.TemporaryDirectory() as folder, loot_at(folder) as loot:
            loot.register_mesh_contact("meshtastic", "!06cec900", node("Penny", 36.47, -6.19))
            for lat, lon in ((float("nan"), 1), (91, 1), (1, 181), (None, None)):
                result = loot.register_mesh_contact("meshtastic", "!06cec900", node(lat=lat, lon=lon))
                self.assertEqual((result["contact"]["lat"], result["contact"]["lon"]), (36.47, -6.19))


class PrivateCapturedReplayTest(unittest.TestCase):
    def test_real_frames_replay_only_into_temporary_loot_with_no_radio_or_upload(self):
        source = os.environ.get("WDG_MESHTASTIC_REPLAY_LOG")
        if not source:
            self.skipTest("set WDG_MESHTASTIC_REPLAY_LOG to the private capture outside the repository")
        from meshtastic_rx_probe import decode_header, decode_public_data, get_profile
        rows = [json.loads(line) for line in Path(source).read_text().splitlines()]
        frames = []
        for row in rows:
            if row.get("crc_status") != "pass" or not row.get("raw_frame_hex"):
                continue
            frame = bytes.fromhex(row["raw_frame_hex"])
            try:
                decode_public_data(frame, get_profile("sfnarrow"))
            except ValueError:
                continue
            frames.append((frame, row))
        self.assertTrue(frames, "capture must contain independently decodable public Data")
        known = {decode_header(frame)["source_node_id"] for frame, _ in frames}
        with tempfile.TemporaryDirectory() as folder:
            with loot_at(folder) as loot:
                for frame, row in frames:
                    identity = decode_header(frame)["source_node_id"]
                    result = loot.register_mesh_contact("meshtastic", identity,
                        node(rssi=row.get("rssi_dbm", 0), snr=row.get("snr_db", 0)))
                    # Exact repeat and route-modified retransmission carry the same author.
                    repeated = loot.register_mesh_contact("meshtastic", identity, result["contact"])
                    self.assertEqual(repeated["xp_award"], 0)
                    relay = bytearray(frame)
                    relay[12] = (relay[12] & ~7) | max(0, (relay[12] & 7) - 1)
                    relay[15] ^= 1
                    self.assertEqual(decode_header(relay)["source_node_id"], identity)
                    retrans = loot.register_mesh_contact("meshtastic", identity, result["contact"])
                    self.assertEqual(retrans["xp_award"], 0)
                self.assertEqual(len(loot.load_meshtastic_contacts()), len(known))
                self.assertEqual(loot.load_xp(), len(known) * MESH_NEW_CONTACT_XP)
            with loot_at(folder) as restarted:
                for identity in known:
                    self.assertEqual(restarted.register_mesh_contact("meshtastic", identity, node())["xp_award"], 0)
                self.assertEqual(restarted.load_xp(), len(known) * MESH_NEW_CONTACT_XP)


if __name__ == "__main__":
    unittest.main()
