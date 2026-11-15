"""Optional GPS-following tile + WDGWars territory cache for the existing map.

All network work runs off the Pyxel thread. Cached territory snapshots live in
the already gitignored maps/ directory; disabling the option never erases them.
"""

import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
import tempfile
import threading
import time

REGION_DEGREES = 0.1
TERRITORY_TTL = 300
RETRY_SECONDS = 120
TILE_RADIUS_KM = 10.0
TILE_MOVE_KM = 3.0
MAX_REGION_CELLS = 2500
MAX_VIEW_REGIONS = 36

# Pyxel's original palette: approximate server-supplied gang colours on 16 colours.
_PALETTE = (0x000000, 0x2b335f, 0x7e2072, 0x19959c,
            0x8b4852, 0x395c98, 0xa9c1ff, 0xeeeeee,
            0xd4186c, 0xd38441, 0xe9c35b, 0x70c6a9,
            0x7696de, 0xa3a3a3, 0xff9798, 0xedc7b0)


def _distance_km(a, b):
    la, lo = math.radians(a[0]), math.radians(a[1])
    lb, lp = math.radians(b[0]), math.radians(b[1])
    dlat = lb - la
    dlon = (lp - lo + math.pi) % (2 * math.pi) - math.pi
    u = math.sin(dlat / 2) ** 2 + math.cos(la) * math.cos(lb) * math.sin(dlon / 2) ** 2
    return 12742.0 * math.asin(min(1.0, math.sqrt(u)))


def _region(lat, lon):
    return (math.floor(lat / REGION_DEGREES + 1e-9),
            math.floor(lon / REGION_DEGREES + 1e-9))


def _as_number(value, lower, upper):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and lower <= result <= upper else None


def _gangs_by_id(raw):
    mapping = {}
    if isinstance(raw, dict):
        for key, item in raw.items():
            if isinstance(item, dict):
                mapping[str(key)] = item
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                ident = item.get("gang_id", item.get("id"))
                if ident is not None:
                    mapping[str(ident)] = item
    return mapping


def _parse_cells(data):
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise ValueError("Invalid territory response")
    rows = data.get("cells")
    if not isinstance(rows, list) or len(rows) > MAX_REGION_CELLS:
        raise ValueError("Invalid territory cell list")
    grid_lat = _as_number(data.get("grid_lat"), 0.00001, 1)
    grid_lng = _as_number(data.get("grid_lng"), 0.00001, 1)
    if grid_lat is None or grid_lng is None:
        raise ValueError("Missing territory grid dimensions")
    gangs = _gangs_by_id(data.get("gangs"))
    cells = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        lat = item.get("lat", item.get("latitude"))
        lon = item.get("lng", item.get("lon", item.get("longitude")))
        if lat is None and "i" in item:
            i = _as_number(item.get("i"), -900000, 900000)
            lat = i * grid_lat if i is not None else None
        if lon is None and "j" in item:
            j = _as_number(item.get("j"), -900000, 900000)
            lon = j * grid_lng if j is not None else None
        lat = _as_number(lat, -90, 90)
        lon = _as_number(lon, -180, 180)
        if lat is None or lon is None:
            continue
        gid = item.get("gang_id", item.get("gid"))
        extra = gangs.get(str(gid), {})
        gang = item.get("gang") or extra.get("name") or extra.get("gang") or ""
        color = item.get("color") or extra.get("color") or ""
        cells.append(dict(lat=lat, lng=lon, gang=str(gang)[:64],
                          gang_id=gid if isinstance(gid, (int, str)) else None,
                          color=str(color)[:16], count=item.get("count"),
                          user_id=item.get("user_id")))
    if rows and not cells:
        raise ValueError("Unrecognised territory coordinates")
    return grid_lat, grid_lng, cells


