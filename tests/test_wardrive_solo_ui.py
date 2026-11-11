"""SOLO menu preparation: real handlers and draw calls, no radio or Pyxel app."""
import ast
import copy
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class Drawing:
    frame_count = 42

    def __init__(self):
        self.keys = set()
        self.calls = []

    def btnp(self, key):
        return key in self.keys

    def __getattr__(self, name):
        if name.startswith("KEY_"):
            return name
        return lambda *args: self.calls.append((name, args))


SOURCE = Path(__file__).resolve().parents[1] / "watchdogs" / "app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
PX = Drawing()
ENV = {"pyxel": PX, "math": math}
for node in TREE.body:
    if isinstance(node, ast.Assign):
        try:
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), ENV)
        except (NameError, AttributeError, TypeError):
            pass
    if isinstance(node, ast.FunctionDef) and node.name == "_menu_tab_layout":
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), ENV)

METHODS = {"_handle_python_attack", "_is_running", "_solo_selected", "_set_solo_selection",
           "_update_solo_profile_picker", "_draw_solo_profile_picker",
           "_draw_lora_protocol_picker", "_draw_menu", "_draw_menu_hacker"}
APP = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "WatchDogsGame")
HARNESS = ast.ClassDef(name="Game", bases=[], keywords=[], decorator_list=[], body=[
    copy.deepcopy(n) for n in APP.body if isinstance(n, ast.FunctionDef) and n.name in METHODS])
exec(compile(ast.fix_missing_locations(ast.Module(body=[HARNESS], type_ignores=[])),
             str(SOURCE), "exec"), ENV)
Game = ENV["Game"]


