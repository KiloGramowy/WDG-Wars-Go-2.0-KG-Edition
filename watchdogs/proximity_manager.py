"""Host-side WDGWars proximity for WatchDogsGo.

Detects other WDGWars nodes (badges, other consoles) over BLE using the
uConsole's OWN hci0 controller — btmgmt to advertise a rotating identity, bleak
to scan — and resolves the ones we hold a key for. The maths lives in
watchdogs/proximity/prox.py, the SAME engine the Pimoroni badge runs, so the two
land in the same rotation slot and see each other.

WHY A SUBPROCESS, NOT A THREAD: the radio needs its own asyncio loop and root;
pyxel needs the main thread. Running both in one process fought the controller
and crashed the render loop. So the radio runs as a child process (peer.py) that
writes a JSON snapshot every window, and the game just reads and draws it. The
window can never take the radio down and vice versa.

CONTROLLER SHARING: the ESP32 BT features (scan_bt, sniffer, evil twin) use the
ESP32's radio over serial and do NOT touch hci0 — but the HOST BT features
(bt_ducky, race_attack, the watch link) do. Call pause() before those and
resume() after, so proximity yields hci0 for the duration.
"""

import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
PEER_SCRIPT = os.path.join(_HERE, "proximity", "peer.py")
SNAPSHOT_PATH = "/tmp/wdgprox_snap.json"

_CFG_DIR = os.path.expanduser("~/.watchdogs")
SECRET_FILE = os.path.join(_CFG_DIR, "prox_secret")
KEYS_FILE = os.path.join(_CFG_DIR, "prox_keys.json")
ENABLED_FILE = os.path.join(_CFG_DIR, "prox_enabled")

# Defaults pair with the badge smoke-test setup so it works out of the box; the
# WDGWars portal would drop the real secret + resolution keys into ~/.watchdogs.
DEFAULT_SECRET = "peer-jeden"
DEFAULT_KNOWN = {"badger": "badger-test"}

ADV_SECS = "3"
SCAN_SECS = "6"
STALE_S = 20.0            # snapshot older than this = radio not really reporting
POLL_MIN_S = 0.4         # re-read the snapshot at most this often


class ProximityManager:
    def __init__(self, snapshot=SNAPSHOT_PATH):
        self.snapshot = snapshot
        self.proc = None
        self.enabled = False
        self.paused = False
        self._snap = {}
        self._snap_mtime = 0.0
        self._last_poll = 0.0
        self._last_spawn = 0.0
        self._respawn_backoff = 5.0     # don't relaunch a dying child faster than this

    # ── config ───────────────────────────────────────────────────────────
    def _secret(self):
        try:
            s = open(SECRET_FILE).read().strip()
            if s:
                return s
        except Exception:
            pass
        return DEFAULT_SECRET

    def _known(self):
        try:
            with open(KEYS_FILE) as f:
                d = json.load(f)
            if isinstance(d, dict) and d:
                return {str(k): str(v) for k, v in d.items()}
        except Exception:
            pass
        return dict(DEFAULT_KNOWN)

    def wanted(self):
        """Persisted on/off preference (default ON)."""
        try:
            return open(ENABLED_FILE).read().strip() != "0"
        except Exception:
            return True

    def _remember(self, on):
        try:
            os.makedirs(_CFG_DIR, exist_ok=True)
            with open(ENABLED_FILE, "w") as f:
                f.write("1" if on else "0")
        except Exception:
            pass

    # ── process control ──────────────────────────────────────────────────
    def _spawn(self):
        if self.running():
            return
        self._last_spawn = time.time()
        args = [sys.executable, PEER_SCRIPT,
                "--secret", self._secret(),
                "--snapshot", self.snapshot,
                "--adv-secs", ADV_SECS, "--scan-secs", SCAN_SECS]
        for label, secret in self._known().items():
            args += ["--know", "%s=%s" % (label, secret)]
        try:
            self.proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True)
        except Exception:
            self.proc = None

    def _kill(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except Exception:
                    self.proc.kill()
            except Exception:
                pass
        self.proc = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    # ── public API ───────────────────────────────────────────────────────
    def start(self):
        """Turn proximity on (and remember it)."""
        self.enabled = True
        self.paused = False
        self._remember(True)
        self._spawn()

    def stop(self):
        """Turn proximity off (and remember it)."""
        self.enabled = False
        self.paused = False
        self._remember(False)
        self._kill()

    def toggle(self):
        if self.enabled:
            self.stop()
        else:
            self.start()
        return self.enabled

    def pause(self):
        """Yield hci0 to a host-BT operation; stays 'enabled' so resume() knows."""
        if self.enabled and not self.paused:
            self.paused = True
            self._kill()

    def resume(self):
        if self.enabled and self.paused:
            self.paused = False
            self._spawn()

    def shutdown(self):
        """Kill the child on app exit WITHOUT changing the on/off preference."""
        self._kill()

    def tick(self, now=None):
        """Call once per frame. Keeps the child alive when it should be, and
        refreshes the snapshot at a modest rate. Cheap; safe to call at 30 fps."""
        now = now or time.time()
        if (self.enabled and not self.paused and not self.running()
                and now - self._last_spawn >= self._respawn_backoff):
            self._spawn()                       # respawn a died child, throttled
        if now - self._last_poll >= POLL_MIN_S:
            self._last_poll = now
            try:
                st = os.stat(self.snapshot)
                if st.st_mtime != self._snap_mtime:
                    with open(self.snapshot) as f:
                        self._snap = json.load(f)
                    self._snap_mtime = st.st_mtime
            except Exception:
                pass

    # ── view for drawing ─────────────────────────────────────────────────
    def fresh(self):
        return bool(self._snap) and (time.time() - self._snap_mtime) < STALE_S

    def rows(self):
        return self._snap.get("rows", []) if self.fresh() else []

    def my_ident(self):
        return self._snap.get("my_ident", "") if self.fresh() else ""

    def meeting(self):
        return self._snap.get("meet") if self.fresh() else None

    def unknown(self):
        return self._snap.get("unknown", 0) if self.fresh() else 0

    def status_text(self):
        if not self.enabled:
            return "PROX OFF"
        if self.paused:
            return "PROX HOLD"
        if not self.fresh():
            return "PROX ..."
        rows = self.rows()
        near = sum(1 for r in rows if r.get("mutual"))
        if not rows:
            return "PROX SCAN"
        return "PROX %d/%d" % (near, len(rows))
