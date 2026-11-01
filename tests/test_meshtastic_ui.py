"""Keyboard, view reuse and radio ownership tests without Pyxel, SPI or RF.

The method harness compiles the actual app methods with a drawing recorder;
it avoids importing the game's unrelated serial/audio/GPS dependencies.
"""
import ast
from contextlib import contextmanager
import copy
import math
from pathlib import Path
from queue import Queue
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from watchdogs.lora_manager import LoRaManager
from watchdogs.meshtastic_manager import MeshtasticManager
from watchdogs.meshtastic_protocol import encode_text


class PyxelRecorder:
    frame_count = 42

    def __init__(self):
        self.pressed = set()
        self.held = set()
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("KEY_"):
            return name
        return lambda *args: self.calls.append((name, args))

    def btnp(self, key, *args):
        return key in self.pressed

    def btn(self, key):
        return key in self.held


PX = PyxelRecorder()
SOURCE = Path(__file__).resolve().parents[1] / "watchdogs" / "app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
METHODS = {
    "_active_lora_protocol", "_stop_radio_owner", "_switch_lora_protocol",
    "_toggle_lora", "_update_lora_protocol_picker", "_draw_lora_protocol_picker",
    "_update_mt_profile_picker", "_draw_mt_profile_picker", "_register_mesh_node",
    "_update_mesh_marker", "_on_mt_node", "_on_mt_text", "_on_mt_status", "_on_mt_tx",
    "_poll_meshtastic", "_meshtastic_view", "_update_mt_screen", "_draw_mt_screen",
    "_update_mc_nodes_panel", "_mesh_contact_actions", "_draw_mc_screen",
    "_update_mc_region_picker", "_scan_loot_dir",
    "_load_loot_totals",
    "_update_input_dialog", "_draw_input_dialog",
    "_draw_radar",
}
ENV = dict(__name__="watchdogs.ui_method_test", __package__="watchdogs",
           pyxel=PX, time=time, math=math, Queue=Queue, Path=Path,
           contextmanager=contextmanager, SimpleNamespace=SimpleNamespace)
for statement in TREE.body:
    if isinstance(statement, ast.Assign):
        try:
            exec(compile(ast.Module(body=[statement], type_ignores=[]), str(SOURCE), "exec"), ENV)
        except (NameError, AttributeError, TypeError):
            pass
    elif isinstance(statement, ast.ClassDef) and statement.name == "MapMarker":
        exec(compile(ast.Module(body=[statement], type_ignores=[]), str(SOURCE), "exec"), ENV)
game_ast = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "WatchDogsGame")
harness_ast = ast.ClassDef(name="GameHarness", bases=[], keywords=[], decorator_list=[],
                          body=[copy.deepcopy(n) for n in game_ast.body
                                if isinstance(n, ast.FunctionDef) and n.name in METHODS])
exec(compile(ast.fix_missing_locations(ast.Module(body=[harness_ast], type_ignores=[])),
             str(SOURCE), "exec"), ENV)
GameHarness = ENV["GameHarness"]


class Radio:
    def __init__(self, protocol, events):
        self.mode = protocol
        self.events = events
        self.running = self.ready = False
        self.last_error = ""
        self.stop_ok = self.start_ok = True
        self.rename_ok = True
        self.queue = Queue()
        self.packets_received = 0
        self.profile_id = "sfnarrow"
        self.channel_name = "SFNarrow"
        self.node_name = "WDG abcd"
        self._known_pubkeys = {}

    def stop(self):
        self.events.append((self.mode, "stop"))
        if self.stop_ok:
            self.running = self.ready = False
        return self.stop_ok

    def start(self, profile):
        self.events.append((self.mode, "start", profile))
        self.profile_id = profile
        self.running = self.ready = self.start_ok
        return self.start_ok

    def start_meshcore(self, region):
        return self.start(region)

    def set_mc_channels(self, channels):
        self.events.append((self.mode, "channels", [c.name for c in channels]))

    def set_mc_active_channel(self, index):
        self.events.append((self.mode, "active_channel", index))

    def send_meshcore_advert(self, *args):
        self.events.append((self.mode, "advert"))

    def set_profile(self, profile):
        self.events.append((self.mode, "profile", profile))
        self.profile_id = profile
        return True

    def set_known_nodes(self, nodes):
        self.known_nodes = copy.deepcopy(nodes)

    def set_node_name(self, name):
        self.events.append((self.mode, "name", name))
        if self.rename_ok:
            self.node_name = name
        return self.rename_ok

    def send_message(self, text):
        self.events.append((self.mode, "send", text))
        return "token" if self.running and self.ready else None


