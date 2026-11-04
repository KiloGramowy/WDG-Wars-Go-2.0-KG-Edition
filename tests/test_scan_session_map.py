"""Native Wi-Fi/BLE RX, map layers and real temporary history; no RF or GUI."""
import ast
import copy
from contextlib import contextmanager
from datetime import datetime
import os
from pathlib import Path
import random
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_mesh_map_history import initializer_range
from test_meshtastic_ui import ENV, PX, SOURCE, TREE
from test_wardrive_alternation import WardriveGame
import test_wardrive_alternation as alternation
import test_scanner_lifecycle as scanner
from watchdogs.app_state import AppState
from watchdogs.loot_manager import LootManager
from watchdogs.network_manager import NetworkManager


WIFI, BLE = "02:00:00:00:00:01", "02:00:00:00:00:02"
GAME = next(node for node in TREE.body if isinstance(node, ast.ClassDef)
            and node.name == "WatchDogsGame")
INIT = next(node for node in GAME.body if isinstance(node, ast.FunctionDef)
            and node.name == "_initialize")
FIELDS = {"loot_points", "_loot_points_ts", "_loot_totals", "_clusters", "_cluster_zoom",
          "_cluster_center", "_cluster_frame", "_cluster_sel", "_cluster_popup",
          "_popup_scroll", "_cracked_ssids"}


def assigns_field(statement):
    targets = statement.targets if isinstance(statement, ast.Assign) else (
        [statement.target] if isinstance(statement, ast.AnnAssign) else [])
    return any(isinstance(target, ast.Attribute) and target.attr in FIELDS for target in targets)


startup = ast.FunctionDef(name="restore_startup", args=ast.arguments(posonlyargs=[],
    args=[ast.arg(arg="self")], kwonlyargs=[], kw_defaults=[], defaults=[]), decorator_list=[],
    body=initializer_range("ble_devices", "particles")
        + initializer_range("_known_ble", "hack_target")
        + [copy.deepcopy(statement) for statement in INIT.body if assigns_field(statement)])
METHODS = {"_handle_serial_line", "_record_scan_map_observation", "_refresh_loot_points",
           "_load_loot_totals", "_update_clusters", "_draw_loot_points", "_draw_wifi",
           "_draw_ble", "_draw_radar", "_draw_cluster_popup", "_classify_auth",
           "_loot_search_exec", "gain_xp", "_save_xp_if_dirty"}
namespace = dict(ENV, WardriveGame=WardriveGame, random=random, re=re,
                 upload_manager=SimpleNamespace(load_wpasec_passwords=lambda _path: {}))
for node in TREE.body:
    if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
        and target.id in {"_BLE_RE", "_AIRTAG_COUNT_RE"} for target in node.targets):
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), namespace)
    if isinstance(node, ast.ClassDef) and node.name in {"BleDevice", "WifiNetwork"}:
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), namespace)
harness = ast.ClassDef(name="SessionMapGame", bases=[ast.Name(id="WardriveGame", ctx=ast.Load())],
    keywords=[], decorator_list=[], body=[startup] + [copy.deepcopy(node) for node in GAME.body
        if isinstance(node, ast.FunctionDef) and node.name in METHODS])
exec(compile(ast.fix_missing_locations(ast.Module(body=[harness], type_ignores=[])),
             str(SOURCE), "exec"), namespace)
SessionMapGame = namespace["SessionMapGame"]


def gps_at(lat=12.5, lon=34.5, valid=True):
    return SimpleNamespace(available=True, fix=SimpleNamespace(valid=valid,
        latitude=lat, longitude=lon, altitude=20, hdop=1))


def game(loot=None, gps=None):
    value = scanner.game()
    value.__class__ = SessionMapGame
    value.loot, value.gps = loot, gps or gps_at()
    value.state = AppState()
    value.net_mgr = NetworkManager(value.state)
    value.restore_startup()
    value._fw_version = "synthetic"
    value._bt_airtag = False
    value._whitelist = SimpleNamespace(is_blocked=lambda _identity: False)
    value.gps_fix = value.gps.fix.valid
    value.player_lat, value.player_lon = 12.5, 34.5
    value.xp = loot.load_xp() if loot else 100
    value._xp_dirty = False
    value._app_dir = str(loot._base.parent) if loot else "/unused"
    value.proj = SimpleNamespace(zoom=10, center_lat=12.5, center_lon=34.5, lon_span=1,
        geo_to_screen=lambda _lat, _lon: (100, 100), screen_visible=lambda _x, _y: True)
    value._sync_mesh_map_visibility = Mock()
    value._active_lora_protocol = lambda: ""
    value._sdr = SimpleNamespace(running=False, visible_aircraft=lambda: [])
    PX.frame_count = 42
    return value


