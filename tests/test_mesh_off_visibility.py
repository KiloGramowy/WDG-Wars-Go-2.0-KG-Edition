"""Exercise native mesh OFF transitions with real temporary contact persistence."""
import copy
from contextlib import contextmanager
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_mesh_map_history import game_for, legacy, loot_at, receive, ENV, XP


class MeshOffVisibilityTests(unittest.TestCase):
    @contextmanager
    def start(self, root):
        legacy(root, count=3)
        with loot_at(root) as loot:
            game = game_for(loot)
            game.player_lat, game.player_lon = 51.1, 17.9
            game._active_lora_protocol = lambda: ""
            game._mesh_map_adapter_protocols = {"meshcore", "meshtastic"}
            for protocol in ("meshcore", "meshtastic"):
                for index in (1, 2, 3):
                    node_id = f"{index:08x}"
                    if protocol == "meshtastic":
                        node_id = "!" + node_id
                    receive(game, protocol, node_id)
                    marker = game.markers[-1]
                    marker.lat, marker.lon = 51.1 + index * 0.1, 17.9
            game._sync_mesh_map_visibility()
            yield game, loot

    def test_both_protocols_expire_farther_first_without_touching_saved_contacts(self):
        with tempfile.TemporaryDirectory() as root, patch("time.monotonic", return_value=1000) as clock, self.start(root) as (game, loot):
            before = loot._db_path.read_bytes()
            keys = copy.copy(game._mesh_session_nodes)
            game._mesh_map_adapter_protocols.clear()
            game._sync_mesh_map_visibility()
            clock.return_value = 1299.999
            game._sync_mesh_map_visibility()
            self.assertEqual(len(game.markers), 6)
            clock.return_value = 1300
            game._sync_mesh_map_visibility()
            self.assertEqual(len(game.markers), 4)
            self.assertTrue(all(not m.mesh_key.endswith("00000003") for m in game.markers))
            clock.return_value = 1450
            game._sync_mesh_map_visibility()
            self.assertEqual(len(game.markers), 2)
            self.assertTrue(all(m.mesh_key.endswith("00000001") for m in game.markers))
            clock.return_value = 1600
            game._sync_mesh_map_visibility()
            self.assertEqual(game.markers, [])
            self.assertEqual(loot._db_path.read_bytes(), before)
            self.assertEqual(game._mesh_session_nodes, keys)
            self.assertEqual(game.xp, XP)

    def test_protocol_switch_expires_old_protocol_only(self):
        with tempfile.TemporaryDirectory() as root, patch("time.monotonic", return_value=1000) as clock, self.start(root) as (game, loot):
            game._mesh_map_adapter_protocols.clear()
            game._active_lora_protocol = lambda: "meshtastic"
            game._sync_mesh_map_visibility()
            clock.return_value = 1700
            game._sync_mesh_map_visibility()
            self.assertEqual(len(game.markers), 3)
            self.assertTrue(all(m.type == "meshtastic" for m in game.markers))

    def test_resume_keeps_remaining_markers_but_does_not_replay_expired_nodes(self):
        with tempfile.TemporaryDirectory() as root, patch("time.monotonic", return_value=1000) as clock, self.start(root) as (game, loot):
            game._mesh_map_adapter_protocols.clear()
            game._sync_mesh_map_visibility()
            clock.return_value = 1451
            game._mesh_map_adapter_protocols = {"meshcore", "meshtastic"}
            game._sync_mesh_map_visibility()
            self.assertEqual(len(game.markers), 2)
            clock.return_value = 3000
            game._sync_mesh_map_visibility()
            self.assertEqual(len(game.markers), 2)
            receive(game, "meshcore", "00000003")
            self.assertEqual(len(game.markers), 3)
            self.assertEqual(game.xp, XP)

    def test_non_mesh_markers_and_protocol_coordinates_are_preserved(self):
        with tempfile.TemporaryDirectory() as root, patch("time.monotonic", return_value=1000) as clock, self.start(root) as (game, loot):
            marker = ENV["MapMarker"](51.1, 17.9, "HS", "handshake")
            game.markers.append(marker)
            coordinates = [(m.lat, m.lon) for m in game.markers]
            game._mesh_map_adapter_protocols.clear()
            game._sync_mesh_map_visibility()
            clock.return_value = 1100
            game._sync_mesh_map_visibility()
            self.assertEqual([(m.lat, m.lon) for m in game.markers], coordinates)
            clock.return_value = 1700
            game._sync_mesh_map_visibility()
            self.assertEqual(game.markers, [marker])

    def test_repeated_off_does_not_extend_deadlines_and_restart_starts_empty(self):
        with tempfile.TemporaryDirectory() as root, patch("time.monotonic", return_value=1000) as clock, self.start(root) as (game, loot):
            game._mesh_map_adapter_protocols.clear()
            game._sync_mesh_map_visibility()
            clock.return_value = 1290
            game._sync_mesh_map_visibility()
            clock.return_value = 1601
            game._sync_mesh_map_visibility()
            self.assertEqual(game.markers, [])
            restarted = game_for(loot)
            self.assertEqual(restarted.markers, [])
            self.assertEqual(len(restarted._mc_nodes) + len(restarted._mt_view["nodes"]), 6)
            self.assertEqual(restarted.xp, XP)

    def test_late_gps_cannot_restore_expired_unlocated_nodes_without_new_reception(self):
        for protocol, node_id in (("meshcore", "00000001"), ("meshtastic", "!00000001")):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as root, \
                    patch("time.monotonic", return_value=1000) as clock:
                legacy(root, positioned=False)
                gps = SimpleNamespace(radio_fix=lambda **kwargs: None)
                with loot_at(root, gps) as loot:
                    game = game_for(loot)
                    game._mesh_map_adapter_protocols = {protocol}
                    receive(game, protocol, node_id, positioned=False)
                    self.assertEqual(game.markers, [])
                    self.assertEqual(game._mesh_map_eligible_nodes, {(protocol, node_id)})
                    game._mesh_map_adapter_protocols.clear()
                    game._sync_mesh_map_visibility()
                    clock.return_value = 1701
                    game._sync_mesh_map_visibility()
                    self.assertEqual(game._mesh_map_eligible_nodes, set())
                    self.assertEqual(game._mesh_session_nodes, {(protocol, node_id)})
                    game._mesh_map_adapter_protocols = {protocol}
                    gps.radio_fix = lambda **kwargs: dict(lat=51.2, lon=18,
                        observed_at=kwargs["now"] - 0.1, quality=1, sats=8, hdop=0.8)
                    game._assign_missing_mesh_gps()
                    self.assertEqual(game.markers, [])
                    self.assertEqual(game.xp, XP)
                    receive(game, protocol, node_id)
                    self.assertEqual(len(game.markers), 1)
                    self.assertEqual(game._mesh_map_eligible_nodes, {(protocol, node_id)})
                    self.assertEqual(game.xp, XP)


if __name__ == "__main__":
    unittest.main()
