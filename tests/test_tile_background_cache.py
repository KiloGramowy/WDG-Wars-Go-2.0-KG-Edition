"""Pixel-equivalence and invalidation checks for the composed map background."""

import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch
import zlib

from watchdogs.tile_manager import (GAME_TO_OSM, OSM_TILE_SIZE, TileRenderer,
                                   _lat_lon_to_tile, _tile_to_lat_lon)


class Image:
    def __init__(self, width, height, direct=True):
        self.width, self.height = width, height
        self.pixels = bytearray(width * height)
        self.direct = direct

    def cls(self, color):
        self.pixels[:] = bytes([color]) * len(self.pixels)

    def data_ptr(self):
        if not self.direct:
            raise AttributeError("backend has no direct pixel access")
        return self.pixels

    def pset(self, x, y, color):
        self.pixels[y * self.width + x] = color


class Pyxel(ModuleType):
    def __init__(self, width, height, direct=True):
        super().__init__("pyxel")
        self.screen = Image(width, height, direct)
        self.direct = direct
        self.images = 0
        self.blits = 0

    def Image(self, width, height):
        self.images += 1
        return Image(width, height, self.direct)

    def pset(self, x, y, color):
        self.screen.pset(x, y, color)

    def blt(self, x, y, image, u, v, width, height):
        self.blits += 1
        for row in range(height):
            start = (v + row) * image.width + u
            dest = (y + row) * self.screen.width + x
            self.screen.pixels[dest:dest + width] = image.pixels[start:start + width]


class Projection:
    """The application's integer sampling and longitude wrap on a small screen."""
    def __init__(self, width=48, height=40, top=4, bottom=34):
        self.width, self.height, self.top, self.bottom = width, height, top, bottom
        self.zoom = 7
        self.center_lat = -0.0439452952604
        self.center_lon = 0.0439453125
        self.lon_span = 0.045

    @property
    def lat_span(self):
        return self.lon_span * (self.bottom - self.top) / self.width

    def geo_to_screen(self, lat, lon):
        dx = lon - self.center_lon
        if dx > 180:
            dx -= 360
        elif dx < -180:
            dx += 360
        dy = self.center_lat - lat
        map_height = self.bottom - self.top
        return (int(self.width / 2 + dx * self.width / self.lon_span),
                int(self.top + map_height / 2 + dy * map_height / self.lat_span))