def game():
    PX.pressed.clear()
    PX.held.clear()
    PX.calls.clear()
    g = GameHarness()
    g.events = []
    g.messages = []
    g._lora = Radio("meshcore", g.events)
    g._mt = Radio("meshtastic", g.events)
    g._mt_event_queue = Queue()
    g._mt_profile = "sfnarrow"
    g._mt_profile_sel = 0
    g._mt_profile_screen = False
    g._mt_screen = True
    g._mc_screen = False
    g.input_mode = False
    g.input_fields = []
    g.input_field_idx = 0
    g._mt_tx_pending = {}
    g._lora_enabled = False
    g._aio_available = True
    g._radio_protocol = ""
    g._lora_protocol_picker = False
    g._lora_protocol_sel = 0
    g._wd = None
    g._watch = SimpleNamespace(connected=False)
    g.menu_open = True
    g.gps_fix = False
    g._mc_region = "eu_uk_narrow"
    g._mc_region_sel = 0
    g._mesh_view_protocol = "meshcore"
    g._mt_view = dict(log=[], input="", scroll=0, nodes_panel=False, node_sel=0,
                      node_action=False, node_action_sel=0, note_editing=False,
                      note_buf="", dm_target=None, chan_picker=False, chan_sel=0,
                      active_ch=0, channels_list=[], node_name="", nodes=[])
    for key, value in copy.deepcopy(g._mt_view).items():
        setattr(g, "_mc_" + key, value)
    g._mc_channels_list = [SimpleNamespace(name="public"), SimpleNamespace(name="private")]
    g._mc_node_name = "MC abcd"
    g._mc_bubbles = []
    g.markers = []
    g.loot = None
    g._loot_totals = {}
    g.xp = 100
    g._xp_dirty = False
    g.state = SimpleNamespace(lora_running=False, lora_mode="", mt_nodes=0, mc_nodes=0, mt_messages=0)
    g.msg = lambda *args: g.messages.append(args)
    g._term_add = lambda *args, **kwargs: g.messages.append(args)
    g._play_mc_notify = Mock()
    g._earn_badge = Mock()
    g._get_char_input = lambda: ""
    g.gain_xp = lambda n: setattr(g, "xp", g.xp + n)
    ENV["AioManager"] = SimpleNamespace(toggle=lambda component, enabled:
                                        g.events.append(("power", enabled)) or True)
    g._aio_power = SimpleNamespace(toggle=ENV["AioManager"].toggle, claim=lambda feature: True)
    return g


