"""Required map credits: production draw methods, fake graphics, no devices/IO."""

import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "watchdogs" / "app.py"
CREDIT = "(c) Stadia Maps | (c) OpenMapTiles | (c) OpenStreetMap"


class Graphics:
    def __init__(self):
        self.calls = []

    def text(self, *args):
        self.calls.append(("builtin_text", args))

    def rect(self, *args):
        self.calls.append(("rect", args))

    def __getattr__(self, name):
        return lambda *args: self.calls.append((name, args))


class DrawRecorder:
    def __getattr__(self, name):
        if name.startswith("_draw_"):
            return lambda *args: self.graphics.calls.append((name, args))
        return False


def harness(*, has_tiles=True, drawn=True):
    """Compile unchanged production bodies without importing hardware managers."""
    graphics = Graphics()
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    env = {"pyxel": graphics, "W": 640, "H": 360, "HUD_TOP": 16,
           "TERM_Y": 234, "C_HUD_BG": 0, "C_TEXT": 7,
           "C_HACK_CYAN": 3, "DrawRecorder": DrawRecorder}
    capture = next(node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name)
                           and target.id == "_PYXEL_BUILTIN_TEXT"
                           for target in node.targets))
    exec(compile(ast.Module(body=[copy.deepcopy(capture)], type_ignores=[]),
                 str(SOURCE), "exec"), env)
    game = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                and node.name == "WatchDogsGame")
    methods = [copy.deepcopy(node) for node in game.body
               if isinstance(node, ast.FunctionDef)
               and node.name in {"_draw_inner", "_draw_map_attribution"}]
    if len(methods) != 2:
        raise AssertionError("Missing production map draw/attribution method")
    cls = ast.ClassDef(name="GameHarness", bases=[ast.Name(id="DrawRecorder", ctx=ast.Load())],
                       keywords=[], body=methods, decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
                 str(SOURCE), "exec"), env)
    instance = env["GameHarness"]()
    instance.graphics = graphics
    instance.proj = object()
    instance._plugins = []
    instance._clusters = []
    instance._auto_map = instance._white_rabbit = instance._solo_track = None
    instance._game_update_notice = None
    instance.tile_renderer = SimpleNamespace(has_tiles=lambda: has_tiles,
                                             draw=lambda *args: drawn)
    return graphics, instance


class MapAttributionTests(unittest.TestCase):
    def test_no_raster_does_not_claim_stadia(self):
        graphics, game = harness()
        game._draw_map_attribution(False)
        self.assertEqual(graphics.calls, [])

    def test_credit_is_builtin_ascii_and_fits_contrasting_map_box(self):
        graphics, game = harness()
        # The app globally substitutes Spleen; required compact credits must
        # retain the original builtin 4x6 function captured before that patch.
        graphics.text = lambda *args: self.fail("Used globally patched UI font")
        game._draw_map_attribution(True)
        rectangle, label = graphics.calls
        self.assertEqual(rectangle[0], "rect")
        self.assertEqual(label, ("builtin_text", (4, 234 - 16, CREDIT, 7)))
        x, y, width, height, background = rectangle[1]
        tx, ty, text, foreground = label[1]
        self.assertTrue(text.isascii())
        self.assertNotEqual(background, foreground)
        self.assertLessEqual(x, tx)
        self.assertLessEqual(y, ty)
        self.assertGreaterEqual(x + width, tx + len(text) * 4)
        self.assertGreaterEqual(y + height, ty + 6)
        self.assertGreaterEqual(y, 16)
        self.assertLessEqual(y + height, 234 - 8)  # separate cluster-hint row
        self.assertLessEqual(x + width, 640)

    def test_actual_main_draw_credits_only_successfully_drawn_tiles(self):
        for available, drawn in ((True, True), (True, False), (False, False)):
            with self.subTest(available=available, drawn=drawn):
                graphics, game = harness(has_tiles=available, drawn=drawn)
                game._draw_inner()
                labels = [args for name, args in graphics.calls
                          if name == "builtin_text" and args[2] == CREDIT]
                self.assertEqual(len(labels), int(available and drawn))
                coastlines = [name for name, _ in graphics.calls
                              if name == "_draw_coastlines"]
                self.assertEqual(bool(coastlines), not (available and drawn))

    def test_live_overlays_do_not_overpaint_required_credit(self):
        graphics, game = harness()
        game._draw_inner()
        names = [name for name, _ in graphics.calls]
        credit_index = names.index("builtin_text")
        for overlay in ("_draw_player", "_draw_markers", "_draw_glitch", "_draw_scanlines"):
            self.assertGreater(credit_index, names.index(overlay))

    def test_repeated_cached_draw_keeps_credit_without_gps_or_network(self):
        graphics, game = harness()
        # No GPS/client/network object is supplied: attribution follows actual
        # drawing, including repeated cache-backed frames, not a live request.
        game._draw_inner()
        game._draw_inner()
        self.assertEqual(sum(name == "builtin_text" and args[2] == CREDIT
                             for name, args in graphics.calls), 2)

    def test_nonmap_screen_does_not_draw_map_credit(self):
        graphics, game = harness()
        game._mt_screen = True
        game._draw_inner()
        self.assertIn("_draw_mt_screen", [name for name, _ in graphics.calls])
        self.assertFalse(any(name == "builtin_text" for name, _ in graphics.calls))


if __name__ == "__main__":
    unittest.main()
