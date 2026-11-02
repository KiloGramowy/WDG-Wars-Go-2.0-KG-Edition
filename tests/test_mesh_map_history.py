"""History stays in contacts; map markers require this session's observations.

Use real temporary save migration and native initialization/mapping methods.
No graphical game, receiver, external scanner, network or player data is used.
"""
import ast
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from watchdogs.loot_manager import LootManager
from watchdogs.mesh_position import mesh_position
from watchdogs.mesh_rules import MESH_NEW_CONTACT_XP


XP = 1597146
SOURCE = Path(__file__).resolve().parents[1] / "watchdogs/app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
NATIVE_GAME = next(node for node in TREE.body
                   if isinstance(node, ast.ClassDef) and node.name == "WatchDogsGame")
INITIALIZE = next(node for node in NATIVE_GAME.body
                  if isinstance(node, ast.FunctionDef) and node.name == "_initialize")


def assigns_attribute(statement, name):
    targets = (statement.targets if isinstance(statement, ast.Assign)
               else [statement.target] if isinstance(statement, ast.AnnAssign) else [])
    return any(isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
               and target.value.id == "self" and target.attr == name for target in targets)


def initializer_range(first, stop):
    start = next(index for index, node in enumerate(INITIALIZE.body)
                 if assigns_attribute(node, first))
    end = next(index for index in range(start + 1, len(INITIALIZE.body))
               if assigns_attribute(INITIALIZE.body[index], stop))
    return copy.deepcopy(INITIALIZE.body[start:end])


# Compile the actual initialization blocks, including every statement between
# the named boundaries. A replay loop reintroduced between markers and particles
# is therefore executed by this regression instead of being silently skipped.
startup = ast.FunctionDef(name="restore_startup", args=ast.arguments(
    posonlyargs=[], args=[ast.arg(arg="self")], vararg=None, kwonlyargs=[],
    kw_defaults=[], kwarg=None, defaults=[]), decorator_list=[],
    body=initializer_range("_mc_nodes", "_flipper") + initializer_range("xp", "particles"))
methods = {"_register_mesh_node", "_assign_missing_mesh_gps", "_update_mesh_marker"}
harness = ast.ClassDef(name="MapGame", bases=[], keywords=[], decorator_list=[],
                      body=[startup] + [copy.deepcopy(node) for node in NATIVE_GAME.body
                                        if isinstance(node, ast.FunctionDef) and node.name in methods])
marker = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "MapMarker")
ENV = dict(__name__="watchdogs.mesh_map_history_test", __package__="watchdogs", time=time,
           C_SUCCESS=11, C_ERROR=8)
exec(compile(ast.fix_missing_locations(ast.Module(body=[copy.deepcopy(marker), harness],
             type_ignores=[])), str(SOURCE), "exec"), ENV)
MapGame = ENV["MapGame"]


def contact(node_id, protocol, positioned=True):
    return dict(id=node_id, protocol=protocol, type="Client", name="Historical " + node_id,
                lat=51.1 if positioned else 0, lon=17.9 if positioned else 0,
                first_seen=12, last_seen=34, note="Keep this saved note", rssi=-100, snr=-4)


def legacy(root, count=1, positioned=True):
    base = Path(root) / "loot"
    base.mkdir()
    data = dict(version=1, sessions={}, totals={}, xp=XP, badges=["wardriver"],
        mc_contacts={f"{index:08x}": contact(f"{index:08x}", "meshcore", positioned)
                     for index in range(1, count + 1)},
        mt_contacts={f"!{index:08x}": contact(f"!{index:08x}", "meshtastic", positioned)
                     for index in range(1, count + 1)})
    (base / "loot_db.json").write_text(json.dumps(data), encoding="utf-8")
    return data


@contextmanager
def loot_at(root, gps=None):
    with patch.object(LootManager, "_periodic_backup_loop", lambda self: None):
        loot = LootManager(str(root), gps_manager=gps)
    try:
        yield loot
    finally:
        with patch.object(os, "sync", lambda: None, create=True):
            loot.close()


def game_for(loot):
    game = MapGame()
    game.loot = loot
    game._lora = SimpleNamespace(_known_pubkeys={})
    game._mt = SimpleNamespace(set_known_nodes=Mock())
    game._load_loot_totals = Mock()
    game._term_add, game.msg = Mock(), Mock()
    game.state = SimpleNamespace(mc_nodes=0, mt_nodes=0)
    game.restore_startup()
    return game


def receive(game, protocol, node_id, positioned=True):
    return game._register_mesh_node(protocol, node_id, "Client", "Current observation",
        51.2 if positioned else 0, 18.0 if positioned else 0, -90, -3, received_at=1000)


