"""Wardriving-only GPS policy mirrored from Piglet KG fd8c95c.

This policy never writes to the receiver, the player's display position, or
LoRa AUTO. Startup persistence/replay belongs to WardriveRecorder.
"""

from collections import deque
import math
import threading
import time


class WardriveGpsPolicy:
    """One process-local cache and first-fix snapshot; never restored on boot."""

    def __init__(self, cache_minutes=720, *, clock=time.monotonic, history_limit=1024):
        if (isinstance(cache_minutes, bool) or not isinstance(cache_minutes, int)
                or not 1 <= cache_minutes <= 10080):
            raise ValueError("GPS cache must be 1..10080 minutes")
        if not isinstance(history_limit, int) or history_limit < 2:
            raise ValueError("GPS sample history must have at least two entries")
        self.cache_minutes = cache_minutes
        self._clock = clock
        self._lock = threading.RLock()
        self._history = deque(maxlen=history_limit)
        self._cache = None
        self._cache_refreshed_at = None
        self._first_fix = None
        self._startup_complete = False

    @staticmethod
    def _time(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("GPS policy requires finite monotonic time")
        return float(value)

    @staticmethod
    def _sample(sample, now):
        if not isinstance(sample, dict) or sample.get("valid", True) is not True:
            return None
        try:
            lat, lon = float(sample["lat"]), float(sample["lon"])
            measured = WardriveGpsPolicy._time(sample["observed_at"])
            if (not math.isfinite(lat) or not math.isfinite(lon)
                    or not -90 <= lat <= 90 or not -180 <= lon <= 180
                    or not 0 <= now - measured < 2.0):
                return None
            altitude = float(sample.get("altitude", 0.0))
            hdop = float(sample.get("hdop", 99.0))
            sats = int(sample.get("sats", 0))
            quality = int(sample.get("quality", 0))
            if not math.isfinite(altitude):
                altitude = 0.0
            if not math.isfinite(hdop) or hdop < 0:
                hdop = 99.0
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        return dict(lat=lat, lon=lon, altitude=altitude, hdop=hdop,
                    sats=max(0, sats), quality=quality, observed_at=measured)

    @staticmethod
    def _position(sample, source, assigned_at, *, cache_refreshed_at=None):
        result = dict(sample, source=source, assigned_at=assigned_at,
                      age=max(0.0, assigned_at - sample["observed_at"]))
        if cache_refreshed_at is not None:
            result["cache_refreshed_at"] = cache_refreshed_at
            result["cache_age"] = max(0.0, assigned_at - cache_refreshed_at)
        # HDOP remains HDOP; there is no invented AccuracyMeters conversion.
        return result

    def feed_sample(self, sample, now=None):
        """Refresh cache on each qualified live loop, as Piglet's loop does.

        Repeated polling may advance the cache refresh time while the measured
        sample is still <2 s old, but never changes its measurement time.
        Return True once, when the quality-gated first fix is frozen.
        """
        now = self._time(self._clock() if now is None else now)
        current = self._sample(sample, now)
        with self._lock:
            if self._history and now < self._history[-1][0]:
                raise ValueError("GPS policy time cannot move backwards")
            first = False
            if current is not None and current["hdop"] <= 10.0 and current["sats"] >= 3:
                self._cache, self._cache_refreshed_at = current, now
                if self._first_fix is None:
                    self._first_fix = self._position(current, "startup_backfill", now)
                    first = True
            # Bounded states allow delayed serial RX to select a position that
            # was known at capture, never a newer measurement from the future.
            self._history.append((now, current, self._cache, self._cache_refreshed_at))
            return first

    def position_for(self, capture_monotonic=None, now=None):
        now = self._time(self._clock() if now is None else now)
        captured = self._time(now if capture_monotonic is None else capture_monotonic)
        if captured > now:
            return None
        with self._lock:
            state = next((state for state in reversed(self._history) if state[0] <= captured), None)
            if state is None:
                return None
            _, current, cached, refreshed = state
            if current is not None and 0 <= captured - current["observed_at"] < 2.0:
                return self._position(current, "measured", captured)
            if (cached is not None and refreshed is not None
                    and 0 <= captured - refreshed <= self.cache_minutes * 60.0):
                return self._position(cached, "cache", captured, cache_refreshed_at=refreshed)
            return None

    @property
    def first_fix_snapshot(self):
        with self._lock:
            return dict(self._first_fix) if self._first_fix is not None else None

    @property
    def startup_waiting(self):
        with self._lock:
            return self._first_fix is None

    @property
    def startup_closing(self):
        with self._lock:
            return self._first_fix is not None and not self._startup_complete

    @property
    def startup_complete(self):
        with self._lock:
            return self._startup_complete

    def complete_startup(self):
        with self._lock:
            if self._first_fix is None:
                raise RuntimeError("Startup cannot complete without an accepted first fix")
            self._startup_complete = True
