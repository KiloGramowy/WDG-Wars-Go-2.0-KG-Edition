"""Measured GPS trail and real WDG projection; no radio, tiles, storage or GUI."""
import ast
from copy import deepcopy
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from watchdogs.wardrive_track import WardriveTrack


SOURCE = Path(__file__).parents[1] / "watchdogs" / "app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
NAMES = {"W", "H", "HUD_TOP", "HUD_BOT", "TERM_H", "MAP_H", "TERM_Y", "ZOOM_LEVELS"}
STATEMENTS = [node for node in TREE.body if
    (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in NAMES
        for outer in node.targets for target in ast.walk(outer)))
    or (isinstance(node, ast.ClassDef) and node.name == "MapProjection")]
ENV = {}
exec(compile(ast.Module(body=STATEMENTS, type_ignores=[]), str(SOURCE), "exec"), ENV)
MapProjection = ENV["MapProjection"]
VIEWPORT = (0, ENV["HUD_TOP"], ENV["W"] - 1, ENV["TERM_Y"] - 1)


def sample(observed, lat=36.0, lon=-6.0, **extra):
    return dict(lat=lat, lon=lon, altitude=10.0, quality=1, sats=8,
                hdop=1.0, observed_at=observed, utc_seconds=43200 + observed,
                sentence="GGA", **extra)


def projection():
    value = MapProjection()
    value.center_lat, value.center_lon, value.zoom = 36.0, -6.0, 13
    return value