class MeshMapHistoryTests(unittest.TestCase):
    def test_twelve_thousand_legacy_contacts_and_xp_survive_without_startup_markers(self):
        with tempfile.TemporaryDirectory() as root:
            original = legacy(root, count=6000)
            with loot_at(root) as loot:
                game = game_for(loot)
                self.assertEqual(game.xp, XP)
                self.assertEqual(loot.load_xp(), XP)
                self.assertEqual(len(game._mc_nodes), 6000)
                self.assertEqual(len(game._mt_view["nodes"]), 6000)
                self.assertEqual(loot.load_contacts(), original["mc_contacts"])
                self.assertEqual(loot.load_meshtastic_contacts(), original["mt_contacts"])
                self.assertEqual(game.markers, [])
                self.assertEqual(game._mesh_session_nodes, set())
                self.assertEqual(loot.load_badges(), {"wardriver"})

    def test_known_node_observation_places_one_marker_without_new_xp(self):
        with tempfile.TemporaryDirectory() as root:
            legacy(root)
            with loot_at(root) as loot:
                game = game_for(loot)
                for protocol, node_id in (("meshcore", "00000001"),
                                          ("meshtastic", "!00000001")):
                    receive(game, protocol, node_id)
                    receive(game, protocol, node_id)
                    self.assertIn((protocol, node_id), game._mesh_session_nodes)
                self.assertEqual(len(game.markers), 2)
                self.assertEqual({marker.type for marker in game.markers}, {"meshcore", "meshtastic"})
                self.assertEqual(game.xp, XP)
                self.assertEqual(loot.load_xp(), XP)
                self.assertTrue(all(marker.label == "Current observation" for marker in game.markers))

    def test_new_nodes_still_get_original_award_once_per_identity(self):
        with tempfile.TemporaryDirectory() as root:
            legacy(root)
            with loot_at(root) as loot:
                game = game_for(loot)
                for protocol, node_id in (("meshcore", "00000002"),
                                          ("meshtastic", "!00000002")):
                    receive(game, protocol, node_id)
                    receive(game, protocol, node_id)
                self.assertEqual(game.xp, XP + 2 * MESH_NEW_CONTACT_XP)
                self.assertEqual(loot.load_xp(), game.xp)
                self.assertEqual(len(game.markers), 2)

    def test_receiver_gps_assignment_alone_does_not_replay_historical_markers(self):
        gps = SimpleNamespace(radio_fix=lambda **kwargs: dict(lat=51.2, lon=18,
            observed_at=kwargs["now"] - 0.1, quality=1, sats=8, hdop=0.8))
        with tempfile.TemporaryDirectory() as root:
            legacy(root, positioned=False)
            with loot_at(root, gps) as loot:
                game = game_for(loot)
                game._assign_missing_mesh_gps()
                self.assertEqual(game.markers, [])
                self.assertEqual(game._mesh_session_nodes, set())
                self.assertEqual(game.xp, XP)
                for contacts in (loot.load_contacts(), loot.load_meshtastic_contacts()):
                    stored = next(iter(contacts.values()))
                    self.assertIsNotNone(mesh_position(stored))
                    self.assertEqual((stored["first_seen"], stored["last_seen"]), (12, 34))

    def test_later_receiver_fix_places_only_contacts_observed_this_session(self):
        for protocol, node_id in (("meshcore", "00000001"), ("meshtastic", "!00000001")):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as root:
                legacy(root, positioned=False)
                gps = SimpleNamespace(radio_fix=Mock(return_value=None))
                with loot_at(root, gps) as loot:
                    game = game_for(loot)
                    receive(game, protocol, node_id, positioned=False)
                    self.assertEqual(game.markers, [])
                    self.assertEqual(game._mesh_session_nodes, {(protocol, node_id)})
                    gps.radio_fix.side_effect = lambda **kwargs: dict(lat=51.2, lon=18,
                        observed_at=kwargs["now"] - 0.1, quality=1, sats=8, hdop=0.8)
                    game._assign_missing_mesh_gps()
                    self.assertEqual(len(game.markers), 1)
                    self.assertEqual(game.markers[0].mesh_key, protocol + ":" + node_id)
                    self.assertTrue(game.markers[0].label.endswith("[RX GPS]"))
                    self.assertEqual(game.xp, XP)

    def test_failed_persistence_does_not_mark_node_as_seen_or_create_marker(self):
        for protocol, node_id in (("meshcore", "00000002"), ("meshtastic", "!00000002")):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as root:
                legacy(root)
                with loot_at(root) as loot:
                    game = game_for(loot)
                    original = loot._db_path.read_bytes()
                    with patch.object(loot, "_save_db", return_value=False):
                        self.assertIs(receive(game, protocol, node_id), False)
                    self.assertEqual(game.markers, [])
                    self.assertEqual(game._mesh_session_nodes, set())
                    self.assertEqual(game.xp, XP)
                    self.assertEqual(loot._db_path.read_bytes(), original)
                    self.assertEqual(len(game._mc_nodes), 1)
                    self.assertEqual(len(game._mt_view["nodes"]), 1)

    def test_restart_retains_observed_contacts_and_xp_but_starts_empty_map(self):
        with tempfile.TemporaryDirectory() as root:
            legacy(root)
            with loot_at(root) as loot:
                game = game_for(loot)
                receive(game, "meshcore", "00000002")
                receive(game, "meshtastic", "!00000002")
                self.assertEqual(len(game.markers), 2)
                self.assertEqual(game.xp, XP + 2 * MESH_NEW_CONTACT_XP)
            with loot_at(root) as restarted:
                game = game_for(restarted)
                self.assertEqual(game.markers, [])
                self.assertEqual(game._mesh_session_nodes, set())
                self.assertEqual(len(game._mc_nodes), 2)
                self.assertEqual(len(game._mt_view["nodes"]), 2)
                self.assertEqual(game.xp, XP + 2 * MESH_NEW_CONTACT_XP)
                receive(game, "meshcore", "00000002")
                self.assertEqual(game.xp, XP + 2 * MESH_NEW_CONTACT_XP)
                self.assertEqual(len(game.markers), 1)


if __name__ == "__main__":
    unittest.main()
