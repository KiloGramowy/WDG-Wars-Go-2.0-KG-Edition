"""Automatic, read-only White Rabbit targets on the existing map.

Only the portal declares a target open or caught. These markers never enter the
game's observation, hacking, reward or upload paths. Account-bound snapshots
remain beneath the already ignored maps/ directory for offline display.
"""

import hashlib
import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
import tempfile
import threading
import time


REFRESH_SECONDS = 60.0
MAX_RABBITS = 512
MAX_CACHE_BYTES = 512 * 1024


def _number(value, lower, upper):
    if type(value) not in (int, float):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and lower <= result <= upper else None


def _identity(portal):
    key = getattr(portal, "_api_key", "")
    endpoint = getattr(portal, "_api_url", "")
    if not isinstance(key, str) or not key or not isinstance(endpoint, str) or not endpoint:
        return ""
    return hashlib.sha256((endpoint + "\0" + key).encode("utf-8")).hexdigest()


def _parse_snapshot(data):
    """Keep only open, unclaimed, unexpired targets from the real API shape."""
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise ValueError("Invalid White Rabbit response")
    rows = data.get("rabbits")
    now = _number(data.get("now"), 1, 1_000_000_000_000)
    if not isinstance(rows, list) or len(rows) > MAX_RABBITS or now is None:
        raise ValueError("Invalid White Rabbit snapshot")
    active, seen, ambiguous = {}, set(), set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        ident = row.get("id")
        if type(ident) not in (str, int) or not str(ident) or len(str(ident)) > 128:
            continue
        ident = str(ident)
        if ident in seen:
            active.pop(ident, None)
            ambiguous.add(ident)
            continue
        seen.add(ident)
        if ident in ambiguous or row.get("state") != "open":
            continue
        # A caught target can retain a null claimed_at during the closing grace
        # period. State and both deadlines take precedence over that field.
        if row.get("claimed_at") is not None or row.get("winner") not in (None, ""):
            continue
        lat, lng = _number(row.get("lat"), -90, 90), _number(row.get("lng"), -180, 180)
        ends = _number(row.get("ends_at"), 1, 1_000_000_000_000)
        closes = row.get("closes_at")
        if closes is not None:
            closes = _number(closes, 1, 1_000_000_000_000)
            if closes is None:
                continue
        expiry = min(ends, closes) if ends is not None and closes is not None else ends
        if lat is None or lng is None or expiry is None or expiry <= now:
            continue
        active[ident] = dict(id=ident, state="open", lat=lat, lng=lng, ends_at=expiry)
    return now, tuple(active.values()), len(rows)