def receive(value, kind, identity=None, rssi=-70, name="Synthetic"):
    if kind == "wifi":
        identity = identity or WIFI
        value._handle_serial_line(f'"1","{name}","Fixture","{identity}","6","WPA2","{rssi}","2.4"')
    else:
        identity = identity or BLE
        value._handle_serial_line(f"1. {identity} RSSI: {rssi} dBm Name: {name}")


@contextmanager
def loot_at(root, stamp, gps):
    with patch("watchdogs.loot_manager.datetime", wraps=datetime) as clock, \
            patch.object(LootManager, "_periodic_backup_loop", lambda _self: None):
        clock.now.return_value = datetime.fromisoformat(stamp)
        loot = LootManager(root, gps)
    try:
        yield loot
    finally:
        with patch.object(os, "sync", lambda: None, create=True):
            loot.close()


def seed_history(root):
    with loot_at(root, "2020-01-01T12:00:00", gps_at()) as loot:
        network = SimpleNamespace(bssid=WIFI, ssid="Old synthetic WiFi", auth="WPA2",
                                  channel="6", rssi="-50")
        loot.save_wardriving_network(network)
        loot.save_bt_device(BLE, -50, "Old synthetic BLE", False, False)
        loot.save_wardriving_bt(BLE, -50, "Old synthetic BLE")
        loot.log_serial("Synthetic historical log; keep this urobek")
        loot.save_xp(12345)
        folder = Path(loot.session_dir)
    whitelist = Path(root) / "whitelist.json"
    whitelist.write_text('[{"type":"ble","mac":"02:00:00:00:00:FF","name":"Keep"}]')
    upload = Path(root) / "plugins" / ".wardrive_state.json"
    upload.parent.mkdir()
    upload.write_text('{"pending":["synthetic-session"],"uploaded":[]}')
    files = [path for path in folder.rglob("*") if path.is_file()] + [whitelist, upload]
    return {path: path.read_bytes() for path in files}


