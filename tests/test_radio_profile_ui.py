"""RF picker and GPS integration tests with temporary preferences and fake radio.

The actual app methods are compiled into the existing Pyxel recorder harness.
No GPS device, player database, radio, upload or real profile change is used.
"""

import ast
import copy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_meshtastic_ui as old_ui
from watchdogs.radio_profiles import PROFILES, RadioProfileController


METHODS = {"_apply_radio_profile", "_poll_radio_profiles", "_update_radio_profile_picker",
           "_draw_radio_profile_picker", "_poll_gps", "_draw_mc_region_picker"}
tree = ast.parse(old_ui.SOURCE.read_text(encoding="utf-8"))
game_ast = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "WatchDogsGame")
environment = dict(old_ui.ENV, BaseGame=old_ui.GameHarness)
harness = ast.ClassDef(name="ProfileGame", bases=[ast.Name(id="BaseGame", ctx=ast.Load())],
                       keywords=[], decorator_list=[], body=[copy.deepcopy(node) for node in game_ast.body
                         if isinstance(node, ast.FunctionDef) and node.name in METHODS])
exec(compile(ast.fix_missing_locations(ast.Module(body=[harness], type_ignores=[])),
             str(old_ui.SOURCE), "exec"), environment)
ProfileGame = environment["ProfileGame"]
PX = old_ui.PX


class RadioProfileUiTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.now = 100.0
        self.clock_patch = patch.object(old_ui.time, "monotonic", side_effect=lambda: self.now)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.controller = RadioProfileController(home=self.home.name,
            initial={"meshtastic": "sfnarrow", "meshcore": "eu_uk_narrow"},
            resolver=lambda lat, lon: dict(code="ES", label="Spain", region="EU_868", boundary_distance_km=100,
                                          land_boundary_distance_km=6.0),
            clock=lambda: self.now)
        self.g = old_ui.game()
        self.g.__class__ = ProfileGame
        self.g._rf_profiles = self.controller
        self.g._rf_last_poll = -float("inf")
        self.g._rf_notices = {}
        self.g._rf_confirm = None
        self.g._mc_region_screen = True
        self.g._mc_active_ch = 1
        self.g.gps = SimpleNamespace(available=False, radio_fix=Mock(return_value=None))
        for manager in (self.g._mt, self.g._lora):
            def retune(key, manager=manager):
                self.g.events.append((manager.mode, "retune", key))
                manager.profile_id = key
                return True
            manager.retune_profile = Mock(side_effect=retune)

    def press(self, key, protocol="meshtastic"):
        PX.pressed = {key}
        self.g._update_radio_profile_picker(protocol)
        PX.pressed.clear()

    def stable_fix(self):
        for _ in range(5):
            self.controller.update(dict(lat=36.46, lon=-6.2, observed_at=self.now,
                                        quality=1, sats=8, hdop=.9))
            self.now += 5
        self.now -= 5

    def text(self):
        return [args[2] for name, args in PX.calls if name == "text"]

    def test_enter_applies_manual_rf_offline_without_start_or_channel_change(self):
        self.g._mt_profile_sel = self.controller.choices("meshtastic").index("longfast")
        self.press(PX.KEY_RETURN)
        self.assertEqual(self.g._mt_profile, "longfast")
        self.assertEqual(self.controller.status("meshtastic")["mode"], "manual")
        self.assertEqual(self.controller.target("meshtastic"), "longfast")
        self.assertEqual(self.g.events, [("meshtastic", "retune", "longfast")])
        self.assertEqual(self.g._mt.channel_name, "SFNarrow")
        self.assertFalse(self.g._mt.running)
        self.assertTrue(self.g._mt_profile_screen is False)
        self.assertEqual(self.g._rf_notices["meshtastic"], "RF profile set; chat channel kept")
        self.assertEqual(self.g.messages[-1][0], "[RF] RF profile set; chat channel kept")

    def test_auto_without_gps_holds_actual_rf_and_never_starts_radio(self):
        self.press(PX.KEY_A)
        self.g._poll_radio_profiles()
        self.assertEqual(self.controller.status("meshtastic")["mode"], "auto")
        self.assertEqual(self.g._rf_notices["meshtastic"],
                         "AUTO requires a GPS fix and an approved local profile")
        self.assertIsNone(self.controller.target("meshtastic"))
        self.assertEqual(self.g._mt_profile, "sfnarrow")
        self.assertEqual(self.g.events, [])
        self.g._draw_radio_profile_picker("meshtastic")
        self.assertIn("AUTO requires a GPS fix", self.text())

    def test_poll_gps_offline_still_updates_controller_once_per_second(self):
        with patch.object(self.controller, "update", wraps=self.controller.update) as update:
            self.g._poll_gps()
            self.g._poll_gps()
            self.assertEqual(update.call_count, 1)
            self.now += 1
            self.g._poll_gps()
            self.assertEqual(update.call_count, 2)
            update.assert_called_with(None)

    def uart_gps(self, *, enabled=True, device="/dev/ttyAMA0", opens=False):
        self.g._gps_enabled = enabled
        self.g.gps_fix = False
        self.g.player_lat, self.g.player_lon = 36.46, -6.2
        self.g.gps = SimpleNamespace(available=False, device=device,
            radio_fix=Mock(return_value=None), read_available=Mock(return_value=[]),
            process_sentences=Mock(), fix=SimpleNamespace(valid=False, latitude=0.0,
                longitude=0.0, satellites=0, satellites_visible=0))
        def open_uart(path):
            self.g.gps.available = opens
            return opens
        self.g.gps._try_open = Mock(side_effect=open_uart)
        self.g._term_add = Mock()
        return self.g.gps

    def test_powered_uart_reopens_after_missed_boot_probe_without_inventing_fix(self):
        gps = self.uart_gps(opens=True)
        self.g._poll_gps()
        gps._try_open.assert_called_once_with("/dev/ttyAMA0")
        gps.read_available.assert_called_once_with()
        gps.process_sentences.assert_not_called()
        self.assertTrue(gps.available)
        self.assertFalse(self.g.gps_fix)
        self.assertEqual((self.g.player_lat, self.g.player_lon), (36.46, -6.2))
        self.g._term_add.assert_called_once_with(
            "[GPS] UART connection restored; waiting for a fix", raw=True)
        self.assertEqual(self.controller.data["bindings"], [])
        self.assertEqual(self.g.events, [])

    def test_failed_uart_retries_at_most_every_five_seconds_and_keeps_status_polling(self):
        gps = self.uart_gps()
        with patch.object(self.controller, "update", wraps=self.controller.update) as update:
            self.g._poll_gps()
            for advance in (0.1, 1.0, 4.9):
                self.now = 100 + advance
                self.g._poll_gps()
            self.assertEqual(gps._try_open.call_count, 1)
            self.now = 105
            self.g._poll_gps()
            self.assertEqual(gps._try_open.call_count, 2)
            self.assertGreaterEqual(update.call_count, 2)
            update.assert_called_with(None)
        self.g._term_add.assert_not_called()
        self.assertFalse(self.g.gps_fix)

    def test_gps_power_off_never_opens_uart(self):
        gps = self.uart_gps(enabled=False, opens=True)
        self.g._poll_gps()
        gps._try_open.assert_not_called()
        gps.read_available.assert_not_called()
        self.assertFalse(gps.available)

    def test_usb_or_unknown_gps_device_is_not_probed_by_uart_recovery(self):
        for device in ("/dev/ttyACM1", "/dev/ttyUSB0", ""):
            with self.subTest(device=device):
                gps = self.uart_gps(device=device, opens=True)
                self.g._poll_gps()
                gps._try_open.assert_not_called()
                gps.read_available.assert_not_called()
                self.assertFalse(self.g.gps_fix)

    def test_available_uart_is_not_reopened_and_valid_fix_still_requires_parser(self):
        gps = self.uart_gps(opens=True)
        gps.available = True
        gps.read_available.return_value = ["checksum-validated-by-real-parser"]
        def parse(sentences):
            gps.fix.valid = True
            gps.fix.latitude, gps.fix.longitude = 36.4605, -6.2005
        gps.process_sentences.side_effect = parse
        self.g._poll_gps()
        gps._try_open.assert_not_called()
        gps.process_sentences.assert_called_once_with(["checksum-validated-by-real-parser"])
        self.assertTrue(self.g.gps_fix)
        self.assertEqual((self.g.player_lat, self.g.player_lon), (36.4605, -6.2005))
        self.g._term_add.assert_not_called()

    def test_s_requires_manual_selected_profile_to_be_successfully_applied(self):
        self.stable_fix()
        self.g._mt_profile_sel = self.controller.choices("meshtastic").index("longfast")
        self.press(PX.KEY_S)
        self.assertIsNone(self.g._rf_confirm)
        self.assertIn("Press ENTER first", self.g._rf_notices["meshtastic"])
        self.assertEqual(self.controller.data["bindings"], [])
        self.press(PX.KEY_RETURN)
        self.press(PX.KEY_S)
        self.assertIsNotNone(self.g._rf_confirm)

    def test_s_preview_requires_y_and_only_creates_two_km_protocol_binding(self):
        self.stable_fix()
        self.press(PX.KEY_S)
        self.assertEqual(self.controller.data["bindings"], [])
        self.g._draw_radio_profile_picker("meshtastic")
        text = self.text()
        self.assertIn("Protocol: MESHTASTIC", text)
        self.assertIn("GPS center: 36.460000, -6.200000", text)
        self.assertIn("CIRCLE - radius 2 km", text)
        self.assertIn("This circle only; not the whole country.", text)
        self.assertIn("Y confirm   N/ESC cancel", text)
        self.press(PX.KEY_RETURN)
        self.assertEqual(self.controller.data["bindings"], [])
        self.press(PX.KEY_Y)
        binding, = self.controller.data["bindings"]
        self.assertEqual((binding["protocol"], binding["profile"], binding["radius_km"]),
                         ("meshtastic", "sfnarrow", 2))
        self.assertEqual(self.controller.status("meshtastic")["mode"], "manual")
        self.assertEqual(self.g.events, [])
        self.assertEqual(self.g._rf_notices["meshtastic"],
                         "Profile saved for the displayed 2 km circle only")

    def test_escape_or_n_cancel_confirmation_without_saving_or_closing_picker(self):
        self.stable_fix()
        self.g._mt_profile_screen = True
        for key in (PX.KEY_ESCAPE, PX.KEY_N):
            self.press(PX.KEY_S)
            self.press(key)
            self.assertIsNone(self.g._rf_confirm)
            self.assertEqual(self.controller.data["bindings"], [])
            self.assertTrue(self.g._mt_profile_screen)

    def test_s_without_credible_gps_does_not_prepare_or_save_binding(self):
        self.press(PX.KEY_S)
        self.assertIsNone(self.g._rf_confirm)
        self.assertEqual(self.controller.data["bindings"], [])

    def test_confirmation_revalidates_lost_gps_before_saving(self):
        self.stable_fix()
        self.press(PX.KEY_S)
        self.controller.update(None)
        self.press(PX.KEY_Y)
        self.assertEqual(self.controller.data["bindings"], [])
        self.assertIn("GPS fix", self.g._rf_notices["meshtastic"])

    def test_forget_requires_existing_binding_and_y_and_leaves_actual_rf(self):
        self.stable_fix()
        self.press(PX.KEY_F)
        self.assertIsNone(self.g._rf_confirm)
        self.assertEqual(self.g._rf_notices["meshtastic"], "No saved assignment for this area")
        self.press(PX.KEY_S)
        self.press(PX.KEY_Y)
        self.press(PX.KEY_F)
        self.assertEqual(len(self.controller.data["bindings"]), 1)
        self.press(PX.KEY_N)
        self.assertEqual(len(self.controller.data["bindings"]), 1)
        self.press(PX.KEY_F)
        self.press(PX.KEY_Y)
        self.assertEqual(self.controller.data["bindings"], [])
        self.assertEqual(self.g._mt_profile, "sfnarrow")
        self.assertEqual(self.g.events, [])
        self.assertEqual(self.g._rf_notices["meshtastic"], "Local AUTO assignment removed")

    def test_busy_retune_keeps_actual_rf_and_is_retried_for_current_owner(self):
        self.g._mt.running = self.g._mt.ready = True
        self.g._mt.retune_profile = Mock(side_effect=[False, True])
        self.g._mt.last_error = "TX pending"
        self.g._mt_profile_sel = self.controller.choices("meshtastic").index("longfast")
        self.press(PX.KEY_RETURN)
        self.assertEqual(self.g._mt_profile, "sfnarrow")
        self.assertEqual(self.g._rf_notices["meshtastic"], "RF pending / error: TX pending")
        self.g._poll_radio_profiles()
        self.assertEqual(self.g._mt_profile, "longfast")
        self.assertEqual(self.g.events, [])  # no protocol starts or adverts

    def test_auto_applies_only_current_protocol_without_advert_or_switch(self):
        self.stable_fix()
        self.controller.remember("meshtastic", "longfast", self.controller.preview_area("meshtastic"))
        self.controller.set_auto("meshtastic")
        self.g.gps.radio_fix.return_value = dict(lat=36.46, lon=-6.2, observed_at=self.now,
                                                 quality=1, sats=8, hdop=.9)
        self.g._lora.running = self.g._lora.ready = True
        self.g._poll_radio_profiles()
        self.assertEqual(self.g._mt_profile, "sfnarrow")
        self.assertEqual(self.g.events, [])
        self.g._lora.running = self.g._lora.ready = False
        self.g._mt.running = self.g._mt.ready = True
        self.now += 1
        self.g._poll_radio_profiles()
        self.assertEqual(self.g._mt_profile, "longfast")
        self.assertEqual(self.g.events, [("meshtastic", "retune", "longfast")])

    def test_rnode_is_not_retuned_and_manual_choice_waits_for_protocol_switch(self):
        self.g._lora.mode = "rnode"
        self.g._lora.running = self.g._lora.ready = True
        self.g._mc_region_sel = self.controller.choices("meshcore").index("eu_uk_default")
        self.press(PX.KEY_RETURN, "meshcore")
        self.g._poll_radio_profiles()
        self.assertEqual(self.controller.target("meshcore"), "eu_uk_default")
        self.assertEqual(self.g._mc_region, "eu_uk_narrow")
        self.assertEqual(self.g.events, [])
        self.g._lora.retune_profile.assert_not_called()

    def test_protocol_switch_uses_desired_profile_after_manual_selection(self):
        self.controller.set_manual("meshtastic", "longfast")
        self.g._lora_enabled = True
        self.assertTrue(self.g._switch_lora_protocol("meshtastic"))
        self.assertIn(("meshtastic", "start", "longfast"), self.g.events)
        self.assertEqual(self.g._mt_profile, "longfast")

    def test_restart_uses_last_applied_rf_over_legacy_mc_config_without_gps(self):
        self.controller.set_manual("meshcore", "eu_uk_narrow_v2")
        self.controller.applied("meshcore", "eu_uk_narrow_v2")
        self.controller.set_auto("meshcore")
        self.controller.set_manual("meshtastic", "longfast")
        self.controller.applied("meshtastic", "longfast")
        self.controller.set_auto("meshtastic")
        # Execute the real initialization block only; the full game constructor
        # would open unrelated graphics/devices and is intentionally excluded.
        # The lifecycle constructor delegates device initialization here.
        init = next(node for node in game_ast.body if isinstance(node, ast.FunctionDef) and node.name == "_initialize")
        start = next(i for i, node in enumerate(init.body) if isinstance(node, ast.ImportFrom)
                     and node.module == "radio_profiles")
        end = next(i for i, node in enumerate(init.body[start:], start)
                   if isinstance(node, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == "_rf_confirm"
                                                          for t in node.targets))
        self.g._mc_region = "eu_uk_narrow"
        self.g._mt_profile = "sfnarrow"
        real_controller = RadioProfileController
        factory = lambda **kwargs: real_controller(home=self.home.name, clock=lambda: self.now, **kwargs)
        with patch("watchdogs.radio_profiles.RadioProfileController", side_effect=factory):
            exec(compile(ast.Module(body=copy.deepcopy(init.body[start:end + 1]), type_ignores=[]),
                         str(old_ui.SOURCE), "exec"),
                 {"self": self.g, "__name__": "watchdogs.ui_init_test", "__package__": "watchdogs"})
        self.assertEqual(self.g._mc_region, "eu_uk_narrow_v2")
        self.assertEqual(self.g._mt_profile, "longfast")
        self.assertIsNone(self.g._rf_profiles.target("meshcore"))
        self.assertIsNone(self.g._rf_profiles.target("meshtastic"))
        self.g._lora_enabled = True
        self.assertTrue(self.g._switch_lora_protocol("meshcore"))
        self.assertIn(("meshcore", "start", "eu_uk_narrow_v2"), self.g.events)

    def test_picker_wrappers_route_both_protocols_to_rf_controls(self):
        self.g._mt_profile_sel = self.controller.choices("meshtastic").index("longfast")
        PX.pressed = {PX.KEY_RETURN}
        self.g._update_mt_profile_picker()
        self.g._mc_region_sel = self.controller.choices("meshcore").index("eu_uk_narrow_v2")
        self.g._update_mc_region_picker()
        PX.pressed.clear()
        self.assertEqual(self.g._mt_profile, "longfast")
        self.assertEqual(self.g._mc_region, "eu_uk_narrow_v2")
        self.g._draw_mc_region_picker()
        self.assertIn("MESHCORE - RF Profile", self.text())

    def test_picker_uses_existing_frame_palette_and_displays_actual_channel(self):
        self.g._draw_radio_profile_picker("meshtastic")
        self.assertTrue(any(name == "rectb" and args[-1] == old_ui.ENV["C_WARNING"] for name, args in PX.calls))
        self.assertTrue(any(name == "rectb" and args[-1] == old_ui.ENV["C_COAST"] for name, args in PX.calls))
        self.assertTrue(any("Chat channel: SFNarrow" in line for line in self.text()))
        self.assertTrue(any("Saved RF / radio OFF" in line for line in self.text()))
        PX.calls.clear()
        self.g._draw_radio_profile_picker("meshcore")
        self.assertTrue(any("Chat channel: private" in line for line in self.text()))

    def test_entire_rf_picker_and_shortcuts_fit_above_terminal(self):
        for protocol in ("meshtastic", "meshcore"):
            with self.subTest(protocol=protocol):
                PX.calls.clear()
                self.g._draw_radio_profile_picker(protocol)
                frames = [args for name, args in PX.calls if name == "rectb"]
                self.assertEqual(len(frames), 2)
                for x, y, width, height, color in frames:
                    self.assertGreaterEqual(y, old_ui.ENV["HUD_TOP"])
                    self.assertLessEqual(y + height, old_ui.ENV["TERM_Y"])
                self.assertEqual(frames[0][-1], old_ui.ENV["C_WARNING"])
                self.assertEqual(frames[1][-1], old_ui.ENV["C_COAST"])
                for name, args in PX.calls:
                    if name == "text":
                        self.assertGreaterEqual(args[1], old_ui.ENV["HUD_TOP"])
                        self.assertLessEqual(args[1] + 8, old_ui.ENV["TERM_Y"], args[2])
                self.assertTrue(any("S Save for this area" in line for line in self.text()))
                self.assertTrue(any("M/ENTER MANUAL" in line for line in self.text()))

    def test_both_menus_and_save_remove_confirmations_fit_existing_frames(self):
        self.stable_fix()
        self.g._mc_channels_list[1].name = "Kanał Łódź"
        for protocol in ("meshtastic", "meshcore"):
            with self.subTest(protocol=protocol):
                for action in ("menu", "save", "remove"):
                    if action == "save":
                        self.press(PX.KEY_S, protocol)
                        self.assertEqual(self.g._rf_confirm["action"], "remember")
                    elif action == "remove":
                        self.press(PX.KEY_Y, protocol)
                        self.press(PX.KEY_F, protocol)
                        self.assertEqual(self.g._rf_confirm["action"], "forget")
                    PX.calls.clear()
                    self.g._draw_radio_profile_picker(protocol)
                    text = self.text()
                    if action == "menu":
                        self.assertTrue(any(line.startswith("Mode: MANUAL    GPS region:") for line in text))
                        if protocol == "meshcore":
                            self.assertIn("Chat channel: Kanał Łódź (unchanged by GPS)", text)
                    else:
                        title = "SAVE" if action == "save" else "REMOVE"
                        self.assertIn(title + " LOCAL AUTO ASSIGNMENT?", text)
                    frames = [args for name, args in PX.calls if name == "rectb"]
                    expected = (100, 33, 440, 184) if protocol == "meshtastic" else (100, 19, 440, 212)
                    self.assertEqual(frames[0][0:4], expected)
                    self.assertEqual(frames[0][-1], old_ui.ENV["C_WARNING"])
                    self.assertEqual(frames[1][-1], old_ui.ENV["C_COAST"])
                    x, y, width, height = expected
                    for name, args in PX.calls:
                        if name != "text":
                            continue
                        text_x, text_y, text = args[:3]
                        self.assertGreaterEqual(text_x, x)
                        # The production UI uses the unchanged Spleen 5x8 BDF.
                        self.assertLessEqual(text_x + len(text) * 5, x + width, text)
                        self.assertGreaterEqual(text_y, y)
                        self.assertLessEqual(text_y + 8, y + height, text)
                        self.assertLessEqual(text_y + 8, old_ui.ENV["TERM_Y"], text)
                self.press(PX.KEY_N, protocol)
        self.assertEqual(self.g.events, [])

    def test_usa_profile_row_displays_unverified_antenna_before_any_retune(self):
        self.g._mt_profile_sel = self.controller.choices("meshtastic").index("us_longfast")
        self.g._draw_radio_profile_picker("meshtastic")
        self.assertTrue(any("USA RF/antenna: TEST PENDING" in line for line in self.text()))
        self.assertEqual(self.g.events, [])


if __name__ == "__main__":
    unittest.main()