class MeshtasticUiTests(unittest.TestCase):
    def test_main_radio_sprite_and_sdr_layout_match_for_ready_mc_and_mt(self):
        g = game()
        g.proj = SimpleNamespace(lon_span=1)
        g.player_lat = g.player_lon = 0
        g.ble_devices = g.wifi_networks = g.loot_points = []
        g._radio_sprite_ok = True
        g._radio_w, g._radio_h = 32, 48
        g._mc_bubbles = [("received message", 300)]
        g._sdr = SimpleNamespace(running=True, mode="433", aircraft={}, total_sensors_seen=0)
        g._lora.running = g._lora.ready = True
        g._draw_radar()
        mc_calls = list(PX.calls)
        self.assertIn(("blt", (594, 64, 2, 0, 0, 32, 48, 0)), mc_calls)
        PX.calls.clear()
        g._lora.running = g._lora.ready = False
        g._mt.running = g._mt.ready = True
        g._draw_radar()
        self.assertEqual(PX.calls, mc_calls)

    def test_main_radio_sprite_is_absent_when_off_or_not_ready(self):
        for protocol, running in (("meshcore", False), ("meshtastic", False),
                                  ("meshcore", True), ("meshtastic", True)):
            g = game()
            g.proj = SimpleNamespace(lon_span=1)
            g.player_lat = g.player_lon = 0
            g.ble_devices = g.wifi_networks = g.loot_points = []
            g._radio_sprite_ok = True
            g._sdr = SimpleNamespace(running=False, aircraft={})
            manager = g._lora if protocol == "meshcore" else g._mt
            manager.running = running
            manager.ready = False
            g._draw_radar()
            self.assertFalse(any(operation == "blt" for operation, arguments in PX.calls))

    def test_picker_defaults_right_meshcore_and_does_not_power_before_enter(self):
        g = game()
        g._toggle_lora()
        self.assertTrue(g._lora_protocol_picker)
        self.assertEqual(g._lora_protocol_sel, 1)
        self.assertFalse(g._lora_enabled)
        self.assertEqual(g.events, [])
        g._draw_lora_protocol_picker()
        labels = {args[2]: args[0] for op, args in PX.calls if op == "text"}
        self.assertLess(labels["Meshtastic"], labels["MeshCore"])
        PX.pressed = {PX.KEY_LEFT}
        g._update_lora_protocol_picker()
        PX.pressed = {PX.KEY_RETURN}
        g._update_lora_protocol_picker()
        self.assertEqual(g._active_lora_protocol(), "meshtastic")
        self.assertEqual(g.events[:2], [("power", True), ("meshtastic", "start", "sfnarrow")])
        self.assertFalse(any("advert" in event for event in g.events))

    def test_switch_stops_previous_owner_before_start_and_syncs_mc_channels(self):
        g = game()
        g._lora_enabled = True
        g._mt.running = g._mt.ready = True
        self.assertTrue(g._switch_lora_protocol("meshcore"))
        self.assertEqual(g.events[0], ("meshtastic", "stop"))
        self.assertEqual(g.events[1], ("meshcore", "channels", ["public", "private"]))
        self.assertEqual(g.events[2], ("meshcore", "active_channel", 0))
        self.assertEqual(g._active_lora_protocol(), "meshcore")

    def test_failed_stop_blocks_switch_and_power_off(self):
        g = game()
        g._lora_enabled = True
        g._lora.running = g._lora.ready = True
        g._lora.stop_ok = False
        self.assertFalse(g._switch_lora_protocol("meshtastic"))
        g._toggle_lora()
        self.assertTrue(g._lora_enabled)
        self.assertFalse(any(e[0] in ("power", "meshtastic") for e in g.events))

    def test_power_off_waits_for_owner_cleanup(self):
        g = game()
        g._lora_enabled = True
        g._mt.running = g._mt.ready = True
        g._toggle_lora()
        self.assertEqual(g.events, [("meshtastic", "stop"), ("power", False)])
        self.assertEqual(g._active_lora_protocol(), "")

    def test_failed_init_is_not_reported_active(self):
        g = game()
        g._lora_enabled = True
        g._mt.start_ok = False
        self.assertFalse(g._switch_lora_protocol("meshtastic"))
        self.assertEqual(g._active_lora_protocol(), "")
        self.assertFalse(g.state.lora_running)

    def test_chat_escape_keeps_receiver_running_and_mc_state(self):
        g = game()
        g._mt.running = g._mt.ready = True
        g._mc_input = "private MC draft"
        PX.pressed = {PX.KEY_ESCAPE}
        g._update_mt_screen()
        self.assertFalse(g._mt_screen)
        self.assertTrue(g._mt.running)
        self.assertEqual(g._mc_input, "private MC draft")
        self.assertEqual(g.events, [])

    def test_render_reuses_exact_mc_geometry_and_palette(self):
        g = game()
        g._mc_log = [("same public text", 11, "12:30")]
        g._mt_view["log"] = copy.deepcopy(g._mc_log)
        g._draw_mc_screen()
        mc_calls = list(PX.calls)
        PX.calls.clear()
        g._draw_mt_screen()
        mt_calls = list(PX.calls)
        self.assertEqual([c for c in mc_calls if c[0] != "text"],
                         [c for c in mt_calls if c[0] != "text"])
        self.assertEqual([(a[0], a[1], a[3]) for op, a in mc_calls if op == "text"],
                         [(a[0], a[1], a[3]) for op, a in mt_calls if op == "text"])
        self.assertTrue(any(op == "text" and "MESHTASTIC [PUBLIC/SFNarrow]" in a[2] for op, a in mt_calls))
        self.assertEqual(g._mc_channels_list[1].name, "private")
        self.assertIn("DM", g._mesh_contact_actions())
        with g._meshtastic_view():
            self.assertNotIn("DM", g._mesh_contact_actions())

    def test_ctrl_n_reuses_node_name_dialog_and_saves_without_tx(self):
        g = game()
        g._mt.running = g._mt.ready = True
        PX.held = {PX.KEY_LCTRL}
        PX.pressed = {PX.KEY_N}
        g._update_mt_screen()
        self.assertTrue(g.input_mode)
        self.assertEqual(g.input_fields, [{"label": "Node Name", "value": "WDG abcd"}])
        self.assertEqual(g._input_pending_cat, -9)
        g._draw_mt_screen()
        self.assertTrue(any(op == "text" and a[2] == "INPUT: Meshtastic" for op, a in PX.calls))
        g.input_fields[0]["value"] = "My Console"
        PX.held.clear()
        PX.pressed = {PX.KEY_RETURN}
        g._update_input_dialog()
        self.assertFalse(g.input_mode)
        self.assertEqual(g._mt.node_name, "My Console")
        self.assertEqual(g._mc_node_name, "MC abcd")
        self.assertEqual(g.events, [("meshtastic", "name", "My Console")])

    def test_invalid_mt_name_keeps_dialog_and_current_name_without_tx(self):
        g = game()
        g._mt.rename_ok = False
        g._mt.last_error = "Node name exceeds 40 UTF-8 bytes"
        g.input_mode = True
        g.input_fields = [{"label": "Node Name", "value": "invalid"}]
        g._input_pending_cat = -9
        PX.pressed = {PX.KEY_RETURN}
        g._update_input_dialog()
        self.assertTrue(g.input_mode)
        self.assertEqual(g._mt.node_name, "WDG abcd")
        self.assertIn(g._mt.last_error, str(g._mt_view["log"]))
        self.assertFalse(any("send" in event or "advert" in event for event in g.events))

    def test_cancel_mt_name_keeps_saved_value_and_radio_running(self):
        g = game()
        g._mt.running = g._mt.ready = True
        g.input_mode = True
        g.input_fields = [{"label": "Node Name", "value": "unsaved"}]
        g._input_pending_cat = -9
        PX.pressed = {PX.KEY_ESCAPE}
        g._update_input_dialog()
        self.assertFalse(g.input_mode)
        self.assertEqual(g._mt.node_name, "WDG abcd")
        self.assertTrue(g._mt.running)
        self.assertEqual(g.events, [])

    def test_callbacks_only_enqueue_and_main_loop_awards_shared_registration(self):
        g = game()
        registered = set()
        def register(protocol, node_id, data, current_xp):
            key = (protocol, node_id)
            award = 0 if key in registered else 20
            registered.add(key)
            return {"contact": dict(data, has_position=False), "xp_total": current_xp + award}
        g.loot = SimpleNamespace(register_mesh_contact=Mock(side_effect=register))
        for unused in range(2):
            g._on_mt_node("!12345678", "Router", "Relay", None, None, -111, -6)
        self.assertEqual(g.xp, 100)
        self.assertEqual(g._mt_view["nodes"], [])
        g._poll_meshtastic()
        self.assertEqual(g.xp, 120)
        self.assertFalse(g._xp_dirty)
        self.assertEqual(len(g._mt_view["nodes"]), 1)
        self.assertEqual(g.markers, [])
        self.assertEqual(g.loot.register_mesh_contact.call_count, 2)

    def test_storage_failure_keeps_xp_and_does_not_crash_game(self):
        g = game()
        g.loot = SimpleNamespace(register_mesh_contact=Mock(side_effect=OSError("disk full")))
        g._on_mt_node("!12345678", "Client", "", None, None, -100, 1)
        g._poll_meshtastic()
        self.assertEqual(g.xp, 100)
        self.assertEqual(g._mt_view["nodes"], [])
        self.assertFalse(any("[MT] RX " in str(message) for message in g.messages))

    def test_saved_mt_callbacks_show_new_and_known_activity_on_main_terminal(self):
        g = game()
        g.loot = SimpleNamespace(register_mesh_contact=Mock(
            side_effect=lambda protocol, node_id, data, current_xp:
            {"contact": dict(data, has_position=False), "xp_total": current_xp}))
        for unused in range(2):
            g._on_mt_node("!12345678", "Client", "Nearby", None, None, -111, -6.5)
        self.assertFalse(any("[MT] RX " in str(message) for message in g.messages))
        g._poll_meshtastic()
        activity = [message[0] for message in g.messages if "[MT] RX " in message[0]]
        self.assertEqual(activity, [
            "[MT] RX new !12345678 Nearby RSSI:-111dBm SNR:-6.5dB",
            "[MT] RX known !12345678 Nearby RSSI:-111dBm SNR:-6.5dB",
        ])
        self.assertEqual(g.xp, 100)

    def test_unverified_frame_never_creates_main_terminal_node_activity(self):
        g = game()
        g.loot = SimpleNamespace(register_mesh_contact=Mock())
        with tempfile.TemporaryDirectory() as temporary:
            g._mt = MeshtasticManager(home=temporary)
            g._mt._on_node = g._on_mt_node
            g._mt._on_text = g._on_mt_text
            frame = encode_text("synthetic software fixture", 0x12345678, 1)
            self.assertFalse(g._mt._handle_frame(frame, {
                "crc_status": "failed", "crc_present": True, "rssi_dbm": -111, "snr_db": -6.5}))
            self.assertFalse(g._mt._handle_frame(b"invalid", {
                "crc_status": "pass", "crc_present": True, "rssi_dbm": -111, "snr_db": -6.5}))
            g._poll_meshtastic()
        g.loot.register_mesh_contact.assert_not_called()
        self.assertEqual(g._mt_view["nodes"], [])
        self.assertEqual(g.xp, 100)
        self.assertFalse(any("[MT] RX " in str(message) for message in g.messages))

    def test_map_namespaces_ids_and_updates_existing_marker(self):
        g = game()
        contact = dict(id="12345678", name="node", lat=0, lon=6, has_position=True)
        g._update_mesh_marker("meshtastic", contact)
        g._update_mesh_marker("meshcore", contact)
        g._update_mesh_marker("meshtastic", dict(contact, name="renamed", lon=7))
        g._update_mesh_marker("meshtastic", dict(contact, id="missing", lat=0, lon=0, has_position=False))
        self.assertEqual(len(g.markers), 2)
        mt = next(m for m in g.markers if m.type == "meshtastic")
        self.assertEqual((mt.lat, mt.lon, mt.label), (0, 7, "renamed"))

    def test_tx_requires_enter_and_tx_done_never_means_delivery(self):
        g = game()
        g._mt.running = g._mt.ready = True
        g._mt_view["input"] = "hello"
        g.loot = SimpleNamespace(save_meshtastic_message=Mock())
        g._update_mt_screen()
        self.assertEqual(g.events, [])
        PX.pressed = {PX.KEY_RETURN}
        g._update_mt_screen()
        self.assertEqual(g.events, [("meshtastic", "send", "hello")])
        self.assertEqual(g._mt_view["log"][0][2], "QUEUED")
        g._on_mt_tx("token", True, "")
        g._poll_meshtastic()
        self.assertEqual(g._mt_view["log"][0][2], "TX")
        self.assertNotIn("delivered", str(g._mt_view["log"]).lower())
        self.assertEqual(g.loot.save_meshtastic_message.call_args.kwargs, {"direction": "tx"})

    def test_profile_changes_offline_without_gps_or_radio_start(self):
        g = game()
        g._mt_profile_sel = 1
        PX.pressed = {PX.KEY_RETURN}
        g._update_mt_profile_picker()
        self.assertEqual(g._mt_profile, "longfast")
        self.assertEqual(g.events, [("meshtastic", "profile", "longfast")])

    def test_old_stopped_or_error_event_does_not_clear_restarted_ready_radio(self):
        for old_state in ("stopped", "error"):
            g = game()
            g._radio_protocol = "meshtastic"
            g.state.lora_running = True
            g.state.lora_mode = "meshtastic"
            g._on_mt_status(old_state, "old run error" if old_state == "error" else "")
            # A new start has already completed before the UI drains the old event.
            g._mt.running = g._mt.ready = True
            g._poll_meshtastic()
            self.assertEqual(g._radio_protocol, "meshtastic")
            self.assertTrue(g.state.lora_running)
            self.assertEqual(g.state.lora_mode, "meshtastic")

    def test_ready_event_only_restores_current_ready_meshtastic_owner(self):
        g = game()
        g._mt.running = g._mt.ready = True
        g._on_mt_status("ready", "")
        g._poll_meshtastic()
        self.assertEqual((g._radio_protocol, g.state.lora_running, g.state.lora_mode),
                         ("meshtastic", True, "meshtastic"))
        # All old MT statuses can remain queued across a switch to MeshCore.
        for state in ("stopped", "error", "ready"):
            g._on_mt_status(state, "old error" if state == "error" else "")
        g._mt.running = g._mt.ready = False
        g._lora.running = g._lora.ready = True
        g._radio_protocol = g.state.lora_mode = "meshcore"
        g._poll_meshtastic()
        self.assertEqual((g._radio_protocol, g.state.lora_running, g.state.lora_mode),
                         ("meshcore", True, "meshcore"))

    def test_received_text_keeps_original_channel_across_profile_switch(self):
        g = game()
        g.loot = SimpleNamespace(save_meshtastic_message=Mock())
        g._on_mt_text("!12345678", "Sender", "old channel packet", -110, 2)
        g._mt.channel_name = "LongFast"
        g._poll_meshtastic()
        g.loot.save_meshtastic_message.assert_called_once_with(
            "SFNarrow", "Sender: old channel packet", -110)

    def test_tx_result_keeps_channel_selected_when_user_pressed_enter(self):
        for success in (True, False):
            g = game()
            g._mt.running = g._mt.ready = True
            g._mt_view["input"] = "old channel message"
            g.loot = SimpleNamespace(save_meshtastic_message=Mock())
            PX.pressed = {PX.KEY_RETURN}
            g._update_mt_screen()
            g._mt.channel_name = "LongFast"
            g._on_mt_tx("token", success, "" if success else "stopped")
            g._poll_meshtastic()
            saved = g.loot.save_meshtastic_message.call_args
            self.assertEqual(saved.args[0], "SFNarrow")
            self.assertEqual(saved.kwargs, {"direction": "tx"})

    def test_meshcore_region_picker_does_not_retune_meshtastic_or_rnode(self):
        for mode in ("meshtastic", "rnode"):
            g = game()
            g._lora_enabled = True
            g._lora.mode = mode
            g._lora.running = g._lora.ready = True
            g._mc_region_sel = 1
            PX.pressed = {PX.KEY_RETURN}
            with patch.dict(sys.modules, {"pyxel": PX}), patch("watchdogs.lora_manager.save_meshcore_config"):
                g._update_mc_region_picker()
            self.assertEqual(g.events, [])


