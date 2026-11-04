"""Session RX identities stay separate from saved contacts and map visibility.

Exercise native registration with temporary synthetic player data only. No radio,
real player files, upload or external service is used.
"""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_mesh_map_history import game_for, legacy, loot_at, receive, XP
from watchdogs.mesh_rules import MESH_NEW_CONTACT_XP


PROTOCOLS = (("meshcore", "00000001", "00000002"),
             ("meshtastic", "!00000001", "!00000002"))


def counts(game):
    return {protocol: sum(kind == protocol for kind, _ in game._mesh_session_nodes)
            for protocol, _, _ in PROTOCOLS}


class MeshSessionCountsTests(unittest.TestCase):
    def test_unique_receptions_include_known_new_and_unlocated_nodes(self):
        with tempfile.TemporaryDirectory() as root:
            legacy(root, positioned=False)
            with loot_at(root) as loot:
                game = game_for(loot)
                self.assertEqual(counts(game), dict(meshcore=0, meshtastic=0))
                for protocol, known, new in PROTOCOLS:
                    for node_id in (known, known, new, new):
                        receive(game, protocol, node_id, positioned=False)
                    self.assertEqual(counts(game)[protocol], 2)
                self.assertEqual(game.markers, [])
                self.assertEqual(game.xp, XP + 2 * MESH_NEW_CONTACT_XP)
                self.assertEqual(loot.load_xp(), game.xp)

    def test_failed_save_does_not_count_an_uncommitted_reception(self):
        with tempfile.TemporaryDirectory() as root:
            legacy(root)
            with loot_at(root) as loot:
                game = game_for(loot)
                before = loot._db_path.read_bytes()
                for protocol, _, new in PROTOCOLS:
                    with patch.object(loot, "_save_db", return_value=False):
                        self.assertIs(receive(game, protocol, new), False)
                self.assertEqual(counts(game), dict(meshcore=0, meshtastic=0))
                self.assertEqual(loot._db_path.read_bytes(), before)
                self.assertEqual(game.xp, XP)

    def test_gps_and_off_leave_session_counts_restart_clears_them(self):
        gps = SimpleNamespace(radio_fix=Mock(return_value=None))
        with tempfile.TemporaryDirectory() as root, \
                patch("time.monotonic", return_value=1000) as clock:
            legacy(root, positioned=False)
            with loot_at(root, gps) as loot:
                game = game_for(loot)
                game._mesh_map_adapter_protocols = {row[0] for row in PROTOCOLS}
                for protocol, known, _ in PROTOCOLS:
                    receive(game, protocol, known, positioned=False)
                expected = dict(meshcore=1, meshtastic=1)
                self.assertEqual(counts(game), expected)
                gps.radio_fix.side_effect = lambda **kwargs: dict(lat=51.2, lon=18,
                    observed_at=kwargs["now"] - 0.1, quality=1, sats=8, hdop=0.8)
                game._assign_missing_mesh_gps()
                self.assertEqual(len(game.markers), 2)
                self.assertEqual(counts(game), expected)
                game._mesh_map_adapter_protocols.clear()
                game._sync_mesh_map_visibility()
                clock.return_value = 1601
                game._sync_mesh_map_visibility()
                self.assertEqual(game.markers, [])
                self.assertEqual(counts(game), expected)
                self.assertEqual(game.xp, XP)
            with loot_at(root, gps) as loot:
                restarted = game_for(loot)
                self.assertEqual(counts(restarted), dict(meshcore=0, meshtastic=0))
                self.assertEqual(len(loot.load_contacts()), 1)
                self.assertEqual(len(loot.load_meshtastic_contacts()), 1)
                self.assertEqual(restarted.xp, XP)

    def test_map_expiry_preserves_database_chat_logs_and_node_exports_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as root, \
                patch("time.monotonic", return_value=1000) as clock:
            legacy(root)
            with loot_at(root) as loot:
                game = game_for(loot)
                game._mesh_map_adapter_protocols = {row[0] for row in PROTOCOLS}
                for protocol, known, _ in PROTOCOLS:
                    receive(game, protocol, known)
                loot.save_meshcore_message("public", "Synthetic MC: Zażółć", -90)
                loot.save_meshtastic_message("SFNarrow", "Synthetic MT: Zażółć\nsecond line", -91)
                files = [loot._db_path] + [Path(loot.session_dir) / name for name in (
                    "meshcore_messages.log", "meshtastic_messages.log", "meshtastic_chat.jsonl",
                    "meshcore_nodes.csv", "meshtastic_nodes.csv")]
                before = {path: path.read_bytes() for path in files}
                self.assertTrue(all(before.values()))
                game._mesh_map_adapter_protocols.clear()
                game._sync_mesh_map_visibility()
                clock.return_value = 1601
                game._sync_mesh_map_visibility()
                self.assertEqual(game.markers, [])
                self.assertEqual({path: path.read_bytes() for path in files}, before)
                self.assertEqual(counts(game), dict(meshcore=1, meshtastic=1))
                self.assertEqual(game.xp, XP)


if __name__ == "__main__":
    unittest.main()
