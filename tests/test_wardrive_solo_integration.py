"""Actual WDG arbiter + real SOLO controller; mocked UART, recorder and clocks."""
import ast
import copy
import logging
import random
import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_scanner_lifecycle as base
import test_game_shutdown as shutdown
from test_wardrive_solo import BOOT, SESSION, caps, frame
from watchdogs.wardrive_solo import WardriveSoloController
from watchdogs.wardrive_track import WardriveTrack
from watchdogs.wardrive_gps import WardriveGpsPolicy
from watchdogs.app_state import Network
from watchdogs.network_manager import NetworkManager


TREE = ast.parse(base.SOURCE.read_text(encoding="utf-8"))
APP = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "WatchDogsGame")
NAMES = set(base.METHODS) | {"_handle_serial_line", "_poll_serial", "_is_running", "_set_solo_selection",
    "_cancel_solo_pending", "_on_solo_observation", "_poll_solo", "_solo_notice",
    "_start_solo_backfill", "_finish_solo_recorder", "_apply_solo_observation",
    "_record_scan_map_observation", "_poll_gps"}
ENV = dict(base.ENV, Base=base.GameHarness, re=re, logging=logging, random=random, Network=Network)
for node in TREE.body:
    if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and
            target.id in ("_BLE_RE", "_AIRTAG_COUNT_RE") for target in node.targets):
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(base.SOURCE), "exec"), ENV)
    if isinstance(node, ast.ClassDef) and node.name in ("WifiNetwork", "BleDevice"):
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(base.SOURCE), "exec"), ENV)
UPDATE = next(node for node in APP.body if isinstance(node, ast.FunctionDef) and node.name == "update")
QUICK_STOP = next(node for node in ast.walk(UPDATE) if isinstance(node, ast.If)
    and "pyxel.KEY_S" in ast.unparse(node.test) and "self._attack_mode" in ast.unparse(node.test))
QUICK_METHOD = ast.FunctionDef(name="_press_quick_stop", args=ast.arguments(posonlyargs=[],
    args=[ast.arg(arg="self")], kwonlyargs=[], kw_defaults=[], defaults=[]),
    decorator_list=[], body=copy.deepcopy(QUICK_STOP.body))
HARNESS = ast.ClassDef(name="SoloGame", bases=[ast.Name(id="Base", ctx=ast.Load())],
    keywords=[], decorator_list=[], body=[copy.deepcopy(node) for node in APP.body
        if isinstance(node, ast.FunctionDef) and node.name in NAMES] + [QUICK_METHOD])
exec(compile(ast.fix_missing_locations(ast.Module(body=[HARNESS], type_ignores=[])),
             str(base.SOURCE), "exec"), ENV)
SoloGame = ENV["SoloGame"]

SHUTDOWN_ENV = dict(shutdown.ENV, Base=shutdown.GameHarness, C_WARNING=9)
SHUTDOWN_NAMES = {"_cleanup", "_finish_solo_recorder", "_poll_solo", "_solo_notice"}
SHUTDOWN_CLASS = ast.ClassDef(name="SoloShutdownGame", bases=[ast.Name(id="Base", ctx=ast.Load())],
    keywords=[], decorator_list=[], body=[copy.deepcopy(node) for node in APP.body
        if isinstance(node, ast.FunctionDef) and node.name in SHUTDOWN_NAMES])
exec(compile(ast.fix_missing_locations(ast.Module(body=[SHUTDOWN_CLASS], type_ignores=[])),
             str(base.SOURCE), "exec"), SHUTDOWN_ENV)
SoloShutdownGame = SHUTDOWN_ENV["SoloShutdownGame"]