class LoRaLifecycleTests(unittest.TestCase):
    def test_stopped_owner_does_not_carry_raw_tx_into_next_protocol(self):
        manager = LoRaManager()
        manager._tx_queue.put(b"old RNode frame")
        self.assertTrue(manager.stop())
        self.assertTrue(manager._tx_queue.empty())

    def test_stop_timeout_retains_owner(self):
        manager = LoRaManager()
        manager.running = manager.ready = True
        worker = Mock()
        worker.is_alive.return_value = True
        manager._thread = worker
        self.assertFalse(manager.stop())
        self.assertIs(manager._thread, worker)
        self.assertTrue(manager.running)

    def test_meshcore_mode_is_selected_before_worker_and_waits_ready(self):
        manager = LoRaManager()
        seen = []
        def worker(*args):
            seen.append((manager.mode, manager.ready))
            manager.ready = True
            manager._ready_event.set()
            manager._stop_event.wait(1)
            manager.running = manager.ready = False
        with patch.object(manager, "_run_sniffer", side_effect=worker):
            self.assertTrue(manager.start_meshcore())
            self.assertEqual(seen, [("meshcore", False)])
            self.assertTrue(manager.stop())

    def test_cleanup_closes_module_spi_even_if_sleep_failed(self):
        manager = LoRaManager()
        radio = Mock()
        radio.sleep.side_effect = OSError("radio off")
        module = SimpleNamespace(spi=Mock())
        with patch("watchdogs.lora_manager.importlib.import_module", return_value=module) as load:
            manager._cleanup_radio(radio)
        load.assert_called_once_with("LoRaRF.SX126x")
        module.spi.close.assert_called_once()

    def test_crc_error_takes_priority_over_rx_done_for_meshcore(self):
        manager = LoRaManager()
        manager.mode = "meshcore"
        manager.running = True
        radio = Mock(IRQ_RX_DONE=2, IRQ_TIMEOUT=512, IRQ_HEADER_ERR=32, IRQ_CRC_ERR=64, RX_CONTINUOUS=0xFFFFFF)
        def irq():
            manager._stop_event.set()
            return 2 | 64
        radio.getIrqStatus.side_effect = irq
        with patch.object(manager, "_init_radio", return_value=radio), \
                patch.object(manager, "_configure_radio"), \
                patch.object(manager, "_cleanup_radio"), \
                patch.object(manager, "_handle_meshcore") as handler:
            manager._run_sniffer(869618000, 8, 5, 62500, "test")
        handler.assert_not_called()
        radio.getRxBufferStatus.assert_not_called()


if __name__ == "__main__":
    unittest.main()
