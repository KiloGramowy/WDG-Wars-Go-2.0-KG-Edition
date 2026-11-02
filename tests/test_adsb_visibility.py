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
methods = {"_draw_aircraft", "_draw_radar", "_sdr_adsb", "_poll_sdr",
           "_sync_mesh_map_visibility"}
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
        self.off_now = 100
        self.manager._adsb_off_decay._clock = lambda: self.off_now
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

    def test_off_and_non_adsb_modes_keep_scan_until_display_expiry(self):
        with patch("watchdogs.sdr_manager.time.time", return_value=1001):
            self.assertTrue(self.manager.stop())
        for running, mode in ((False, "adsb"), (False, ""), (True, "433"),
                              (True, "emergency")):
            with self.subTest(running=running, mode=mode):
                PX.calls.clear()
                self.manager.running, self.manager.mode = running, mode
                with patch("watchdogs.sdr_manager.time.time", return_value=10000):
                    self.manager.prune_stale()
                self.assertEqual(self.draw(10000), (True, True))
                self.assertIn("ABC123", self.manager.aircraft)
        self.off_now = 700
        PX.calls.clear()
        self.assertEqual(self.draw(10000), (False, False))

    def test_combined_adsb_and_433_keeps_fresh_aircraft_visible(self):
        self.manager.mode = "adsb+433"
        self.assertEqual(self.draw(1001), (True, True))

    def test_freshness_boundary_is_identical_in_both_views_without_pruning(self):
        for age, expected in ((ADSB_TIMEOUT, True), (ADSB_TIMEOUT + 0.01, False)):
            with self.subTest(age=age):
                PX.calls.clear()
                self.assertEqual(self.draw(1000 + age), (expected, expected))
                self.assertIn("ABC123", self.manager.aircraft)

    def test_callsign_and_velocity_keep_last_position_in_receiver_range(self):
        for kind in ("1", "4"):
            self.parse(1000 + ADSB_TIMEOUT + 1, sbs(kind))
            self.assertEqual(self.manager.aircraft["ABC123"].last_seen, 1061)
            self.assertEqual(self.manager.aircraft["ABC123"].position_seen, 1000)
            self.assertEqual(self.draw(1061), (True, True))
        self.parse(1062, sbs(lat="51.2", lon="0.2"))
        self.assertEqual((self.manager.aircraft["ABC123"].lat,
                          self.manager.aircraft["ABC123"].lon), (51.2, .2))
        PX.calls.clear()
        self.assertEqual(self.draw(1062)[0], True)

    def test_invalid_or_incomplete_position_does_not_refresh_old_fix(self):
        for lat, lon in (("nan", "0.1"), ("51.1", "inf"), ("91", "0.1"),
                         ("51.1", "181"), ("51.1", ""), ("0", "0")):
            with self.subTest(lat=lat, lon=lon):
                self.parse(1061, sbs(lat=lat, lon=lon))
                self.assertEqual(self.manager.aircraft["ABC123"].position_seen, 1000)
                self.assertEqual((self.manager.aircraft["ABC123"].lat,
                                  self.manager.aircraft["ABC123"].lon), (51.1, .1))
                self.assertEqual(self.draw(1061), (True, True))

    def test_unknown_invalid_or_future_reception_timestamps_are_hidden(self):
        aircraft = self.manager.aircraft["ABC123"]
        for value in (0, -1, float("nan"), float("inf"), 1002):
            with self.subTest(last_seen=value):
                aircraft.last_seen = value
                self.assertEqual(self.manager.visible_aircraft(now=1001), [])

    def test_stop_request_keeps_last_view_while_reader_shutdown_is_pending(self):
        self.manager._stop_requested = True
        self.assertEqual(self.draw(10000), (True, True))
        self.assertTrue(self.manager.running)
        self.assertIn("ABC123", self.manager.aircraft)

    def test_confirmed_stop_starts_display_decay_and_preserves_saved_history(self):
        self.manager.total_aircraft_seen = 12001
        self.manager.sensors["retained"] = object()
        recorded_log = self.manager._adsb_log = io.StringIO("saved aircraft row\n")
        with patch("watchdogs.sdr_manager.time.time", return_value=1001):
            self.assertTrue(self.manager.stop())
        self.assertIn("ABC123", self.manager.aircraft)
        self.assertEqual(self.manager.total_aircraft_seen, 12001)
        self.assertIn("retained", self.manager.sensors)
        self.assertTrue(recorded_log.closed)
        self.assertEqual(self.draw(10000), (True, True))
        self.assertTrue(self.manager.stop())  # A repeated STOP keeps the view.
        PX.calls.clear()
        self.assertEqual(self.draw(20000), (True, True))
        self.off_now = 700
        PX.calls.clear()
        self.assertEqual(self.draw(20000), (False, False))

    def test_native_off_decay_hides_farther_aircraft_before_near_in_both_views(self):
        self.manager.update_gps(51, 0)
        self.parse(1000, sbs(lat="54", lon="0").replace("ABC123", "FAR123"))
        self.parse(1000, sbs(lat="52", lon="0").replace("ABC123", "MID123"))
        with patch("watchdogs.sdr_manager.time.time", return_value=1001):
            self.assertTrue(self.manager.stop())
        for off_now, expected in ((399.99, {"ABC123", "FAR123", "MID123"}),
                                  (400, {"ABC123", "MID123"}),
                                  (550, {"ABC123"}), (700, set())):
            with self.subTest(off_now=off_now):
                self.off_now = off_now
                self.assertEqual({ac.icao for ac in self.manager.visible_aircraft(now=10000)}, expected)
        PX.calls.clear()
        self.assertEqual(self.draw(10000), (False, False))

    def test_stopping_cannot_resurrect_an_already_expired_marker(self):
        self.assertEqual(self.draw(1061), (False, False))
        with patch("watchdogs.sdr_manager.time.time", return_value=1061):
            self.assertTrue(self.manager.stop())
        self.assertNotIn("ABC123", self.manager.aircraft)
        self.assertEqual(self.draw(10000), (False, False))

    def test_active_receiver_prunes_after_last_message_timeout(self):
        self.parse(1050, sbs("4"))
        with patch("watchdogs.sdr_manager.time.time", return_value=1110):
            self.manager.prune_stale()
        self.assertIn("ABC123", self.manager.aircraft)
        with patch("watchdogs.sdr_manager.time.time", return_value=1110.01):
            self.manager.prune_stale()
        self.assertNotIn("ABC123", self.manager.aircraft)

    def test_resume_waits_one_reception_window_before_expiring_frozen_scan(self):
        with patch("watchdogs.sdr_manager.time.time", return_value=1001):
            self.assertTrue(self.manager.stop())
        process, thread = Mock(), Mock()
        process.poll.return_value = None
        thread.is_alive.return_value = False
        with patch.object(self.manager, "has_dump1090", return_value=True), \
                patch.object(self.manager, "_spawn_owned", return_value=process), \
                patch("watchdogs.sdr_manager.subprocess.run"), \
                patch("watchdogs.sdr_manager.time.sleep"), \
                patch("watchdogs.sdr_manager.threading.Thread", return_value=thread), \
                patch("watchdogs.sdr_manager.time.time", return_value=10000):
            self.assertTrue(self.manager.start_adsb())
        self.assertEqual(self.manager.aircraft["ABC123"].last_seen, 1000)
        self.assertFalse(self.manager._adsb_off_decay.active)
        for now, expected in ((10000, True), (10060, True), (10060.01, False)):
            with self.subTest(now=now):
                PX.calls.clear()
                self.assertEqual(self.draw(now), (expected, expected))
        # A received callsign refreshes range even without new coordinates.
        self.parse(10061, sbs("1"))
        PX.calls.clear()
        self.assertEqual(self.draw(10062), (True, True))

    def test_resuming_cannot_resurrect_an_expired_off_marker(self):
        with patch("watchdogs.sdr_manager.time.time", return_value=1001):
            self.assertTrue(self.manager.stop())
        self.off_now = 700
        process, thread = Mock(), Mock()
        process.poll.return_value = None
        thread.is_alive.return_value = False
        with patch.object(self.manager, "has_dump1090", return_value=True), \
                patch.object(self.manager, "_spawn_owned", return_value=process), \
                patch("watchdogs.sdr_manager.subprocess.run"), \
                patch("watchdogs.sdr_manager.time.sleep"), \
                patch("watchdogs.sdr_manager.threading.Thread", return_value=thread), \
                patch("watchdogs.sdr_manager.time.time", return_value=10000):
            self.assertTrue(self.manager.start_adsb())
        self.assertNotIn("ABC123", self.manager.aircraft)
        self.assertEqual(self.manager.visible_aircraft(now=10000), [])

    def test_first_immediate_receiver_packet_survives_resume_of_off_scan(self):
        with patch("watchdogs.sdr_manager.time.time", return_value=1001):
            self.assertTrue(self.manager.stop())
        process, thread = Mock(), Mock()
        process.poll.return_value = None
        thread.is_alive.return_value = False
        observed = []

        def first_packet():
            observed.append((self.manager.running, self.manager.mode,
                             self.manager._adsb_off_decay.active))
            self.manager._parse_sbs_line(sbs().replace("ABC123", "DEF456"))

        thread.start.side_effect = first_packet
        with patch.object(self.manager, "has_dump1090", return_value=True), \
                patch.object(self.manager, "_spawn_owned", return_value=process), \
                patch("watchdogs.sdr_manager.subprocess.run"), \
                patch("watchdogs.sdr_manager.time.sleep"), \
                patch("watchdogs.sdr_manager.threading.Thread", return_value=thread), \
                patch("watchdogs.sdr_manager.time.time", return_value=10000):
            self.assertTrue(self.manager.start_adsb())
        self.assertIn("DEF456", self.manager.aircraft)
        fresh = self.manager.aircraft["DEF456"]
        self.assertEqual((fresh.last_seen, fresh.position_seen), (10000, 10000))
        self.assertIn(fresh, self.manager.visible_aircraft(now=10000))
        self.assertEqual(observed, [(True, "adsb", False)])

    def test_other_sources_own_their_visibility_and_expiry(self):
        external = Aircraft(icao="FED123", lat=52, lon=1, last_seen=1)
        external.acquisition_source = "source_adapter"
        self.manager.aircraft[external.icao] = external
        with patch("watchdogs.sdr_manager.time.time", return_value=1001):
            self.manager.prune_stale()
            self.assertTrue(self.manager.stop())
        self.assertIs(self.manager.aircraft[external.icao], external)
        self.assertNotIn(external, self.manager.visible_aircraft(now=10000))

    def test_adapter_draw_view_cannot_change_native_distance_expiry(self):
        self.manager.update_gps(51, 0)
        external = Aircraft(icao="FED123", lat=51.001, lon=0, last_seen=1000)
        external.acquisition_source = "source_adapter"
        native_view = self.manager.visible_aircraft
        self.manager.visible_aircraft = lambda now=None: native_view(now=now) + [external]
        with patch("watchdogs.sdr_manager.time.time", return_value=1001):
            self.assertTrue(self.manager.stop())
        # There is one native marker, so its own deadline remains ten minutes.
        self.off_now = 400
        self.assertEqual(native_view(now=10000), [self.manager.aircraft["ABC123"]])
        self.off_now = 700
        self.assertEqual(native_view(now=10000), [])

    def test_first_rf_message_does_not_inherit_an_adapter_position(self):
        external = Aircraft(icao="ABC123", lat=52, lon=1, last_seen=1000)
        external.acquisition_source = "source_adapter"
        self.manager.aircraft[external.icao] = external
        self.parse(1001, sbs("1"))
        aircraft = self.manager.aircraft["ABC123"]
        self.assertIsNot(aircraft, external)
        self.assertTrue(self.manager._owns_aircraft(aircraft))
        self.assertFalse(aircraft.has_position)
        self.assertEqual(self.manager.visible_aircraft(now=1001), [])
        self.parse(1002, sbs())
        self.assertEqual(self.manager.visible_aircraft(now=1002), [aircraft])

    def test_native_xp_dedup_survives_stop_and_new_position_for_same_aircraft(self):
        game = drawing_game(self.manager)
        game.xp = 1597146
        game._sdr_aircraft_xp = set()
        game._term_add = Mock()
        game.gain_xp = lambda amount: setattr(game, "xp", game.xp + amount)
        game._poll_sdr()
        self.assertEqual(game.xp, 1597156)
        self.assertEqual(game._sdr_aircraft_xp, {"ABC123"})
        with patch("watchdogs.sdr_manager.time.time", return_value=1001):
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
        with patch("watchdogs.sdr_manager.time.time", return_value=1001):
            self.assertFalse(self.manager.stop())
        self.assertTrue(self.manager.running)
        self.assertEqual(self.manager.mode, "adsb")
        self.assertIn("ABC123", self.manager.aircraft)
        self.assertFalse(self.manager._adsb_log.closed)
        self.assertEqual(self.draw(10000), (True, True))
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
