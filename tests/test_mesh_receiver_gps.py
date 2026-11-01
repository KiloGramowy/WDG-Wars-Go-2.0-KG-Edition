"""Receiver GPS provenance using synthetic fixes only; no radio or serial I/O."""

from copy import deepcopy
import ast
import csv
from datetime import datetime
import os
from pathlib import Path
from queue import Queue
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from watchdogs.mesh_position import mesh_position, node_position, receiver_observation
from watchdogs.loot_manager import LootManager


class MeshReceiverGpsTests(unittest.TestCase):
    def setUp(self):
        self.now, self.wall_now = 100.0, 1_000.0
        self.fix = dict(lat=12.5, lon=34.5, observed_at=98.0, quality=1, sats=8, hdop=0.8)
        self.gps = SimpleNamespace(radio_fix=Mock(side_effect=lambda **kwargs: deepcopy(self.fix)))

    def observation(self, kind="reception"):
        return receiver_observation(self.gps, kind=kind, now=self.now, wall_now=self.wall_now)

    def loot_manager(self, app_dir=None):
        if app_dir is None:
            directory = tempfile.TemporaryDirectory()
            self.addCleanup(directory.cleanup)
            app_dir = directory.name
        with patch.object(LootManager, "_periodic_backup_loop", lambda self: None):
            loot = LootManager(str(app_dir), gps_manager=self.gps)
        def close():
            with patch.object(os, "sync", lambda: None, create=True):
                loot.close()
        self.addCleanup(close)
        return loot

    @staticmethod
    def exported_rows(loot, protocol):
        filename = "meshcore_nodes.csv" if protocol == "meshcore" else "meshtastic_nodes.csv"
        with (Path(loot.session_dir) / filename).open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))

    @staticmethod
    def contact_data(**changes):
        return dict(dict(type="Client", name="synthetic contact", lat=0, lon=0, rssi=-90, snr=-3,
                         first_seen=100, last_seen=200, note="synthetic retained note"), **changes)

    def test_node_position_returns_only_valid_canonical_coordinates(self):
        self.assertEqual(node_position(dict(lat="12.5", lon="34.5", receiver_lat=1, receiver_lon=2)),
                         dict(lat=12.5, lon=34.5, source="node"))

    def test_node_position_rejects_invalid_missing_or_zero_pair(self):
        for data in (None, [], {}, dict(lat=0, lon=0), dict(lat=True, lon=20),
                     dict(lat="invalid", lon=20), dict(lat=float("nan"), lon=20),
                     dict(lat=10, lon=float("inf")), dict(lat=90.1, lon=10),
                     dict(lat=10, lon=-180.1)):
            with self.subTest(data=data):
                self.assertIsNone(node_position(data))

    def test_node_position_allows_single_zero_and_geographic_bounds(self):
        for lat, lon in ((0, 10), (10, 0), (-90, -180), (90, 180)):
            with self.subTest(lat=lat, lon=lon):
                self.assertEqual(node_position(dict(lat=lat, lon=lon)), dict(lat=lat, lon=lon, source="node"))

    def test_reception_records_fix_time_and_quality_without_mutating_provider(self):
        original = deepcopy(self.fix)
        observed = self.observation()
        self.gps.radio_fix.assert_called_once_with(now=100.0)
        self.assertEqual(observed, dict(lat=12.5, lon=34.5, source="receiver_gps", kind="reception",
            observed_at=998.0, assigned_at=1000.0, quality=1, sats=8, hdop=0.8))
        self.assertEqual(self.fix, original)

    def test_user_assignment_uses_current_assignment_not_a_historical_node_timestamp(self):
        observed = self.observation(kind="user_assignment")
        self.assertEqual(observed["kind"], "user_assignment")
        self.assertEqual(observed["assigned_at"], self.wall_now)
        self.assertEqual(observed["observed_at"], self.wall_now - 2)
        contact = dict(lat=0, lon=0, first_seen=100, last_seen=200, receiver_position=observed)
        original = deepcopy(contact)
        self.assertEqual(mesh_position(contact)["source"], "receiver_assignment")
        self.assertEqual(contact, original)

    def test_fix_freshness_boundaries_and_future_monotonic_sample(self):
        for age, valid in ((0, True), (10, True), (10.000001, False), (-0.000001, False)):
            self.fix["observed_at"] = self.now - age
            with self.subTest(age=age):
                self.assertEqual(self.observation() is not None, valid)

    def test_only_measured_gps_quality_types_are_accepted(self):
        for quality in (1, 2, 4, 5):
            self.fix["quality"] = quality
            with self.subTest(quality=quality):
                self.assertIsNotNone(self.observation())
        for quality in (0, 3, 6, 7, 8, True, "1", 1.0, None):
            self.fix["quality"] = quality
            with self.subTest(quality=quality):
                self.assertIsNone(self.observation())

    def test_satellite_and_hdop_quality_requirements(self):
        for field, value in (("sats", 3), ("sats", True), ("sats", "8"),
                             ("hdop", 0), ("hdop", -0.1), ("hdop", 3.1),
                             ("hdop", float("nan")), ("hdop", float("inf")), ("hdop", True)):
            previous = self.fix[field]
            self.fix[field] = value
            with self.subTest(field=field, value=value):
                self.assertIsNone(self.observation())
            self.fix[field] = previous
        self.fix.update(sats=4, hdop=3)
        self.assertIsNotNone(self.observation())

    def test_receiver_coordinates_require_finite_bounds_and_nonzero_pair(self):
        for lat, lon in ((0, 0), (91, 10), (10, -181), (float("nan"), 10),
                         (10, float("inf")), (True, 10), (None, 10)):
            self.fix.update(lat=lat, lon=lon)
            with self.subTest(lat=lat, lon=lon):
                self.assertIsNone(self.observation())

    def test_lost_fix_and_display_or_map_positions_are_not_fallbacks(self):
        self.gps.radio_fix.return_value = None
        self.gps.radio_fix.side_effect = None
        self.gps.fix = SimpleNamespace(valid=True, latitude=12.5, longitude=34.5)
        self.gps.player_lat, self.gps.player_lon = 12.5, 34.5
        self.assertIsNone(self.observation())
        self.assertIsNone(receiver_observation(SimpleNamespace(fix=self.gps.fix),
                                              now=self.now, wall_now=self.wall_now))

    def test_missing_sample_fields_fail_closed(self):
        for field in tuple(self.fix):
            value = self.fix.pop(field)
            with self.subTest(missing=field):
                self.assertIsNone(self.observation())
            self.fix[field] = value

    def test_invalid_clocks_and_kind_do_not_call_fix_provider(self):
        for now, wall in ((float("nan"), 1000), (float("inf"), 1000), (-1, 1000),
                          (True, 1000), (100, float("nan")), (100, float("inf")),
                          (100, 0), (100, -1), (100, True)):
            with self.subTest(now=now, wall=wall):
                self.assertIsNone(receiver_observation(self.gps, now=now, wall_now=wall))
        self.assertIsNone(receiver_observation(self.gps, kind="historic_guess", now=100, wall_now=1000))
        self.gps.radio_fix.assert_not_called()

    def test_default_clock_conversion_uses_actual_fix_age(self):
        with patch("watchdogs.mesh_position.time.monotonic", return_value=105), \
                patch("watchdogs.mesh_position.time.time", return_value=2000):
            observed = receiver_observation(self.gps)
        self.gps.radio_fix.assert_called_once_with(now=105)
        self.assertEqual(observed["observed_at"], 1993)
        self.assertEqual(observed["assigned_at"], 2000)

    def test_node_position_wins_without_overwriting_either_source_for_both_protocols(self):
        observed = self.observation()
        for protocol in ("meshcore", "meshtastic"):
            contact = dict(protocol=protocol, lat=56.5, lon=78.5, has_position=True,
                           first_seen=100, last_seen=200, receiver_position=deepcopy(observed))
            original = deepcopy(contact)
            with self.subTest(protocol=protocol):
                self.assertEqual(mesh_position(contact), dict(lat=56.5, lon=78.5, source="node"))
                self.assertEqual(contact, original)

    def test_receiver_position_does_not_turn_unknown_node_coordinates_into_node_gps(self):
        observed = self.observation()
        for protocol in ("meshcore", "meshtastic"):
            contact = dict(protocol=protocol, lat=0, lon=0, has_position=False, receiver_position=observed)
            with self.subTest(protocol=protocol):
                self.assertEqual(mesh_position(contact), dict(lat=12.5, lon=34.5, source="receiver_gps",
                                                              observed_at=998, assigned_at=1000))
                self.assertEqual((contact["lat"], contact["lon"], contact["has_position"]), (0, 0, False))

    def test_genuine_stored_observation_remains_historical_without_refreshed_time(self):
        contact = dict(lat=0, lon=0, receiver_position=self.observation())
        with patch("watchdogs.mesh_position.time.monotonic", side_effect=AssertionError("no age refresh")), \
                patch("watchdogs.mesh_position.time.time", side_effect=AssertionError("no wall refresh")):
            self.assertEqual(mesh_position(contact)["observed_at"], 998)

    def test_stored_observation_requires_every_provenance_time_and_quality_field(self):
        observed = self.observation()
        for field in observed:
            incomplete = {key: value for key, value in observed.items() if key != field}
            with self.subTest(missing=field):
                self.assertIsNone(mesh_position(dict(lat=0, lon=0, receiver_position=incomplete)))

    def test_stored_observation_rejects_invalid_sources_kinds_and_timestamps(self):
        observed = self.observation()
        for field, value in (("source", "node"), ("source", "receiver_assignment"),
                             ("kind", "historic_guess"), ("observed_at", 0),
                             ("observed_at", float("nan")), ("assigned_at", float("inf")),
                             ("assigned_at", 997), ("assigned_at", True)):
            invalid = dict(observed, **{field: value})
            with self.subTest(field=field, value=value):
                self.assertIsNone(mesh_position(dict(lat=0, lon=0, receiver_position=invalid)))

    def test_missing_node_and_receiver_position_cannot_manufacture_a_location(self):
        for contact in (None, [], {}, dict(lat=0, lon=0), dict(lat=0, lon=0, receiver_position=None)):
            with self.subTest(contact=contact):
                self.assertIsNone(mesh_position(contact))

    def test_real_loot_exports_reception_position_but_keeps_node_coordinates_separate(self):
        loot = self.loot_manager()
        observed = self.observation()
        for protocol, node_id in (("meshcore", "abababababababab"), ("meshtastic", "!12345678")):
            data = self.contact_data(receiver_position=deepcopy(observed))
            if protocol == "meshcore":
                data["pubkey"] = "ab" * 32
            result = loot.register_mesh_contact(protocol, node_id, data, current_xp=loot._db.get("xp", 100))
            contact = result["contact"]
            with self.subTest(protocol=protocol):
                self.assertEqual((contact["lat"], contact["lon"], contact["has_position"]), (0, 0, False))
                self.assertEqual(contact["receiver_position"], observed)
                row = self.exported_rows(loot, protocol)[0]
                self.assertEqual((float(row["lat"]), float(row["lon"])), (12.5, 34.5))
                self.assertEqual(row["position_source"], "receiver_gps")
                self.assertEqual((float(row["node_lat"]), float(row["node_lon"])), (0, 0))
                self.assertEqual(float(row["position_observed_at"]), 998)
                self.assertEqual(row["timestamp"], datetime.fromtimestamp(998).strftime("%Y-%m-%dT%H:%M:%S"))
                if protocol == "meshcore":
                    self.assertEqual(row["public_key"], "ab" * 32)

    def test_real_backfill_assigns_both_protocols_without_changing_history_xp_or_ledger(self):
        loot = self.loot_manager()
        for protocol, node_id in (("meshcore", "abababababababab"), ("meshtastic", "!12345678")):
            loot.register_mesh_contact(protocol, node_id, self.contact_data(), current_xp=loot._db.get("xp", 100))
        previous = deepcopy(loot._db)
        with patch("watchdogs.mesh_position.time.monotonic", return_value=self.now), \
                patch("watchdogs.mesh_position.time.time", return_value=self.wall_now):
            result = loot.assign_receiver_gps_to_unlocated_contacts()
        self.assertFalse(result["waiting_for_fix"])
        self.assertEqual({protocol: len(rows) for protocol, rows in result["contacts"].items()},
                         {"meshcore": 1, "meshtastic": 1})
        self.assertEqual(loot._db["xp"], previous["xp"])
        self.assertEqual(loot._db["mesh_discovery_events"], previous["mesh_discovery_events"])
        for protocol, key in (("meshcore", "mc_contacts"), ("meshtastic", "mt_contacts")):
            for node_id, old in previous[key].items():
                contact = loot._db[key][node_id]
                for field in ("first_seen", "last_seen", "note", "lat", "lon", "has_position"):
                    self.assertEqual(contact[field], old[field])
                self.assertEqual(contact["receiver_position"]["kind"], "user_assignment")
                self.assertEqual(contact["receiver_position"]["assigned_at"], self.wall_now)
                row = self.exported_rows(loot, protocol)[0]
                self.assertEqual(row["position_source"], "receiver_assignment")
                self.assertEqual((float(row["lat"]), float(row["lon"])), (12.5, 34.5))
                self.assertEqual(row["timestamp"], datetime.fromtimestamp(1000).strftime("%Y-%m-%dT%H:%M:%S"))

    def test_repeated_backfill_is_empty_and_receiver_provenance_survives_restart(self):
        loot = self.loot_manager()
        loot.register_mesh_contact("meshtastic", "!12345678", self.contact_data(), current_xp=100)
        with patch("watchdogs.mesh_position.time.monotonic", return_value=self.now), \
                patch("watchdogs.mesh_position.time.time", return_value=self.wall_now):
            loot.assign_receiver_gps_to_unlocated_contacts()
            previous = deepcopy(loot._db)
            result = loot.assign_receiver_gps_to_unlocated_contacts()
        self.assertEqual(result, {"waiting_for_fix": False, "contacts": {"meshcore": [], "meshtastic": []}})
        self.assertEqual(loot._db, previous)
        restarted = self.loot_manager(loot._base.parent)
        contact = restarted.load_meshtastic_contacts()["!12345678"]
        self.assertEqual(contact["receiver_position"], previous["mt_contacts"]["!12345678"]["receiver_position"])
        self.assertEqual(restarted._db["xp"], previous["xp"])
        self.assertEqual(restarted._db["mesh_discovery_events"], previous["mesh_discovery_events"])

    def test_genuine_node_gps_later_replaces_effective_receiver_position_without_new_xp(self):
        loot = self.loot_manager()
        for protocol, node_id in (("meshcore", "abababababababab"), ("meshtastic", "!12345678")):
            initial = loot.register_mesh_contact(protocol, node_id,
                self.contact_data(receiver_position=self.observation()), current_xp=loot._db.get("xp", 100))
            observed = deepcopy(initial["contact"]["receiver_position"])
            result = loot.register_mesh_contact(protocol, node_id,
                self.contact_data(lat=56.5, lon=78.5), current_xp=loot._db["xp"])
            with self.subTest(protocol=protocol):
                self.assertFalse(result["new_event"])
                self.assertEqual(result["xp_award"], 0)
                self.assertEqual(result["contact"]["receiver_position"], observed)
                self.assertEqual(mesh_position(result["contact"]), dict(lat=56.5, lon=78.5, source="node"))
                row = self.exported_rows(loot, protocol)[0]
                self.assertEqual((float(row["lat"]), float(row["lon"])), (56.5, 78.5))
                self.assertEqual(row["position_source"], "node")

    def test_no_fresh_fix_leaves_contacts_and_waiting_exports_unchanged(self):
        loot = self.loot_manager()
        for protocol, node_id in (("meshcore", "abababababababab"), ("meshtastic", "!12345678")):
            loot.register_mesh_contact(protocol, node_id, self.contact_data(), current_xp=loot._db.get("xp", 100))
        previous = deepcopy(loot._db)
        exported = {protocol: self.exported_rows(loot, protocol) for protocol in ("meshcore", "meshtastic")}
        for fix in (None, dict(self.fix, observed_at=0), dict(self.fix, quality=0)):
            with self.subTest(fix=fix), patch.object(self.gps, "radio_fix", return_value=fix), \
                    patch("watchdogs.mesh_position.time.monotonic", return_value=self.now), \
                    patch("watchdogs.mesh_position.time.time", return_value=self.wall_now):
                result = loot.assign_receiver_gps_to_unlocated_contacts()
            self.assertTrue(result["waiting_for_fix"])
            self.assertEqual(result["contacts"], {"meshcore": [], "meshtastic": []})
            self.assertEqual(loot._db, previous)
            for protocol in ("meshcore", "meshtastic"):
                self.assertEqual(self.exported_rows(loot, protocol), exported[protocol])
                row = exported[protocol][0]
                self.assertEqual((float(row["lat"]), float(row["lon"])), (0, 0))
                self.assertEqual(row["position_source"], "waiting_for_receiver_gps")

    def test_meshtastic_callback_captures_receiver_gps_before_later_ui_queue_drain(self):
        from test_meshtastic_ui import game
        g = game()
        g.gps = self.gps
        g.loot = self.loot_manager()
        g._load_loot_totals = Mock()
        with patch("watchdogs.mesh_position.time.monotonic", return_value=100), \
                patch("watchdogs.mesh_position.time.time", return_value=1000):
            g._on_mt_node("!12345678", "Client", "synthetic", None, None, -90, -3)
        self.fix.update(lat=23.5, lon=45.5, observed_at=198)
        with patch("watchdogs.mesh_position.time.monotonic", return_value=200), \
                patch("watchdogs.mesh_position.time.time", return_value=2000):
            g._poll_meshtastic()
        contact = g.loot.load_meshtastic_contacts()["!12345678"]
        self.assertEqual((contact["lat"], contact["lon"]), (0, 0))
        self.assertEqual((contact["receiver_position"]["lat"], contact["receiver_position"]["lon"]), (12.5, 34.5))
        self.assertEqual(contact["receiver_position"]["observed_at"], 998)
        self.assertEqual(contact["last_seen"], 1000)
        self.assertIn("[RX GPS]", g.markers[0].label)

    def test_meshcore_callback_also_captures_receiver_gps_before_queue_drain(self):
        source = Path(__file__).resolve().parents[1] / "watchdogs" / "app.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        game_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "WatchDogsGame")
        callback = next(node for node in game_class.body if isinstance(node, ast.FunctionDef) and node.name == "_on_mc_node")
        namespace = {"time": time}
        exec(compile(ast.Module(body=[deepcopy(callback)], type_ignores=[]), str(source), "exec"), namespace)
        g = SimpleNamespace(gps=self.gps, _mc_event_queue=Queue())
        with patch("watchdogs.mesh_position.time.monotonic", return_value=100), \
                patch("watchdogs.mesh_position.time.time", return_value=1000):
            namespace["_on_mc_node"](g, "abababababababab", 1, "synthetic", None, None, -90, -3, b"a" * 32)
        self.fix.update(lat=23.5, lon=45.5, observed_at=198)
        event = g._mc_event_queue.get_nowait()
        self.assertEqual(event[0], "node")
        self.assertEqual((event[-2]["lat"], event[-2]["lon"]), (12.5, 34.5))
        self.assertEqual(event[-2]["observed_at"], 998)
        self.assertEqual(event[-1], 1000)

    def test_later_packet_without_receiver_fix_preserves_prior_genuine_observation(self):
        loot = self.loot_manager()
        observed = self.observation()
        for protocol, node_id in (("meshcore", "abababababababab"), ("meshtastic", "!12345678")):
            loot.register_mesh_contact(protocol, node_id, self.contact_data(receiver_position=observed),
                                       current_xp=loot._db.get("xp", 100))
            result = loot.register_mesh_contact(protocol, node_id,
                self.contact_data(receiver_position=None, last_seen=300), current_xp=loot._db["xp"])
            with self.subTest(protocol=protocol):
                self.assertEqual(result["contact"]["receiver_position"], observed)
                self.assertEqual(result["xp_award"], 0)
                self.assertEqual(mesh_position(result["contact"])["source"], "receiver_gps")


if __name__ == "__main__":
    unittest.main()