def scalar_background(maps, proj, width, height, top, bottom, initial=11):
    """Independent scalar reference for the original nearest-neighbour raster.

    Read the packed tile bytes directly, without the production decoder, native
    image, background cache or source cache. Retain the original tile order and
    integer clipping so a changed sampling/output path cannot pass unnoticed.
    """
    pixels = bytearray([initial]) * (width * height)
    zoom = GAME_TO_OSM.get(proj.zoom)
    if zoom is None:
        return False, pixels
    first_x, first_y = _lat_lon_to_tile(proj.center_lat + proj.lat_span,
                                       proj.center_lon - proj.lon_span, zoom)
    last_x, last_y = _lat_lon_to_tile(proj.center_lat - proj.lat_span,
                                     proj.center_lon + proj.lon_span, zoom)
    drawn = False
    covered = []
    for tx in range(first_x, last_x + 1):
        for ty in range(first_y, last_y + 1):
            try:
                raw = zlib.decompress((maps / str(zoom) / f"{tx}_{ty}.dat").read_bytes())
            except (OSError, zlib.error):
                continue
            left, upper = proj.geo_to_screen(*_tile_to_lat_lon(tx, ty, zoom))
            right, lower = proj.geo_to_screen(*_tile_to_lat_lon(tx + 1, ty + 1, zoom))
            if right <= left or lower <= upper:
                continue
            for y in range(max(top, upper), min(bottom, lower)):
                source_y = int((y - upper) * (OSM_TILE_SIZE / (lower - upper)))
                for x in range(max(0, left), min(width, right)):
                    source_x = int((x - left) * (OSM_TILE_SIZE / (right - left)))
                    if not (0 <= source_x < OSM_TILE_SIZE and 0 <= source_y < OSM_TILE_SIZE):
                        continue
                    index = source_y * OSM_TILE_SIZE + source_x
                    color = (raw[index // 2] >> 4) if index % 2 == 0 else (raw[index // 2] & 15)
                    covered.append((y * width + x, color))
                    drawn = True
    if drawn:
        for y in range(top, bottom):
            pixels[y * width:(y + 1) * width] = bytes(width)
        for index, color in covered:
            pixels[index] = color
    return drawn, pixels


class TileBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.maps = Path(self.temporary.name)
        (self.maps / "manifest.json").write_text(json.dumps({"tile_count": 9}))
        self.proj = Projection()
        self.px = Pyxel(48, 40)
        patcher = patch.dict(sys.modules, {"pyxel": self.px})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.renderer = TileRenderer(self.maps)
        self.populate(12)

    def write_tile(self, zoom, tx, ty, solid=None):
        path = self.maps / str(zoom) / f"{tx}_{ty}.dat"
        path.parent.mkdir(parents=True, exist_ok=True)
        if solid is None:
            raw = bytearray(OSM_TILE_SIZE * OSM_TILE_SIZE // 2)
            for y in range(OSM_TILE_SIZE):
                for x in range(0, OSM_TILE_SIZE, 2):
                    a = (x // 17 + y // 19 + tx + ty) % 16
                    b = ((x + 1) // 17 + y // 19 + tx + ty) % 16
                    raw[(y * OSM_TILE_SIZE + x) // 2] = (a << 4) | b
        else:
            raw = bytes([(solid << 4) | solid]) * (OSM_TILE_SIZE * OSM_TILE_SIZE // 2)
        path.write_bytes(zlib.compress(raw))
        return path

    def populate(self, zoom):
        tx, ty = _lat_lon_to_tile(self.proj.center_lat, self.proj.center_lon, zoom)
        for x in range(tx - 2, tx + 3):
            for y in range(ty - 2, ty + 3):
                self.write_tile(zoom, x, y)

    def draw(self):
        p = self.proj
        return self.renderer.draw(p, p.width, p.height, p.top, p.bottom)

    def assert_reference(self):
        p = self.proj
        self.px.screen.cls(11)
        expected_drawn, expected = scalar_background(self.maps, p, p.width, p.height,
                                                     p.top, p.bottom)
        self.assertEqual(self.draw(), expected_drawn)
        self.assertEqual(self.px.screen.pixels, expected)

    def test_cached_pixels_match_scalar_reference_and_preserve_hud_terminal(self):
        self.assert_reference()
        background = self.renderer._background
        self.assertIsNotNone(background)
        self.assert_reference()
        self.assertIs(self.renderer._background, background)
        self.assertEqual(self.px.images, 1)
        self.assertEqual(self.px.blits, 2)

    def test_live_overlay_is_not_baked_into_background(self):
        self.assert_reference()
        clean = bytes(self.px.screen.pixels)
        x, y = 20, 15
        self.px.pset(x, y, (clean[y * 48 + x] + 1) % 16)
        self.draw()
        self.assertEqual(self.px.screen.pixels, clean)
        self.px.pset(x + 1, y + 1, 8)  # next frame's moving object remains drawable
        self.assertEqual(self.px.screen.pixels[(y + 1) * 48 + x + 1], 8)
        self.assertEqual(self.px.images, 1)

    def test_repeated_view_does_not_repeat_pixel_rendering(self):
        with patch.object(self.renderer, "_draw_tile", wraps=self.renderer._draw_tile) as render:
            self.draw()
            count = render.call_count
            self.assertGreater(count, 0)
            for _ in range(30):
                self.draw()
            self.assertEqual(render.call_count, count)
            self.assertEqual(self.px.blits, 31)

    def test_pan_rebuilds_and_matches_original_sampling(self):
        self.assert_reference()
        before = bytes(self.px.screen.pixels)
        self.proj.center_lon += self.proj.lon_span / 10
        self.assert_reference()
        self.assertNotEqual(self.px.screen.pixels, before)
        self.assertEqual(self.px.images, 2)

    def test_subpixel_smoothing_reuses_identical_integer_projection(self):
        self.draw()
        background = self.renderer._background
        self.proj.center_lon += 1e-13
        self.proj.center_lat += 1e-13
        self.assert_reference()
        self.assertIs(self.renderer._background, background)

    def test_zoom_rebuilds_with_different_tile_level(self):
        self.assert_reference()
        self.proj.zoom = 9
        self.proj.lon_span /= 2
        self.populate(13)
        self.assert_reference()
        self.assertEqual(self.px.images, 2)

    def test_viewport_dimensions_and_clipping_invalidate_background(self):
        self.draw()
        self.proj.width, self.proj.height = 52, 46
        self.proj.top, self.proj.bottom = 6, 37
        self.px.screen = Image(52, 46)
        self.assert_reference()
        self.assertEqual(self.px.images, 2)
        self.assertEqual((self.renderer._background.width, self.renderer._background.height),
                         (52, 46))

    def test_changed_source_invalidates_background_and_decoded_tile_cache(self):
        self.assert_reference()
        before = bytes(self.px.screen.pixels)
        tx, ty = _lat_lon_to_tile(self.proj.center_lat, self.proj.center_lon, 12)
        path = self.write_tile(12, tx, ty, solid=8)
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        self.assert_reference()
        self.assertNotEqual(self.px.screen.pixels, before)
        self.assertEqual(self.px.images, 2)

    def test_removed_source_does_not_leave_ghost_background(self):
        self.draw()
        for path in self.maps.glob("*/*.dat"):
            path.unlink()
        self.px.screen.cls(11)
        self.assertFalse(self.draw())
        self.assertFalse(self.renderer.tiles_visible)
        self.assertEqual(set(self.px.screen.pixels), {11})

    def test_late_tile_appears_after_a_cached_empty_view(self):
        for path in self.maps.glob("*/*.dat"):
            path.unlink()
        self.assertFalse(self.draw())
        tx, ty = _lat_lon_to_tile(self.proj.center_lat, self.proj.center_lon, 12)
        self.write_tile(12, tx, ty, solid=5)
        self.assert_reference()
        self.assertTrue(self.renderer.tiles_visible)
        self.assertEqual(self.px.images, 2)

    def test_partial_coverage_keeps_black_gaps_and_opaque_zero_pixels(self):
        for path in self.maps.glob("*/*.dat"):
            path.unlink()
        self.proj.lon_span = 0.18
        tx, ty = _lat_lon_to_tile(self.proj.center_lat, self.proj.center_lon, 12)
        self.write_tile(12, tx, ty, solid=0)
        self.assert_reference()
        p = self.proj
        for y in range(p.top, p.bottom):
            self.assertEqual(set(self.px.screen.pixels[y * p.width:(y + 1) * p.width]), {0})

    def test_manifest_reload_discards_both_caches_and_removed_manifest(self):
        self.draw()
        self.assertTrue(self.renderer._cache)
        self.renderer.reload_manifest()
        self.assertFalse(self.renderer._cache)
        self.assertIsNone(self.renderer._background)
        self.assert_reference()
        (self.maps / "manifest.json").unlink()
        self.renderer.reload_manifest()
        self.assertFalse(self.renderer.has_tiles())

    def test_unsupported_zoom_returns_to_coastlines_without_blitting_old_map(self):
        self.draw()
        self.proj.zoom = 0
        self.px.screen.cls(11)
        before = self.px.blits
        self.assertFalse(self.draw())
        self.assertFalse(self.renderer.tiles_visible)
        self.assertIsNone(self.renderer._background)
        self.assertEqual(self.px.blits, before)
        self.assertEqual(set(self.px.screen.pixels), {11})

    def test_backend_without_native_image_uses_existing_renderer(self):
        with patch.object(self.px, "Image", side_effect=MemoryError("fixture")):
            self.assert_reference()
            self.assertIsNone(self.renderer._background)

    def test_legacy_backend_without_screen_or_image_uses_pset(self):
        screen = self.px.screen
        legacy = ModuleType("pyxel")
        legacy.pset = screen.pset
        p = self.proj
        screen.cls(11)
        expected_drawn, expected = scalar_background(self.maps, p, p.width, p.height,
                                                     p.top, p.bottom)
        with patch.dict(sys.modules, {"pyxel": legacy}):
            self.assertEqual(self.draw(), expected_drawn)
        self.assertEqual(screen.pixels, expected)
        self.assertIsNone(self.renderer._background)

    def test_transient_read_failure_is_retried_without_view_or_file_change(self):
        tx, ty = _lat_lon_to_tile(self.proj.center_lat, self.proj.center_lon, 12)
        tile_path = self.maps / "12" / f"{tx}_{ty}.dat"
        original = Path.read_bytes
        failures = []

        def read_once(path):
            if path == tile_path and not failures:
                failures.append(path)
                raise OSError("temporary read failure")
            return original(path)

        signature = self.renderer._tile_signature(12, tx, ty)
        with patch.object(Path, "read_bytes", read_once):
            self.assertFalse(self.draw())
            self.assertIsNone(self.renderer._background_key)
            self.assert_reference()
        self.assertEqual(failures, [tile_path])
        self.assertEqual(self.renderer._tile_signature(12, tx, ty), signature)
        self.assertIsNotNone(self.renderer._background_key)
        self.assertEqual(self.px.images, 2)

    def test_partial_background_with_unreadable_tile_is_not_kept_as_complete(self):
        self.proj.lon_span = 0.18  # multiple existing visible tiles
        tx, ty = _lat_lon_to_tile(self.proj.center_lat, self.proj.center_lon, 12)
        tile_path = self.maps / "12" / f"{tx}_{ty}.dat"
        original = Path.read_bytes

        def unreadable_center(path):
            if path == tile_path:
                raise OSError("temporary read failure")
            return original(path)

        with patch.object(Path, "read_bytes", unreadable_center):
            self.assertTrue(self.draw())  # other visible tiles can still be shown
            self.assertIsNone(self.renderer._background_key)
        self.assert_reference()
        self.assertIsNotNone(self.renderer._background_key)

    def test_image_without_direct_access_uses_pset_once_then_blits(self):
        self.px.direct = False
        self.px.screen.direct = False
        self.assert_reference()
        self.assert_reference()
        self.assertEqual(self.px.images, 1)
        self.assertEqual(self.px.blits, 2)

    def test_failed_blit_falls_back_without_caching_a_partial_frame(self):
        with patch.object(self.px, "blt", side_effect=RuntimeError("fixture")):
            self.assert_reference()
            self.assertIsNone(self.renderer._background)
        self.assert_reference()

    def test_corrupt_tile_can_be_repaired_without_restarting_renderer(self):
        for path in self.maps.glob("*/*.dat"):
            path.write_bytes(b"invalid zlib")
        self.assertFalse(self.draw())
        tx, ty = _lat_lon_to_tile(self.proj.center_lat, self.proj.center_lon, 12)
        self.write_tile(12, tx, ty, solid=3)
        self.assert_reference()


if __name__ == "__main__":
    unittest.main()