def game():
    g = base.game()
    g.__class__ = SoloGame
    g._wardrive_selected = set()
    g._wardrive_phase = g._wardrive_last_phase = ""
    g._wardrive_phase_done = 0.0
    g._wardrive_paused = g._wardrive_save_failed = False
    g._wardrive_save_errors_at_start = None
    g._fw_version, g._esp32, g._serial_busy = "1.7.0", True, False
    g._shutting_down = False
    g._solo_profile = "kg"
    g._solo_backfill_waiting = False
    g._solo_backfill_needs_stop = False
    g._solo_backfill_resume = None
    g._whitelist = SimpleNamespace(is_blocked=Mock(return_value=False))
    g._solo_track = WardriveTrack()
    g._solo_recorder = SimpleNamespace(submit=Mock(return_value=True), tick=Mock(),
        poll_events=Mock(return_value=[]), close=Mock(return_value=True), poll_events_available=False,
        seal_startup=Mock(return_value=True))
    g.loot = SimpleNamespace(active=True, log_serial=Mock())
    g.net_mgr = SimpleNamespace(parse_network_line=Mock(return_value=None))
    g._et_scan_pending = False
    g.serial.read_available = Mock(return_value=[])
    g.serial.close = Mock()
    g._solo = WardriveSoloController(g._start_scan_cmd, lambda: g._request_esp_stop(solo=True),
        g._on_solo_observation, cancel_pending=g._cancel_solo_pending, notice=g._solo_notice,
        monotonic=lambda: base.CLOCK.now, utc_clock=lambda: base.CLOCK.now)
    return g


class WardriveSoloIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.g = game()

    def sent(self):
        return [call.args[0] for call in self.g.serial.send_command.call_args_list]

    def advance(self, seconds):
        base.CLOCK.now += seconds
        base.PX.frame_count += round(seconds * 30)

    def ack(self):
        self.g._handle_esp_operation_state("all operations stopped.")
        self.advance(0.5)
        self.g._dispatch_pending_esp_command()

    def emit(self, line):
        self.g._handle_serial_line(line)

    def running(self, *, state_ack=True):
        g = self.g
        self.assertTrue(g._set_solo_selection(wifi=True))
        if g._esp_stop_waiting:
            self.ack()
        self.assertEqual(self.sent()[-1], "wardrive_solo_caps")
        self.advance(0.1)
        with patch("watchdogs.wardrive_solo.secrets.token_hex", return_value=SESSION):
            self.emit(caps())
        self.assertFalse(g._is_running("solo_wifi"))
        if g._esp_stop_waiting:
            self.ack()
        self.assertEqual(self.sent()[-1], f"wardrive_solo {SESSION} kg 1 0")
        self.advance(0.1)
        self.emit(caps(emitted=1_400_000, clock_monotonic_us=1_400_000))
        self.advance(0.1)
        if state_ack:
            self.emit(frame("state", emitted=1_500_000, clock_monotonic_us=1_500_000,
                session=SESSION, profile="kg", wifi=True, ble=False, phase="wifi", cycles=0))
            self.assertTrue(g._is_running("solo_wifi"))
        return g

    def gps_polling(self, g, *, accepted_before=True):
        """Real GPS policy/track, with only receiver I/O and monotonic clock fake."""
        clock_patch = patch("watchdogs.wardrive_track.time.monotonic", lambda: base.CLOCK.now)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        sample = dict(lat=36.0, lon=-6.0, altitude=12.0, quality=1, sats=8,
            hdop=1.0, observed_at=base.CLOCK.now, utc_seconds=43200, sentence="GGA")
        g.gps = SimpleNamespace(available=True, read_available=Mock(return_value=[]),
            process_sentences=Mock(), wardrive_sample=Mock(return_value=sample),
            fix=SimpleNamespace(valid=True, latitude=36.0, longitude=-6.0,
                                satellites=8, satellites_visible=12))
        g._poll_radio_profiles = Mock()
        g.player_lat, g.player_lon = 36.0, -6.0
        g.gps_fix, g._manual_move = True, True
        policy = WardriveGpsPolicy(clock=lambda: base.CLOCK.now)
        g._solo_recorder.policy = policy
        if accepted_before:
            self.assertTrue(policy.feed_sample(sample))
            policy.complete_startup()
        return sample, policy

    def test_actual_gps_poll_moves_short_solo_samples_without_waiting_for_scan_completion(self):
        g = self.running()
        sample, _ = self.gps_polling(g)
        before = list(self.sent())
        g._poll_gps()
        for delta in (0.00002, 0.00009):  # About 2 and 10 metres; below the old filter.
            self.advance(0.5)
            sample = dict(sample, lat=36.0 + delta, observed_at=base.CLOCK.now)
            g.gps.wardrive_sample.return_value = sample
            g.gps.fix.latitude = sample["lat"]
            g._poll_gps()
            self.assertEqual((g.player_lat, g.player_lon), (sample["lat"], sample["lon"]))
            self.assertTrue(g.gps_fix)
        self.assertEqual(g._solo_track.point_count, 3)
        self.assertEqual([point[0] for point in g._solo_track.iter_points()],
                         [36.0, 36.00002, 36.00009])
        self.assertEqual(self.sent(), before)
        g._solo_recorder.submit.assert_not_called()
        g._solo_recorder.seal_startup.assert_not_called()
        self.assertFalse(g._manual_move)
        self.assertEqual(g._poll_radio_profiles.call_count, 3)

    def test_actual_gps_poll_loss_does_not_move_solo_player_using_legacy_fix_or_cache(self):
        g = self.running()
        sample, policy = self.gps_polling(g)
        g._poll_gps()
        self.advance(3)
        g.gps.wardrive_sample.return_value = None
        g.gps.fix.latitude, g.gps.fix.longitude = 37.0, -5.0
        g._poll_gps()
        self.assertEqual(policy.position_for()["source"], "cache")
        self.assertEqual((g.player_lat, g.player_lon), (36.0, -6.0))
        self.assertFalse(g.gps_fix)
        self.assertEqual(g._solo_track.point_count, 1)
        self.advance(0.5)
        g.gps.wardrive_sample.return_value = dict(sample, lat=36.00005,
                                                observed_at=base.CLOCK.now)
        g._poll_gps()
        self.assertEqual(g._solo_track.point_count, 2)
        self.assertEqual(len({point[2] for point in g._solo_track.iter_points()}), 2)
        self.assertTrue(g.gps_fix)

    def test_actual_gps_poll_solo_off_preserves_legacy_short_motion_filter(self):
        g = self.g
        sample, _ = self.gps_polling(g)
        g.gps.fix.latitude = 36.00002
        g.gps.wardrive_sample.return_value = dict(sample, lat=36.00002)
        g._poll_gps()
        self.assertEqual((g.player_lat, g.player_lon), (36.0, -6.0))
        self.assertTrue(g.gps_fix)
        self.assertEqual(g._solo_track.point_count, 0)
        self.assertEqual(self.sent(), [])

    def test_actual_gps_poll_freezes_first_quality_fix_once_before_stop_and_seal(self):
        g = self.running()
        sample, policy = self.gps_polling(g, accepted_before=False)
        before = list(self.sent())
        g.gps.wardrive_sample.return_value = dict(sample, sats=2)
        g._poll_gps()
        self.assertIsNone(policy.first_fix_snapshot)
        self.assertEqual(self.sent(), before)
        self.advance(0.5)
        first = dict(sample, lat=36.00005, observed_at=base.CLOCK.now)
        g.gps.wardrive_sample.return_value = first
        g._poll_gps()
        frozen = policy.first_fix_snapshot
        self.assertEqual(frozen["lat"], first["lat"])
        self.assertEqual(self.sent(), before + ["stop"])
        self.assertTrue(g._solo_backfill_waiting)
        g._solo_recorder.seal_startup.assert_not_called()
        self.advance(0.5)
        g.gps.wardrive_sample.return_value = dict(first, lat=36.002,
                                                observed_at=base.CLOCK.now)
        g._poll_gps()
        self.assertEqual(policy.first_fix_snapshot, frozen)
        self.assertEqual(self.sent(), before + ["stop"])
        self.emit(frame("stopped", emitted=1_800_000, session=SESSION, last_seq=0, dropped=0))
        g._poll_solo()
        g._solo_recorder.seal_startup.assert_not_called()
        self.ack()
        g._poll_solo()
        g._solo_recorder.seal_startup.assert_called_once_with()
        self.assertEqual(policy.first_fix_snapshot, frozen)

    def test_sniff_to_solo_caps_cannot_cross_stop_without_universal_ack(self):
        g = self.g
        g._wardrive_selected.add("wardriving")
        g.wifi_scanning = True
        self.assertTrue(g._set_solo_selection(wifi=True))
        self.assertEqual(self.sent(), ["stop"])
        self.assertFalse(g._wardrive_selected)
        self.assertFalse(g._solo.confirmed_wifi)
        self.advance(2)
        g._solo.tick()
        g._dispatch_pending_esp_command()
        self.assertEqual(self.sent(), ["stop"])
        g._handle_esp_operation_state("stop command received - stopping all operations...")
        g._dispatch_pending_esp_command()
        self.assertEqual(self.sent(), ["stop"])
        self.ack()
        self.assertEqual(self.sent(), ["stop", "wardrive_solo_caps"])
        self.assertEqual(g._solo.state, "await_caps")

    def test_caps_and_start_ack_timers_begin_at_actual_uart_write(self):
        g = self.g
        g.wifi_scanning = True
        g._set_solo_selection(wifi=True)
        self.advance(4)
        g._solo.tick()
        self.assertTrue(g._solo.desired_wifi)
        self.ack()
        self.advance(4)
        g._solo.tick()
        self.assertTrue(g._solo.desired_wifi)
        self.assertFalse(g._solo.confirmed_wifi)
        self.emit(caps())
        self.advance(4)
        g._solo.tick()
        self.assertTrue(g._solo.desired_wifi)

    def test_early_full_wifi_and_ble_replies_survive_post_flush_send_delay(self):
        # SerialManager waits 100 ms after write/flush; replies can already be
        # queued then. Exercise the real app TX path and controller together.
        for wifi, ble, protocol in ((True, False, "wifi"), (False, True, "ble")):
            with self.subTest(protocol=protocol):
                self.g = game()
                g = self.g
                g._solo_profile = "full"
                transmitted_at = {}
                sent = Mock(wraps=g._solo.command_sent)
                g._solo.command_sent = sent

                def delayed_send(command):
                    transmitted_at[command] = base.CLOCK.now
                    self.advance(0.1)
                    return True

                def source_us(host_time):
                    return round((host_time - 99.0) * 1_000_000)

                g.serial.send_command.side_effect = delayed_send
                self.assertTrue(g._set_solo_selection(wifi=wifi, ble=ble))
                if g._esp_stop_waiting:
                    self.ack()
                probe_at = transmitted_at["wardrive_solo_caps"]
                probe_clock = source_us(probe_at + 0.005)
                with patch("watchdogs.wardrive_solo.secrets.token_hex", return_value=SESSION):
                    self.emit(caps(emitted=probe_clock, clock_monotonic_us=probe_clock))
                if g._esp_stop_waiting:
                    self.ack()
                command = f"wardrive_solo {SESSION} full {int(wifi)} {int(ble)}"
                start_at = transmitted_at[command]
                # The device generated all three frames before send_command
                # returned, but the main loop consumes them afterwards.
                actual_caps = source_us(start_at + 0.005)
                self.emit(caps(emitted=actual_caps, clock_monotonic_us=actual_caps))
                state_clock = source_us(start_at + 0.010)
                self.emit(frame("state", emitted=state_clock,
                    clock_monotonic_us=state_clock, session=SESSION, profile="full",
                    wifi=wifi, ble=ble, phase=protocol, cycles=0))
                observation = dict(session=SESSION, seq=1, protocol=protocol,
                    capture_monotonic_us=source_us(start_at + 0.025),
                    mac="AA:BB:CC:DD:EE:FF", rssi=-70)
                observation.update(dict(ssid="synthetic", auth="WPA2_PSK", channel=6)
                    if wifi else dict(name="synthetic"))
                self.emit(frame("observation", emitted=source_us(start_at + 0.026),
                    **observation))

                self.assertEqual((g._solo.confirmed_wifi, g._solo.confirmed_ble), (wifi, ble))
                self.assertEqual(g._solo.invalid_events, 0)
                g._solo_recorder.submit.assert_called_once()
                accepted = g._solo_recorder.submit.call_args.args[0]
                self.assertEqual(accepted["protocol"], protocol)
                self.assertLessEqual(start_at, accepted["observed_at"])
                self.assertLessEqual(accepted["observed_at"], base.CLOCK.now)
                self.assertAlmostEqual(accepted["clock_uncertainty_s"], 0.05)
                for call in sent.call_args_list:
                    before_send = transmitted_at[call.args[0]]
                    self.assertAlmostEqual(call.kwargs["monotonic"], before_send)
                    self.assertAlmostEqual(call.kwargs["utc"], before_send)

    def test_delayed_failed_solo_send_does_not_anchor_a_transmission(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                self.g = game()
                g = self.g
                g._solo.command_sent = Mock()

                def failed_send(_command):
                    self.advance(0.1)
                    if raises:
                        raise OSError("synthetic write failure")
                    return False

                g.serial.send_command.side_effect = failed_send
                self.assertFalse(g._begin_esp_scan("wardrive_solo_caps",
                    "solo_control", "Check SOLO firmware"))
                g._solo.command_sent.assert_not_called()
                self.assertFalse(g._solo.confirmed_wifi)
                self.assertFalse(g._solo.confirmed_ble)

    def test_solo_to_ordinary_scan_waits_for_universal_stop(self):
        g = self.running()
        before = list(self.sent())
        self.assertTrue(g._start_scan_cmd("scan_bt", "ble_scan", "BLE scan"))
        self.assertEqual(self.sent(), before + ["stop"])
        self.assertTrue(g._solo.confirmed_wifi)
        self.assertFalse(g._solo.desired_wifi)
        self.advance(1)
        g._dispatch_pending_esp_command()
        self.assertNotIn("scan_bt", self.sent())
        self.ack()
        self.assertEqual(self.sent()[-1], "scan_bt")
        self.assertFalse(g._solo.confirmed_wifi)
        self.assertTrue(g._ble_scan_only)

    def test_stop_timeout_never_sends_competing_caps_or_scan(self):
        g = self.g
        g.wifi_scanning = True
        g._set_solo_selection(wifi=True)
        self.advance(5)
        g._dispatch_pending_esp_command()
        self.assertEqual(self.sent(), ["stop"])
        self.assertFalse(g._solo.desired_wifi)
        self.assertTrue(g._esp_stop_timed_out)
        self.assertFalse(g._start_scan_cmd("scan_bt", "ble_scan", "BLE scan"))
        self.assertEqual(self.sent(), ["stop"])

    def test_failed_idle_caps_restore_blocks_other_scan_until_universal_ack(self):
        for timed_out in (False, True):
            with self.subTest(timed_out=timed_out):
                self.g = game()
                g = self.g
                self.assertTrue(g._set_solo_selection(wifi=True))
                if g._esp_stop_waiting:
                    self.ack()
                before = list(self.sent())
                self.assertEqual(before[-1], "wardrive_solo_caps")
                self.emit(frame("error", code="restore_failed",
                    message="Cannot verify and restore radio channels"))
                self.assertEqual(self.sent(), before + ["stop"])
                self.assertTrue(g._solo.busy)
                self.assertFalse(g._solo.confirmed_wifi)
                if timed_out:
                    self.advance(5)
                    g._solo.tick()
                    g._dispatch_pending_esp_command()
                    self.assertTrue(g._solo.busy)
                    self.assertTrue(g._esp_stop_timed_out)
                    self.assertFalse(g._start_scan_cmd("scan_bt", "ble_scan", "BLE scan"))
                    self.assertFalse(g._set_solo_selection(wifi=True))
                    self.assertTrue(g._solo.busy)
                    self.assertEqual(self.sent(), before + ["stop"])
                    self.assertTrue(g._request_esp_stop(retry=True))
                    self.ack()
                    self.assertFalse(g._solo.busy)
                    self.assertTrue(g._start_scan_cmd("scan_bt", "ble_scan", "BLE scan"))
                else:
                    self.assertTrue(g._start_scan_cmd("scan_bt", "ble_scan", "BLE scan"))
                    self.assertEqual(self.sent(), before + ["stop"])
                    self.advance(1)
                    g._dispatch_pending_esp_command()
                    self.assertNotIn("scan_bt", self.sent())
                    self.ack()
                self.assertEqual(self.sent()[-1], "scan_bt")
                self.assertFalse(g._solo.busy)
                self.assertTrue(g._ble_scan_only)
    def test_explicit_system_and_quick_stop_retry_without_auto_retries_or_queued_restart(self):
        for quick in (False, True):
            with self.subTest(quick=quick):
                self.g = game()
                g = self.g
                g.wifi_scanning = True
                g._set_solo_selection(wifi=True)
                self.advance(5)
                g._dispatch_pending_esp_command()
                self.assertTrue(g._esp_stop_timed_out)
                g._request_esp_stop()  # An automatic request never retries.
                g._solo.tick()
                self.assertEqual(self.sent(), ["stop"])
                if quick:
                    g._press_quick_stop()
                else:
                    g._execute_item("stop", "_stop_all", "STOP ALL", [])
                self.assertEqual(self.sent(), ["stop", "stop"])
                self.assertFalse(g._esp_stop_timed_out)
                self.assertTrue(g._esp_stop_waiting)
                self.assertIsNone(g._pending_cmd)
                self.assertFalse(g._solo.desired_wifi)
                self.ack()
                self.assertEqual(self.sent(), ["stop", "stop"])

    def test_first_fix_for_off_solo_queue_never_stops_running_sniff_even_when_seal_queue_was_full(self):
        g = self.running()
        g._solo.cancel()
        self.ack()
        g._start_scan_cmd("scan_networks", "wardriving", "WiFi Sniff")
        g._wardrive_selected = {"wardriving"}
        g._wardrive_phase = "wardriving"
        self.assertTrue(g.wifi_scanning)
        before = list(self.sent())
        g._solo_recorder.seal_startup.side_effect = [False, True, True]
        g._start_solo_backfill()
        self.assertFalse(g._solo_backfill_needs_stop)
        self.assertTrue(g._solo_backfill_waiting)
        self.assertEqual(self.sent(), before)
        g._poll_solo()
        self.assertEqual(g._solo_recorder.seal_startup.call_count, 2)
        self.assertTrue(g.wifi_scanning)
        self.assertEqual(g._wardrive_selected, {"wardriving"})
        self.assertEqual(self.sent(), before)

    def test_active_solo_backfill_requires_universal_stop_ack_and_completion_before_resume(self):
        g = self.running()
        before = list(self.sent())
        g._start_solo_backfill()
        self.assertTrue(g._solo_backfill_needs_stop)
        self.assertEqual(self.sent(), before + ["stop"])
        g._poll_solo()
        g._solo_recorder.seal_startup.assert_not_called()
        self.emit(frame("stopped", emitted=1_800_000, session=SESSION, last_seq=0, dropped=0))
        self.assertFalse(g._solo.busy)
        g._poll_solo()
        g._solo_recorder.seal_startup.assert_not_called()
        self.ack()
        self.assertTrue(g._solo_recorder.seal_startup.called)
        self.assertTrue(g._solo_backfill_waiting)
        self.assertEqual(self.sent(), before + ["stop"])
        g._poll_solo()
        self.assertEqual(self.sent(), before + ["stop"])
        g._solo_recorder.poll_events.return_value = [{"event": "backfill_done"}]
        g._poll_solo()
        g._solo_recorder.poll_events.return_value = []
        self.assertFalse(g._solo_backfill_waiting)
        if g._esp_stop_waiting:
            self.ack()
        else:
            g._dispatch_pending_esp_command()
        self.assertEqual(self.sent()[-1], "wardrive_solo_caps")
        self.assertFalse(g._solo.confirmed_wifi)

    def test_start_ack_after_cancellation_cannot_turn_solo_on(self):
        g = self.running(state_ack=False)
        self.assertTrue(g._solo.busy)
        g._start_scan_cmd("scan_bt", "ble_scan", "BLE scan")
        self.emit(frame("state", emitted=1_500_000, clock_monotonic_us=1_500_000,
            session=SESSION, profile="kg", wifi=True, ble=False, phase="wifi", cycles=0))
        self.assertFalse(g._solo.confirmed_wifi)
        self.assertNotIn("scan_bt", self.sent())
        self.ack()
        self.assertEqual(self.sent()[-1], "scan_bt")

    def test_uart_disconnect_clears_confirmed_modes_without_scan_restart(self):
        g = self.running()
        serial = g.serial
        count = serial.send_command.call_count
        serial.read_available.side_effect = OSError("synthetic unplug")
        g._poll_serial()
        self.assertIsNone(g.serial)
        self.assertFalse(g._solo.confirmed_wifi)
        self.assertFalse(g._solo.desired_wifi)
        self.assertFalse(g._solo.busy)
        self.advance(1)
        g._solo.tick()
        self.assertEqual(serial.send_command.call_count, count)

    def test_solo_records_go_once_to_recorder_and_never_to_legacy_scheduler(self):
        g = self.running()
        g._toggle_wardrive = Mock(side_effect=AssertionError("Legacy SNIFF controller"))
        g._wardrive_tick = Mock(side_effect=AssertionError("Legacy SNIFF cycle"))
        event = frame("observation", emitted=1_700_000, session=SESSION, seq=1,
            capture_monotonic_us=1_600_000, protocol="wifi", mac="AA:BB:CC:DD:EE:FF",
            ssid="synthetic", auth="WPA2_PSK", channel=6, rssi=-70)
        self.advance(0.2)
        self.emit(event)
        self.emit(event)
        g._solo_recorder.submit.assert_called_once()
        record = g._solo_recorder.submit.call_args.args[0]
        self.assertEqual(record["identity"], (BOOT, SESSION, 1))
        self.assertEqual(record["capture_monotonic_us"], 1_600_000)
        g._toggle_wardrive.assert_not_called()
        g._wardrive_tick.assert_not_called()
        g.net_mgr.parse_network_line.assert_not_called()
        self.assertTrue(g.loot.log_serial.called)

    def test_unassigned_solo_observations_do_not_borrow_the_current_global_fix(self):
        g = self.g
        g._known_wifi, g._known_ble = set(), set()
        g.wifi_networks, g.ble_devices, g.state.networks = [], [], []
        g.gain_xp = Mock()
        # A newer global fix is deliberately available: it must NOT replace
        # the recorder's explicit unavailable position for an earlier capture.
        g.gps = SimpleNamespace(available=True, fix=SimpleNamespace(valid=True, latitude=51, longitude=0.2))
        g._session_scan_points = {}
        mac = "AA:BB:CC:DD:EE:FF"
        for protocol, expected_xp in (("wifi", 15), ("ble", 10)):
            observation = dict(protocol=protocol, mac=mac, ssid="synthetic", name="synthetic",
                               auth="WPA2_PSK", channel=6, rssi=-70, observed_at=100.0)
            g._apply_solo_observation(observation, None)
            self.assertEqual(g._session_scan_points, {})
            self.assertEqual(g.gain_xp.call_args.args, (expected_xp,))
        self.assertEqual(len(g.wifi_networks), 1)
        self.assertEqual(len(g.ble_devices), 1)

    def test_backfill_adds_assigned_map_point_without_second_xp_award(self):
        g = self.g
        g._known_wifi, g._known_ble = set(), set()
        g.wifi_networks, g.ble_devices, g.state.networks = [], [], []
        g.gain_xp = Mock()
        g._session_scan_points = {}
        observation = dict(protocol="wifi", mac="AA:BB:CC:DD:EE:FF", ssid="synthetic",
                           auth="WPA2_PSK", channel=6, rssi=-70, observed_at=100.0)
        g._apply_solo_observation(observation, None)
        self.assertEqual(g._session_scan_points, {})
        g._apply_solo_observation(observation,
            dict(lat=36.46, lon=-6.2, source="startup_backfill"), award_xp=False)
        point = g._session_scan_points[("wifi", observation["mac"])]
        self.assertEqual((point["lat"], point["lon"], point["position_source"]),
                         (36.46, -6.2, "startup_backfill"))
        g.gain_xp.assert_called_once_with(15)
        self.assertEqual(len(g.wifi_networks), 1)

    def test_mac_case_normalization_reuses_existing_wifi_and_ble_objects(self):
        g = self.g
        g._known_wifi, g._known_ble = set(), set()
        g.wifi_networks, g.ble_devices, g.state.networks = [], [], []
        g.gain_xp = Mock()
        g._session_scan_points = {}
        for protocol, expected_xp in (("wifi", 15), ("ble", 10)):
            observation = dict(protocol=protocol, mac="AA:BB:CC:DD:EE:FF", ssid="synthetic",
                name="synthetic", auth="WPA2_PSK", channel=6, rssi=-70, observed_at=100.0)
            g._apply_solo_observation(observation, None)
            self.assertEqual(g.gain_xp.call_args.args, (expected_xp,))
            repeat = dict(observation, mac=observation["mac"].lower(), ssid="fresh-repeat",
                name="fresh-repeat", auth="WPA3_PSK", channel=11, rssi=-40, observed_at=101.0)
            g._apply_solo_observation(repeat, None)
            self.assertEqual(g.gain_xp.call_args.args, (1,))
        self.assertEqual(len(g.wifi_networks), 1)
        self.assertEqual(len(g.ble_devices), 1)
        self.assertEqual([call.args[0] for call in g.gain_xp.call_args_list], [15, 1, 10, 1])
        self.assertEqual(len(g.state.networks), 1)
        target = g.state.networks[0]
        self.assertEqual((target.bssid, target.ssid, target.channel, target.auth, target.rssi),
                         ("AA:BB:CC:DD:EE:FF", "fresh-repeat", "11", "WPA3_PSK", "-40"))
        self.assertEqual((g.wifi_networks[0].ssid, g.wifi_networks[0].rssi), ("fresh-repeat", -40))
        self.assertEqual((g.ble_devices[0].name, g.ble_devices[0].rssi), ("fresh-repeat", -40))
        self.assertEqual(g._known_wifi, {"AA:BB:CC:DD:EE:FF"})
        self.assertEqual(g._known_ble, {"AA:BB:CC:DD:EE:FF"})

    def test_older_solo_backfill_cannot_roll_back_newer_sniff_position_or_live_metadata(self):
        for protocol in ("wifi", "ble"):
            with self.subTest(protocol=protocol):
                self.g = game()
                g = self.g
                g._known_wifi, g._known_ble = set(), set()
                g.wifi_networks, g.ble_devices, g.state.networks = [], [], []
                g.gain_xp = Mock()
                g._session_scan_points = {}
                mac = "AA:BB:CC:DD:EE:FF"
                old = dict(protocol=protocol, mac=mac, ssid="old-solo", name="old-solo",
                    auth="WPA2_PSK", channel=6, rssi=-70, observed_at=100.0)
                g._apply_solo_observation(old, None)
                self.advance(100)
                g.gps = SimpleNamespace(available=True,
                    fix=SimpleNamespace(valid=True, latitude=51, longitude=0.2))
                g.net_mgr = NetworkManager(g.state)
                g.loot.save_wardriving_network = Mock(return_value=True)
                g.loot.save_bt_device = Mock(return_value=True)
                g.loot.save_wardriving_bt = Mock(return_value=True)
                if protocol == "wifi":
                    self.emit('"1","fresh-sniff","vendor","AA:BB:CC:DD:EE:FF","11","WPA3_PSK","-40","2.4GHz"')
                else:
                    self.emit("1. AA:BB:CC:DD:EE:FF RSSI: -40 dBm Name: fresh-sniff")
                key = ("wifi" if protocol == "wifi" else "bt", mac)
                before = dict(g._session_scan_points[key])
                count = g.gain_xp.call_count
                g._apply_solo_observation(old, dict(lat=36.46, lon=-6.2, source="startup_backfill"), award_xp=False)
                self.assertEqual(g._session_scan_points[key], before)
                self.assertEqual(g.gain_xp.call_count, count)
                target = (g.wifi_networks if protocol == "wifi" else g.ble_devices)[0]
                self.assertEqual((target.lat, target.lon, target.rssi), (51, 0.2, -40))
                self.assertEqual(target.ssid if protocol == "wifi" else target.name, "fresh-sniff")

    def test_cleanup_commits_and_applies_final_recorder_events_before_xp_and_loot_close(self):
        g = shutdown.game()
        g.__class__ = SoloShutdownGame
        g._solo = SimpleNamespace(cancel=Mock(), stop_acknowledged=Mock(), tick=Mock(), busy=False)
        g._solo_backfill_resume = None
        g._solo_backfill_waiting = False
        class Recorder:
            committed = False
            pending = True
            def close(self, timeout):
                g.events.append("solo-recorder-close")
                self.committed = True
                return True
            @property
            def poll_events_available(self):
                return self.pending
            def tick(self):
                pass
            def poll_events(self, limit):
                if not self.pending:
                    return []
                self.pending = False
                return [dict(event="saved", observation={"identity": "synthetic-final"}, position=None)]
        g._solo_recorder = recorder = Recorder()
        def apply(*_args, **_kwargs):
            self.assertTrue(recorder.committed)
            g.events.append("solo-ui-apply")
        g._apply_solo_observation = Mock(side_effect=apply)
        g._cleanup()
        self.assertLess(g.events.index("serial-drain"), g.events.index("solo-recorder-close"))
        self.assertLess(g.events.index("solo-recorder-close"), g.events.index("solo-ui-apply"))
        self.assertLess(g.events.index("solo-ui-apply"), g.events.index("xp-save"))
        self.assertLess(g.events.index("xp-save"), g.events.index("loot-close"))
        self.assertEqual(g.events.count("solo-recorder-close"), 1)


if __name__ == "__main__":
    unittest.main()
