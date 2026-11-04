"""Native footer rendering with synthetic session observations; no graphical/RF I/O."""
import ast
import copy
from types import SimpleNamespace
import unittest

from test_meshtastic_ui import ENV, GameHarness, PX, SOURCE, TREE, game


native_game = next(node for node in TREE.body
                   if isinstance(node, ast.ClassDef) and node.name == "WatchDogsGame")
hud_method = next(node for node in native_game.body
                  if isinstance(node, ast.FunctionDef) and node.name == "_draw_hud_bottom")
namespace = dict(ENV, GameHarness=GameHarness)
harness = ast.ClassDef(name="HudGame", bases=[ast.Name(id="GameHarness", ctx=ast.Load())],
    keywords=[], decorator_list=[], body=[copy.deepcopy(hud_method)])
exec(compile(ast.fix_missing_locations(ast.Module(body=[harness], type_ignores=[])),
             str(SOURCE), "exec"), namespace)
HudGame = namespace["HudGame"]


class SessionObservations:
    """Generate distinct synthetic identities without allocating a million-entry set."""
    def __init__(self, meshcore=0, meshtastic=0):
        self.counts = (("meshcore", meshcore), ("meshtastic", meshtastic))

    def __iter__(self):
        for protocol, count in self.counts:
            for index in range(count):
                yield protocol, str(index)


def hud_game():
    g = game()
    g.__class__ = HudGame
    PX.frame_count = 42
    g.ble_devices, g.wifi_networks = [], []
    g.wifi_scanning = g.ble_scanning = g.sniffing = g.capturing_hs = False
    g.state.submitted_forms = 0
    g.state.evil_twin_captured_data = []
    g.state.portal_running = g.state.evil_twin_running = False
    g._sdr = SimpleNamespace(running=False, mode="", aircraft={}, total_sensors_seen=0,
                            visible_aircraft=lambda: [], emergency_active=False)
    g.gps = SimpleNamespace(available=False)
    g.gps_sats_vis = 0
    g.player_lat, g.player_lon = 51.1, 17.9
    return g


def draw(g):
    PX.calls.clear()
    g._draw_hud_bottom()
    return [args for operation, args in PX.calls if operation == "text"]


def labels(calls):
    return [args[2] for args in calls]


