"""The automatic map uses fresh measured GPS, independently of manual map pan."""

import ast
import copy
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


SOURCE = Path(__file__).resolve().parents[1] / "watchdogs" / "app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
GAME = next(node for node in TREE.body if isinstance(node, ast.ClassDef)
            and node.name == "WatchDogsGame")
METHODS = [copy.deepcopy(node) for node in GAME.body
           if isinstance(node, ast.FunctionDef)
           and node.name in ("_restore_map_view", "_auto_map_position", "_poll_auto_map")]
ENV = dict(Path=Path, logging=logging, __name__=__name__, TileRenderer=Mock())
exec(compile(ast.fix_missing_locations(ast.Module(
    body=[ast.ClassDef(name="MapGame", bases=[], keywords=[],
                       decorator_list=[], body=METHODS)],
    type_ignores=[])), str(SOURCE), "exec"), ENV)


class AutoMapGpsTests(unittest.TestCase):
    def setUp(self):
        self.controller = SimpleNamespace(poll=Mock(return_value=False))
        self.gps = SimpleNamespace(wardrive_sample=Mock(
            return_value={"lat": 36.215, "lon": -6.17}))
        self.game = ENV["MapGame"]()
        self.game.__dict__.update(
            _auto_map=self.controller, _gps_enabled=True, gps=self.gps,
            gps_fix=True, player_lat=51.1, player_lon=17.9,
            _turf_portal=object(), _map_downloading=False,
            _app_dir="/isolated-auto-map-test", tile_renderer=Mock())

    def poll(self):
        self.game._poll_auto_map()

    def test_manual_pan_cannot_change_the_requested_gps_neighborhood(self):
        self.poll()
        self.controller.poll.assert_called_once_with(
            36.215, -6.17, True, portal=self.game._turf_portal,
            manual_tiles_busy=False)
        self.assertEqual((self.game.player_lat, self.game.player_lon), (51.1, 17.9))

    def test_stale_display_fix_cannot_start_requests_without_a_measured_sample(self):
        self.gps.wardrive_sample.return_value = None
        self.poll()
        self.controller.poll.assert_called_once_with(
            0.0, 0.0, False, portal=self.game._turf_portal,
            manual_tiles_busy=False)

    def test_disabled_gps_cannot_reuse_a_previous_fix(self):
        self.game._gps_enabled = False
        self.poll()
        self.gps.wardrive_sample.assert_not_called()
        self.assertIs(self.controller.poll.call_args.args[2], False)

    def test_missing_receiver_still_drains_finished_map_work(self):
        del self.game.gps
        self.controller.poll.return_value = True
        self.poll()
        self.assertIs(self.controller.poll.call_args.args[2], False)
        self.game.tile_renderer.reload_manifest.assert_called_once_with()

    def test_manual_download_is_still_passed_to_the_worker_guard(self):
        self.game._map_downloading = True
        self.poll()
        self.assertTrue(self.controller.poll.call_args.kwargs["manual_tiles_busy"])

    def test_rabbit_reads_continue_without_gps_or_an_enabled_auto_map_controller(self):
        self.game._white_rabbit = SimpleNamespace(poll=Mock())
        self.game._auto_map = None
        self.game._gps_enabled = False
        self.poll()
        self.game._white_rabbit.poll.assert_called_once_with(portal=self.game._turf_portal)
        self.gps.wardrive_sample.assert_not_called()

    def test_a_failed_rabbit_poll_does_not_block_measured_gps_map_work(self):
        self.game._white_rabbit = SimpleNamespace(poll=Mock(side_effect=ValueError()))
        self.game._white_rabbit_failed = False
        with self.assertLogs(__name__, level="WARNING") as captured:
            self.poll()
            self.poll()
        self.assertEqual(len(captured.output), 1)
        self.assertEqual(self.controller.poll.call_count, 2)
        self.assertTrue(self.controller.poll.call_args.args[2])

    def test_cached_startup_view_is_not_a_fresh_gps_measurement(self):
        self.controller.enabled = True
        self.controller.cached_center = (40.25, -4.5)
        self.game.gps_fix = False
        self.gps.wardrive_sample.return_value = None
        self.game._restore_map_view()
        self.assertEqual((self.game.player_lat, self.game.player_lon), (40.25, -4.5))
        self.assertFalse(self.game.gps_fix)
        self.poll()
        self.assertIs(self.controller.poll.call_args.args[2], False)

    def test_disabled_auto_map_does_not_replace_the_existing_default_view(self):
        self.controller.enabled = False
        self.controller.cached_center = (40.25, -4.5)
        self.game._restore_map_view()
        self.assertEqual((self.game.player_lat, self.game.player_lon), (51.1, 17.9))


if __name__ == "__main__":
    unittest.main()
