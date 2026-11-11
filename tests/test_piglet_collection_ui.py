"""Real game import/presentation methods; fake Pyxel, no network or radio."""
import ast
import copy
from datetime import datetime, timezone
import logging
from pathlib import Path
import random
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from watchdogs.app_state import AppState, Network


SOURCE = Path(__file__).resolve().parents[1] / "watchdogs" / "app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
APP = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "WatchDogsGame")
NAMES = {"_apply_piglet_observation", "_apply_piglet_events", "_poll_piglet_collection",
         "_update_piglet_collection_screen", "_draw_piglet_collection_screen",
         "_piglet_import_summary",
         "_piglet_has_unfinished_work", "_cancel_piglet_collection",
         "_yield_piglet_threads",
         "_apply_solo_observation", "_record_scan_map_observation", "_game_update_notice_allowed",
         "_is_running", "_handle_python_attack"}
PX = SimpleNamespace(frame_count=42, btnp=lambda _key: False,
                     **{name: name for name in ("KEY_ESCAPE", "KEY_P", "KEY_S", "KEY_RETURN", "KEY_LEFT", "KEY_RIGHT", "KEY_Y", "KEY_N")},
                     **{name: Mock() for name in ("rect", "rectb", "text", "line")})
ENV = dict(time=time, random=random, logging=logging, pyxel=PX, Network=Network)
for node in TREE.body:
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        try:
            ENV[node.targets[0].id] = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            pass
    if isinstance(node, ast.ClassDef) and node.name in ("WifiNetwork", "BleDevice"):
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), ENV)
harness = ast.ClassDef(name="ImportGame", bases=[], keywords=[], decorator_list=[],
                      body=[copy.deepcopy(node) for node in APP.body
                            if isinstance(node, ast.FunctionDef) and node.name in NAMES])
exec(compile(ast.fix_missing_locations(ast.Module(body=[harness], type_ignores=[])), str(SOURCE), "exec"), ENV)
Game = ENV["ImportGame"]


def record(protocol="wifi", position=None, stamp=None, mac="02:00:00:00:00:01"):
    return dict(protocol=protocol, mac=mac, ssid="Source WiFi", name="Source BLE", rssi=-70,
                channel=6, auth="WPA2", captured_at=stamp or datetime.now(timezone.utc).isoformat(),
                source_time="2026-11-08 12:34:56", position=position, record_id="a" * 64)


def game():
    value = Game()
    value._known_wifi, value._known_ble = set(), set()
    value._session_scan_points = {}
    value.wifi_networks, value.ble_devices = [], []
    value.state = AppState()
    value.gps = SimpleNamespace(available=True, fix=SimpleNamespace(valid=True, latitude=51, longitude=17))
    value.gain_xp, value._term_add = Mock(), Mock()
    value._piglet_poll_at, value._piglet_error = 0, ""
    value.xp, value._xp_dirty = 135, True
    store = SimpleNamespace(process_pending=Mock(return_value={"committed": False, "xp_total": 135}),
                            pending_events=Mock(return_value=[]), acknowledge_events=Mock(return_value=True))
    value._piglet_collection = SimpleNamespace(store=store,
        snapshot=Mock(return_value={"state": "active", "message": "Testing", "progress": {}}),
        stop=Mock(return_value=True), cancel=Mock(return_value=True), cancel_requested=False,
        start=Mock(return_value=True), settings=dict(ssid="Test", password="test-only", token="0" * 64),
        backend=SimpleNamespace(address="10.72.0.1"))
    value._piglet_collection_screen = True
    value._piglet_pairing_visible = False
    value._ensure_piglet_collection = Mock(return_value=True)
    value._plugins = []
    return value