class ScanSessionMapTests(unittest.TestCase):
    def test_startup_history_refresh_and_global_search_do_not_populate_any_map_layer(self):
        with tempfile.TemporaryDirectory() as root:
            original = seed_history(root)
            with loot_at(root, "2021-01-01T12:00:00", gps_at()) as loot:
                value = game(loot, loot._gps)
                value._refresh_loot_points()
                self.assertGreaterEqual(len(value.loot_points), 2)
                self.assertEqual(value._session_scan_points, {})
                self.assertIn(WIFI, value._known_wifi)
                self.assertEqual(value._known_ble, {BLE})
                database = loot._db_path.read_bytes()
                PX.calls.clear()
                value._draw_loot_points()
                value._draw_wifi()
                value._draw_ble()
                self.assertEqual(value._clusters, [])
                self.assertEqual(PX.calls, [])
                value._draw_radar()
                self.assertFalse(any(op == "pset" and args[-1] in (namespace["C_SUCCESS"],
                    namespace["C_HACK_CYAN"]) for op, args in PX.calls))
                value._loot_search = "Old synthetic"
                value._loot_search_exec()
                self.assertEqual(len(value._loot_search_results), 2)
                self.assertEqual(value.xp, 12345)
                self.assertEqual({path: path.read_bytes() for path in original}, original)
                self.assertEqual(loot._db_path.read_bytes(), database)

    def test_known_receptions_reappear_once_and_update_without_new_discovery_awards(self):
        with tempfile.TemporaryDirectory() as root:
            original = seed_history(root)
            with loot_at(root, "2021-01-01T12:00:00", gps_at()) as loot:
                value = game(loot, loot._gps)
                self.assertEqual(value._known_ble, {BLE})
                self.assertEqual(value._known_wifi, {WIFI})
                for kind in ("wifi", "bt"):
                    receive(value, kind)
                    if kind == "wifi":
                        PX.calls.clear()
                        value._draw_loot_points()
                        self.assertEqual(value._clusters[0]["count"], 1)
                        self.assertTrue(any(operation == "circ" and args[2] == 2
                                            for operation, args in PX.calls))
                    receive(value, kind, rssi=-90, name="Updated")
                self.assertEqual(len(value._session_scan_points), 2)
                self.assertEqual(value.ble_devices, [])
                self.assertEqual(value.wifi_networks, [])
                self.assertEqual(value.xp, 12349)  # Existing one-XP repeat rule.
                self.assertTrue(all(point["label"] == "Updated" and int(point["rssi"]) == -90
                                    for point in value._session_scan_points.values()))
                value._draw_loot_points()
                self.assertEqual(len(value._clusters), 1)
                self.assertEqual(value._clusters[0]["count"], 2)
                value._cluster_popup = value._clusters[0]
                PX.calls.clear()
                value._draw_cluster_popup()
                texts = [args[2] for operation, args in PX.calls if operation == "text"]
                self.assertIn("Point networks: 2", texts)
                self.assertEqual(texts.count("Updated"), 2)
                PX.calls.clear()
                value._draw_radar()
                points = [args for operation, args in PX.calls if operation == "pset"
                          and args[-1] in (namespace["C_SUCCESS"], namespace["C_HACK_CYAN"])]
                self.assertEqual(len(points), 2)
                value._refresh_loot_points()
                value._draw_loot_points()
                self.assertEqual(value._clusters[0]["count"], 2)
                self.assertEqual({path: path.read_bytes() for path in original}, original)

    def test_saved_ble_reopened_through_native_startup_gets_repeat_xp_and_one_fresh_point(self):
        with tempfile.TemporaryDirectory() as root:
            # Isolate bt_devices.csv: a WiGLE BLE export must not mask its loader.
            with loot_at(root, "2020-01-01T12:00:00", gps_at()) as loot:
                self.assertTrue(loot.save_bt_device(BLE, -50, "Historical BLE", False, False))
                loot.log_serial("Preserve the original BLE log")
                loot.save_xp(12345)
                folder = Path(loot.session_dir)
                self.assertFalse((folder / "wardriving.csv").exists())
            original = {path: path.read_bytes() for path in folder.rglob("*") if path.is_file()}
            with loot_at(root, "2021-01-01T12:00:00", gps_at()) as loot:
                value = game(loot, loot._gps)
                self.assertEqual(value._known_ble, {BLE})
                self.assertEqual(value._known_wifi, set())
                value._refresh_loot_points()
                value._draw_loot_points()
                self.assertEqual(value._session_scan_points, {})
                self.assertEqual(value._clusters, [])
                self.assertEqual(value.xp, 12345)
                receive(value, "bt")
                self.assertEqual(value.xp, 12346)
                self.assertEqual(value.ble_devices, [])
                self.assertEqual(set(value._session_scan_points), {("bt", BLE)})
                point = value._session_scan_points[("bt", BLE)]
                self.assertEqual((point["lat"], point["lon"]), (12.5, 34.5))
                value._draw_loot_points()
                self.assertEqual(value._clusters[0]["count"], 1)
                value._save_xp_if_dirty()
                self.assertEqual(loot.load_xp(), 12346)
                self.assertEqual({path: path.read_bytes() for path in original}, original)
            with loot_at(root, "2022-01-01T12:00:00", gps_at()) as loot:
                restarted = game(loot, loot._gps)
                self.assertEqual(restarted.xp, 12346)
                self.assertEqual(restarted._known_ble, {BLE})
                self.assertEqual(restarted._session_scan_points, {})
                self.assertEqual({path: path.read_bytes() for path in original}, original)

    def test_new_ble_and_wifi_keep_full_awards_without_cross_protocol_deduplication(self):
        with tempfile.TemporaryDirectory() as root:
            original = seed_history(root)
            with loot_at(root, "2021-01-01T12:00:00", gps_at()) as loot:
                value = game(loot, loot._gps)
                # Each address exists historically only in the other protocol.
                for kind, address, full_award in (("bt", WIFI, 10), ("wifi", BLE, 15)):
                    with self.subTest(kind=kind):
                        before = value.xp
                        receive(value, kind, identity=address)
                        self.assertEqual(value.xp - before, full_award)
                        receive(value, kind, identity=address)
                        self.assertEqual(value.xp - before, full_award + 1)
                self.assertEqual(len(value._session_scan_points), 2)
                self.assertEqual(len(value.ble_devices), 1)
                self.assertEqual(len(value.wifi_networks), 1)
                self.assertEqual({path: path.read_bytes() for path in original}, original)

    def test_native_comma_and_quote_names_reopen_in_the_correct_protocol_with_existing_xp_rules(self):
        for name in ('Sensor, Kitchen', '"Sensor, Kitchen', 'Sensor "Kitchen", BLE', '"Sensor", Kitchen'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                with loot_at(root, "2020-01-01T12:00:00", gps_at()) as loot:
                    self.assertTrue(loot.save_bt_device(BLE, -50, name, False, False))
                    self.assertTrue(loot.save_wardriving_bt(BLE, -50, name))
                    network = SimpleNamespace(bssid=WIFI, ssid=name, auth="WPA2", channel="6", rssi="-50")
                    self.assertTrue(loot.save_wardriving_network(network))
                    loot.log_serial("Preserve native comma/quote-name history")
                    loot.save_xp(12345)
                    folder = Path(loot.session_dir)
                original = {path: path.read_bytes() for path in folder.rglob("*") if path.is_file()}
                with loot_at(root, "2021-01-01T12:00:00", gps_at()) as loot:
                    value = game(loot, loot._gps)
                    self.assertEqual(value._known_ble, {BLE})
                    self.assertEqual(value._known_wifi, {WIFI})
                    self.assertEqual(value._session_scan_points, {})
                    self.assertEqual(value.xp, 12345)
                    for kind, address, award in (("bt", BLE, 1), ("wifi", WIFI, 1),
                                                 ("bt", WIFI, 10), ("wifi", BLE, 15)):
                        before = value.xp
                        receive(value, kind, identity=address)
                        self.assertEqual(value.xp - before, award)
                        receive(value, kind, identity=address)
                        self.assertEqual(value.xp - before, award + 1)
                    self.assertEqual(len(value._session_scan_points), 4)
                    value._save_xp_if_dirty()
                    self.assertEqual(loot.load_xp(), value.xp)
                    self.assertEqual({path: path.read_bytes() for path in original}, original)

    def test_shared_mac_protocols_survive_second_reopen_and_only_update_their_own_rssi(self):
        def save(loot, kind, rssi, name):
            if kind == "wifi":
                return loot.save_wardriving_network(SimpleNamespace(
                    bssid=BLE, ssid=name, auth="WPA2", channel="6", rssi=str(rssi)))
            loot.save_bt_device(BLE, rssi, name, False, False)
            return loot.save_wardriving_bt(BLE, rssi, name)

        def rows(path):
            records = path.read_bytes().splitlines(keepends=True)[2:]
            self.assertEqual(len(records), 2)
            by_type = {record.rsplit(b",", 1)[-1].strip(): record for record in records}
            self.assertEqual(set(by_type), {b"WIFI", b"BLE"})
            return by_type

        for first in ("wifi", "bt"):
            for name in ("Sensor, Kitchen", '"Sensor, Kitchen'):
                with self.subTest(first=first, name=name), tempfile.TemporaryDirectory() as root:
                    with loot_at(root, "2020-01-01T12:00:00", gps_at()) as loot:
                        self.assertTrue(save(loot, first, -50, name))
                        loot.log_serial("Preserve the first protocol's original history")
                        loot.save_xp(12345)
                        first_folder = Path(loot.session_dir)
                    original = {path: path.read_bytes() for path in first_folder.rglob("*") if path.is_file()}
                    second = "bt" if first == "wifi" else "wifi"
                    full_award = 10 if second == "bt" else 15
                    with loot_at(root, "2021-01-01T12:00:00", gps_at()) as loot:
                        value = game(loot, loot._gps)
                        self.assertEqual(value._known_wifi, {BLE} if first == "wifi" else set())
                        self.assertEqual(value._known_ble, {BLE} if first == "bt" else set())
                        self.assertEqual(value._session_scan_points, {})
                        receive(value, second, identity=BLE)
                        self.assertEqual(value.xp, 12345 + full_award)
                        receive(value, second, identity=BLE)
                        self.assertEqual(value.xp, 12346 + full_award)
                        path = Path(loot.session_dir) / "wardriving.csv"
                        second_row = path.read_bytes().splitlines(keepends=True)[2]
                        receive(value, first, identity=BLE)
                        self.assertEqual(value.xp, 12347 + full_award)
                        typed = rows(path)
                        second_type = b"BLE" if second == "bt" else b"WIFI"
                        self.assertEqual(typed[second_type], second_row)
                        # Existing line endings and the other protocol's row
                        # must survive replacing a stronger same-protocol row.
                        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
                        header = path.read_bytes().splitlines(keepends=True)[:2]
                        previous = rows(path)
                        self.assertTrue(save(loot, "wifi", -40, name))
                        updated = rows(path)
                        self.assertEqual(updated[b"BLE"], previous[b"BLE"])
                        self.assertNotEqual(updated[b"WIFI"], previous[b"WIFI"])
                        self.assertIn(b",-40,", updated[b"WIFI"])
                        self.assertTrue(save(loot, "bt", -30, name))
                        latest = rows(path)
                        self.assertEqual(latest[b"WIFI"], updated[b"WIFI"])
                        self.assertIn(b",-30,", latest[b"BLE"])
                        self.assertEqual(path.read_bytes().splitlines(keepends=True)[:2], header)
                        unchanged = path.read_bytes()
                        for kind, rssi in (("wifi", -40), ("wifi", -80), ("bt", -30), ("bt", -80)):
                            self.assertFalse(save(loot, kind, rssi, "Weaker, ignored"))
                            self.assertEqual(path.read_bytes(), unchanged)
                        self.assertEqual(set(value._session_scan_points), {("wifi", BLE), ("bt", BLE)})
                        value._save_xp_if_dirty()
                        second_folder = Path(loot.session_dir)
                    preserved = dict(original)
                    preserved.update({path: path.read_bytes() for path in second_folder.rglob("*") if path.is_file()})
                    with loot_at(root, "2022-01-01T12:00:00", gps_at()) as loot:
                        restarted = game(loot, loot._gps)
                        self.assertEqual(restarted._known_wifi, {BLE})
                        self.assertEqual(restarted._known_ble, {BLE})
                        self.assertEqual(restarted._session_scan_points, {})
                        self.assertEqual(restarted.xp, 12347 + full_award)
                        for kind in ("wifi", "bt", "wifi", "bt"):
                            before = restarted.xp
                            receive(restarted, kind, identity=BLE)
                            self.assertEqual(restarted.xp - before, 1)
                        restarted._save_xp_if_dirty()
                        self.assertEqual(loot.load_xp(), restarted.xp)
                        self.assertEqual({path: path.read_bytes() for path in preserved}, preserved)

    def test_native_comma_quote_history_points_reopen_with_real_positions_and_names(self):
        from collections import Counter

        third = "02:00:00:00:00:03"
        expected = Counter({
            ("wifi", WIFI, '"AP, Kitchen', 12.5, 34.5, "-50"): 1,
            ("bt", BLE, '"Beacon, South', 13.5, 35.5, "-60"): 2,
            ("bt", third, 'One, two, three, four, "Following', 14.5, 36.5, "-70"): 2,
        })
        with tempfile.TemporaryDirectory() as root:
            with loot_at(root, "2020-01-01T12:00:00", gps_at()) as loot:
                network = SimpleNamespace(bssid=WIFI, ssid='"AP, Kitchen', auth="WPA2",
                                          channel="6", rssi="-50")
                self.assertTrue(loot.save_wardriving_network(network))
                for identity, name, rssi, lat, lon in (
                        (BLE, '"Beacon, South', -60, 13.5, 35.5),
                        (third, 'One, two, three, four, "Following', -70, 14.5, 36.5)):
                    loot._gps.fix = gps_at(lat, lon).fix
                    self.assertTrue(loot.save_bt_device(identity, rssi, name, False, False))
                    self.assertTrue(loot.save_wardriving_bt(identity, rssi, name))
                loot.log_serial("Preserve native historical names and their real GPS fixes")
                folder = Path(loot.session_dir)
            original = {path: path.read_bytes() for path in folder.rglob("*") if path.is_file()}
            with loot_at(root, "2021-01-01T12:00:00", gps_at()) as loot:
                points = loot.get_gps_points()
                actual = Counter((point["type"], point["bssid"], point["label"], point["lat"],
                                  point["lon"], point["rssi"]) for point in points)
                self.assertEqual(actual, expected)
                self.assertTrue(all(point["channel"] == "6" for point in points if point["type"] == "wifi"))
                self.assertTrue(all(point["auth"] == "[BLE]" for point in points if point["type"] == "bt"))
                self.assertEqual(loot.get_gps_points(), points)
                self.assertEqual({path: path.read_bytes() for path in original}, original)

    def test_native_scheduled_wifi_ble_handover_retains_both_observations(self):
        fixture = alternation.AlternationTests()
        fixture.setUp()
        value = fixture.g
        value.gps = gps_at()
        fixture.both()
        value.net_mgr.parse_network_line.return_value = SimpleNamespace(
            bssid="AA:BB:CC:DD:EE:FF", ssid="Synthetic", channel="6", auth="WPA2", rssi="-70")
        value._handle_serial_line("synthetic WiFi row")
        value.net_mgr.parse_network_line.return_value = None
        fixture.complete("wardriving")
        fixture.advance()
        fixture.ack()
        self.assertEqual(value._wardrive_phase, "bt_scanning")
        value._handle_serial_line("1. 11:22:33:44:55:66 RSSI: -70 dBm Name: Synthetic")
        points = copy.deepcopy(value._session_scan_points)
        fixture.complete("bt_scanning")
        fixture.advance()
        fixture.ack()
        self.assertEqual(value._wardrive_phase, "wardriving")
        self.assertEqual(len(points), 2)
        self.assertEqual(value._session_scan_points, points)

    def test_missing_invalid_or_manual_map_positions_never_create_zero_or_invented_points(self):
        for lat, lon, valid in ((12.5, 34.5, False), (0, 0, True), (float("nan"), 3, True),
                                (91, 3, True)):
            with self.subTest(lat=lat, lon=lon, valid=valid):
                value = game(gps=gps_at(lat, lon, valid))
                for kind in ("wifi", "bt"):
                    receive(value, kind)
                self.assertEqual(value._session_scan_points, {})
                PX.calls.clear()
                value._draw_loot_points(); value._draw_wifi(); value._draw_ble()
                self.assertEqual(PX.calls, [])
                value.gps.fix = gps_at().fix
                value._refresh_loot_points()  # A later fix is not another reception.
                self.assertEqual(value._session_scan_points, {})
                for kind in ("wifi", "bt"):
                    receive(value, kind)
                self.assertEqual(len(value._session_scan_points), 2)

    def test_positions_are_real_receiver_observations_repeats_move_without_jitter(self):
        value = game()
        for kind in ("wifi", "bt"):
            receive(value, kind)
        self.assertEqual(value.xp, 125)  # Preserve 15/10 XP for brand-new devices.
        self.assertTrue(all((point["lat"], point["lon"]) == (12.5, 34.5)
                            for point in value._session_scan_points.values()))
        value.player_lat, value.player_lon = 40, 60  # Map/manual position is not RX GPS.
        value.gps.fix = gps_at(13, 35).fix
        for kind in ("wifi", "bt"):
            receive(value, kind, rssi=-80)
        self.assertEqual(value.xp, 127)
        self.assertEqual(len(value._session_scan_points), 2)
        self.assertTrue(all((point["lat"], point["lon"]) == (13, 35)
                            for point in value._session_scan_points.values()))
        self.assertEqual((value.wifi_networks[0].lat, value.ble_devices[0].lat), (13, 13))
        value.gps.fix.valid = False
        receive(value, "wifi")
        self.assertEqual(value._session_scan_points[("wifi", WIFI)]["lat"], 13)

    def test_stop_handover_and_elapsed_time_keep_results_but_restart_resets_map(self):
        with tempfile.TemporaryDirectory() as root:
            original = seed_history(root)
            with loot_at(root, "2021-01-01T12:00:00", gps_at()) as loot:
                value = game(loot, loot._gps)
                for kind in ("wifi", "bt"):
                    receive(value, kind)
                points = copy.deepcopy(value._session_scan_points)
                value._clear_esp_scan_state(cancel_pending=True)
                value._wardrive_selected = {"wardriving", "bt_scanning"}
                value._cancel_wardrive_plan(clear_active=True)
                scanner.CLOCK.now += 100000
                for _ in range(3):
                    value._refresh_loot_points()
                    value._draw_loot_points()
                self.assertEqual(value._session_scan_points, points)
                restarted = game(loot, loot._gps)
                restarted._refresh_loot_points()
                restarted._draw_loot_points()
                self.assertEqual(restarted._session_scan_points, {})
                self.assertEqual(restarted._clusters, [])
                self.assertIn(WIFI, restarted._known_wifi)
                self.assertEqual(restarted._known_ble, loot.get_known_devices()[0])
                self.assertEqual(loot.load_xp(), 12345)
                self.assertEqual({path: path.read_bytes() for path in original}, original)


if __name__ == "__main__":
    unittest.main()