def _palette_color(value, default=5):
    if not isinstance(value, str):
        return default
    text = value.lstrip("#")
    if len(text) == 3 and all(c in "0123456789abcdefABCDEF" for c in text):
        text = "".join(c * 2 for c in text)
    if len(text) != 6 or any(c not in "0123456789abcdefABCDEF" for c in text):
        return default
    rgb = int(text, 16)
    r, g, b = (rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255
    return min(range(1, 16), key=lambda k: (
        ((r - (_PALETTE[k] >> 16 & 255)) ** 2) +
        ((g - (_PALETTE[k] >> 8 & 255)) ** 2) +
        ((b - (_PALETTE[k] & 255)) ** 2)))


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".auto-map-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class AutoMap:
    """One independent worker for missing map tiles and one for territory data."""

    def __init__(self, maps_dir):
        self.maps_dir = Path(maps_dir)
        self.territory_dir = self.maps_dir / "wdgwars_territories"
        self.config_path = self.maps_dir / "auto_map.json"
        self.enabled = False
        self._last_tile_center = None
        self._tile_busy = False
        self._territory_busy = False
        self._tile_retry_at = 0.0
        self._territory_retry_at = 0.0
        self._closed = False
        self._generation = 0
        self._events = Queue()
        self._tile_thread = None
        self._territory_thread = None
        self._region_data = {}
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                raise ValueError("Invalid automatic map configuration")
            self.enabled = config.get("enabled") is True
            center = config.get("last_tile_center")
            if (isinstance(center, list) and len(center) == 2
                    and _as_number(center[0], -90, 90) is not None
                    and _as_number(center[1], -180, 180) is not None
                    and (self.maps_dir / "manifest.json").is_file()):
                self._last_tile_center = (float(center[0]), float(center[1]))
        except (OSError, ValueError, TypeError):
            pass

    @property
    def tile_busy(self):
        return self._tile_busy

    @property
    def cached_center(self):
        """Last completed cached area for initial map view, never a GPS fix."""
        return self._last_tile_center

    def set_enabled(self, enabled):
        if self._closed:
            return False
        enabled = bool(enabled)
        previous = self.enabled
        if enabled != self.enabled:
            self._generation += 1
        self.enabled = enabled
        if enabled and not previous:
            self._tile_retry_at = 0.0
            self._territory_retry_at = 0.0
        try:
            _write_json(self.config_path, dict(
                enabled=self.enabled,
                last_tile_center=self._last_tile_center))
        except OSError:
            self.enabled = previous
            self._generation += 1
            raise
        return self.enabled

    def _valid_worker(self, generation):
        return not self._closed and self.enabled and generation == self._generation

    def _read_region(self, key):
        if key in self._region_data:
            return self._region_data[key]
        path = self.territory_dir / ("%d_%d.json" % key)
        try:
            if path.stat().st_size > 600000:
                raise ValueError("oversize")
            item = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(item, dict):
                raise ValueError("invalid cache")
            fetched_at = _as_number(item.get("fetched_at"), 0, 1000000000000)
            if fetched_at is None:
                raise ValueError("invalid cache timestamp")
            # Apply the same coordinate/cell limits to disk and HTTP data.
            # Numeric strings must be normalized before arithmetic/drawing.
            grid_lat, grid_lng, cells = _parse_cells({**item, "ok": True})
            self._region_data[key] = dict(fetched_at=fetched_at,
                grid_lat=grid_lat, grid_lng=grid_lng, cells=cells)
        except (OSError, ValueError, TypeError):
            self._region_data[key] = None
        # Bound Python RAM without deleting any offline disk snapshots.
        while len(self._region_data) > 128:
            self._region_data.pop(next(iter(self._region_data)))
        return self._region_data[key]

    def _needed_regions(self, lat, lon):
        i, j = _region(lat, lon)
        return {(x, y) for x in range(i - 1, i + 2)
                for y in range(j - 1, j + 2)
                if -900 <= x <= 899 and -1800 <= y <= 1799}

    def _tile_worker(self, center, generation):
        try:
            from .tile_manager import download_tiles
            def abort(pct, message):
                if not self._valid_worker(generation):
                    raise InterruptedError("Automatic map download cancelled")
            result = download_tiles(center[0], center[1], self.maps_dir,
                                    radius_km=TILE_RADIUS_KM, callback=abort,
                                    stop_on_error=True)
            self._events.put(("tiles", generation, center,
                              result.get("errors", 1) == 0))
        except Exception:
            self._events.put(("tiles", generation, center, False))

    def _territory_worker(self, keys, fetch, generation):
        succeeded = False
        try:
            i0, i1 = min(k[0] for k in keys), max(k[0] for k in keys)
            j0, j1 = min(k[1] for k in keys), max(k[1] for k in keys)
            south, north = i0 * REGION_DEGREES, (i1 + 1) * REGION_DEGREES
            west, east = j0 * REGION_DEGREES, (j1 + 1) * REGION_DEGREES
            raw = fetch((south, west, north, east), zoom=8)
            glat, glng, cells = _parse_cells(raw)
            if not self._valid_worker(generation):
                return
            written_at = time.time()
            by_region = {key: [] for key in keys}
            for cell in cells:
                key = _region(cell["lat"], cell["lng"])
                if key in by_region:
                    by_region[key].append(cell)
            for key, region_cells in by_region.items():
                if not self._valid_worker(generation):
                    return
                _write_json(self.territory_dir / ("%d_%d.json" % key),
                            dict(fetched_at=written_at, grid_lat=glat,
                                 grid_lng=glng, cells=region_cells))
            succeeded = True
        except Exception:
            pass
        finally:
            # A disabled/retired request must also release the busy flag.
            self._events.put(("territories", generation, tuple(keys), succeeded))

    def poll(self, lat, lon, gps_valid, portal=None, manual_tiles_busy=False):
        """Called on game thread. Never performs network or blocking disk writes."""
        updated_tiles = False
        while True:
            try:
                kind, generation, payload, ok = self._events.get_nowait()
            except Empty:
                break
            if kind == "tiles":
                self._tile_busy = False
                updated_tiles = True  # a partial download may still add valid tiles
                if generation == self._generation and ok:
                    self._last_tile_center = payload
                    try:
                        _write_json(self.config_path, dict(
                            enabled=self.enabled,
                            last_tile_center=self._last_tile_center))
                    except OSError:
                        pass
                else:
                    self._tile_retry_at = time.monotonic() + RETRY_SECONDS
            elif kind == "territories":
                self._territory_busy = False
                if generation == self._generation and ok:
                    for key in payload:
                        self._region_data.pop(key, None)
                    # A newly reached region can fetch immediately; saved
                    # regions already have their own five-minute TTL.
                    self._territory_retry_at = 0.0
                else:
                    self._territory_retry_at = time.monotonic() + RETRY_SECONDS

        if not self.enabled or self._closed or not gps_valid:
            return updated_tiles
        lat = _as_number(lat, -85, 85)
        lon = _as_number(lon, -179.8, 179.8)
        if lat is None or lon is None:
            return updated_tiles
        now = time.monotonic()
        center = lat, lon
        if (not self._tile_busy and not manual_tiles_busy
                and now >= self._tile_retry_at and
                (self._last_tile_center is None or
                 _distance_km(center, self._last_tile_center) >= TILE_MOVE_KM)):
            # Stadia key is required for both manual and automatic tile download.
            from .tile_manager import _load_stadia_key
            if _load_stadia_key():
                self._tile_busy = True
                generation = self._generation
                worker = threading.Thread(target=self._tile_worker,
                    args=(center, generation), daemon=True, name="wdg-auto-tiles")
                self._tile_thread = worker
                worker.start()
            else:
                self._tile_retry_at = now + RETRY_SECONDS

        if (not self._territory_busy and now >= self._territory_retry_at
                and portal is not None and
                getattr(portal, "_api_key", "") and
                hasattr(portal, "fetch_member_territories")):
            needed = self._needed_regions(lat, lon)
            missing = {key for key in needed if (
                (entry := self._read_region(key)) is None or
                time.time() - entry.get("fetched_at", 0) >= TERRITORY_TTL)}
            if missing:
                self._territory_busy = True
                generation = self._generation
                worker = threading.Thread(target=self._territory_worker,
                    args=(missing, portal.fetch_member_territories, generation),
                    daemon=True, name="wdg-auto-territories")
                self._territory_thread = worker
                worker.start()
        return updated_tiles

    def _view_cells(self, proj):
        if not self.enabled or proj.zoom < 9:
            return ()
        lat0, lat1 = (proj.center_lat - proj.lat_span / 2,
                      proj.center_lat + proj.lat_span / 2)
        lon0, lon1 = (proj.center_lon - proj.lon_span / 2,
                      proj.center_lon + proj.lon_span / 2)
        if lon0 < -180 or lon1 > 180:
            return ()
        i0, j0 = _region(lat0, lon0)
        i1, j1 = _region(lat1, lon1)
        if (i1 - i0 + 1) * (j1 - j0 + 1) > MAX_VIEW_REGIONS:
            return ()
        unique = {}
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                entry = self._read_region((i, j))
                if entry:
                    for cell in entry["cells"]:
                        key = (cell["lat"], cell["lng"])
                        unique[key] = (cell, entry["grid_lat"], entry["grid_lng"])
        return tuple(unique.values())

    def _current_cell(self, lat, lon):
        entry = self._read_region(_region(lat, lon))
        if not entry:
            return None
        glat, glng = entry["grid_lat"], entry["grid_lng"]
        for cell in entry["cells"]:
            if (math.floor(lat / glat + 1e-9) == math.floor(cell["lat"] / glat + 1e-9)
                    and math.floor(lon / glng + 1e-9) == math.floor(cell["lng"] / glng + 1e-9)):
                return cell, entry
        return None

    def draw(self, pyxel, proj, width, top, bottom, *, gps=None, own_gang=""):
        """Draw clipped sector outlines above raster tiles, below live game markers."""
        if not self.enabled:
            return
        for cell, glat, glng in self._view_cells(proj):
            lat, lon = cell["lat"], cell["lng"]
            x0, y0 = proj.geo_to_screen(lat + glat, lon)
            x1, y1 = proj.geo_to_screen(lat, lon + glng)
            left, right = min(x0, x1), max(x0, x1)
            upper, lower = min(y0, y1), max(y0, y1)
            if right < 0 or left >= width or lower < top or upper >= bottom:
                continue
            left, right = max(0, left), min(width - 1, right)
            upper, lower = max(top, upper), min(bottom - 1, lower)
            if right <= left or lower <= upper:
                continue
            colour = _palette_color(cell.get("color"), 5)
            if not cell.get("gang_id"):
                colour = 5
            pyxel.rectb(left, upper, right - left + 1, lower - upper + 1, colour)
        if gps is not None:
            current = self._current_cell(gps[0], gps[1])
            if current:
                cell, entry = current
                gang = cell.get("gang") or "UNCLAIMED"
                ours = bool(own_gang and gang.casefold() == own_gang.casefold())
                label = ("OWN " if ours else "TURF ") + gang[:21]
                age = time.time() - entry.get("fetched_at", 0)
                if age > TERRITORY_TTL:
                    label += " *"
                pyxel.rect(3, top + 2, min(width - 6, 10 + len(label) * 5), 11, 0)
                pyxel.text(6, top + 4, label, _palette_color(cell.get("color"), 7))

    def close(self):
        """Retire workers without touching ESP32, map files or player data."""
        self._closed = True
        self._generation += 1
        complete = True
        for thread in (self._tile_thread, self._territory_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=1.0)
                complete = complete and not thread.is_alive()
        return complete