class WardriveSoloUI(unittest.TestCase):
    def setUp(self):
        PX.keys.clear()
        PX.calls.clear()
        self.game = Game()
        self.game._solo = None
        self.game._solo_recorder = SimpleNamespace(failed=False)
        self.game._solo_profile = "full"
        self.game._solo_profile_sel = 0
        self.game._solo_profile_picker = False
        self.game.menu_open = True
        self.game.msg = Mock()

    def test_sniff_keys_commands_and_state_are_preserved(self):
        cats = dict(ENV["MENU_CATS"])
        self.assertEqual(cats["SNIFF"][:2], [
            ("1", "WiFi Sniff", "scan_networks", "wardriving", None),
            ("2", "BT Sniff", "scan_bt", "bt_scanning", None)])
        names = [name for name, _items in ENV["MENU_CATS"]]
        self.assertEqual(names[names.index("SNIFF") + 1], "WARDRIVING")
        self.assertEqual([row[3] for row in cats["WARDRIVING"]],
                         ["solo_wifi", "solo_ble", "piglet_collection"])
        self.assertEqual(cats["WARDRIVING"][2],
                         ("3", "Piglet Collection", "_piglet_collection", "piglet_collection", None))

    def test_all_eight_full_tab_labels_fit_original_panel(self):
        cats = ENV["MENU_CATS"] + [("PLUGINS", [])]
        layout = ENV["_menu_tab_layout"](cats, 310)
        self.assertEqual(layout[0][0], 0)
        self.assertEqual(sum(width for offset, width in layout), 310)
        for (label, _), (offset, width) in zip(cats, layout):
            self.assertGreaterEqual(width, len(label) * 5 + 6)
            self.assertLessEqual(offset + width, 310)

    def test_wifi_opens_picker_without_starting_legacy_scans(self):
        self.game._toggle_wardrive = Mock(side_effect=AssertionError("legacy scheduler"))
        self.game._send = Mock(side_effect=AssertionError("UART before firmware contract"))
        self.game._handle_python_attack("_solo_wifi", "solo_wifi", "WiFi Wardrive", [])
        self.assertTrue(self.game._solo_profile_picker)
        self.assertFalse(self.game.menu_open)
        self.assertFalse(self.game._is_running("solo_wifi"))

    def test_unsupported_firmware_stays_off(self):
        self.game._handle_python_attack("_solo_ble", "solo_ble", "BLE Wardrive", [])
        self.assertFalse(self.game._solo_profile_picker)
        self.assertFalse(self.game._is_running("solo_ble"))
        self.assertIn("firmware extension", self.game.msg.call_args.args[0])

    def test_escape_has_no_start_or_profile_side_effect(self):
        self.game._solo_profile_picker = True
        self.game._set_solo_selection = Mock()
        PX.keys.add(PX.KEY_ESCAPE)
        self.game._update_solo_profile_picker()
        self.assertFalse(self.game._solo_profile_picker)
        self.assertEqual(self.game._esc_consumed_frame, PX.frame_count)
        self.assertEqual(self.game._solo_profile, "full")
        self.game._set_solo_selection.assert_not_called()

    def test_selection_and_confirmed_on_are_separate(self):
        adapter = SimpleNamespace(confirmed_wifi=False, confirmed_ble=True, request=Mock(return_value=True))
        self.game._solo = adapter
        PX.keys.add(PX.KEY_RIGHT)
        self.game._update_solo_profile_picker()
        PX.keys = {PX.KEY_RETURN}
        self.game._update_solo_profile_picker()
        adapter.request.assert_called_once_with(wifi=True, ble=None, profile="kg")
        self.assertFalse(self.game._is_running("solo_wifi"))
        self.assertTrue(self.game._is_running("solo_ble"))
        adapter.confirmed_wifi = True
        self.assertTrue(self.game._is_running("solo_wifi"))

    def test_wifi_off_and_ble_toggle_keep_other_selection(self):
        adapter = SimpleNamespace(confirmed_wifi=True, confirmed_ble=False, request=Mock(return_value=True))
        self.game._solo = adapter
        self.game._handle_python_attack("_solo_wifi", "solo_wifi", "WiFi Wardrive", [])
        adapter.request.assert_called_once_with(wifi=False, ble=None, profile="full")
        self.assertFalse(self.game._solo_profile_picker)
        adapter.request.reset_mock()
        self.game._handle_python_attack("_solo_ble", "solo_ble", "BLE Wardrive", [])
        adapter.request.assert_called_once_with(wifi=None, ble=True, profile="full")

    def test_picker_reuses_lora_geometry_and_fits_above_output(self):
        self.game._lora_protocol_sel = 0
        self.game._draw_lora_protocol_picker()
        old_geometry = [(name, args) for name, args in PX.calls if name != "text"]
        PX.calls.clear()
        self.game._draw_solo_profile_picker()
        self.assertEqual(old_geometry, [(name, args) for name, args in PX.calls if name != "text"])
        for name, args in PX.calls:
            if name == "text":
                x, y, text, colour = args
                self.assertGreaterEqual(x, 100)
                self.assertLessEqual(x + len(text) * 5, 540)
                self.assertLess(y + 8, ENV["TERM_Y"])

    def test_existing_category_sprites_remain_attached_by_name(self):
        for label in ("ATTACK", "ADDONS", "SYSTEM", "WARDRIVING", "CRISIS"):
            with self.subTest(label=label):
                PX.calls.clear()
                self.game.menu_cat = next(i for i, row in enumerate(ENV["MENU_CATS"]) if row[0] == label)
                self.game._current_menu_sprite = "SNIFF"
                self.game._menu_sprites = {key: key + ".png" for key in ("ATTACK", "ADDONS", "SYSTEM", "WARDRIVING")}
                self.game._hacker_sprite_ok = False
                self.game._hacker_w = 0
                image = SimpleNamespace(load=Mock())
                saved = ENV["pyxel"]
                ENV["pyxel"] = SimpleNamespace(image=lambda bank: image, frame_count=42, **{
                    name: getattr(PX, name) for name in ("rect", "circ", "line", "pset", "text")})
                try:
                    self.game._draw_menu_hacker()
                finally:
                    ENV["pyxel"] = saved
                expected = "SYSTEM" if label == "CRISIS" else label
                image.load.assert_called_once_with(0, 0, expected + ".png")


if __name__ == "__main__":
    unittest.main()
