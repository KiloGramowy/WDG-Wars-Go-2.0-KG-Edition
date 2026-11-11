"""Current-session visual GPS trail; source observations and logs stay untouched.

Only fresh measured ``GpsManager.wardrive_sample`` positions enter this layer.
The 4096-point visual budget retains the newest half verbatim and thins older
interior points per segment. If short segments prevent thinning, the oldest
complete visual segments are removed. Counters expose both forms of reduction;
this is not a GPS cache, observation store or persistent track journal.
"""

from array import array
import math
import time


class WardriveTrack:
    def __init__(self, max_points=4096):
        if type(max_points) is not int or max_points < 8:
            raise ValueError("Visual track capacity must be at least eight points")
        self.max_points = max_points
        self.clear()

    def clear(self):
        self._lat = array("d")
        self._lon = array("d")
        self._observed = array("d")
        self._breaks = bytearray()
        self._last_observed = None
        self._gap = True
        self.received_samples = 0
        self.stationary_samples = 0
        self.decimation_count = 0  # historical visual points removed, not source data
        self.dropped_segments = 0
        self._revision = 0
        self._screen_cache = None

    @property
    def point_count(self):
        return len(self._lat)

    def iter_points(self):
        """Yield retained visual (lat, lon, monotonic time, segment_start)."""
        return zip(self._lat, self._lon, self._observed, map(bool, self._breaks))

    def break_segment(self):
        """Loss/OFF never draws a line to the next independently measured fix."""
        self._gap = True

    def add_sample(self, sample, *, active=True, now=None):
        """Accept one real GGA/RMC receive identity; repeated UI polls add nothing.

        Coordinates are not distance-filtered: short real movement stays.
        Cache, startup-backfill and display/player coordinates are not inputs.
        """
        if not active or sample is None:
            self.break_segment()
            return False
        try:
            now = time.monotonic() if now is None else now
            lat, lon, observed = (sample[key] for key in ("lat", "lon", "observed_at"))
            if (sample.get("sentence") not in ("GGA", "RMC")
                    or sample.get("cached") or sample.get("backfill")
                    or sample.get("source", "measured") not in ("measured", "gps")
                    or sample.get("position_source", "measured") not in ("measured", "gps")
                    or any(type(value) not in (int, float) or not math.isfinite(value)
                           for value in (lat, lon, observed, now))
                    or not -90 <= lat <= 90 or not -180 <= lon <= 180
                    or not 0 <= now - observed < 2.0):
                self.break_segment()
                return False
        except (KeyError, TypeError, AttributeError):
            self.break_segment()
            return False
        if self._last_observed is not None and observed <= self._last_observed:
            return False
        if self._last_observed is not None and observed - self._last_observed >= 2.0:
            # Even if the UI missed the intervening None polls, the previous
            # measured sample expired before this new receive identity.
            self.break_segment()
        self._last_observed = observed
        self.received_samples += 1
        if self._lat and not self._gap and lat == self._lat[-1] and lon == self._lon[-1]:
            self.stationary_samples += 1
            return False
        self._lat.append(lat)
        self._lon.append(lon)
        self._observed.append(observed)
        self._breaks.append(bool(self._gap or self.point_count == 1))
        self._gap = False
        if self.point_count > self.max_points:
            self._compact_visual()
        self._revision += 1
        self._screen_cache = None
        return True

    def _compact_visual(self):
        count = self.point_count
        recent = count // 2
        starts = [i for i, value in enumerate(self._breaks) if value]
        ranges = list(zip(starts, starts[1:] + [count]))
        keep = set(range(recent, count))
        for start, end in ranges:
            old_end = min(end, recent)
            if start < old_end:
                keep.update((start, old_end - 1))
                keep.update(range(start + 2, old_end - 1, 2))
        if len(keep) > self.max_points:
            # Preserve every remaining segment endpoint. Removing an entire
            # oldest visual segment is safer than joining unrelated fixes.
            target = self.max_points * 3 // 4
            for start, end in ranges:
                if end > recent:
                    break
                keep.difference_update(range(start, end))
                self.dropped_segments += 1
                if len(keep) <= target:
                    break
        indices = sorted(keep)
        self.decimation_count += count - len(indices)
        self._lat = array("d", (self._lat[i] for i in indices))
        self._lon = array("d", (self._lon[i] for i in indices))
        self._observed = array("d", (self._observed[i] for i in indices))
        self._breaks = bytearray(self._breaks[i] for i in indices)
        self._breaks[0] = 1

    @staticmethod
    def _clip(x0, y0, x1, y1, bounds):
        left, top, right, bottom = bounds
        if left <= x0 <= right and left <= x1 <= right and top <= y0 <= bottom and top <= y1 <= bottom:
            return round(x0), round(y0), round(x1), round(y1)
        if (max(x0, x1) < left or min(x0, x1) > right
                or max(y0, y1) < top or min(y0, y1) > bottom):
            return None
        dx, dy = x1 - x0, y1 - y0
        enter, leave = 0.0, 1.0
        for direction, distance in ((-dx, x0 - left), (dx, right - x0),
                                    (-dy, y0 - top), (dy, bottom - y0)):
            if direction == 0:
                if distance < 0:
                    return None
                continue
            ratio = distance / direction
            if direction < 0:
                enter = max(enter, ratio)
            else:
                leave = min(leave, ratio)
            if enter > leave:
                return None
        clamp_x = lambda value: max(left, min(right, round(value)))
        clamp_y = lambda value: max(top, min(bottom, round(value)))
        return (clamp_x(x0 + enter * dx), clamp_y(y0 + enter * dy),
                clamp_x(x0 + leave * dx), clamp_y(y0 + leave * dy))

    def screen_segments(self, projection, bounds):
        """Clipped lines using WDG's shared ``geo_to_screen`` map projection.

        This method has no tile/scan dependency. Repeated frames reuse geometry
        until retained points, viewport or the existing map view changes.
        Collinear same-direction pixel edges coalesce only within a segment.
        """
        bounds = tuple(bounds)
        if (len(bounds) != 4 or not all(math.isfinite(value) for value in bounds)
                or bounds[0] > bounds[2] or bounds[1] > bounds[3]):
            raise ValueError("Invalid map viewport")
        view = tuple(getattr(projection, key, None) for key in
                     ("center_lat", "center_lon", "zoom", "lon_span", "lat_span"))
        key = (id(projection), view, bounds, self._revision)
        if self._screen_cache is not None and self._screen_cache[0] == key:
            return self._screen_cache[1]
        lines, previous, can_merge = [], None, False
        for i in range(self.point_count):
            x, y = projection.geo_to_screen(self._lat[i], self._lon[i])
            if not (math.isfinite(x) and math.isfinite(y)):
                previous, can_merge = None, False
                continue
            if previous is not None and not self._breaks[i]:
                px, py, previous_lon = previous
                # MapProjection wraps longitude around its centre. Never draw
                # a false line through the map when that screen seam is crossed.
                seam = abs(self._lon[i] - previous_lon) > 180 and abs(x - px) > (bounds[2] - bounds[0]) / 2
                clipped = None if seam else self._clip(px, py, x, y, bounds)
                if clipped is None:
                    can_merge = False
                elif clipped[:2] != clipped[2:]:
                    old = lines[-1] if lines and can_merge else None
                    if (old and old[2:] == clipped[:2]
                            and (old[2] - old[0]) * (clipped[3] - clipped[1])
                                == (old[3] - old[1]) * (clipped[2] - clipped[0])
                            and (old[2] - old[0]) * (clipped[2] - clipped[0])
                                + (old[3] - old[1]) * (clipped[3] - clipped[1]) > 0):
                        lines[-1] = old[:2] + clipped[2:]
                    else:
                        lines.append(clipped)
                    can_merge = True
            else:
                can_merge = False
            previous = x, y, self._lon[i]
        result = tuple(lines)
        self._screen_cache = key, result
        return result