def _save_snapshot(path, document, valid):
    """Publish a small private snapshot atomically; never follow a cache link."""
    if path.parent.is_symlink() or path.is_symlink():
        raise OSError("White Rabbit cache is a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".rabbit-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, separators=(",", ":"), allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        if valid():
            os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class WhiteRabbit:
    """One bounded portal worker, independent of GPS and Auto Map + Turf."""

    def __init__(self, maps_dir, *, clock=None, wall_clock=None):
        self.cache_dir = Path(maps_dir) / "white_rabbits"
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or time.time
        self._account = ""
        self._generation = 0
        self._closed = False
        self._busy = False
        self._thread = None
        self._events = Queue()
        self._next_request_at = 0.0
        self._targets = ()
        self._server_at_anchor = 0.0
        self._monotonic_anchor = self._clock()
        self._snapshot_count = 0
        self._state = "unconfigured"
        self._error = ""
        self._cache_saved = False

    def _now(self):
        return self._server_at_anchor + max(0.0, self._clock() - self._monotonic_anchor)

    def _active(self):
        now = self._now()
        return tuple(row for row in self._targets if row["ends_at"] > now)

    def _adopt(self, server_now, targets, count, *, elapsed=0.0):
        self._server_at_anchor = server_now + max(0.0, elapsed)
        self._monotonic_anchor = self._clock()
        self._targets = targets
        self._snapshot_count = count

    def _load_cache(self):
        path = self.cache_dir / (self._account + ".json")
        try:
            if self.cache_dir.is_symlink() or path.is_symlink() or path.stat().st_size > MAX_CACHE_BYTES:
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema") != 1 or data.get("account") != self._account:
                return
            fetched = _number(data.get("fetched_at"), 1, 1_000_000_000_000)
            if fetched is None or fetched > self._wall_clock() + 5:
                return
            server_now, targets, count = _parse_snapshot(data)
            self._adopt(server_now, targets, count, elapsed=self._wall_clock() - fetched)
            self._state, self._cache_saved = "cached", True
        except (OSError, ValueError, TypeError, UnicodeError):
            pass

    def _valid(self, generation, account):
        return not self._closed and generation == self._generation and account == self._account

    def _worker(self, fetch, generation, account):
        try:
            data = fetch()
            server_now, targets, count = _parse_snapshot(data)
            fetched = self._wall_clock()
            fetched_monotonic = self._clock()
            if not self._valid(generation, account):
                return
            saved, error = False, ""
            try:
                document = dict(schema=1, account=account, fetched_at=fetched,
                                ok=True, now=server_now, rabbits=list(targets))
                _save_snapshot(self.cache_dir / (account + ".json"), document,
                               lambda: self._valid(generation, account))
                saved = self._valid(generation, account)
            except (OSError, ValueError, TypeError):
                error = "cache_write_error"
            self._events.put((generation, account,
                              (server_now, targets, count, saved, fetched_monotonic), error))
        except Exception as error:
            # Exception text can contain credential-bearing URLs. Keep a class
            # name only; a failed fetch never replaces the saved good snapshot.
            self._events.put((generation, account, None, type(error).__name__))
        finally:
            self._events.put((generation, account, "finished", ""))

    def poll(self, portal=None):
        """Start on the first configured frame, then at most once per minute."""
        account = _identity(portal) if not self._closed else ""
        if account != self._account:
            self._generation += 1
            self._account = account
            self._targets, self._snapshot_count = (), 0
            self._next_request_at = 0.0
            self._error, self._cache_saved = "", False
            self._state = "unconfigured"
            if account:
                self._state = "waiting"
                self._load_cache()
        while True:
            try:
                generation, request_account, result, error = self._events.get_nowait()
            except Empty:
                break
            if result == "finished":
                self._busy = False
                continue
            if not self._valid(generation, request_account):
                continue
            self._error = error
            if result is None:
                self._state = "offline"
            else:
                server_now, targets, count, saved, fetched_monotonic = result
                self._adopt(server_now, targets, count,
                            elapsed=self._clock() - fetched_monotonic)
                self._cache_saved, self._state = saved, "online"
        if (not self._closed and account and not self._busy
                and self._clock() >= self._next_request_at):
            fetch = getattr(portal, "fetch_rabbit_snapshot", None)
            if callable(fetch):
                self._next_request_at = self._clock() + REFRESH_SECONDS
                self._busy = True
                worker = threading.Thread(target=self._worker,
                    args=(fetch, self._generation, account), daemon=True, name="wdg-white-rabbit")
                self._thread = worker
                try:
                    worker.start()
                except (RuntimeError, MemoryError):
                    self._busy = False
                    self._error, self._state = "worker_start_error", "offline"
        return self.stats()

    def stats(self):
        return dict(configured=bool(self._account), busy=self._busy,
                    active=len(self._active()) if self._account and not self._closed else 0,
                    snapshot_count=self._snapshot_count,
                    state="closed" if self._closed else self._state,
                    cache_saved=self._cache_saved, error=self._error)

    def draw(self, pyxel, projection, width, top, bottom):
        """Draw white bunny glyphs only; do not create game observation targets."""
        if not self._account or self._closed:
            return 0
        drawn = 0
        for row in self._active():
            x, y = projection.geo_to_screen(row["lat"], row["lng"])
            if not all(type(v) in (int, float) and math.isfinite(v) for v in (x, y)):
                continue
            x, y = round(x), round(y)
            if x < 5 or x >= width - 5 or y < top + 11 or y >= bottom - 6:
                continue
            pyxel.rect(x - 4, y - 4, 9, 8, 0)
            pyxel.rect(x - 3, y - 10, 3, 7, 0)
            pyxel.rect(x + 1, y - 10, 3, 7, 0)
            pyxel.rect(x - 3, y + 2, 7, 4, 0)
            pyxel.rect(x - 3, y - 3, 7, 5, 7)
            pyxel.rect(x - 2, y - 9, 1, 6, 7)
            pyxel.rect(x + 2, y - 9, 1, 6, 7)
            pyxel.rect(x - 2, y + 2, 5, 3, 7)
            pyxel.pset(x - 1, y - 1, 0)
            pyxel.pset(x + 1, y - 1, 0)
            drawn += 1
        return drawn

    def close(self):
        self._closed = True
        self._generation += 1
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        return self._thread is None or not self._thread.is_alive()
