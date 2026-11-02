"""Live ADS-B visibility using real SBS parsing and native drawing methods.

These tests use temporary in-memory observations and drawing records only.
No receiver, subprocess, player save, graphical window or network is opened.
"""
import ast
import copy
import io
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from watchdogs.sdr_manager import ADSB_TIMEOUT, Aircraft, SDRManager


class DrawingRecorder:
    frame_count = 42

    def __init__(self):
        self.calls = []

    def __getattr__(self, operation):
        return lambda *args: self.calls.append((operation, args))


PX = DrawingRecorder()
SOURCE = Path(__file__).resolve().parents[1] / "watchdogs/app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
ENV = dict(pyxel=PX, math=math)
for statement in TREE.body:
    if isinstance(statement, ast.Assign):
        try:
            ast.literal_eval(statement.value)
        except (ValueError, TypeError):
            continue
        exec(compile(ast.Module(body=[statement], type_ignores=[]), str(SOURCE), "exec"), ENV)
native_game = next(node for node in TREE.body
                   if isinstance(node, ast.ClassDef) and node.name == "WatchDogsGame")
methods = {"_draw_aircraft", "_draw_radar", "_sdr_adsb", "_poll_sdr"}
harness = ast.ClassDef(name="DrawingGame", bases=[], keywords=[], decorator_list=[],
                      body=[copy.deepcopy(node) for node in native_game.body
                            if isinstance(node, ast.FunctionDef) and node.name in methods])
exec(compile(ast.fix_missing_locations(ast.Module(body=[harness], type_ignores=[])),
             str(SOURCE), "exec"), ENV)
DrawingGame = ENV["DrawingGame"]


def sbs(kind="3", lat="51.1", lon="0.1"):
    fields = [""] * 22
    fields[0], fields[1], fields[4] = "MSG", kind, "ABC123"
    if kind == "3":
        fields[11], fields[14], fields[15] = "38000", lat, lon
    elif kind == "1":
        fields[10] = "TEST123"
    elif kind == "4":
        fields[12], fields[13] = "200", "90"
    return ",".join(fields)


def drawing_game(manager):
    game = DrawingGame()
    game._sdr = manager
    game.proj = SimpleNamespace(zoom=4, lon_span=1,
                                geo_to_screen=lambda lat, lon: (100, 100),
                                screen_visible=lambda x, y: True)
    game.player_lat, game.player_lon = 51, 0
    game.ble_devices, game.wifi_networks, game.markers, game.loot_points = [], [], [], []
    game._active_lora_protocol = lambda: ""
    return game


