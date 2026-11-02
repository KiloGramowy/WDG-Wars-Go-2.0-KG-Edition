"""Distance-ordered display expiry after a scan is switched off.

Only visibility keys and deadlines are kept here. Saved contacts, observations,
radio ownership and networking remain with their callers.
"""
import math
import time
from typing import Callable, Hashable, Iterable


def _coordinate(lat, lon):
    if (type(lat) not in (int, float) or type(lon) not in (int, float)
            or not math.isfinite(lat) or not math.isfinite(lon)
            or not -90 <= lat <= 90 or not -180 <= lon <= 180
            or (lat == 0 and lon == 0)):
        return None
    return float(lat), float(lon)


def _distance(receiver, point):
    lat1, lon1 = map(math.radians, receiver)
    lat2, lon2 = map(math.radians, point)
    a = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * math.asin(math.sqrt(min(1.0, max(0.0, a))))


class OffScanDecay:
    """Freeze keys on OFF, then hide farther observations before nearer ones.

    The first of several keys expires at five minutes and the last at ten;
    intervening deadlines are evenly spread. A single key expires at ten
    minutes. Missing coordinates use a deterministic key order before known
    distances, rather than inventing a receiver or observation location.
    """

    def __init__(self, first_after: float = 300, last_after: float = 600,
                 clock: Callable[[], float] | None = None):
        if (type(first_after) not in (int, float)
                or type(last_after) not in (int, float)
                or not math.isfinite(first_after) or not math.isfinite(last_after)
                or not 0 <= first_after <= last_after):
            raise ValueError("Invalid OFF visibility interval")
        self.first_after = float(first_after)
        self.last_after = float(last_after)
        self._clock = clock
        self._deadlines: dict[Hashable, float] = {}
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def _now(self, now):
        result = (time.monotonic() if self._clock is None else self._clock()) if now is None else now
        if type(result) not in (int, float) or not math.isfinite(result):
            raise ValueError("Invalid monotonic timestamp")
        return result

    def freeze(self, observations: Iterable[tuple[Hashable, float, float]],
               receiver: tuple[float, float] | None = None,
               now: float | None = None):
        """Capture once per OFF transition; repeated calls never reset expiry."""
        if self._active:
            return
        started = self._now(now)
        origin = _coordinate(*receiver) if receiver is not None else None
        ranked = []
        keys = set()
        for key, lat, lon in observations:
            if key in keys:
                raise ValueError("Duplicate OFF visibility key")
            keys.add(key)
            point = _coordinate(lat, lon)
            distance = _distance(origin, point) if origin is not None and point is not None else None
            tie = (type(key).__qualname__, repr(key))
            ranked.append(((0, 0.0, tie) if distance is None else (1, -distance, tie), key))
        ranked.sort(key=lambda item: item[0])
        count = len(ranked)
        deadlines = {}
        for index, (_, key) in enumerate(ranked):
            delay = (self.last_after if count == 1 else
                     self.first_after + (self.last_after - self.first_after) * index / (count - 1))
            deadlines[key] = started + delay
        self._deadlines = deadlines
        self._active = True

    def visible(self, key: Hashable, now: float | None = None) -> bool:
        if not self._active:
            return True
        deadline = self._deadlines.get(key)
        return deadline is not None and self._now(now) < deadline

    def clear(self):
        """Cancel the OFF schedule when scanning resumes."""
        self._deadlines.clear()
        self._active = False