class WardriveTrackTests(unittest.TestCase):
    def setUp(self):
        self.track = WardriveTrack()
        self.proj = projection()

    def add(self, observed, lat=36.0, lon=-6.0, **kwargs):
        return self.track.add_sample(sample(observed, lat, lon), now=observed, **kwargs)

    def lines(self):
        return self.track.screen_segments(self.proj, VIEWPORT)

    def test_short_measured_motion_below_thirty_metres_is_retained(self):
        for i in range(3):
            self.assertTrue(self.add(i + 1, lat=36 + i * 0.00005))
        self.assertEqual(self.track.point_count, 3)
        self.assertEqual([row[0] for row in self.track.iter_points()], [36, 36.00005, 36.0001])
        lines = self.lines()
        self.assertTrue(lines)
        self.assertEqual(lines[0][:2], self.proj.geo_to_screen(36, -6))
        self.assertEqual(lines[-1][2:], self.proj.geo_to_screen(36.0001, -6))

    def test_polling_identity_stationary_and_out_of_order_samples_do_not_add_edges(self):
        value = sample(1)
        self.assertTrue(self.track.add_sample(value, now=1))
        for now in (1.1, 1.5, 1.99):
            self.assertFalse(self.track.add_sample(value, now=now))
        self.assertEqual(self.track.received_samples, 1)
        self.assertFalse(self.add(2))
        self.assertEqual(self.track.stationary_samples, 1)
        self.assertFalse(self.track.add_sample(sample(1.9, lat=36.001), now=2))
        self.assertTrue(self.add(3, lat=36.0001))
        self.assertEqual(self.track.point_count, 2)
        self.assertEqual(self.track.received_samples, 3)
        self.assertEqual(len(self.lines()), 1)

    def test_start_without_fix_then_recovery_needs_no_scan_completion(self):
        for _ in range(4):
            self.assertFalse(self.track.add_sample(None, now=1))
        self.assertEqual(self.track.point_count, 0)
        self.assertTrue(self.add(2))
        self.assertEqual(self.lines(), ())
        self.assertTrue(self.add(3, lat=36.0001))
        self.assertEqual(len(self.lines()), 1)

    def test_loss_cache_and_backfill_do_not_connect_recovery(self):
        self.add(1)
        self.add(2, lat=36.0001)
        self.track.add_sample(None, now=3)
        for extra in ({"source": "cache"}, {"cached": True}, {"backfill": True},
                      {"position_source": "receiver_assignment"}):
            self.assertFalse(self.track.add_sample(sample(3, lat=37, **extra), now=3))
        self.add(4, lat=36.001)
        self.add(5, lat=36.0011)
        self.assertEqual([row[3] for row in self.track.iter_points()], [True, False, True, False])
        self.assertEqual(len(self.lines()), 2)

    def test_off_preserves_history_but_breaks_next_active_segment(self):
        self.add(1)
        self.add(2, lat=36.0001)
        self.assertFalse(self.add(3, lat=36.0002, active=False))
        self.add(4, lat=36.0003)
        self.assertEqual(self.track.point_count, 3)
        self.assertEqual([row[3] for row in self.track.iter_points()], [True, False, True])
        self.assertEqual(len(self.lines()), 1)

    def test_expired_sample_interval_breaks_track_even_if_ui_missed_none_poll(self):
        self.add(1)
        self.add(2, lat=36.0001)
        self.add(4, lat=36.0002)
        self.assertEqual([row[3] for row in self.track.iter_points()], [True, False, True])
        self.assertEqual(len(self.lines()), 1)

    def test_stale_future_invalid_and_unmeasured_values_are_rejected(self):
        invalid = [sample(1), sample(4), sample(math.nan), sample(3, lat=91),
                   sample(3, lon=math.inf), dict(lat=36, lon=-6, observed_at=3),
                   None, {}, "not a sample"]
        for value in invalid:
            with self.subTest(value=value):
                self.assertFalse(self.track.add_sample(value, now=3))
        self.assertEqual(self.track.point_count, 0)
        self.assertTrue(self.track.add_sample(dict(sample(3), sentence="RMC"), now=3))

    def test_input_mapping_and_source_observations_are_never_changed(self):
        value = sample(10)
        original = deepcopy(value)
        self.track.add_sample(value, now=10)
        self.track.screen_segments(self.proj, VIEWPORT)
        self.assertEqual(value, original)
        self.track.clear()
        self.assertEqual(self.track.point_count, 0)
        self.assertEqual(self.track.decimation_count, 0)
        self.assertTrue(self.track.add_sample(value, now=10))

    def test_visual_budget_preserves_endpoints_and_latest_half(self):
        track = WardriveTrack(max_points=16)
        for i in range(17):
            track.add_sample(sample(i, lat=36 + i * 0.00001), now=i)
        points = list(track.iter_points())
        self.assertLessEqual(len(points), 16)
        self.assertEqual(points[0][2], 0)
        self.assertEqual(points[-1][2], 16)
        self.assertEqual([row[2] for row in points[-9:]], list(range(8, 17)))
        self.assertEqual(track.decimation_count, 17 - track.point_count)
        self.assertEqual(track.received_samples, 17)
        self.assertEqual(track.dropped_segments, 0)

    def test_many_short_segments_stay_bounded_without_false_connections(self):
        track = WardriveTrack(max_points=16)
        for i in range(100):
            track.break_segment()
            track.add_sample(sample(i, lat=36 + i * 0.000001), now=i)
            self.assertLessEqual(track.point_count, 16)
        self.assertGreater(track.dropped_segments, 0)
        self.assertEqual(track.decimation_count, 100 - track.point_count)
        self.assertTrue(all(row[3] for row in track.iter_points()))
        self.assertEqual(track.screen_segments(self.proj, VIEWPORT), ())
        self.assertEqual(list(track.iter_points())[-1][2], 99)

    def test_visual_compaction_does_not_join_separate_segments(self):
        track = WardriveTrack(max_points=16)
        for i in range(90):
            if i % 10 == 0:
                track.break_segment()
            track.add_sample(sample(i, lat=36 + i * 0.000001), now=i)
            self.assertLessEqual(track.point_count, 16)
        points = list(track.iter_points())
        for previous, current in zip(points, points[1:]):
            if not current[3]:
                self.assertEqual(int(previous[2]) // 10, int(current[2]) // 10)

    def test_shared_map_projection_has_no_tile_dependency_and_invalidates_on_pan_zoom(self):
        self.add(1)
        self.add(2, lat=36.0001)
        first = self.lines()
        self.proj.tile_renderer = SimpleNamespace(has_tiles=Mock(side_effect=AssertionError("No tile dependency")))
        self.assertIs(self.lines(), first)
        self.proj.center_lat += 0.0002
        second = self.lines()
        self.assertNotEqual(first, second)
        self.proj.zoom -= 1
        self.assertNotEqual(second, self.lines())
        self.proj.tile_renderer.has_tiles.assert_not_called()

    def test_full_visual_budget_bounds_projection_work_and_cached_frames_reuse_it(self):
        for i in range(4096):
            self.add(i, lat=36 + (i % 200 - 100) * 0.00001,
                     lon=-6 + (-0.003 if i % 2 else 0.003))
        self.proj.geo_to_screen = Mock(wraps=self.proj.geo_to_screen)
        lines = self.lines()
        self.assertEqual(self.proj.geo_to_screen.call_count, 4096)
        self.assertLessEqual(len(lines), 4095)
        for _ in range(100):
            self.assertIs(self.lines(), lines)
        self.assertEqual(self.proj.geo_to_screen.call_count, 4096)
        self.proj.center_lon += 0.00001
        self.lines()
        self.assertEqual(self.proj.geo_to_screen.call_count, 8192)

    def test_clip_keeps_hud_output_out_and_renders_edges_with_both_ends_outside(self):
        track = WardriveTrack()
        track.add_sample(sample(1, lat=35.98, lon=-6.02), now=1)
        track.add_sample(sample(2, lat=36.02, lon=-5.98), now=2)
        lines = track.screen_segments(self.proj, VIEWPORT)
        self.assertEqual(len(lines), 1)
        for x0, y0, x1, y1 in lines:
            for x, y in ((x0, y0), (x1, y1)):
                self.assertTrue(VIEWPORT[0] <= x <= VIEWPORT[2])
                self.assertTrue(VIEWPORT[1] <= y <= VIEWPORT[3])

    def test_screen_pixel_decimation_preserves_raw_short_movements_and_direction_changes(self):
        for i in range(6):
            self.add(i, lat=36 + i * 0.0001)
        self.assertEqual(self.track.point_count, 6)
        self.assertEqual(len(self.lines()), 1)
        self.add(6, lat=36.0002)
        self.assertEqual(len(self.lines()), 2)
        self.assertEqual(self.track.point_count, 7)

    def test_longitude_seam_never_draws_a_false_world_crossing(self):
        self.track.add_sample(sample(1, lat=0, lon=179.9), now=1)
        self.track.add_sample(sample(2, lat=0, lon=-179.9), now=2)
        self.proj.center_lat, self.proj.center_lon, self.proj.zoom = 0, 0, 0
        self.assertEqual(self.lines(), ())
        self.proj.center_lon = 180
        self.proj.zoom = 8
        self.assertEqual(len(self.lines()), 1)


if __name__ == "__main__":
    unittest.main()