class ImportPresentationTests(unittest.TestCase):
    def setUp(self):
        self.g = game()
        PX.btnp = lambda _key: False

    def test_missing_source_gps_never_falls_back_to_collector(self):
        g = self.g
        g._apply_piglet_observation(record(), new_identity=True)
        self.assertFalse(g._session_scan_points)
        self.assertFalse(g.wifi_networks)
        g._term_add.assert_not_called()
        g.gain_xp.assert_not_called()
        self.assertIn("02:00:00:00:00:01", g._known_wifi)

    def test_positioned_wifi_and_ble_stay_in_source_without_map_or_terminal_replay(self):
        for protocol in ("wifi", "ble"):
            g = game()
            item = record(protocol, dict(lat=40.5, lon=-3.5, source="piglet"))
            original = copy.deepcopy(item)
            g._apply_piglet_observation(item, new_identity=True)
            self.assertFalse(g._session_scan_points)
            self.assertFalse(g.wifi_networks or g.ble_devices or g.state.networks)
            g._term_add.assert_not_called()
            self.assertEqual(item, original)
            g.gain_xp.assert_not_called()

    def test_old_import_does_not_replace_current_live_position_or_labels(self):
        g = self.g
        live = record(position=dict(lat=51, lon=17))
        live.update(observed_at=time.monotonic(), ssid="Live", rssi=-30)
        g._apply_solo_observation(live, live["position"])
        g.gain_xp.reset_mock()
        g._apply_piglet_observation(record(position=dict(lat=40, lon=-3), stamp="2020-01-01T00:00:00Z"))
        point = g._session_scan_points[("wifi", live["mac"])]
        self.assertEqual((point["lat"], point["lon"], point["label"]), (51, 17, "Live"))
        self.assertEqual(g.wifi_networks[0].ssid, "Live")
        self.assertEqual(g.state.networks[0].ssid, "Live")
        g.gain_xp.assert_not_called()

    def test_base_reward_is_not_repeated_by_outbox_replay(self):
        event = dict(record=record(), new_identity=True, event_id="a" * 64)
        for _ in range(2):
            self.assertEqual(self.g._apply_piglet_events([event]), [event["event_id"]])
        self.assertEqual(len(self.g.wifi_networks), 0)
        self.assertEqual(len(self.g._known_wifi), 1)
        self.g.gain_xp.assert_not_called()

    def test_unknown_source_date_keeps_source_position_without_inventing_time(self):
        g = self.g
        item = record(position=dict(lat=40.5, lon=-3.5, source="piglet"))
        item.update(captured_at=None, capture_time_known=False, source_time="2000-00-00 00:00:00")
        original = copy.deepcopy(item)
        g._apply_piglet_observation(item, new_identity=True)
        self.assertFalse(g._session_scan_points)
        self.assertFalse(g.wifi_networks)
        self.assertEqual(item, original)
        g.gain_xp.assert_not_called()

    def test_unknown_source_date_cannot_replace_dated_or_live_position(self):
        for live in (False, True):
            g = game()
            dated = record(position=dict(lat=51, lon=17), stamp="2020-01-01T00:00:00Z")
            dated.update(ssid="Dated", rssi=-30)
            if live:
                dated["observed_at"] = time.monotonic()
                g._apply_solo_observation(dated, dated["position"])
            else:
                g._apply_piglet_observation(dated)
            g.gain_xp.reset_mock()
            unknown = record(position=dict(lat=40, lon=-3))
            unknown.update(captured_at=None, capture_time_known=False, source_time="2000-00-00 21:34:32")
            g._apply_piglet_observation(unknown)
            if live:
                point = g._session_scan_points[("wifi", dated["mac"])]
                self.assertEqual((point["lat"], point["lon"], point["label"]), (51, 17, "Dated"))
            else:
                self.assertFalse(g._session_scan_points or g.wifi_networks)
            g.gain_xp.assert_not_called()

    def test_hud_unique_counts_do_not_add_known_import_map_objects_to_saved_rows(self):
        import test_mesh_hud as hud
        g = self.g
        for _ in range(2):
            g._apply_piglet_observation(record(), new_identity=False)
            g._apply_piglet_observation(record(protocol="ble"), new_identity=False)
        view = hud.hud_game()
        view._known_wifi, view._known_ble = g._known_wifi, g._known_ble
        view.wifi_networks, view.ble_devices = g.wifi_networks, g.ble_devices
        view._loot_totals.update(wifi=123, bt=456)
        labels = hud.labels(hud.draw(view))
        self.assertIn("WiFi:1", labels)
        self.assertIn("BLE:1", labels)
        self.assertEqual((len(g.wifi_networks), len(g.ble_devices)), (0, 0))
        g.gain_xp.assert_not_called()

    def test_presentation_batch_is_bounded(self):
        g = self.g
        g._apply_piglet_observation = Mock()
        events = [dict(record=record(), new_identity=True, event_id=str(i)) for i in range(1000)]
        self.assertEqual(len(g._apply_piglet_events(events)), 20)
        self.assertEqual(g._apply_piglet_observation.call_count, 20)

    def test_noop_does_not_clear_unsaved_live_xp(self):
        self.g._poll_piglet_collection()
        self.assertEqual(self.g.xp, 135)
        self.assertTrue(self.g._xp_dirty)

    def test_committed_batch_updates_xp_once_and_presents_without_reward(self):
        g = self.g
        store = g._piglet_collection.store
        store.process_pending.return_value = dict(committed=True, xp_total=150)
        store.pending_events.return_value = [dict(record=record(), new_identity=True, event_id="a" * 64)]
        g._poll_piglet_collection()
        self.assertEqual(g.xp, 150)
        self.assertFalse(g._xp_dirty)
        g.gain_xp.assert_not_called()
        store.acknowledge_events.assert_called_once_with(["a" * 64])
        g._poll_piglet_collection()
        self.assertEqual(store.process_pending.call_count, 1)

    def test_failed_ui_does_not_ack_or_undo_committed_xp(self):
        g = self.g
        store = g._piglet_collection.store
        store.process_pending.return_value = dict(committed=True, xp_total=150)
        store.pending_events.return_value = [dict(record=record(), new_identity=True, event_id="a" * 64)]
        g._apply_piglet_events = Mock(side_effect=OSError("synthetic UI failure"))
        with self.assertLogs(level="ERROR"):
            g._poll_piglet_collection()
        store.acknowledge_events.assert_not_called()
        self.assertEqual(g.xp, 150)
        self.assertTrue(g._piglet_error)

    def test_publication_failure_shows_error_without_losing_durable_file_xp(self):
        g = self.g
        g._piglet_collection.store.process_pending.return_value = dict(
            committed=True, xp_total=150, error="Import completion pending; committed XP retained")
        g._poll_piglet_collection()
        self.assertEqual(g.xp, 150)
        self.assertFalse(g._xp_dirty)
        self.assertEqual(g._piglet_error, "Import completion pending; committed XP retained")

    def test_escape_active_opens_confirmation_with_safe_default_no(self):
        g = self.g
        g._piglet_pairing_visible = True
        PX.btnp = lambda key: key == PX.KEY_ESCAPE
        g._update_piglet_collection_screen()
        self.assertTrue(g._piglet_collection_screen)
        self.assertTrue(g._piglet_cancel_confirm)
        self.assertFalse(g._piglet_cancel_yes)
        self.assertEqual(g._esc_consumed_frame, PX.frame_count)
        g._piglet_collection.cancel.assert_not_called()
        PX.btnp = lambda key: key == PX.KEY_RETURN
        g._update_piglet_collection_screen()
        self.assertFalse(g._piglet_cancel_confirm)
        g._piglet_collection.cancel.assert_not_called()

    def test_escape_idle_closes_without_cancel(self):
        g = self.g
        g._piglet_collection.snapshot.return_value = dict(state="stopped", progress={})
        PX.btnp = lambda key: key == PX.KEY_ESCAPE
        g._update_piglet_collection_screen()
        self.assertFalse(g._piglet_collection_screen)
        g._piglet_collection.cancel.assert_not_called()

    def test_confirm_yes_waits_for_cleanup_before_leaving(self):
        g = self.g
        g._piglet_cancel_confirm, g._piglet_cancel_yes = True, False
        PX.btnp = lambda key: key == PX.KEY_Y
        g._update_piglet_collection_screen()
        g._piglet_collection.cancel.assert_called_once()
        self.assertTrue(g._piglet_collection_screen)
        self.assertTrue(g._piglet_leave_after_cancel)
        g._piglet_status = dict(state="stopping")
        g._update_piglet_collection_screen()
        self.assertTrue(g._piglet_collection_screen)
        g._piglet_status = dict(state="cancelled")
        g._update_piglet_collection_screen()
        self.assertFalse(g._piglet_collection_screen)

    def test_cancelled_poll_never_processes_or_acknowledges_more_records(self):
        g = self.g
        g._piglet_collection.cancel_requested = True
        g._poll_piglet_collection()
        g._piglet_collection.store.process_pending.assert_not_called()
        g._piglet_collection.store.acknowledge_events.assert_not_called()

    def test_pending_import_after_network_stop_still_requires_confirmation(self):
        g = self.g
        g._piglet_collection.snapshot.return_value = dict(state="stopped", progress={"pending_files": 1})
        PX.btnp = lambda key: key == PX.KEY_ESCAPE
        g._update_piglet_collection_screen()
        self.assertTrue(g._piglet_cancel_confirm)

    def test_confirmation_draw_explains_only_unfinished_work_is_discarded(self):
        self.g._piglet_cancel_confirm = True
        PX.text.reset_mock()
        self.g._draw_piglet_collection_screen()
        labels = [call.args[2] for call in PX.text.call_args_list]
        self.assertIn("STOP PIGLET COLLECTION?", labels)
        self.assertIn("Completed imports and already awarded XP stay saved.", labels)
        self.assertIn("Unconfirmed source CSV stay on Piglet for another try.", labels)

    def test_enter_stops_active_collection_and_starts_stopped_collection(self):
        g = self.g
        PX.btnp = lambda key: key == PX.KEY_RETURN
        g._update_piglet_collection_screen()
        g._piglet_collection.cancel.assert_called_once_with()
        g._piglet_collection.snapshot.return_value = dict(state="stopped")
        g._update_piglet_collection_screen()
        g._piglet_collection.start.assert_called_once()

    def test_menu_action_opens_collection_without_radio_or_network_start(self):
        g = self.g
        g._piglet_collection_screen = False
        g.menu_open = True
        g._handle_python_attack("_piglet_collection", "piglet_collection", "Piglet Collection", [])
        self.assertTrue(g._piglet_collection_screen)
        self.assertFalse(g.menu_open)
        g._piglet_collection.start.assert_not_called()

    def test_menu_indicator_uses_cached_confirmed_receiver_state(self):
        g = self.g
        for state in ("starting", "active", "stopping", "stopped", "complete", "error"):
            g._piglet_status = dict(state=state)
            self.assertEqual(g._is_running("piglet_collection"), state == "active")
        g._piglet_collection.snapshot.assert_not_called()

    def test_collection_workers_get_one_bounded_window_while_active_or_pending(self):
        g = self.g
        states = [dict(state=phase) for phase in ("starting", "active", "stopping")]
        states += [dict(state="stopped", progress={"pending_files": 1}),
                   dict(state="complete", progress={"import": {"pending_events": 1}})]
        for status in states:
            with self.subTest(status=status), patch.object(time, "sleep") as pause:
                g._piglet_status = status
                g._yield_piglet_threads()
                pause.assert_called_once_with(0.001)
        g._piglet_collection.snapshot.assert_not_called()

    def test_inactive_or_missing_collection_does_not_delay_solo(self):
        g = self.g
        for status in (None, {}, dict(state="stopped"), dict(state="complete"), dict(state="error")):
            with self.subTest(status=status), patch.object(time, "sleep") as pause:
                g._piglet_status = status
                g._yield_piglet_threads()
                pause.assert_not_called()
        g._piglet_collection = None
        g._piglet_status = dict(state="active")
        with patch.object(time, "sleep") as pause:
            g._yield_piglet_threads()
            pause.assert_not_called()

    def test_worker_window_occurs_once_after_serial_gps_and_import_poll(self):
        update = next(node for node in APP.body if isinstance(node, ast.FunctionDef) and node.name == "update")
        calls = [(node.func.attr, node.lineno) for node in ast.walk(update)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"]
        locations = [line for name, line in calls if name == "_yield_piglet_threads"]
        self.assertEqual(len(locations), 1)
        for name in ("_poll_serial", "_poll_gps", "_poll_solo", "_poll_piglet_collection"):
            self.assertLess(next(line for candidate, line in calls if candidate == name), locations[0])

    def test_pairing_is_shown_before_start_and_p_still_hides_it(self):
        g = self.g
        g._piglet_status = dict(state="stopped")
        g._handle_python_attack("_piglet_collection", "piglet_collection", "Piglet Collection", [])
        self.assertTrue(g._piglet_pairing_visible)
        PX.text.reset_mock()
        g._draw_piglet_collection_screen()
        labels = [call.args[2] for call in PX.text.call_args_list]
        self.assertIn("UConsole hotspot SSID: Test", labels)
        self.assertIn("UConsole hotspot password: test-only", labels)
        self.assertIn("Collector IPv4 address: 10.72.0.1", labels)
        self.assertIn("Collector port: 8765", labels)
        self.assertIn("Collection pairing token: " + "0" * 64, labels)
        self.assertIn("Piglet WebUI: save pairing. START here, then reboot Piglet.", labels)
        PX.btnp = lambda key: key == PX.KEY_P
        g._update_piglet_collection_screen()
        self.assertFalse(g._piglet_pairing_visible)
        g._piglet_collection.start.assert_not_called()

    def test_opening_active_collection_hides_pairing_and_start_hides_it_again(self):
        g = self.g
        for phase in ("starting", "active", "stopping"):
            g._piglet_status = dict(state=phase)
            g._handle_python_attack("_piglet_collection", "piglet_collection", "Piglet Collection", [])
            self.assertFalse(g._piglet_pairing_visible)
        g._piglet_collection.snapshot.return_value = dict(state="stopped")
        g._piglet_pairing_visible = True
        PX.btnp = lambda key: key == PX.KEY_RETURN
        g._update_piglet_collection_screen()
        self.assertFalse(g._piglet_pairing_visible)
        g._piglet_collection.start.assert_called_once()

    def test_draw_states_progress_and_pairing_without_mutation(self):
        g = self.g
        for state in ("starting", "active", "complete", "error", "stopped"):
            for paired in (False, True):
                g._piglet_pairing_visible = paired
                g._piglet_status = dict(state=state, message="Test status", progress=dict(
                    active_devices=["test-device-001"], pending_files=1, total_files=2, wifi=4, ble=3, duplicates=2,
                    current_file=dict(name="source.csv", size=1000, offset=500, state="receiving")))
                g._draw_piglet_collection_screen()
        g._piglet_collection.start.assert_not_called()

    def test_peer_activity_is_distinct_from_hotspot_and_pending_import(self):
        g = self.g
        for state, connected, expected in (
                ("active", [], "WAITING FOR PIGLET KG"),
                ("starting", [], "WAITING FOR PIGLET KG"),
                ("active", ["piglet-test"], "ACTIVE PIGLET KG - piglet-test"),
                ("stopped", ["piglet-test"], "NOT CONNECTED"),
                ("complete", [], "NOT CONNECTED")):
            with self.subTest(state=state, connected=connected):
                g._piglet_status = dict(state=state, progress=dict(
                    connected_devices=connected, active_devices=["old-unfinished-session"], pending_files=1))
                PX.text.reset_mock()
                g._draw_piglet_collection_screen()
                labels = [call.args[2] for call in PX.text.call_args_list]
                self.assertIn("Piglet: " + expected, labels)
                self.assertFalse(any("old-unfinished-session" in label for label in labels))
                self.assertIn("Hotspot: " + state.upper(), labels)

    def test_import_progress_and_summary_are_text_not_transfer_bytes_or_graphic_bar(self):
        g = self.g
        PX.text.reset_mock()
        PX.rect.reset_mock()
        g._piglet_status = dict(state="active", progress=dict(current_file=dict(
            name="received.csv", size=1000, offset=1000, state="processing"),
            **{"import": dict(percent=27, status="importing", new_wifi=4, new_ble=3,
                              duplicates=2, base_xp=91)}))
        g._draw_piglet_collection_screen()
        labels = [call.args[2] for call in PX.text.call_args_list]
        self.assertIn("IMPORT CSV: 27%", labels)
        self.assertIn("Status: IMPORTING", labels)
        self.assertIn("New WiFi: 4   New BLE: 3   Skipped duplicates: 2", labels)
        self.assertIn("Awarded XP: 91", labels)
        self.assertFalse(any("100%" in label or "bytes" in label for label in labels))
        self.assertEqual(PX.rect.call_count, 1, "Only the existing screen background is filled")

    def test_download_phase_switches_to_global_import_progress_without_changing_summary(self):
        g = self.g
        current = dict(name="source.csv", size=1000, offset=500, state="receiving")
        summary = dict(percent=23, status="importing", base_xp=10)
        g._piglet_status = dict(state="active", progress={"current_file": current, "import": summary})
        original = copy.deepcopy(g._piglet_status)
        PX.text.reset_mock()
        g._draw_piglet_collection_screen()
        labels = [call.args[2] for call in PX.text.call_args_list]
        self.assertIn("DOWNLOADING CSV: 50%", labels)
        self.assertIn("Status: DOWNLOADING", labels)
        self.assertEqual(g._piglet_status, original)
        current.update(state="processing", offset=1000)
        PX.text.reset_mock()
        g._draw_piglet_collection_screen()
        labels = [call.args[2] for call in PX.text.call_args_list]
        self.assertIn("IMPORT CSV: 23%", labels)
        self.assertIn("Status: IMPORTING", labels)
        self.assertNotIn("IMPORT CSV: 100%", labels)

    def test_empty_file_download_does_not_divide_by_zero_or_claim_complete_import(self):
        self.g._piglet_status = dict(state="active", progress={
            "current_file": dict(name="empty.csv", size=0, offset=0, state="receiving"),
            "import": dict(percent=23, status="importing")})
        PX.text.reset_mock()
        self.g._draw_piglet_collection_screen()
        labels = [call.args[2] for call in PX.text.call_args_list]
        self.assertIn("DOWNLOADING CSV: 0%", labels)
        self.assertIn("Status: DOWNLOADING", labels)

    def test_summary_formatting_hook_preserves_optional_details_without_mutating_cache(self):
        summary = dict(percent=100, status="complete", awarded_xp=0, details=["Additional import result"])
        self.assertEqual(self.g._piglet_import_summary(summary), summary)
        self.assertIsNot(self.g._piglet_import_summary(summary), summary)
        PX.text.reset_mock()
        self.g._piglet_status = dict(progress={"import": summary})
        self.g._draw_piglet_collection_screen()
        self.assertIn("Additional import result", [call.args[2] for call in PX.text.call_args_list])

    def test_update_notice_waits_until_collection_screen_closes(self):
        self.assertFalse(self.g._game_update_notice_allowed())
        self.g._piglet_collection_screen = False
        self.assertTrue(self.g._game_update_notice_allowed())

    def test_shutdown_keeps_shared_loot_and_usb_when_collection_did_not_close(self):
        import test_game_shutdown as shutdown
        g = shutdown.game()
        g._piglet_collection = SimpleNamespace(close=Mock(return_value=False))
        g._cleanup()
        self.assertIn("_piglet_collection", g._cleanup_errors)
        g.loot.close.assert_not_called()
        self.assertTrue(g.controller.states["usb"])


if __name__ == "__main__":
    unittest.main()
