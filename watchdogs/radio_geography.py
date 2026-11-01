"""Offline country areas for RF region selection, using bundled Natural Earth data.

This module does not infer a local radio preset or validate a GPS fix. The caller
must apply fix freshness, stability and a boundary guard before changing RF.
Polygon boundary distance includes coasts, islands, lakes, holes and manual-only
policy circles. A separate international-line distance permits a wider land
border guard without treating every coast as an international boundary. Source geometry is
cartographic (1:10m), not survey precision or evidence of RF network coverage.
No network access and no geometry package are needed at runtime.
"""

from functools import lru_cache
import json
import math
from pathlib import Path

_KM_PER_RADIAN = 6371.0088
_ON_BOUNDARY_DEGREES = 1e-9
LAND_BOUNDARY_DISTANCE_CAP_KM = 6.0


@lru_cache(maxsize=1)
def _catalog():
    with Path(__file__).with_name("radio_countries.json").open(encoding="utf-8") as stream:
        catalog = json.load(stream)
    # Build acceleration metadata once; retain all source geometry unchanged.
    for area in catalog["areas"]:
        area["_boundary_chunks"] = [chunk for polygon in area["polygons"]
                                     for ring in polygon["rings"] for chunk in _line_chunks(ring, closed=True)]
    if "land_borders" in catalog:
        borders_by_country = {area["code"]: [] for area in catalog["areas"]}
        for border in catalog["land_borders"]:
            chunks = _line_chunks(border["points"])
            for code in border["countries"]:
                if code in borders_by_country:
                    borders_by_country[code].extend(chunks)
        catalog["_land_border_chunks"] = borders_by_country
    return catalog


def _line_chunks(points, *, closed=False):
    """Small open polylines with bboxes; never join separate source lines."""
    if len(points) < 2:
        return []
    points = list(points)
    if closed and points[-1] != points[0]:
        points.append(points[0])
    chunks = []
    for start in range(0, len(points) - 1, 32):
        section = points[start:start + 33]
        bbox = (min(p[0] for p in section), min(p[1] for p in section),
                max(p[0] for p in section), max(p[1] for p in section))
        chunks.append((bbox, section))
    return chunks


def _ring_location(lon, lat, ring):
    """Return 1 inside, 0 outside, -1 on a lon/lat polygon boundary."""
    inside = False
    x1, y1 = ring[-1]
    for x2, y2 in ring:
        dx, dy = x2 - x1, y2 - y1
        cross = (lon - x1) * dy - (lat - y1) * dx
        if (
            abs(cross) <= _ON_BOUNDARY_DEGREES * max(_ON_BOUNDARY_DEGREES, abs(dx), abs(dy))
            and min(x1, x2) - _ON_BOUNDARY_DEGREES <= lon <= max(x1, x2) + _ON_BOUNDARY_DEGREES
            and min(y1, y2) - _ON_BOUNDARY_DEGREES <= lat <= max(y1, y2) + _ON_BOUNDARY_DEGREES
        ):
            return -1
        if (y1 > lat) != (y2 > lat):
            crossing_x = x1 + (lat - y1) * dx / dy
            if lon < crossing_x:
                inside = not inside
        x1, y1 = x2, y2
    return int(inside)


def _polygon_location(lon, lat, polygon):
    west, south, east, north = polygon["bbox"]
    if not west <= lon <= east or not south <= lat <= north:
        return 0
    rings = polygon["rings"]
    location = _ring_location(lon, lat, rings[0])
    if location != 1:
        return location
    for hole in rings[1:]:
        hole_location = _ring_location(lon, lat, hole)
        if hole_location == -1:
            return -1
        if hole_location == 1:
            return 0
    return 1


def _bbox_distance_squared(lon, lat, bbox, scale_x, scale_y):
    west, south, east, north = bbox
    delta_y = max(south - lat, 0.0, lat - north) * scale_y
    if west <= lon <= east or east - west >= 180:
        delta_x = 0.0
    else:
        delta_x = min(abs((west - lon + 180) % 360 - 180),
                      abs((east - lon + 180) % 360 - 180)) * scale_x
    return delta_x * delta_x + delta_y * delta_y