class MeshHudTests(unittest.TestCase):
    def test_both_session_counters_are_visible_with_radio_off_and_empty_map(self):
        g = hud_game()
        calls = draw(g)
        self.assertIn("MC:0", labels(calls))
        self.assertIn("MT:0", labels(calls))
        self.assertIn("LoRa:OFF", labels(calls))
        self.assertIn("GPS offline", labels(calls))

    def test_counts_observed_identities_instead_of_history_packets_or_positions(self):
        g = hud_game()
        g._mc_nodes = [dict(id=f"{index:08x}", name="Saved") for index in range(111)]
        g._mt_view["nodes"] = [dict(id="!00000001", name="Saved MT")]
        g.state.mc_nodes, g.state.mt_nodes = 111, 1
        g._loot_totals.update(mc_nodes=1000, mt_nodes=2000)
        self.assertIn("MC:0", labels(draw(g)))
        for protocol, node_id in (("meshcore", "00000001"),
                                  ("meshcore", "00000100"),
                                  ("meshtastic", "!00000001")):
            for _ in range(2):
                g._register_mesh_node(protocol, node_id, "Client", "Observed",
                                      0, 0, -90, -3)
        self.assertEqual(g.markers, [])  # None of these observations has a position.
        calls = draw(g)
        self.assertIn("MC:2", labels(calls))
        self.assertIn("MT:1", labels(calls))
        self.assertNotIn("MC:112", labels(calls))
        self.assertNotIn("MT:2000", labels(calls))

    def test_new_mesh_observation_uses_existing_white_growth_flash(self):
        g = hud_game()
        draw(g)
        PX.frame_count = 43
        g._mesh_session_nodes.add(("meshcore", "00000001"))
        calls = draw(g)
        counter = next(args for args in calls if args[2] == "MC:1")
        self.assertEqual(counter[3], 7)
        self.assertTrue(any(args[2] == "+" and args[0] >= counter[0] for args in calls))
        PX.frame_count = 60
        calls = draw(g)
        self.assertNotEqual(next(args[3] for args in calls if args[2] == "MC:1"), 7)
        self.assertFalse(any(args[2] == "+" for args in calls))

    def test_large_session_and_saved_totals_leave_radio_and_gps_legible(self):
        g = hud_game()
        g._loot_totals.update(bt=17578, wifi=17528, hs=123, passwords=1234)
        g._mesh_session_nodes = SessionObservations(meshcore=10000, meshtastic=10000)
        g.gps_fix = True
        g._mt.running = g._mt.ready = True
        calls = draw(g)
        names = labels(calls)
        self.assertIn("MC:10000", names)
        self.assertIn("MT:10000", names)
        self.assertIn("LoRa:Meshtastic", names)
        self.assertIn("51.10000N 17.90000E", names)
        counters = sorted((args for args in calls if args[2].split(":")[0]
                           in {"BLE", "WiFi", "HS", "PWD", "PWN", "MC", "MT"}),
                          key=lambda args: args[0])
        self.assertEqual(len(counters), 7)
        for left, right in zip(counters, counters[1:]):
            self.assertLessEqual(left[0] + len(left[2]) * 5, right[0])
        self.assertLessEqual(counters[-1][0] + len(counters[-1][2]) * 5, 434)
        lora = next(args for args in calls if args[2] == "LoRa:Meshtastic")
        gps = next(args for args in calls if args[2] == "51.10000N 17.90000E")
        self.assertLessEqual(lora[0] + len(lora[2]) * 5, gps[0] - 13)
        self.assertLessEqual(gps[0] + len(gps[2]) * 5, 640)

    def test_long_scanner_summary_is_visibly_bounded_before_radio(self):
        g = hud_game()
        g._mesh_session_nodes = SessionObservations(meshcore=10000, meshtastic=10000)
        g.wifi_scanning = g.ble_scanning = g.sniffing = g.capturing_hs = True
        g.state.portal_running = g.state.evil_twin_running = True
        g._sdr.running, g._sdr.mode = True, "adsb433emerg"
        g._sdr.aircraft = {str(index): None for index in range(100)}
        g._sdr.visible_aircraft = lambda: [None] * 50
        g._sdr.total_sensors_seen = 100000
        g._sdr.emergency_active = g._muted = True
        calls = draw(g)
        summary = next(args for args in calls if args[2].startswith("WiFi "))
        self.assertTrue(summary[2].endswith("..."))
        self.assertLessEqual(summary[0] + len(summary[2]) * 5, 434)
        self.assertIn("LoRa:OFF", labels(calls))
        self.assertIn("GPS offline", labels(calls))

    def test_help_keeps_full_legend_when_it_fits_and_keys_when_crowded(self):
        g = hud_game()
        g.menu_open = False
        text = "".join(labels(draw(g)))
        for word in ("[TAB]", "Menu", "[`]", "Loot", "[S]", "Stop"):
            self.assertIn(word, text)
        g._loot_totals.update(bt=17578, wifi=17528, hs=123, passwords=1234)
        g._mesh_session_nodes = SessionObservations(meshcore=10000, meshtastic=10000)
        calls = draw(g)
        text = "".join(labels(calls))
        for key in ("[TAB]", "[`]", "[S]"):
            self.assertIn(key, text)
        for word in ("Menu", "Loot", "Stop"):
            self.assertNotIn(word, text)
        for x, _, text, _ in calls:
            if "[" in text:
                self.assertLessEqual(x + len(text) * 5, 434)

    def test_extreme_mesh_counts_have_explicit_lower_bound_display(self):
        g = hud_game()
        g._mesh_session_nodes = SessionObservations(meshcore=1000000, meshtastic=1000001)
        calls = draw(g)
        self.assertIn("MC:999999+", labels(calls))
        self.assertIn("MT:999999+", labels(calls))
        for x, _, text, _ in calls:
            if text.startswith(("MC:", "MT:")):
                self.assertLessEqual(x + len(text) * 5, 434)

    def test_large_totals_and_all_tools_do_not_cover_searching_gps_or_ready_radio(self):
        g = hud_game()
        g._loot_totals.update(bt=10**12, wifi=10**12, hs=10**12, passwords=10**12)
        g._mesh_session_nodes = SessionObservations(meshcore=1000000, meshtastic=1000000)
        g._mt.running = g._mt.ready = True
        g.gps.available, g.gps_sats_vis = True, 12
        g.wifi_scanning = g.ble_scanning = g.sniffing = g.capturing_hs = True
        g.state.portal_running = g.state.evil_twin_running = True
        g._muted = True
        calls = draw(g)
        lora = next(args for args in calls if args[2] == "LoRa:Meshtastic")
        gps = next(args for args in calls if args[2] == "Waiting fix Vis:12")
        self.assertLessEqual(lora[0] + len(lora[2]) * 5, gps[0] - 19)
        for x, _, text, _ in calls:
            if text != lora[2] and text != gps[2]:
                self.assertLessEqual(x + len(text) * 5, lora[0] - 6)
        for key in ("BLE", "WiFi", "HS", "PWD", "MC", "MT"):
            self.assertIn(key + ":999999+", labels(calls))
        self.assertEqual(g._loot_totals["wifi"], 10**12)


if __name__ == "__main__":
    unittest.main()
