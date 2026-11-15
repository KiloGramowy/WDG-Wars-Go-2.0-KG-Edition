"""Offline-first auto map/turf scheduling, geometric cache and renderer tests.

Every network response is synthetic. No hardware, live API or player data.
"""

import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from watchdogs.auto_map import (
    AutoMap, _distance_km, _gangs_by_id, _palette_color,
    _parse_cells, _region, _write_json,
)
from watchdogs import tile_manager

REAL_DOWNLOAD_TILES = tile_manager.download_tiles


LAT, LON = 36.215, -6.17


class SyntheticPortal:
    _api_key = "a" * 64

    def __init__(self):
        self.requests = []
        self.payload = {
            "ok": True, "grid_lat": 0.02, "grid_lng": 0.02,
            "gangs": {"7": {"name": "SYNTH GANG", "color": "#30ff90"}},
            "cells": [{"lat": 36.20, "lng": -6.18, "gang_id": 7, "count": 6}],
        }

    def fetch_member_territories(self, bbox, *, zoom=8):
        self.requests.append((bbox, zoom))
        return self.payload


class Projection:
    zoom = 11
    center_lat, center_lon = LAT, LON
    lat_span, lon_span = 0.1, 0.2

    def geo_to_screen(self, lat, lon):
        return (int(320 + (lon - self.center_lon) * 640 / self.lon_span),
                int(100 + (self.center_lat - lat) * 200 / self.lat_span))


class FakePyxel:
    def __init__(self):
        self.calls = []

    def rectb(self, *args):
        self.calls.append(("border", args))

    def rect(self, *args):
        self.calls.append(("fill", args))

    def text(self, *args):
        self.calls.append(("text", args))


class AutoMapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.maps = Path(self.temp.name) / "maps"
        self.controller = AutoMap(self.maps)
        self.addCleanup(self.controller.close)
        self.portal = SyntheticPortal()
        self.downloads = []

        def download(lat, lon, maps_dir, *, radius_km, callback, stop_on_error):
            self.downloads.append((lat, lon, radius_km, stop_on_error))
            Path(maps_dir).mkdir(exist_ok=True, parents=True)
            (Path(maps_dir) / "manifest.json").write_text('{"tile_count":1}')
            callback(100, "done")
            return {"tile_count": 1, "errors": 0}

        self.download_patch = self.enterContext(
            patch("watchdogs.tile_manager.download_tiles", side_effect=download))
        self.key_patch = self.enterContext(
            patch("watchdogs.tile_manager._load_stadia_key", return_value="synthetic-key"))

    def finish(self):
        for thread in (self.controller._tile_thread, self.controller._territory_thread):
            if thread is not None:
                thread.join(3)
                self.assertFalse(thread.is_alive(), "Synthetic map worker did not finish")
        # Drain events but do not launch new work.
        return self.controller.poll(LAT, LON, False, portal=self.portal)

    def start(self):
        self.controller.set_enabled(True)
        self.assertFalse(self.controller.poll(LAT, LON, True, portal=self.portal))
        return self.finish()

    def test_default_is_opt_in_and_creates_no_cache(self):
        with patch("watchdogs.tile_manager.download_tiles") as downloaded:
            self.controller.poll(LAT, LON, True, portal=self.portal)
            downloaded.assert_not_called()
        self.assertFalse(self.controller.enabled)
        self.assertIsNone(self.controller._territory_thread)
        self.assertFalse(self.maps.exists())

    def test_non_object_config_keeps_default_without_preventing_startup(self):
        self.maps.mkdir()
        for value in ([], None, True, 7, "invalid"):
            with self.subTest(config=value):
                (self.maps / "auto_map.json").write_text(json.dumps(value))
                controller = AutoMap(self.maps)
                self.addCleanup(controller.close)
                self.assertFalse(controller.enabled)

    def test_saved_numeric_center_does_not_trigger_another_download(self):
        self.maps.mkdir()
        (self.maps / "manifest.json").write_text('{"tile_count":1}')
        (self.maps / "auto_map.json").write_text(json.dumps({
            "enabled": True, "last_tile_center": [str(LAT), str(LON)]}))
        controller = AutoMap(self.maps)
        self.addCleanup(controller.close)
        with patch("watchdogs.tile_manager.download_tiles") as downloaded:
            controller.poll(LAT, LON, True)
            downloaded.assert_not_called()
        self.assertEqual(controller.cached_center, (LAT, LON))

    def test_completed_download_restores_an_immutable_view_center_without_gps_requests(self):
        self.start()
        restarted = AutoMap(self.maps)
        self.addCleanup(restarted.close)
        self.assertTrue(restarted.enabled)
        self.assertEqual(restarted.cached_center, (LAT, LON))
        self.assertIsInstance(restarted.cached_center, tuple)
        old_requests = len(self.portal.requests)
        old_downloads = len(self.downloads)
        restarted.poll(*restarted.cached_center, False, portal=self.portal)
        self.assertEqual(len(self.portal.requests), old_requests)
        self.assertEqual(len(self.downloads), old_downloads)
        self.assertIsNone(restarted._tile_thread)
        self.assertIsNone(restarted._territory_thread)

    def test_view_center_rejects_missing_manifest_or_invalid_saved_coordinates(self):
        self.maps.mkdir()
        config = self.maps / "auto_map.json"
        config.write_text(json.dumps({"enabled": True, "last_tile_center": [LAT, LON]}))
        missing = AutoMap(self.maps)
        self.addCleanup(missing.close)
        self.assertIsNone(missing.cached_center)
        (self.maps / "manifest.json").write_text('{"tile_count":1}')
        for center in (None, [], [LAT], [LAT, LON, 1], [True, LON], [91, LON],
                       [LAT, -181], [float("nan"), LON], [LAT, "invalid"]):
            with self.subTest(center=center):
                config.write_text(json.dumps({"enabled": True, "last_tile_center": center}))
                before = config.read_bytes()
                invalid = AutoMap(self.maps)
                self.addCleanup(invalid.close)
                self.assertIsNone(invalid.cached_center)
                self.assertEqual(config.read_bytes(), before)

    def test_disabling_downloads_retains_cached_view_information_for_later_opt_in(self):
        self.start()
        self.controller.set_enabled(False)
        restarted = AutoMap(self.maps)
        self.addCleanup(restarted.close)
        self.assertFalse(restarted.enabled)
        self.assertEqual(restarted.cached_center, (LAT, LON))

    def test_numeric_cache_fields_are_usable_after_offline_restart(self):
        self.start()
        key = _region(LAT, LON)
        path = self.controller.territory_dir / ("%d_%d.json" % key)
        value = json.loads(path.read_text())
        for field in ("fetched_at", "grid_lat", "grid_lng"):
            value[field] = str(value[field])
        for field in ("lat", "lng"):
            value["cells"][0][field] = str(value["cells"][0][field])
        path.write_text(json.dumps(value))
        original = path.read_bytes()
        controller = AutoMap(self.maps)
        self.addCleanup(controller.close)
        controller.poll(LAT, LON, True, portal=self.portal, manual_tiles_busy=True)
        self.assertEqual(controller._current_cell(LAT, LON)[0]["gang"], "SYNTH GANG")
        controller.draw(FakePyxel(), Projection(), 640, 16, 234, gps=(LAT, LON))
        self.assertEqual(len(self.portal.requests), 1)
        self.assertEqual(path.read_bytes(), original)

    def test_invalid_cache_cannot_block_reads_or_destroy_saved_bytes(self):
        self.start()
        key = _region(LAT, LON)
        path = self.controller.territory_dir / ("%d_%d.json" % key)
        original = json.loads(path.read_text())
        cases = [dict(original, fetched_at=value) for value in
                 (None, [], {}, True, "invalid", float("nan"))]
        cases += [dict(original, cells=value) for value in
                  ([None], [{"lat": "invalid", "lng": LON}],
                   [{"lat": LAT, "lng": float("inf")}], [None] * 2501)]
        for index, value in enumerate(cases):
            with self.subTest(case=index):
                path.write_text(json.dumps(value))
                before = path.read_bytes()
                self.controller._region_data.clear()
                self.assertTrue(self.controller._read_region(key) is None,
                                "Malformed cache must be ignored")
                self.assertEqual(path.read_bytes(), before)

    def test_enabled_setting_persists_and_off_preserves_downloaded_data(self):
        self.start()
        self.assertTrue(self.controller.enabled)
        old = self.maps / "wdgwars_territories" / ("%d_%d.json" % _region(LAT, LON))
        self.assertTrue(old.is_file())
        self.controller.set_enabled(False)
        self.assertTrue(old.is_file())
        again = AutoMap(self.maps)
        self.assertFalse(again.enabled)
        again.set_enabled(True)
        self.assertTrue(again.enabled)
        again.close()
        self.assertTrue(old.is_file())

    def test_missing_gps_never_starts_background_downloads(self):
        self.controller.set_enabled(True)
        with patch("watchdogs.tile_manager.download_tiles") as downloaded:
            self.controller.poll(LAT, LON, False, portal=self.portal)
            downloaded.assert_not_called()
        self.assertEqual(self.portal.requests, [])
        self.assertIsNone(self.controller._tile_thread)

    def test_one_bbox_fills_nine_adjacent_regions(self):
        self.assertTrue(self.start())
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(self.downloads[0][2:], (10.0, True))
        self.assertEqual(len(self.portal.requests), 1)
        bbox, zoom = self.portal.requests[0]
        self.assertEqual(zoom, 8)
        self.assertLess(bbox[0], LAT)
        self.assertGreater(bbox[2], LAT)
        self.assertEqual(len(list((self.maps / "wdgwars_territories").glob("*.json"))), 9)
        current = self.controller._current_cell(LAT, LON)
        self.assertIsNotNone(current)
        self.assertEqual(current[0]["gang"], "SYNTH GANG")
        self.assertEqual(current[0]["color"], "#30ff90")

    def test_movement_keeps_previous_regions_and_only_fetches_new_strip(self):
        self.start()
        old = set((self.maps / "wdgwars_territories").glob("*.json"))
        self.controller.poll(LAT + 0.12, LON, True, portal=self.portal)
        self.finish()
        self.assertEqual(len(self.portal.requests), 2)
        self.assertEqual(len(self.downloads), 2)
        new = set((self.maps / "wdgwars_territories").glob("*.json"))
        self.assertTrue(old.issubset(new))
        self.assertEqual(len(new), 12)

    def test_same_center_uses_cache_without_new_requests(self):
        self.start()
        self.controller.poll(LAT, LON, True, portal=self.portal)
        self.finish()
        self.assertEqual(len(self.portal.requests), 1)
        self.assertEqual(len(self.downloads), 1)

    def test_offline_or_no_key_retains_existing_territory_cache(self):
        self.start()
        new = AutoMap(self.maps)
        self.addCleanup(new.close)
        old = new._current_cell(LAT, LON)
        self.assertEqual(old[0]["gang"], "SYNTH GANG")
        offline = SyntheticPortal()
        offline._api_key = ""
        new.poll(LAT, LON, True, portal=offline, manual_tiles_busy=True)
        self.assertEqual(offline.requests, [])
        self.assertEqual(new._current_cell(LAT, LON)[0]["gang"], "SYNTH GANG")
        self.assertTrue((self.maps / "manifest.json").is_file())

    def test_stale_cell_refreshes_but_failed_api_keeps_previous_snapshot(self):
        self.start()
        key = _region(LAT, LON)
        path = self.controller.territory_dir / ("%d_%d.json" % key)
        old = json.loads(path.read_text())
        stale = dict(old, fetched_at=0)
        _write_json(path, stale)
        self.controller._region_data.clear()
        self.controller._territory_retry_at = 0
        self.portal.payload = {"ok": False, "cells": []}
        self.controller.poll(LAT, LON, True, portal=self.portal,
                             manual_tiles_busy=True)
        self.finish()
        self.assertEqual(len(self.portal.requests), 2)
        self.assertEqual(json.loads(path.read_text())["fetched_at"], 0)
        self.assertEqual(self.controller._current_cell(LAT, LON)[0]["gang"], "SYNTH GANG")

    def test_absent_stadia_key_does_not_block_territory_fetch(self):
        with patch("watchdogs.tile_manager._load_stadia_key", return_value=""):
            self.controller.set_enabled(True)
            self.controller.poll(LAT, LON, True, portal=self.portal)
            self.finish()
        self.assertEqual(len(self.downloads), 0)
        self.assertEqual(len(self.portal.requests), 1)

    def test_stop_during_fetch_preserves_cache_and_no_new_downloads(self):
        entered = threading.Event()
        release = threading.Event()
        def delayed(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            kwargs["callback"](0, "done")
            return {"errors": 0}
        with patch("watchdogs.tile_manager.download_tiles", side_effect=delayed):
            self.controller.set_enabled(True)
            self.controller.poll(LAT, LON, True, portal=None)
            self.assertTrue(entered.wait(2))
            self.controller.set_enabled(False)
            release.set()
            self.finish()
        self.assertFalse(self.controller.enabled)
        self.assertIsNone(self.controller._last_tile_center)

    def test_compact_gang_lookup_and_fallback_coordinates(self):
        result = SyntheticPortal().payload
        glat, glng, cells = _parse_cells(result)
        self.assertEqual((glat, glng), (0.02, 0.02))
        self.assertEqual(cells[0]["gang"], "SYNTH GANG")
        self.assertEqual(cells[0]["color"], "#30ff90")
        result["cells"] = [{"i": 1810, "j": -308, "gang_id": 7}]
        self.assertEqual(_parse_cells(result)[2][0]["lat"], 36.2)

    def test_bad_grid_invalid_rows_and_oversized_reply_fail_closed(self):
        for payload in (
            {}, {"ok": True, "cells": []},
            {"ok": True, "grid_lat": "nan", "grid_lng": 0.02, "cells": []},
            {"ok": True, "grid_lat": 0.02, "grid_lng": 0.02,
             "cells": [{}]},
            {"ok": True, "grid_lat": 0.02, "grid_lng": 0.02,
             "cells": [{}] * 2501},
        ):
            with self.subTest(payload=payload.get("ok")):
                with self.assertRaises(ValueError):
                    _parse_cells(payload)

    def test_draw_uses_cached_gang_colour_without_covering_hud_or_terminal(self):
        self.start()
        pyxel = FakePyxel()
        self.controller.draw(pyxel, Projection(), 640, 16, 234,
                             gps=(LAT, LON), own_gang="SYNTH GANG")
        rects = [args for kind, args in pyxel.calls if kind == "border"]
        words = [args[2] for kind, args in pyxel.calls if kind == "text"]
        self.assertTrue(rects)
        self.assertTrue(any(s.startswith("OWN SYNTH GANG") for s in words))
        self.assertTrue(all(16 <= args[1] < 234 for args in rects))

    def test_disabling_hide_overlay_but_does_not_erase_files(self):
        self.start()
        self.controller.set_enabled(False)
        pyxel = FakePyxel()
        self.controller.draw(pyxel, Projection(), 640, 16, 234, gps=(LAT, LON))
        self.assertEqual(pyxel.calls, [])
        self.assertEqual(len(list(self.controller.territory_dir.glob("*.json"))), 9)

    def test_crossing_longitude_wrap_has_correct_distance(self):
        self.assertLess(_distance_km((0, 179.9), (0, -179.9)), 30)

    def test_palette_maps_valid_hex_and_falls_back_safely(self):
        self.assertIn(_palette_color("#a0b8ed"), range(1, 16))
        self.assertIn(_palette_color("#abc"), range(1, 16))
        self.assertEqual(_palette_color("malformed"), 5)

    def test_auto_download_stops_after_one_offline_error(self):
        def broken(_req, timeout):
            raise OSError("synthetic offline")
        with patch.object(tile_manager, "DOWNLOAD_TIERS", [(10, 1)]), \
                patch.object(tile_manager, "_tiles_in_radius",
                             return_value=[(1, 1), (1, 2), (1, 3)]), \
                patch.object(tile_manager, "_tile_url", return_value="https://example.invalid"), \
                patch.object(tile_manager, "urlopen", side_effect=broken) as opened, \
                patch.object(tile_manager.time, "sleep", return_value=None):
            result = REAL_DOWNLOAD_TILES(
                LAT, LON, self.maps, radius_km=10, stop_on_error=True)
            self.assertEqual(result["errors"], 1)
            self.assertEqual(opened.call_count, 1)
            self.assertTrue((self.maps / "manifest.json").is_file())
            REAL_DOWNLOAD_TILES(
                LAT, LON, self.maps, radius_km=10)
            self.assertEqual(opened.call_count, 4)


if __name__ == "__main__":
    unittest.main()