class AdsbVisibilityTests(unittest.TestCase):
    def setUp(self):
        PX.calls.clear()
        self.manager = SDRManager()
        self.manager.running, self.manager.mode = True, "adsb"
        self.parse(1000, sbs())
        self.parse(1000, sbs("1"))

    def parse(self, now, line):
        with patch("watchdogs.sdr_manager.time.time", return_value=now):
            self.manager._parse_sbs_line(line)

    def draw(self, now):
        game = drawing_game(self.manager)
        with patch("watchdogs.sdr_manager.time.time", return_value=now):
            game._draw_aircraft()
            game._draw_radar()
        map_visible = any(op == "text" and args[2] == "TEST123 38k"
                          for op, args in PX.calls)
        # This aircraft is displaced from the player's radar cross.
        radar_visible = ("pset", (614, 36, ENV["C_HACK_CYAN"])) in PX.calls
        return map_visible, radar_visible

    def test_real_fresh_sbs_position_appears_on_map_and_radar(self):
        self.assertEqual(self.draw(1001), (True, True))

    def test_off_and_non_adsb_modes_hide_cached_positions_in_both_views(self):
        for running, mode in ((False, "adsb"), (False, ""), (True, "433"),
                              (True, "emergency")):
            with self.subTest(running=running, mode=mode):
                PX.calls.clear()
                self.manager.running, self.manager.mode = running, mode
                self.assertEqual(self.draw(1001), (False, False))
                self.assertIn("ABC123", self.manager.aircraft)

    def test_combined_adsb_and_433_keeps_fresh_aircraft_visible(self):
        self.manager.mode = "adsb+433"
        self.assertEqual(self.draw(1001), (True, True))

    def test_freshness_boundary_is_identical_in_both_views_without_pruning(self):
        for age, expected in ((ADSB_TIMEOUT, True), (ADSB_TIMEOUT + 0.01, False)):
            with self.subTest(age=age):
                PX.calls.clear()
                self.assertEqual(self.draw(1000 + age), (expected, expected))
                self.assertIn("ABC123", self.manager.aircraft)

    def test_callsign_and_velocity_do_not_refresh_an_old_position(self):
        for kind in ("1", "4"):
            self.parse(1000 + ADSB_TIMEOUT + 1, sbs(kind))
            self.assertEqual(self.manager.aircraft["ABC123"].last_seen, 1061)
            self.assertEqual(self.manager.aircraft["ABC123"].position_seen, 1000)
            self.assertEqual(self.draw(1061), (False, False))
        self.parse(1062, sbs())
        PX.calls.clear()
        self.assertEqual(self.draw(1062), (True, True))

    def test_invalid_or_incomplete_position_does_not_refresh_old_fix(self):
        for lat, lon in (("nan", "0.1"), ("51.1", "inf"), ("91", "0.1"),
                         ("51.1", "181"), ("51.1", ""), ("0", "0")):
            with self.subTest(lat=lat, lon=lon):
                self.parse(1061, sbs(lat=lat, lon=lon))
                self.assertEqual(self.manager.aircraft["ABC123"].position_seen, 1000)
                self.assertEqual(self.draw(1061), (False, False))

    def test_unknown_invalid_or_future_timestamps_are_hidden(self):
        aircraft = self.manager.aircraft["ABC123"]
        for value in (0, -1, float("nan"), float("inf"), 1002):
            with self.subTest(position_seen=value):
                aircraft.position_seen = value
                self.assertEqual(self.manager.visible_aircraft(now=1001), [])

    def test_stop_request_hides_positions_while_reader_shutdown_is_pending(self):
        self.manager._stop_requested = True
        self.assertEqual(self.draw(1001), (False, False))
        self.assertTrue(self.manager.running)
        self.assertIn("ABC123", self.manager.aircraft)

    def test_confirmed_stop_clears_only_live_aircraft_cache(self):
        self.manager.total_aircraft_seen = 12001
        self.manager.sensors["retained"] = object()
        recorded_log = self.manager._adsb_log = io.StringIO("saved aircraft row\n")
        self.assertTrue(self.manager.stop())
        self.assertEqual(self.manager.aircraft, {})
        self.assertEqual(self.manager.total_aircraft_seen, 12001)
        self.assertIn("retained", self.manager.sensors)
        self.assertTrue(recorded_log.closed)
        # Re-enabling a source cannot resurrect the old map position.
        self.manager.running, self.manager.mode = True, "adsb"
        self.assertEqual(self.draw(1001), (False, False))

    def test_native_xp_dedup_survives_stop_and_new_position_for_same_aircraft(self):
        game = drawing_game(self.manager)
        game.xp = 1597146
        game._sdr_aircraft_xp = set()
        game._term_add = Mock()
        game.gain_xp = lambda amount: setattr(game, "xp", game.xp + amount)
        game._poll_sdr()
        self.assertEqual(game.xp, 1597156)
        self.assertEqual(game._sdr_aircraft_xp, {"ABC123"})
        self.assertTrue(self.manager.stop())
        self.manager.running, self.manager.mode = True, "adsb"
        self.parse(1062, sbs())
        game._poll_sdr()
        self.assertEqual(game.xp, 1597156)
        self.assertEqual(game._sdr_aircraft_xp, {"ABC123"})

    def test_failed_stop_retains_aircraft_ownership_and_open_log(self):
        reader = Mock()
        reader.is_alive.return_value = True
        self.manager._threads.append(reader)
        self.manager._adsb_log = io.StringIO("keep pending reader output\n")
        self.assertFalse(self.manager.stop())
        self.assertTrue(self.manager.running)
        self.assertEqual(self.manager.mode, "adsb")
        self.assertIn("ABC123", self.manager.aircraft)
        self.assertFalse(self.manager._adsb_log.closed)
        self.assertEqual(self.draw(1001), (False, False))
        self.manager._adsb_log.close()

    def test_new_manager_has_no_historical_aircraft(self):
        self.assertEqual(SDRManager().aircraft, {})

    def test_failed_stop_is_not_reported_as_stopped(self):
        game = drawing_game(self.manager)
        game._sdr_enabled = True
        game.msg, game._term_add = Mock(), Mock()
        with patch.object(self.manager, "stop", return_value=False):
            game._sdr_adsb()
        self.assertIn("pending", game.msg.call_args.args[0].lower())
        self.assertIn("ownership retained", game._term_add.call_args.args[0])
        self.assertNotIn("radar stopped", game._term_add.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
