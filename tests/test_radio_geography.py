"""Offline coordinate fixtures only; no GPS, network, player data or RF hardware."""

import json
import hashlib
import math
from pathlib import Path
import unittest
from unittest.mock import patch

from watchdogs import radio_geography as geography


class RadioGeographyTests(unittest.TestCase):
    def test_supported_country_fixtures(self):
        fixtures = [
            ("ES", 40.4168, -3.7038), ("GB", 51.5074, -0.1278),
            ("DE", 52.52, 13.405), ("FR", 48.8566, 2.3522),
            ("PL", 52.2297, 21.0122), ("NL", 52.0907, 5.1214),
            ("BE", 50.8503, 4.3517), ("PT", 38.7223, -9.1393),
            ("IT", 41.9, 12.5), ("CZ", 50.0755, 14.4378),
            ("AT", 48.2082, 16.3738), ("DK", 56.1629, 10.2039),
            ("SE", 59.3293, 18.0686), ("NO", 59.9139, 10.7522),
            ("FI", 60.1699, 24.9384), ("IE", 53.3498, -6.2603),
            ("CH", 46.948, 7.4474), ("US", 39.7392, -104.9903),
        ]
        for code, lat, lon in fixtures:
            with self.subTest(code=code):
                area = geography.resolve_area(lat, lon)
                self.assertIsNotNone(area)
                self.assertEqual(area["code"], code)
                self.assertEqual(area["region"], "US" if code == "US" else "EU_868")
                self.assertGreater(area["boundary_distance_km"], 0)
                self.assertTrue(math.isfinite(area["boundary_distance_km"]))
                self.assertIsNotNone(area["land_boundary_distance_km"])
                self.assertGreaterEqual(area["land_boundary_distance_km"], 0)
                self.assertLessEqual(area["land_boundary_distance_km"], 6)

    def test_san_fernando_land_and_coast(self):
        inland = geography.resolve_area(36.4666, -6.1988)
        self.assertEqual(inland["code"], "ES")
        self.assertGreater(inland["boundary_distance_km"], 1.5)
        self.assertLess(inland["boundary_distance_km"], 2.3)
        # A wider international-border guard must not mistake the nearby coast
        # for a land border. The distance is capped, not an RF coverage claim.
        self.assertEqual(inland["land_boundary_distance_km"], 6.0)
        self.assertIsNone(geography.resolve_area(36.4, -6.25))

    def test_selected_islands_and_alaska(self):
        for code, lat, lon in [
            ("ES", 39.5696, 2.6502), ("FR", 42.15, 9.13),
            ("US", 61.2181, -149.9003), ("US", 19.8206, -155.4681),
            ("US", 52.913, 173.17), ("PT", 37.75, -25.67),
        ]:
            with self.subTest(code=code, lat=lat, lon=lon):
                self.assertEqual(geography.resolve_area(lat, lon)["code"], code)

    def test_sea_unsupported_territories_and_countries_are_unknown(self):
        for lat, lon in [
            (54, 3), (0, 0), (36.4, -6.25), (45, -30),
            (28.4636, -16.2518), (35.8894, -5.3213), (35.2923, -2.9381),
            (78.2232, 15.6469), (70.98, -8.68), (4.9224, -52.3135),
            (18.4655, -66.1057), (13.4443, 144.7937), (28.2072, -177.3735),
            (35.6762, 139.6503), (45.4215, -75.6972), (60.1553, 28.8542),
        ]:
            with self.subTest(lat=lat, lon=lon):
                self.assertIsNone(geography.resolve_area(lat, lon))

    def test_unsupported_enclaves_holes_and_disputed_land_are_unknown(self):
        for lat, lon in [
            (41.9029, 12.4534), (43.9367, 12.4464), (42.5063, 1.5218),
            (43.7384, 7.4246), (36.1408, -5.3536), (38.6841, -7.1006),
            (51.438333, 4.931667), (47.696944, 8.690278),
            (42.463929, 1.979071), (45.970833, 8.970833),
        ]:
            with self.subTest(lat=lat, lon=lon):
                self.assertIsNone(geography.resolve_area(lat, lon))

    def test_invalid_inputs(self):
        for lat, lon in [
            (None, 0), (0, None), ("51.5", -0.1), (0, []),
            (True, 0), (0, False), (math.nan, 0), (0, math.nan),
            (math.inf, 0), (0, -math.inf), (91, 0), (-91, 0),
            (0, 181), (0, -181), (10 ** 1000, 0), (0, 10 ** 1000),
        ]:
            with self.subTest(lat=lat, lon=lon):
                self.assertIsNone(geography.resolve_area(lat, lon))

    def test_border_has_small_distance_and_exact_vertices_are_unknown(self):
        # Kehl east of the Rhine; all coordinates are fixtures, not observations.
        area = geography.resolve_area(48.576, 7.82)
        self.assertEqual(area["code"], "DE")
        self.assertLess(area["boundary_distance_km"], 3)
        for area in geography._catalog()["areas"]:
            lon, lat = area["polygons"][0]["rings"][0][0]
            with self.subTest(country=area["code"]):
                self.assertIsNone(geography.resolve_area(lat, lon))

    def test_usa_canada_border_sides(self):
        south = geography.resolve_area(48.99, -100)
        self.assertEqual(south["code"], "US")
        self.assertLess(south["boundary_distance_km"], 0.4)
        self.assertLess(south["land_boundary_distance_km"], 2.0)
        self.assertIsNone(geography.resolve_area(49.01, -100))

    def test_polygon_holes_concavity_overlap_and_distance(self):
        polygon = {"bbox": [0, 0, 4, 4], "rings": [
            [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
            [[1, 1], [2, 1], [2, 2], [1, 2], [1, 1]],
        ]}
        a = {"code": "AA", "label": "Fixture", "region": "EU_868", "polygons": [polygon]}
        with patch.object(geography, "_catalog", return_value={"areas": [a]}):
            self.assertIsNone(geography.resolve_area(1.5, 1.5))
            self.assertIsNone(geography.resolve_area(1, 1.5))
            self.assertIsNone(geography.resolve_area(0, 3))
            area = geography.resolve_area(2.01, 1.5)
            self.assertLess(area["boundary_distance_km"], 1.2)
        with patch.object(geography, "_catalog", return_value={"areas": [a, dict(a, code="BB")]}):
            self.assertIsNone(geography.resolve_area(3, 3))
        concave = {"bbox": [0, 0, 4, 4], "rings": [[[0, 0], [4, 0], [4, 1], [1, 1], [1, 4], [0, 4], [0, 0]]]}
        self.assertEqual(geography._polygon_location(3, 3, concave), 0)

    def test_known_source_coastline_generalization_stays_unknown(self):
        # This real Copenhagen city-center coordinate is outside the source land
        # polygon. Do not invent a country via a city/radius fallback to fill it.
        self.assertIsNone(geography.resolve_area(55.6761, 12.5683))

    def test_policy_circle_boundary_is_unknown_and_margin_is_reported(self):
        square = {"bbox": [-2, -2, 2, 2], "rings": [[[-2, -2], [2, -2], [2, 2], [-2, 2], [-2, -2]]]}
        catalog = {"areas": [{"code": "AA", "label": "Fixture", "region": "EU_868", "polygons": [square]}], "manual_only_circles": [{"lat": 0, "lon": 0, "radius_km": 10}]}
        with patch.object(geography, "_catalog", return_value=catalog):
            self.assertIsNone(geography.resolve_area(0, 0))
            result = geography.resolve_area(0.1, 0)
            self.assertAlmostEqual(result["boundary_distance_km"], 1.1195, places=3)

    def test_catalog_provenance_size_and_scope(self):
        path = Path(geography.__file__).with_name("radio_countries.json")
        self.assertLess(path.stat().st_size, 2_200_000)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["areas"]), 18)
        self.assertEqual(data["source"]["commit"], "ca96624a56bd078437bca8184e78163e5039ad19")
        self.assertEqual(data["source"]["license"], "Public domain")
        self.assertEqual(len(data["manual_only_circles"]), 4)
        for source in data["source"]["files"]:
            self.assertEqual(len(source["sha256"]), 64)
        source = next(source for source in data["source"]["files"] if source["name"] == "ne_10m_admin_0_boundary_lines_land.geojson")
        self.assertEqual(source["sha256"], "74d9c16229c095fde65943a9919e337682f044bcebccb120764f38edf3b70f4a")
        self.assertEqual(len(data["land_borders"]), 970)
        # Extending proximity metadata must not change any country polygon.
        packed = json.dumps(data["areas"], sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(hashlib.sha256(packed).hexdigest(), "6dd312df59562f0b22281ebd44a92ebbfe97816a26c71fe39dafd82885fefe6e")

    def test_international_lines_are_open_country_scoped_and_capped(self):
        polygon = {"bbox": [-3, -3, 3, 3], "rings": [[[-3, -3], [3, -3], [3, 3], [-3, 3], [-3, -3]]]}
        area = {"code": "AA", "label": "Fixture", "region": "EU_868", "polygons": [polygon]}
        # Closing this L-shaped polyline would invent a diagonal across query.
        line = {"countries": ["AA"], "points": [[-1, -1], [1, -1], [1, 1]]}
        catalog = {"areas": [area], "land_borders": [line]}
        with patch.object(geography, "_catalog", return_value=catalog):
            self.assertEqual(geography.resolve_area(0, 0)["land_boundary_distance_km"], 6)
            self.assertLess(geography.resolve_area(-.99, 0)["land_boundary_distance_km"], 2)
            self.assertEqual(geography.resolve_area(-1, 0)["land_boundary_distance_km"], 0)
        catalog["land_borders"] = [dict(line, countries=["BB"])]
        with patch.object(geography, "_catalog", return_value=catalog):
            self.assertEqual(geography.resolve_area(-1, 0)["land_boundary_distance_km"], 6)
        del catalog["land_borders"]
        with patch.object(geography, "_catalog", return_value=catalog):
            self.assertIsNone(geography.resolve_area(0, 0)["land_boundary_distance_km"])

    def test_chunk_distance_preserves_segments_wrap_and_closed_ring(self):
        # Independent scalar projection, without chunk/bbox acceleration.
        def reference(lon, lat, points):
            scale_y = math.pi / 180 * 6371.0088
            scale_x = scale_y * math.cos(math.radians(lat))
            nearest = math.inf
            for a, b in zip(points, points[1:]):
                ax = ((a[0] - lon + 180) % 360 - 180) * scale_x
                bx = ((b[0] - lon + 180) % 360 - 180) * scale_x
                ay, by = (a[1] - lat) * scale_y, (b[1] - lat) * scale_y
                if abs(bx - ax) > 180 * scale_x: continue
                dx, dy = bx - ax, by - ay
                dot = -(ax * dx + ay * dy)
                length = dx * dx + dy * dy
                t = max(0, min(1, dot / length)) if length else 0
                nearest = min(nearest, math.hypot(ax + t * dx, ay + t * dy))
            return nearest
        fixtures = [
            (0.5, 0.5, [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]),
            (179.9, 52, [[179.7, 51.9], [-179.8, 52.1], [-179.6, 52.3]]),
            (-179.9, 52, [[179.7, 51.9], [-179.8, 52.1], [-179.6, 52.3]]),
            (12, 60, [[i / 10, 59.5 + (i % 3) / 10] for i in range(180)]),
        ]
        for lon, lat, points in fixtures:
            with self.subTest(lon=lon, lat=lat):
                actual = geography._line_distance_km(lon, lat, geography._line_chunks(points))
                self.assertAlmostEqual(actual, reference(lon, lat, points), places=8)


if __name__ == "__main__":
    unittest.main()