def _line_distance_km(lon, lat, chunks, *, cap=math.inf):
    scale_x = math.radians(1) * _KM_PER_RADIAN * math.cos(math.radians(lat))
    scale_y = math.radians(1) * _KM_PER_RADIAN
    nearest_squared = cap * cap
    candidates = []
    for bbox, points in chunks:
        lower_bound = _bbox_distance_squared(lon, lat, bbox, scale_x, scale_y)
        if lower_bound <= nearest_squared:
            candidates.append((lower_bound, points))
    # Test nearby chunks first so distant country/island edges are pruned.
    candidates.sort(key=lambda candidate: candidate[0])
    for lower_bound, points in candidates:
        if lower_bound > nearest_squared:
            break
        previous_lon, previous_lat = points[0]
        for current_lon, current_lat in points[1:]:
            x1 = ((previous_lon - lon + 180) % 360 - 180) * scale_x
            y1 = (previous_lat - lat) * scale_y
            x2 = ((current_lon - lon + 180) % 360 - 180) * scale_x
            y2 = (current_lat - lat) * scale_y
            if abs(x2 - x1) <= 180 * scale_x:
                dx, dy = x2 - x1, y2 - y1
                length_squared = dx * dx + dy * dy
                fraction = 0 if not length_squared else max(0.0, min(1.0, -(x1 * dx + y1 * dy) / length_squared))
                near_x, near_y = x1 + fraction * dx, y1 + fraction * dy
                nearest_squared = min(nearest_squared, near_x * near_x + near_y * near_y)
            previous_lon, previous_lat = current_lon, current_lat
    return math.sqrt(nearest_squared)


def _boundary_distance_km(lon, lat, polygons, *, chunks=None):
    """Local equirectangular point-to-segment distance; accurate near the guard.

    Longitudes are wrapped around the query, including Alaska's Aleutians.
    At distances relevant to a 2km guard projection error is small compared to
    source generalization; source/simplification error is not a GPS accuracy.
    """
    if chunks is None:
        chunks = [chunk for polygon in polygons for ring in polygon["rings"]
                  for chunk in _line_chunks(ring, closed=True)]
    return _line_distance_km(lon, lat, chunks)


def _great_circle_distance_km(lat1, lon1, lat2, lon2):
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    haversine = math.sin(delta_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(delta_lon / 2) ** 2
    return 2 * _KM_PER_RADIAN * math.asin(math.sqrt(min(1.0, max(0.0, haversine))))


def resolve_area(lat, lon):
    """Return country/region and distance to its boundary, or None if unknown.

    Only finite numeric latitude/longitude inputs are accepted. Exact mapped
    boundaries and overlapping matches are deliberately unresolved. Land holes,
    disputed areas and unsupported countries return None. Valid results contain
    code, label, region, boundary_distance_km and land_boundary_distance_km.
    The latter is capped at 6km (6 means >=6km from the mapped line); None
    means the separate line dataset is unavailable. GPS trust policy belongs
    to the caller; a result alone must not trigger an RF change.
    """
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    if not -90 <= lat <= 90 or not -180 <= lon <= 180 or not math.isfinite(lat) or not math.isfinite(lon):
        return None
    catalog = _catalog()
    policy_margin = math.inf
    for circle in catalog.get("manual_only_circles", ()):
        margin = _great_circle_distance_km(lat, lon, circle["lat"], circle["lon"]) - circle["radius_km"]
        if margin <= 0:
            return None
        policy_margin = min(policy_margin, margin)
    matches = []
    for area in catalog["areas"]:
        found = False
        for polygon in area["polygons"]:
            location = _polygon_location(lon, lat, polygon)
            if location == -1:
                return None
            found = found or location == 1
        if found:
            matches.append(area)
    if len(matches) != 1:
        return None
    area = matches[0]
    chunks = catalog.get("_land_border_chunks", {}).get(area["code"])
    if chunks is None and "land_borders" in catalog:
        # Also support small injected fixtures without the catalog cache.
        chunks = [chunk for border in catalog["land_borders"] if area["code"] in border["countries"]
                  for chunk in _line_chunks(border["points"])]
    return {
        "code": area["code"],
        "label": area["label"],
        "region": area["region"],
        "boundary_distance_km": min(policy_margin, _boundary_distance_km(
            lon, lat, area["polygons"], chunks=area.get("_boundary_chunks"))),
        "land_boundary_distance_km": None if chunks is None else _line_distance_km(
            lon, lat, chunks, cap=LAND_BOUNDARY_DISTANCE_CAP_KM),
    }
